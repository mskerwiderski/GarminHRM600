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
import time
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
        elif f == 5:  # grant notification: {file_id:1, handle:2} (observed on HRM 600)
            grant: dict[str, Any] = {"kind": "grant_notification"}
            for sf, sw, sv in parse_fields(value):
                if sf == 1 and sw == 2 and isinstance(sv, bytes):
                    for gf, gw, gv in parse_fields(sv):
                        if gw == 1 and isinstance(gv, bytes):
                            num = struct.unpack("<Q", gv)[0]
                            if gf == 1:
                                grant["id1"] = num
                            elif gf == 2:
                                grant["id2"] = num
                elif sf == 2 and sw == 0 and isinstance(sv, int):
                    grant["handle"] = sv
            events.append(grant)
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


def is_fit(data: bytes) -> bool:
    return len(data) >= 12 and data[8:12] == b".FIT"


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

    REQUEST_TIMEOUT_S = 6.0

    def __init__(self, client: Any, log: Callable[[str], None]) -> None:
        self.client = client
        self.log = log
        self.files: list[SyncFile] = []
        self.notified: list[tuple[float, SyncFile]] = []  # (unix arrival time, file)
        self.listing_complete = False
        self.next_page_id: int | None = None
        self.pending: list[SyncFile] = []
        self.requested: SyncFile | None = None
        self.deadline: float | None = None
        self.stream: StreamState | None = None
        self.completed: list[tuple[SyncFile, bytes]] = []
        self.failed: list[tuple[SyncFile, str]] = []
        self.used_service_ids: set[int] = set()
        self.exclude_synced = True
        # grant bookkeeping: the strap retransmits grants until consumed, and
        # field-5 notifications map stream handles to file ids authoritatively
        self.seen_handles: set[int] = set()
        self.handle_map: dict[int, tuple[int, int]] = {}
        self.by_key: dict[tuple[int, int], SyncFile] = {}
        # counter -> {"total": int, "buf": bytearray} for chunked protobuf transport
        self._chunks: dict[int, dict[str, Any]] = {}
        # only the first file of a type carries the type name; propagate it
        self._type_names: dict[int, str] = {}

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

    def request_file_list(
        self,
        cursor_id: int | None = None,
        start_page_id: int | None = None,
        exclude_synced: bool = True,
    ) -> None:
        self.exclude_synced = exclude_synced
        self.send_smart_request(
            "FileSync FileListRequest",
            build_file_list_request(
                cursor_id=cursor_id, start_page_id=start_page_id, exclude_synced=exclude_synced
            ),
        )

    def register_files(self, files: list[SyncFile]) -> None:
        self.resolve_type_names(files)
        for f in files:
            self.by_key.setdefault((f.id1, f.id2), f)

    def queue_downloads(self, files: list[SyncFile]) -> None:
        self.register_files(files)
        self.pending.extend(files)
        self.advance()

    def advance(self) -> None:
        if self.requested is not None or self.stream is not None:
            return
        if not self.pending:
            return
        self.requested = self.pending.pop(0)
        self.deadline = time.monotonic() + self.REQUEST_TIMEOUT_S
        self.log(f"Requesting file {self.requested.describe()}")
        self.send_smart_request("FileSync FileRequest", build_file_request(self.requested.raw))

    def tick(self) -> None:
        """Fail a request that got neither a grant nor a definitive error."""
        if self.requested is not None and self.deadline is not None and time.monotonic() > self.deadline:
            self.failed.append((self.requested, "timeout"))
            self.log(f"File request timed out: {self.requested.describe()}")
            self.requested = None
            self.deadline = None
            self.advance()

    # ---- incoming protobuf ----

    def on_gfdi_frame(self, parsed: dict[str, Any]) -> None:
        type_id = parsed["type_id"]
        body = parsed["body"]
        smart_payload: bytes | None = None
        if type_id & 0x8000:
            kind = type_id & 0xFF
            if kind in (0x2B, 0x2C) and len(body) >= 14:
                smart_payload = self.on_transport_chunk(kind, body)
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

    def on_transport_chunk(self, kind: int, body: bytes) -> bytes | None:
        """Reassemble the chunked compact protobuf transport and ACK each chunk.

        Body: [counter:2][data_offset:4][total_len:4][chunk_len:4][data].
        The strap retransmits un-ACKed chunks and never sends the next one,
        so the ACKs are required for any payload > ~495 bytes.
        """
        from .gfdi import build_compact_protobuf_status_ack

        counter = struct.unpack("<H", body[0:2])[0]
        offset, total, chunk_len = struct.unpack("<III", body[2:14])
        data = body[14:14 + chunk_len]
        if offset == 0 and chunk_len >= total:
            if kind == 0x2C:
                # un-ACKed 0x2c responses get retransmitted every ~5 s; the
                # duplicate grants then derail the request/response matching
                from .gfdi import build_compact_status_ack

                _, sequence = self.client.next_compact_ids()
                self.client.enqueue_gfdi(
                    f"Compact generic ACK kind=0x{kind:02x} counter=0x{counter:04x}",
                    build_compact_status_ack(kind, sequence),
                )
            return data
        _, sequence = self.client.next_compact_ids()
        state = self._chunks.setdefault(counter, {"total": total, "buf": bytearray()})
        if offset == len(state["buf"]):
            state["buf"].extend(data)
        elif offset < len(state["buf"]):
            self.log(f"Duplicate chunk counter=0x{counter:04x} offset={offset} - re-acking")
        else:
            self.log(f"Chunk gap counter=0x{counter:04x}: got offset {offset}, have {len(state['buf'])}")
            return None
        self.client.enqueue_gfdi(
            f"Compact chunk ACK counter=0x{counter:04x} offset={offset}",
            build_compact_protobuf_status_ack(kind, counter, offset, sequence),
        )
        if len(state["buf"]) >= state["total"]:
            del self._chunks[counter]
            self.log(f"Reassembled {state['total']}B protobuf (counter=0x{counter:04x})")
            return bytes(state["buf"])
        return None

    def resolve_type_names(self, files: list[SyncFile]) -> None:
        for f in files:
            if f.type_code is None:
                continue
            if f.type_name:
                self._type_names[f.type_code] = f.type_name
            else:
                f.type_name = self._type_names.get(f.type_code)

    def handle_event(self, event: dict[str, Any]) -> None:
        kind = event["kind"]
        if kind == "file_list_response":
            files = event["files"]
            self.register_files(files)
            self.files.extend(files)
            if event.get("next_page_id") is not None:
                self.next_page_id = event["next_page_id"]
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
            self.register_files(event["files"])
            now = time.time()
            for f in event["files"]:
                self.log(f"New file notification: {f.describe()}")
                self.files.append(f)
                self.notified.append((now, f))
        elif kind == "grant_notification":
            self.on_grant_notification(event)
        elif kind == "file_response":
            self.on_file_response(event)
        else:
            self.log(f"FileSync event: {event}")

    def on_grant_notification(self, event: dict[str, Any]) -> None:
        handle16 = event.get("handle")
        key = (event.get("id1"), event.get("id2"))
        if handle16 is None or key[0] is None:
            self.log(f"Grant notification incomplete: {event}")
            return
        self.handle_map[handle16] = key
        sync_file = self.by_key.get(key)
        self.log(
            f"Grant notification: handle=0x{handle16:04x} -> "
            f"{sync_file.describe() if sync_file else key}"
        )
        # recover a grant whose file_response we missed or mis-attributed
        if (
            handle16 not in self.seen_handles
            and sync_file is not None
            and self.stream is None
            and self.requested is None
        ):
            self.failed = [(f, r) for f, r in self.failed if (f.id1, f.id2) != key]
            self.log(f"Recovering un-consumed grant 0x{handle16:04x}")
            self.start_stream(sync_file, handle16)

    def on_file_response(self, event: dict[str, Any]) -> None:
        status = event.get("status")
        handle16 = event.get("handle")
        if status == 0 and handle16 is not None:
            if handle16 in self.seen_handles:
                self.log(f"Stale/duplicate grant handle=0x{handle16:04x} - ignoring")
                return
            # trust the field-5 handle map over request order when available
            target: SyncFile | None = None
            mapped = self.handle_map.get(handle16)
            if mapped is not None:
                target = self.by_key.get(mapped)
            if target is None:
                target = self.requested
            if target is None:
                self.log(f"Grant handle=0x{handle16:04x} with no attributable file - ignoring")
                return
            if self.stream is not None:
                self.log(f"Grant handle=0x{handle16:04x} while a stream is active - ignoring")
                return
            if target is self.requested:
                self.requested = None
                self.deadline = None
            else:
                key = (target.id1, target.id2)
                self.pending = [f for f in self.pending if (f.id1, f.id2) != key]
                self.failed = [(f, r) for f, r in self.failed if (f.id1, f.id2) != key]
            self.start_stream(target, handle16)
            return
        if self.requested is None:
            self.log(f"FileResponse without pending request: {event}")
            return
        self.failed.append((self.requested, f"status={status}"))
        self.log(f"File request failed: status={status} for {self.requested.describe()}")
        self.requested = None
        self.deadline = None
        self.advance()

    def start_stream(self, sync_file: SyncFile, handle16: int) -> None:
        self.seen_handles.add(handle16)
        service_id = next(
            (sid for sid in FILE_TRANSFER_SERVICE_IDS if sid not in self.used_service_ids),
            None,
        )
        if service_id is None:
            self.used_service_ids.clear()
            service_id = FILE_TRANSFER_SERVICE_IDS[0]
        self.used_service_ids.add(service_id)
        self.stream = StreamState(file=sync_file, handle16=handle16, service_id=service_id)
        self.log(f"File granted, stream handle=0x{handle16:04x}; registering ML service 0x{service_id:04x}")
        from .multilink import build_register_request

        self.client.enqueue_raw(
            f"Register file-transfer service 0x{service_id:04x}",
            build_register_request(service_id),
        )

    # ---- incoming multilink events ----

    def on_register_ok(self, service_id: int, ml_handle: int) -> None:
        stream = self.stream
        if stream is None or service_id != stream.service_id or stream.ml_handle is not None:
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
        # the HRM 600 streams files raw; deflate is known from watches (GB),
        # so try plain FIT first and keep inflate as a fallback
        if is_fit(raw):
            self.log(f"Raw FIT stream, {len(raw)}B")
            self.completed.append((stream.file, raw))
        else:
            data = inflate(raw)
            if data is not None and is_fit(data):
                self.log(f"Inflated to {len(data)}B")
                self.completed.append((stream.file, data))
            else:
                self.log("Neither raw FIT nor inflatable; discarding")
                self.failed.append((stream.file, "not a FIT stream"))
        self.advance()
