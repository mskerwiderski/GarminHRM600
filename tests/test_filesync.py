"""Offline tests for the protobuf file sync (V2) layer."""

from __future__ import annotations

import struct
import zlib

from hrm600.filesync import (
    FileSyncV2,
    build_file_list_request,
    build_file_request,
    build_stream_open_request,
    extract_file_sync_payloads,
    field_fixed64,
    file_id,
    parse_file,
    parse_file_sync_service,
)
from hrm600.gfdi import build_compact_smart_message, parse_gfdi_message
from hrm600.protobuf import field_len, field_varint, parse_fields


class FakeClient:
    def __init__(self) -> None:
        self.gfdi: list[tuple[str, bytes]] = []
        self.raw: list[tuple[str, bytes]] = []
        self._counter = 0x0100
        self._seq = 1

    def enqueue_gfdi(self, label: str, gfdi_bytes: bytes) -> None:
        self.gfdi.append((label, gfdi_bytes))

    def enqueue_raw(self, label: str, data: bytes) -> None:
        self.raw.append((label, data))

    def next_compact_ids(self) -> tuple[int, int]:
        self._counter += 1
        self._seq += 1
        return self._counter, self._seq


def make_file_raw(id1: int, id2: int, size: int, type_code: int, type_name: str | None) -> bytes:
    type_msg = field_varint(3, type_code)
    if type_name is not None:
        type_msg += field_len(2, type_name.encode())
    return (
        field_len(1, field_fixed64(1, id1) + field_fixed64(2, id2))
        + field_len(2, type_msg)
        + field_varint(3, size)
        + field_varint(5, 33652)
    )


def smart_with_file_sync(body: bytes) -> bytes:
    return field_len(43, body)


def test_file_list_request_contains_exclusion_flags() -> None:
    smart = build_file_list_request()
    payloads = extract_file_sync_payloads(smart)
    assert len(payloads) == 1
    fields = dict(
        (f, v) for f, w, v in parse_fields(payloads[0]) if w == 2
    )
    req_fields = {f: (w, v) for f, w, v in parse_fields(fields[9])}
    assert req_fields[4] == (2, file_id(42405, 42405))
    assert req_fields[5] == (2, file_id(42405, 42405))


def test_file_list_request_without_flags_and_with_cursor() -> None:
    smart = build_file_list_request(cursor_id=7, exclude_synced=False)
    payload = extract_file_sync_payloads(smart)[0]
    req = [v for f, w, v in parse_fields(payload) if f == 9][0]
    assert parse_fields(req) == [(1, 0, 7)]


def test_parse_file_and_roundtrip_through_file_request() -> None:
    raw = make_file_raw(1234, 5678, 999, 8, "monitor")
    parsed = parse_file(raw)
    assert (parsed.id1, parsed.id2, parsed.size) == (1234, 5678, 999)
    assert parsed.type_code == 8
    assert parsed.type_name == "monitor"
    assert parsed.filename() == "monitor_1234_5678.fit"

    smart = build_file_request(raw)
    payload = extract_file_sync_payloads(smart)[0]
    req = [v for f, w, v in parse_fields(payload) if f == 1][0]
    inner = {f: v for f, w, v in parse_fields(req)}
    assert inner[1] == raw
    assert inner[2] == 24
    assert inner[5] == 15


def test_parse_file_sync_service_events() -> None:
    file_raw = make_file_raw(1, 2, 100, 8, "monitor")
    listing = field_len(10, field_varint(2, 3) + field_len(4, file_raw))
    response = field_len(2, field_varint(1, 0) + field_varint(3, 0x1234))

    events = parse_file_sync_service(listing + response)

    assert events[0]["kind"] == "file_list_response"
    assert events[0]["cursor_id"] == 3
    assert events[0]["files"][0].id1 == 1
    assert events[1] == {"kind": "file_response", "status": 0, "handle": 0x1234}


def test_stream_open_request_layout() -> None:
    assert build_stream_open_request(0x0102) == bytes.fromhex("000002010000")


def make_compact_frame(smart_payload: bytes) -> dict:
    gfdi = build_compact_smart_message(smart_payload, frame_kind=0x39, sequence=3, counter=5)
    return parse_gfdi_message(gfdi)


def test_full_sync_flow_list_request_stream_inflate() -> None:
    client = FakeClient()
    fs = FileSyncV2(client, log=lambda line: None)

    fs.request_file_list()
    assert client.gfdi[0][0].startswith("FileSync FileListRequest")

    file_raw = make_file_raw(11, 22, 500, 8, "monitor")
    listing = smart_with_file_sync(field_len(10, field_len(4, file_raw)))
    fs.on_gfdi_frame(make_compact_frame(listing))
    assert fs.listing_complete is True
    assert len(fs.files) == 1

    fs.queue_downloads(fs.files)
    assert client.gfdi[-1][0].startswith("FileSync FileRequest")

    grant = smart_with_file_sync(field_len(2, field_varint(1, 0) + field_varint(3, 0xBEEF)))
    fs.on_gfdi_frame(make_compact_frame(grant))
    assert fs.stream is not None and fs.stream.handle16 == 0xBEEF
    # register request for service 0x2018 was queued
    label, reg = client.raw[-1]
    assert "0x2018" in label
    assert struct.unpack("<H", reg[10:12])[0] == 0x2018

    fs.on_register_ok(0x2018, 9)
    label, open_req = client.raw[-1]
    assert open_req == bytes([9]) + build_stream_open_request(0xBEEF)

    payload = zlib.compress(b"FIT" * 100)
    fs.on_service_payload(0x2018, 9, b"\x00\x00\x00" + payload[:20])
    fs.on_service_payload(0x2018, 9, payload[20:])
    fs.on_service_close(0x2018, 9, 0)

    assert fs.idle is True
    assert len(fs.completed) == 1
    sync_file, data = fs.completed[0]
    assert data == b"FIT" * 100
    assert sync_file.id1 == 11


def test_failed_file_response_advances_queue() -> None:
    client = FakeClient()
    fs = FileSyncV2(client, log=lambda line: None)
    f1 = parse_file(make_file_raw(1, 1, 10, 8, "monitor"))
    f2 = parse_file(make_file_raw(2, 2, 20, 8, "monitor"))
    fs.queue_downloads([f1, f2])

    denial = smart_with_file_sync(field_len(2, field_varint(1, 3)))
    fs.on_gfdi_frame(make_compact_frame(denial))

    assert len(fs.failed) == 1
    assert fs.requested is not None and fs.requested.id1 == 2
