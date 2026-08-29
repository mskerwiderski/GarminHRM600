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


FIT_CONTENT = b"\x0e\x10\x00\x00\x64\x00\x00\x00.FIT\x00\x00" + b"x" * 100


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

    fit_bytes = FIT_CONTENT
    payload = zlib.compress(fit_bytes)
    fs.on_service_payload(0x2018, 9, b"\x00\x00\x00" + payload[:20])
    fs.on_service_payload(0x2018, 9, payload[20:])
    fs.on_service_close(0x2018, 9, 0)

    assert fs.idle is True
    assert len(fs.completed) == 1
    sync_file, data = fs.completed[0]
    assert data == fit_bytes
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


def test_compact_status_ack_matches_captured_watch_ack() -> None:
    from hrm600.gfdi import build_compact_status_ack

    # the fenix 8 acked the strap's 0x8132 session frame with exactly this
    assert build_compact_status_ack(0x32, sequence=1).hex() == "09000081ba13009de9"


def make_chunked_frames(counter: int, smart_payload: bytes, chunk_size: int) -> list[dict]:
    from hrm600.gfdi import build_gfdi_message

    frames = []
    for off in range(0, len(smart_payload), chunk_size):
        chunk = smart_payload[off:off + chunk_size]
        body = (
            struct.pack("<H", counter)
            + struct.pack("<III", off, len(smart_payload), len(chunk))
            + chunk
        )
        frames.append(parse_gfdi_message(build_gfdi_message(0x842C, body)))
    return frames


def test_chunked_transport_reassembles_and_acks() -> None:
    client = FakeClient()
    fs = FileSyncV2(client, log=lambda line: None)

    file_raw = make_file_raw(7, 8, 123, 0, "STORE_AND_FORWARD_HR_DATA_FIT")
    listing = smart_with_file_sync(field_len(10, field_len(4, file_raw)))
    frames = make_chunked_frames(0x01F2, listing, chunk_size=20)
    assert len(frames) > 2

    fs.on_gfdi_frame(frames[0])
    fs.on_gfdi_frame(frames[0])  # retransmit of an un-acked chunk
    for frame in frames[1:]:
        fs.on_gfdi_frame(frame)

    assert fs.listing_complete is True
    assert len(fs.files) == 1
    assert fs.files[0].type_name == "STORE_AND_FORWARD_HR_DATA_FIT"

    acks = [gfdi for label, gfdi in client.gfdi if label.startswith("Compact chunk ACK")]
    assert len(acks) == len(frames) + 1  # one per chunk plus the duplicate
    parsed = parse_gfdi_message(acks[0])
    # [orig=5044][ACK][counter][offset:4][kept][no_error]
    expected = (
        struct.pack("<H", 5044) + b"\x00" + struct.pack("<H", 0x01F2)
        + struct.pack("<I", 0) + b"\x00\x00"
    )
    assert parsed["body"] == expected


def test_type_name_propagates_across_entries_and_pages() -> None:
    client = FakeClient()
    fs = FileSyncV2(client, log=lambda line: None)

    named = make_file_raw(1, 1, 10, 0, "STORE_AND_FORWARD_HR_DATA_FIT")
    unnamed = make_file_raw(2, 2, 10, 0, None)
    page1 = smart_with_file_sync(
        field_len(10, field_varint(2, 5) + field_len(4, named) + field_len(4, unnamed))
    )
    fs.on_gfdi_frame(make_compact_frame(page1))
    page2 = smart_with_file_sync(field_len(10, field_len(4, make_file_raw(3, 3, 10, 0, None))))
    fs.on_gfdi_frame(make_compact_frame(page2))

    assert [f.type_name for f in fs.files] == ["STORE_AND_FORWARD_HR_DATA_FIT"] * 3
    assert fs.listing_complete is True


def grant_response(handle: int) -> dict:
    return make_compact_frame(
        smart_with_file_sync(field_len(2, field_varint(1, 0) + field_varint(3, handle)))
    )


def grant_notification(id1: int, id2: int, handle: int) -> dict:
    body = field_len(1, file_id(id1, id2)) + field_varint(2, handle)
    return make_compact_frame(smart_with_file_sync(field_len(5, body)))


