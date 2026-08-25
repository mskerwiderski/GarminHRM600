"""Protobuf-based file sync (Garmin "new sync protocol", FileSyncService).

The HRM 600 rejects the classic GFDI file transfer (5002 -> UNSUPPORTED).
Instead it uses the Smart protobuf FileSyncService (container field 43):

  FileListRequest -> FileListResponse (files with id1/id2, type, size)
  FileRequest     -> FileResponse (status, 16-bit stream handle)
  then: register Multi-Link service 0x2018, write [00 00 handle:2 00 00],
  receive the file as a deflate-compressed raw stream, end = handle close.

Protocol facts re-implemented from Gadgetbridge (AGPL, no code copied):
gdi_file_sync_service.proto, FileSyncServiceHandler.kt,
GarminSupport.downloadFileFromServiceV2, CommunicatorV2.startTransfer.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable

from .protobuf import field_len, field_varint, parse_fields, varint

SMART_FILE_SYNC_FIELD = 43

FILE_TRANSFER_SERVICE_IDS = [0x2018, 0x4018, 0x6018, 0xA018, 0xC018, 0xE018]

FLAGS_SYNCED = 42405  # 0xa5a5, "already synced" exclusion flag


def field_fixed64(field_no: int, value: int) -> bytes:
    return varint((field_no << 3) | 1) + struct.pack("<Q", value)


def file_id(id1: int, id2: int) -> bytes:
    return field_fixed64(1, id1) + field_fixed64(2, id2)


def smart_file_sync(body: bytes) -> bytes:
    return field_len(SMART_FILE_SYNC_FIELD, body)


def build_file_list_request(
    cursor_id: int | None = None,
    start_page_id: int | None = None,
    exclude_synced: bool = True,
) -> bytes:
    req = b""
    if cursor_id is not None:
        req += field_varint(1, cursor_id)
    elif start_page_id is not None:
        req += field_varint(2, start_page_id)
    if exclude_synced:
        flags = file_id(FLAGS_SYNCED, FLAGS_SYNCED)
        req += field_len(4, flags) + field_len(5, flags)
    return smart_file_sync(field_len(9, req))


def build_file_request(file_raw: bytes) -> bytes:
    """file_raw: the File submessage exactly as received in the list response."""
    req = (
        field_len(1, file_raw)
        + field_varint(2, 24)
        + field_varint(3, 0)
        + field_varint(4, 0)
        + field_varint(5, 15)
    )
    return smart_file_sync(field_len(1, req))


def build_stream_open_request(stream_handle: int) -> bytes:
    """Payload written to the freshly registered file-transfer ML service."""
    return b"\x00\x00" + struct.pack("<H", stream_handle) + b"\x00\x00"


@dataclass
class SyncFile:
    raw: bytes  # File submessage bytes, echoed back in FileRequest
    id1: int = 0
    id2: int = 0
    size: int = 0
    page_id: int = 0
    type_code: int | None = None
    type_name: str | None = None

    def describe(self) -> str:
        return (
            f"id={self.id1}/{self.id2}  type={self.type_name or '?'}"
            f"({self.type_code if self.type_code is not None else '?'})  "
            f"size={self.size}  page={self.page_id}"
        )

    def filename(self) -> str:
        name = (self.type_name or f"type{self.type_code}" or "file").lower()
        return f"{name}_{self.id1}_{self.id2}.fit"


def parse_file(payload: bytes) -> SyncFile:
    out = SyncFile(raw=payload)
    for f, wire, value in parse_fields(payload):
        if f == 1 and wire == 2 and isinstance(value, bytes):
            for sf, sw, sv in parse_fields(value):
                if sw == 1 and isinstance(sv, bytes):
                    num = struct.unpack("<Q", sv)[0]
                    if sf == 1:
                        out.id1 = num
                    elif sf == 2:
                        out.id2 = num
        elif f == 2 and wire == 2 and isinstance(value, bytes):
            for sf, sw, sv in parse_fields(value):
                if sf == 2 and sw == 2 and isinstance(sv, bytes):
                    out.type_name = sv.decode(errors="replace")
                elif sf == 3 and sw == 0 and isinstance(sv, int):
                    out.type_code = sv
        elif f == 3 and wire == 0 and isinstance(value, int):
            out.size = value
        elif f == 5 and wire == 0 and isinstance(value, int):
            out.page_id = value
    return out


def parse_file_sync_service(payload: bytes) -> list[dict[str, Any]]:
    """Decode a FileSyncService submessage into a list of events."""
    events: list[dict[str, Any]] = []
    for f, wire, value in parse_fields(payload):
        if wire != 2 or not isinstance(value, bytes):
            continue
        if f == 2:  # FileResponse
            resp: dict[str, Any] = {"kind": "file_response"}
            for sf, sw, sv in parse_fields(value):
                if sw == 0 and isinstance(sv, int):
                    if sf == 1:
                        resp["status"] = sv
                    elif sf == 3:
                        resp["handle"] = sv
            events.append(resp)
        elif f == 10:  # FileListResponse
            listing: dict[str, Any] = {"kind": "file_list_response", "files": []}
            for sf, sw, sv in parse_fields(value):
                if sf == 2 and sw == 0 and isinstance(sv, int):
                    listing["cursor_id"] = sv
                elif sf == 3 and sw == 0 and isinstance(sv, int):
                    listing["next_page_id"] = sv
                elif sf == 4 and sw == 2 and isinstance(sv, bytes):
                    listing["files"].append(parse_file(sv))
            events.append(listing)
        elif f == 12:  # NewFileNotification
            notif: dict[str, Any] = {"kind": "new_file_notification", "files": []}
            for sf, sw, sv in parse_fields(value):
                if sf == 1 and sw == 2 and isinstance(sv, bytes):
                    notif["files"].append(parse_file(sv))
            events.append(notif)
        else:
            events.append({"kind": f"file_sync_field_{f}", "raw_hex": value.hex()})
    return events


def extract_file_sync_payloads(smart_payload: bytes) -> list[bytes]:
    """Pull FileSyncService submessages (field 43) out of a Smart container."""
    return [
        value
        for f, wire, value in parse_fields(smart_payload)
        if f == SMART_FILE_SYNC_FIELD and wire == 2 and isinstance(value, bytes)
    ]


def inflate(data: bytes) -> bytes | None:
    for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS, zlib.MAX_WBITS | 16):
        try:
            return zlib.decompress(data, wbits)
        except zlib.error:
            continue
    return None


@dataclass
class StreamState:
    file: SyncFile
    handle16: int
    service_id: int | None = None
    ml_handle: int | None = None
    started: bool = False
    buffer: bytearray = field(default_factory=bytearray)


class FileSyncV2:
    """Drives the protobuf file sync against an Hrm600Client.

    Wire up:
      client.on_gfdi_frame       -> self.on_gfdi_frame
      client.on_register_ok      -> self.on_register_ok
      client.on_service_payload  -> self.on_service_payload
      client.on_service_close    -> self.on_service_close
    """

    def __init__(self, client: Any, log: Callable[[str], None]) -> None:
        self.client = client
        self.log = log
        self.files: list[SyncFile] = []
        self.listing_complete = False
        self.pending: list[SyncFile] = []
        self.requested: SyncFile | None = None
        self.stream: StreamState | None = None
        self.completed: list[tuple[SyncFile, bytes]] = []
        self.failed: list[tuple[SyncFile, str]] = []
        self.used_service_ids: set[int] = set()
        self.exclude_synced = True

    @property
    def idle(self) -> bool:
        return self.requested is None and self.stream is None and not self.pending

    # ---- outgoing ----

    def send_smart_request(self, label: str, protobuf: bytes) -> None:
        from .gfdi import build_compact_eventsharing_message

        counter, sequence = self.client.next_compact_ids()
        self.client.enqueue_gfdi(
            f"{label} seq={sequence} counter=0x{counter:04x}",
            build_compact_eventsharing_message(protobuf, sequence=sequence, counter=counter),
        )

    def request_file_list(self, cursor_id: int | None = None, exclude_synced: bool = True) -> None:
        self.exclude_synced = exclude_synced
        self.send_smart_request(
            "FileSync FileListRequest",
            build_file_list_request(cursor_id=cursor_id, exclude_synced=exclude_synced),
        )

    def queue_downloads(self, files: list[SyncFile]) -> None:
        self.pending.extend(files)
        self.advance()

    def advance(self) -> None:
        if self.requested is not None or self.stream is not None:
            return
        if not self.pending:
            return
        self.requested = self.pending.pop(0)
        self.log(f"Requesting file {self.requested.describe()}")
        self.send_smart_request("FileSync FileRequest", build_file_request(self.requested.raw))

    # ---- incoming protobuf ----

    def on_gfdi_frame(self, parsed: dict[str, Any]) -> None:
        type_id = parsed["type_id"]
        body = parsed["body"]
        smart_payload: bytes | None = None
        if type_id & 0x8000:
            kind = type_id & 0xFF
            if kind in (0x2B, 0x2C) and len(body) >= 14:
                smart_payload = body[14:]
            elif len(body) >= 2:
                smart_payload = body[2:]
        elif type_id in (5043, 5044) and len(body) >= 14:
            chunk_len = struct.unpack("<I", body[10:14])[0]
            smart_payload = body[14:14 + chunk_len]
        if not smart_payload:
            return
        for sync_payload in extract_file_sync_payloads(smart_payload):
            for event in parse_file_sync_service(sync_payload):
                self.handle_event(event)

    def handle_event(self, event: dict[str, Any]) -> None:
        kind = event["kind"]
        if kind == "file_list_response":
            files = event["files"]
            self.files.extend(files)
            self.log(
                f"File list page: {len(files)} file(s), cursor={event.get('cursor_id')}, "
                f"next_page={event.get('next_page_id')}"
            )
            cursor = event.get("cursor_id")
            if cursor is not None:
                self.request_file_list(cursor_id=cursor, exclude_synced=self.exclude_synced)
            else:
                self.listing_complete = True
        elif kind == "new_file_notification":
            for f in event["files"]:
                self.log(f"New file notification: {f.describe()}")
                self.files.append(f)
        elif kind == "file_response":
            self.on_file_response(event)
        else:
            self.log(f"FileSync event: {event}")

    def on_file_response(self, event: dict[str, Any]) -> None:
        if self.requested is None:
            self.log(f"FileResponse without pending request: {event}")
            return
        status = event.get("status")
        handle16 = event.get("handle")
        if status != 0 or handle16 is None:
            self.failed.append((self.requested, f"status={status}"))
            self.log(f"File request failed: status={status} for {self.requested.describe()}")
            self.requested = None
            self.advance()
            return
        service_id = next(
            (sid for sid in FILE_TRANSFER_SERVICE_IDS if sid not in self.used_service_ids),
            None,
        )
        if service_id is None:
            self.used_service_ids.clear()
            service_id = FILE_TRANSFER_SERVICE_IDS[0]
        self.used_service_ids.add(service_id)
        self.stream = StreamState(file=self.requested, handle16=handle16, service_id=service_id)
        self.requested = None
        self.log(f"File granted, stream handle=0x{handle16:04x}; registering ML service 0x{service_id:04x}")
        from .multilink import build_register_request

        self.client.enqueue_raw(
            f"Register file-transfer service 0x{service_id:04x}",
            build_register_request(service_id),
        )

    # ---- incoming multilink events ----

    def on_register_ok(self, service_id: int, ml_handle: int) -> None:
        stream = self.stream
        if stream is None or service_id != stream.service_id:
            return
        stream.ml_handle = ml_handle
        self.log(f"Stream service registered (handle {ml_handle}); opening stream 0x{stream.handle16:04x}")
        self.client.enqueue_raw(
            f"Open file stream 0x{stream.handle16:04x}",
            bytes([ml_handle]) + build_stream_open_request(stream.handle16),
        )

    def on_service_payload(self, service_id: int, ml_handle: int, payload: bytes) -> None:
        stream = self.stream
        if stream is None or ml_handle != stream.ml_handle:
            return
        if not stream.started:
            if payload[:3] == b"\x00\x00\x00":
                stream.started = True
                stream.buffer.extend(payload[3:])
            else:
                self.log(f"Unexpected first stream message: {payload.hex()[:40]}")
            return
        stream.buffer.extend(payload)

    def on_service_close(self, service_id: int, ml_handle: int, status: int) -> None:
        stream = self.stream
        if stream is None or ml_handle != stream.ml_handle:
            return
        self.stream = None
        raw = bytes(stream.buffer)
        self.log(f"Stream closed: {len(raw)}B compressed for {stream.file.describe()}")
        if not raw:
            self.failed.append((stream.file, "empty stream"))
            self.advance()
            return
        data = inflate(raw)
        if data is None:
            self.log("Inflate failed; keeping compressed bytes")
            self.failed.append((stream.file, "inflate failed"))
            self.completed.append((stream.file, raw))
        else:
            self.log(f"Inflated to {len(data)}B")
            self.completed.append((stream.file, data))
        self.advance()
