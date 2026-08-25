"""Offline tests for the GFDI file transfer layer."""

from __future__ import annotations

import struct
from datetime import datetime, timezone

from hrm600.crc import garmin_crc
from hrm600.filetransfer import (
    GARMIN_TIME_EPOCH,
    MSG_DOWNLOAD_REQUEST,
    MSG_FILE_TRANSFER_DATA,
    MSG_RESPONSE,
    DirectoryEntry,
    FileTransfer,
    build_download_request,
    build_file_transfer_ack,
    build_filter_message,
    build_supported_file_types_request,
    parse_directory,
    parse_status_response,
)
from hrm600.gfdi import build_gfdi_message, parse_gfdi_message


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []

    def enqueue_gfdi(self, label: str, gfdi_bytes: bytes) -> None:
        self.sent.append((label, gfdi_bytes))


def make_directory_bytes() -> bytes:
    ts = 1_000_000  # garmin timestamp
    entry1 = struct.pack("<HBBHBBII", 1, 128, 32, 1, 0, 0x80, 1234, ts)
    zero_entry = bytes(16)
    return entry1 + zero_entry


def test_download_request_layout() -> None:
    gfdi = build_download_request(0, new=True)
    parsed = parse_gfdi_message(gfdi)

    assert parsed["crc_ok"] is True
    assert parsed["type_id"] == MSG_DOWNLOAD_REQUEST
    assert parsed["body"] == bytes.fromhex("0000" + "00000000" + "01" + "0000" + "00000000")


def test_file_transfer_ack_layout() -> None:
    gfdi = build_file_transfer_ack(0x1234)
    parsed = parse_gfdi_message(gfdi)

    assert parsed["type_id"] == MSG_RESPONSE
    expected = struct.pack("<H", MSG_FILE_TRANSFER_DATA) + b"\x00\x00" + struct.pack("<I", 0x1234)
    assert parsed["body"] == expected


def test_simple_builders() -> None:
    assert parse_gfdi_message(build_supported_file_types_request())["body"] == b""
    assert parse_gfdi_message(build_filter_message())["body"] == b"\x03"


def test_parse_status_response_for_download_request() -> None:
    body = struct.pack("<H", MSG_DOWNLOAD_REQUEST) + b"\x00" + b"\x00" + struct.pack("<I", 512)
    status = parse_status_response(body)

    assert status["orig_type"] == MSG_DOWNLOAD_REQUEST
    assert status["status_name"] == "ACK"
    assert status["download_status_name"] == "OK"
    assert status["max_file_size"] == 512


def test_parse_directory_skips_zero_entries_and_decodes_date() -> None:
    entries = parse_directory(make_directory_bytes())

    assert len(entries) == 1
    entry = entries[0]
    assert entry.file_index == 1
    assert entry.type_name == "MONITOR"
    assert entry.is_fit is True
    assert entry.file_size == 1234
    assert entry.date == datetime.fromtimestamp(1_000_000 + GARMIN_TIME_EPOCH, tz=timezone.utc)


def make_data_frame(offset: int, chunk: bytes, running_crc: int) -> dict:
    crc = garmin_crc(chunk, running_crc)
    body = bytes([0]) + struct.pack("<H", crc) + struct.pack("<I", offset) + chunk
    gfdi = build_gfdi_message(MSG_FILE_TRANSFER_DATA, body)
    return parse_gfdi_message(gfdi), crc


def make_download_ack(size: int) -> dict:
    body = struct.pack("<H", MSG_DOWNLOAD_REQUEST) + b"\x00\x00" + struct.pack("<I", size)
    return parse_gfdi_message(build_gfdi_message(MSG_RESPONSE, body))


def test_full_directory_then_file_download_flow() -> None:
    client = FakeClient()
    ft = FileTransfer(client, log=lambda line: None)

    ft.start_directory_download()
    assert client.sent[0][0].startswith("DownloadRequest directory")

    directory_bytes = make_directory_bytes()
    ft.on_gfdi_frame(make_download_ack(len(directory_bytes)))
    assert ft.current.expected_size == len(directory_bytes)

    frame, crc = make_data_frame(0, directory_bytes[:16], 0)
    ft.on_gfdi_frame(frame)
    frame, crc = make_data_frame(16, directory_bytes[16:], crc)
    ft.on_gfdi_frame(frame)

    assert ft.directory is not None and len(ft.directory) == 1
    assert ft.idle is True
    # two chunk ACKs with advancing offsets
    ack_offsets = [
        struct.unpack("<I", parse_gfdi_message(gfdi)["body"][4:8])[0]
        for label, gfdi in client.sent
        if label.startswith("FileTransfer ACK")
    ]
    assert ack_offsets == [16, 32]

    ft.queue_downloads([ft.directory[0]])
    assert ft.idle is False
    payload = bytes(range(100)) * 3
    ft.on_gfdi_frame(make_download_ack(len(payload)))
    frame, crc = make_data_frame(0, payload[:200], 0)
    ft.on_gfdi_frame(frame)
    frame, crc = make_data_frame(200, payload[200:], crc)
    ft.on_gfdi_frame(frame)

    assert len(ft.completed) == 1
    entry, data = ft.completed[0]
    assert entry.file_index == 1
    assert data == payload
    assert ft.idle is True


def test_refused_download_clears_state() -> None:
    client = FakeClient()
    ft = FileTransfer(client, log=lambda line: None)
    ft.start_file_download(DirectoryEntry(5, 128, 4, 1, 0, 0, 100, None))

    body = struct.pack("<H", MSG_DOWNLOAD_REQUEST) + b"\x00" + b"\x01" + struct.pack("<I", 0)
    ft.on_gfdi_frame(parse_gfdi_message(build_gfdi_message(MSG_RESPONSE, body)))

    assert ft.current is None
    assert ft.idle is True
    assert ft.completed == []