def test_duplicate_grant_is_ignored() -> None:
    client = FakeClient()
    fs = FileSyncV2(client, log=lambda line: None)
    f1 = parse_file(make_file_raw(1, 1, 10, 0, "STORE_AND_FORWARD_HR_DATA_FIT"))
    f2 = parse_file(make_file_raw(2, 2, 10, 0, None))
    fs.queue_downloads([f1, f2])

    fs.on_gfdi_frame(grant_response(0x0005))          # grant for f1
    assert fs.stream is not None and fs.stream.file is f1
    fs.on_service_close(0x2018, 9, 0)                 # unmatched close: ml_handle None
    fs.stream = None
    fs.advance()                                      # now requesting f2

    fs.on_gfdi_frame(grant_response(0x0005))          # retransmitted grant for f1
    # must NOT be bound to the pending request for f2
    assert fs.stream is None
    assert fs.requested is f2


def test_field5_map_overrides_request_order() -> None:
    client = FakeClient()
    fs = FileSyncV2(client, log=lambda line: None)
    f1 = parse_file(make_file_raw(1, 1, 10, 0, "STORE_AND_FORWARD_HR_DATA_FIT"))
    f2 = parse_file(make_file_raw(2, 2, 10, 0, None))
    fs.queue_downloads([f1, f2])
    assert fs.requested is f1

    # strap announces that handle 7 belongs to f2 (not the requested f1)
    fs.on_gfdi_frame(grant_notification(2, 2, 7))
    fs.on_gfdi_frame(grant_response(7))

    assert fs.stream is not None and fs.stream.file is f2
    assert fs.requested is f1          # f1 request still outstanding
    assert all(f is not f2 for f in fs.pending)


def test_unconsumed_grant_is_recovered_after_timeout() -> None:
    client = FakeClient()
    fs = FileSyncV2(client, log=lambda line: None)
    f1 = parse_file(make_file_raw(1, 1, 10, 0, "STORE_AND_FORWARD_HR_DATA_FIT"))
    fs.queue_downloads([f1])
    assert fs.requested is f1

    fs.deadline = 0.0                  # force the timeout
    fs.tick()
    assert fs.requested is None
    assert fs.failed and fs.failed[0][0] is f1

    fs.on_gfdi_frame(grant_notification(1, 1, 3))
    assert fs.stream is not None and fs.stream.file is f1
    assert fs.failed == []


def test_single_chunk_response_frames_get_generic_ack() -> None:
    from hrm600.gfdi import build_gfdi_message

    client = FakeClient()
    fs = FileSyncV2(client, log=lambda line: None)
    listing = smart_with_file_sync(field_len(10, field_len(4, make_file_raw(1, 1, 10, 0, "x"))))
    body = struct.pack("<H", 0x0042) + struct.pack("<III", 0, len(listing), len(listing)) + listing
    fs.on_gfdi_frame(parse_gfdi_message(build_gfdi_message(0x842C, body)))

    assert fs.listing_complete is True
    acks = [gfdi for label, gfdi in client.gfdi if label.startswith("Compact generic ACK")]
    assert len(acks) == 1
    parsed = parse_gfdi_message(acks[0])
    assert parsed["body"] == struct.pack("<H", 5044) + b"\x00"


def test_non_fit_stream_content_is_discarded() -> None:
    client = FakeClient()
    fs = FileSyncV2(client, log=lambda line: None)
    f1 = parse_file(make_file_raw(1, 1, 10, 0, "STORE_AND_FORWARD_HR_DATA_FIT"))
    fs.queue_downloads([f1])
    fs.on_gfdi_frame(grant_response(1))
    fs.on_register_ok(0x2018, 9)
    fs.on_service_payload(0x2018, 9, b"\x00\x00\x00" + zlib.compress(b"garbage" * 30))
    fs.on_service_close(0x2018, 9, 0)

    assert fs.completed == []
    assert fs.failed and fs.failed[0][1] == "not a FIT file"


def test_next_page_id_is_stored_from_listing() -> None:
    client = FakeClient()
    fs = FileSyncV2(client, log=lambda line: None)
    listing = smart_with_file_sync(
        field_len(10, field_varint(3, 1282) + field_len(4, make_file_raw(1, 1, 10, 0, "x")))
    )
    fs.on_gfdi_frame(make_compact_frame(listing))
    assert fs.next_page_id == 1282
