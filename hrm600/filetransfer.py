"""GFDI file transfer: directory listing and file download.

Protocol facts re-implemented from the Gadgetbridge Garmin support (AGPL) —
see docs/gfdi-filetransfer.md for the distilled reference. No code copied.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .crc import garmin_crc
from .gfdi import build_gfdi_message

MSG_RESPONSE = 5000
MSG_DOWNLOAD_REQUEST = 5002
MSG_UPLOAD_REQUEST = 5003
MSG_FILE_TRANSFER_DATA = 5004
MSG_CREATE_FILE = 5005
MSG_FILTER = 5007
MSG_SET_FILE_FLAG = 5008
MSG_FILE_AVAILABLE = 5009
MSG_BATTERY_STATUS = 5023
MSG_SYSTEM_EVENT = 5030
MSG_SUPPORTED_FILE_TYPES = 5031
MSG_SYNCHRONIZATION = 5037

GARMIN_TIME_EPOCH = 631065600

STATUS_NAMES = {0: "ACK", 1: "NAK", 2: "UNSUPPORTED"}
DOWNLOAD_STATUS_NAMES = {
    0: "OK",
    1: "INDEX_UNKNOWN",
    2: "INDEX_NOT_READABLE",
    3: "NO_SPACE_LEFT",
    4: "INVALID",
    5: "NOT_READY",
    6: "CRC_INCORRECT",
}
TRANSFER_STATUS_NAMES = {
    0: "OK",
    1: "RESEND",
    2: "ABORT",
    3: "CRC_MISMATCH",
    4: "OFFSET_MISMATCH",
    5: "SYNC_PAUSED",
}

# (data_type, sub_type) -> name; data_type 128 means FIT file
FILETYPE_NAMES = {
    (0, 0): "DIRECTORY",
    (8, 255): "DEVICE_XML",
    (128, 1): "DEVICE",
    (128, 2): "SETTINGS",
    (128, 3): "SPORTS",
    (128, 4): "ACTIVITY",
    (128, 5): "WORKOUTS",
    (128, 6): "COURSES",
    (128, 9): "WEIGHT",
    (128, 10): "TOTALS",
    (128, 11): "GOALS",
    (128, 15): "MONITOR_A",
    (128, 20): "SUMMARY",
    (128, 28): "MONITOR_DAILY",
    (128, 29): "RECORDS",
    (128, 31): "HRM_TYPE_31",
    (128, 32): "MONITOR",
    (128, 44): "METRICS",
    (128, 49): "SLEEP",
}


def garmin_ts_to_datetime(wire_ts: int) -> datetime | None:
    if wire_ts == 0:
        return None
    return datetime.fromtimestamp(wire_ts + GARMIN_TIME_EPOCH, tz=timezone.utc)


def build_download_request(
    file_index: int,
    data_offset: int = 0,
    new: bool = True,
    crc_seed: int = 0,
    data_size: int = 0,
) -> bytes:
    body = (
        struct.pack("<H", file_index)
        + struct.pack("<I", data_offset)
        + bytes([1 if new else 0])
        + struct.pack("<H", crc_seed)
        + struct.pack("<I", data_size)
    )
    return build_gfdi_message(MSG_DOWNLOAD_REQUEST, body)


def build_file_transfer_ack(next_offset: int, transfer_status: int = 0) -> bytes:
    body = (
        struct.pack("<H", MSG_FILE_TRANSFER_DATA)
        + bytes([0])  # status ACK
        + bytes([transfer_status])
        + struct.pack("<I", next_offset)
    )
    return build_gfdi_message(MSG_RESPONSE, body)


def build_supported_file_types_request() -> bytes:
    return build_gfdi_message(MSG_SUPPORTED_FILE_TYPES, b"")


def build_filter_message(filter_type: int = 3) -> bytes:
    return build_gfdi_message(MSG_FILTER, bytes([filter_type]))


def parse_file_transfer_data(body: bytes) -> dict[str, Any] | None:
    if len(body) < 7:
        return None
    return {
        "flags": body[0],
        "crc": struct.unpack("<H", body[1:3])[0],
        "data_offset": struct.unpack("<I", body[3:7])[0],
        "data": body[7:],
    }


def parse_status_response(body: bytes) -> dict[str, Any] | None:
    """Parse a 5000 RESPONSE body into a dict keyed by the original message."""
    if len(body) < 3:
        return None
    orig_type = struct.unpack("<H", body[0:2])[0]
    status = body[2]
    out: dict[str, Any] = {
        "orig_type": orig_type,
        "status": status,
        "status_name": STATUS_NAMES.get(status, f"UNKNOWN_{status}"),
    }
    rest = body[3:]
    if orig_type == MSG_DOWNLOAD_REQUEST and len(rest) >= 5:
        out["download_status"] = rest[0]
        out["download_status_name"] = DOWNLOAD_STATUS_NAMES.get(rest[0], f"UNKNOWN_{rest[0]}")
        out["max_file_size"] = struct.unpack("<I", rest[1:5])[0]
    elif orig_type == MSG_FILE_TRANSFER_DATA and len(rest) >= 5:
        out["transfer_status"] = rest[0]
        out["transfer_status_name"] = TRANSFER_STATUS_NAMES.get(rest[0], f"UNKNOWN_{rest[0]}")
        out["data_offset"] = struct.unpack("<I", rest[1:5])[0]
    elif orig_type == MSG_SUPPORTED_FILE_TYPES and len(rest) >= 1:
        types = []
        i = 1
        count = rest[0]
        for _ in range(count):
            if i + 3 > len(rest):
                break
            data_type, sub_type, name_len = rest[i], rest[i + 1], rest[i + 2]
            i += 3
            name = rest[i:i + name_len].decode(errors="replace")
            i += name_len
            types.append({
                "data_type": data_type,
                "sub_type": sub_type,
                "name": name,
                "known_as": FILETYPE_NAMES.get((data_type, sub_type)),
            })
        out["file_types"] = types
    return out


@dataclass
class DirectoryEntry:
    file_index: int
    data_type: int
    sub_type: int
    file_number: int
    specific_flags: int
    file_flags: int
    file_size: int
    date: datetime | None

    @property
    def type_name(self) -> str:
        return FILETYPE_NAMES.get(
            (self.data_type, self.sub_type), f"TYPE_{self.data_type}_{self.sub_type}"
        )

    @property
    def is_fit(self) -> bool:
        return self.data_type == 128

    def describe(self) -> str:
        date = self.date.strftime("%Y-%m-%d %H:%M:%S") if self.date else "-"
        return (
            f"index={self.file_index:5d}  {self.type_name:14s} "
            f"({self.data_type}/{self.sub_type})  number={self.file_number:5d}  "
            f"size={self.file_size:8d}  flags={self.file_flags:#04x}/{self.specific_flags:#04x}  {date}"
        )

    def filename(self) -> str:
        stamp = self.date.strftime("%Y%m%dT%H%M%SZ") if self.date else "nodate"
        ext = "fit" if self.is_fit else "bin"
        return f"{self.type_name.lower()}_{self.file_index}_{stamp}.{ext}"


def parse_directory(data: bytes) -> list[DirectoryEntry]:
    entries: list[DirectoryEntry] = []
    usable = len(data) - (len(data) % 16)
    for off in range(0, usable, 16):
        (file_index, data_type, sub_type, file_number, specific_flags,
         file_flags, file_size, wire_ts) = struct.unpack("<HBBHBBII", data[off:off + 16])
        if file_index == 0 and data_type == 0 and sub_type == 0 and file_number == 0 \
                and specific_flags == 0 and file_flags == 0 and file_size == 0:
            continue
        entries.append(DirectoryEntry(
            file_index=file_index,
            data_type=data_type,
            sub_type=sub_type,
            file_number=file_number,
            specific_flags=specific_flags,
            file_flags=file_flags,
            file_size=file_size,
            date=garmin_ts_to_datetime(wire_ts),
        ))
    return entries


def parse_file_available(body: bytes) -> dict[str, Any] | None:
    if len(body) < 16:
        return None
    (file_index, data_type, sub_type, file_number, specific_flags,
     file_flags, file_size, wire_ts) = struct.unpack("<HBBHBBII", body[:16])
    return {
        "file_index": file_index,
        "data_type": data_type,
        "sub_type": sub_type,
        "type_name": FILETYPE_NAMES.get((data_type, sub_type), f"TYPE_{data_type}_{sub_type}"),
        "file_number": file_number,
        "specific_flags": specific_flags,
        "file_flags": file_flags,
        "file_size": file_size,
        "date": garmin_ts_to_datetime(wire_ts),
    }


def parse_synchronization(body: bytes) -> dict[str, Any] | None:
    if len(body) < 2:
        return None
    sync_type = body[0]
    size = body[1]
    if size == 4 and len(body) >= 6:
        bitmask = struct.unpack("<I", body[2:6])[0]
    elif size == 8 and len(body) >= 10:
        bitmask = struct.unpack("<Q", body[2:10])[0]
    else:
        return {"sync_type": sync_type, "raw_hex": body.hex()}
    return {"sync_type": sync_type, "bitmask": bitmask}


@dataclass
class FileDownload:
    entry: DirectoryEntry
    expected_size: int = 0
    buffer: bytearray = field(default_factory=bytearray)
    running_crc: int = 0
    crc_ok: bool = True
    started: bool = False
    failed: str | None = None


class FileTransfer:
    """Download state machine, driven by GFDI frames from Hrm600Client.

    Attach via `client.on_gfdi_frame = ft.on_gfdi_frame`; outgoing frames are
    queued through `client.enqueue_gfdi` and sent by the caller's pump loop.
    """

    def __init__(self, client: Any, log: Callable[[str], None]) -> None:
        self.client = client
        self.log = log
        self.current: FileDownload | None = None
        self.directory: list[DirectoryEntry] | None = None
        self.completed: list[tuple[DirectoryEntry, bytes]] = []
        self.supported_types: list[dict[str, Any]] | None = None
        self.pending_downloads: list[DirectoryEntry] = []

    @property
    def idle(self) -> bool:
        return self.current is None and not self.pending_downloads

    def request_supported_file_types(self) -> None:
        self.client.enqueue_gfdi("SupportedFileTypesRequest", build_supported_file_types_request())

    def start_directory_download(self) -> None:
        entry = DirectoryEntry(0, 0, 0, 0, 0, 0, 0, None)
        self.current = FileDownload(entry=entry)
        self.client.enqueue_gfdi("DownloadRequest directory (index 0)", build_download_request(0))

    def start_file_download(self, entry: DirectoryEntry) -> None:
        self.current = FileDownload(entry=entry)
        self.client.enqueue_gfdi(
            f"DownloadRequest index {entry.file_index} ({entry.type_name})",
            build_download_request(entry.file_index),
        )

    def queue_downloads(self, entries: list[DirectoryEntry]) -> None:
        self.pending_downloads.extend(entries)
        self.advance()

    def advance(self) -> None:
        if self.current is not None:
            return
        if self.pending_downloads:
            self.start_file_download(self.pending_downloads.pop(0))

    def on_gfdi_frame(self, parsed: dict[str, Any]) -> None:
        type_id = parsed["type_id"]
        body = parsed["body"]
        if type_id == MSG_RESPONSE:
            self.on_status(parse_status_response(body))
        elif type_id == MSG_FILE_TRANSFER_DATA:
            self.on_data(parse_file_transfer_data(body))
        elif type_id == MSG_FILE_AVAILABLE:
            info = parse_file_available(body)
            self.log(f"FILE_AVAILABLE {info}")
        elif type_id == MSG_SYNCHRONIZATION:
            info = parse_synchronization(body)
            self.log(f"SYNCHRONIZATION {info} -> sending FILTER")
            self.client.enqueue_gfdi("Filter (type 3)", build_filter_message())

    def on_status(self, status: dict[str, Any] | None) -> None:
        if status is None:
            return
        orig = status["orig_type"]
        if orig == MSG_DOWNLOAD_REQUEST:
            self.on_download_status(status)
        elif orig == MSG_SUPPORTED_FILE_TYPES:
            self.supported_types = status.get("file_types", [])
            self.log(f"Supported file types ({status['status_name']}): {self.supported_types}")
        elif orig == MSG_FILTER:
            self.log(f"Filter status: {status['status_name']} -> directory download")
            if self.current is None and self.directory is None:
                self.start_directory_download()

    def on_download_status(self, status: dict[str, Any]) -> None:
        dl = self.current
        if dl is None:
            self.log(f"Download status without active download: {status}")
            return
        ok = status["status"] == 0 and status.get("download_status") == 0
        if not ok:
            dl.failed = (
                f"{status['status_name']}/{status.get('download_status_name', '?')}"
            )
            self.log(f"Download of index {dl.entry.file_index} refused: {dl.failed}")
            self.current = None
            self.advance()
            return
        dl.expected_size = status["max_file_size"]
        dl.started = True
        self.log(
            f"Download of index {dl.entry.file_index} accepted, size={dl.expected_size}"
        )

    def on_data(self, data: dict[str, Any] | None) -> None:
        dl = self.current
        if data is None or dl is None:
            return
        if data["data_offset"] != len(dl.buffer):
            self.log(
                f"Offset mismatch: got {data['data_offset']}, have {len(dl.buffer)} - re-acking"
            )
            self.client.enqueue_gfdi(
                f"FileTransfer ACK offset {len(dl.buffer)}",
                build_file_transfer_ack(len(dl.buffer)),
            )
            return
        chunk = data["data"]
        dl.running_crc = garmin_crc(chunk, dl.running_crc)
        if dl.running_crc != data["crc"]:
            dl.crc_ok = False
            self.log(
                f"CRC mismatch at offset {data['data_offset']}: "
                f"got {data['crc']:#06x}, computed {dl.running_crc:#06x} (continuing)"
            )
        dl.buffer.extend(chunk)
        self.client.enqueue_gfdi(
            f"FileTransfer ACK offset {len(dl.buffer)}",
            build_file_transfer_ack(len(dl.buffer)),
        )
        if dl.expected_size and len(dl.buffer) >= dl.expected_size:
            self.finish_current()

    def finish_current(self) -> None:
        dl = self.current
        assert dl is not None
        self.current = None
        if dl.entry.data_type == 0 and dl.entry.file_index == 0:
            self.directory = parse_directory(bytes(dl.buffer))
            self.log(f"Directory downloaded: {len(dl.buffer)}B, {len(self.directory)} entries")
        else:
            self.completed.append((dl.entry, bytes(dl.buffer)))
            self.log(
                f"File index {dl.entry.file_index} downloaded: {len(dl.buffer)}B crc_ok={dl.crc_ok}"
            )
        self.advance()
