"""Offline protocol tests.

Expected byte strings come from validated captures in
openrd-ble-running-dynamics (MIT, Sam Dumont).
"""

from __future__ import annotations

from hrm600.bootstrap import (
    build_watch_core_feature_request,
    build_watch_device_information_ack,
)
from hrm600.cobs import cobs_decode_garmin, cobs_encode_garmin
from hrm600.eventsharing import (
    build_activity_state_started_proto,
    build_compact_eventsharing_subscribe_response,
    build_eventsharing_subscribe_request,
    build_eventsharing_subscribe_response,
    build_running_algorithm_input_proto,
    decode_smart_payload,
)
from hrm600.gfdi import (
    build_compact_eventsharing_message,
    build_protobuf_message,
    build_system_event,
    extract_compact_payload,
    parse_compact_frame,
    parse_gfdi_message,
    parse_protobuf_transport,
)
from hrm600.multilink import build_register_request, parse_register_response


def test_multilink_register_request_matches_gadgetbridge_wire_format() -> None:
    msg = build_register_request(service_id=1, client_id=2, reliable=False)

    assert msg.hex() == "00000200000000000000010000"


def test_multilink_register_response_decodes_service_handle_and_status() -> None:
    parsed = parse_register_response(bytes.fromhex("000102000000000000000100000700"))

    assert parsed == {
        "client_id": 2,
        "service_id": 1,
        "status": 0,
        "handle": 7,
        "reliable": 0,
    }


def test_gfdi_system_event_roundtrips_through_garmin_cobs() -> None:
    gfdi = build_system_event("SYNC_READY", 0)
    encoded = cobs_encode_garmin(gfdi)
    decoded = cobs_decode_garmin(encoded)

    assert gfdi.hex() == "0800a6130800d5c5"
    assert decoded == gfdi
    assert parse_gfdi_message(decoded) == {
        "length": 8,
        "type_id": 5030,
        "type_name": "SYSTEM_EVENT",
        "body": bytes.fromhex("0800"),
        "crc_ok": True,
    }


def test_eventsharing_subscribe_response_hex_matches_recovered_watch_reply() -> None:
    assert (
        build_eventsharing_subscribe_response([22, 23]).hex()
        == "f2010e120c0a04080010160a0408001017"
    )


def test_compact_eventsharing_subscribe_response_matches_watch_reply() -> None:
    assert (
        build_compact_eventsharing_subscribe_response([22, 23]).hex()
        == "f2011212100a060800120208160a06080012020817"
    )


def test_compact_eventsharing_subscribe_request_matches_watch_requests() -> None:
    assert build_eventsharing_subscribe_request([20]).hex() == "f201060a040a020814"
    assert (
        build_eventsharing_subscribe_request([20, 21]).hex()
        == "f2010a0a080a0208140a020815"
    )


def test_protobuf_request_parses_wire_header_and_payload() -> None:
    protobuf = build_eventsharing_subscribe_response([22, 23])
    gfdi = build_protobuf_message(
        msg_type=5043,
        request_id=0x1234,
        protobuf=protobuf,
    )

    parsed = parse_protobuf_transport(gfdi)

    assert parsed == {
        "msg_type": 5043,
        "msg_name": "PROTOBUF_REQUEST",
        "request_id": 0x1234,
        "data_offset": 0,
        "total_len": 17,
        "chunk_len": 17,
        "protobuf": protobuf,
    }


def test_activity_state_started_proto_matches_eventsharing_type_22() -> None:
    assert (
        build_activity_state_started_proto().hex()
        == "f2010d1a0b0816ba3f06080110001800"
    )


def test_running_algorithm_input_proto_matches_eventsharing_type_23() -> None:
    assert (
        build_running_algorithm_input_proto(speed_mps=3.0, grade_pct=0.0).hex()
        == "f201141a120817c23f0d08800612080d0000000010b817"
    )


def test_compact_eventsharing_frame_wraps_runtime_alerts_like_watch_traffic() -> None:
    protobuf = build_running_algorithm_input_proto(speed_mps=3.0, grade_pct=0.0)

    gfdi = build_compact_eventsharing_message(
        protobuf,
        sequence=1,
        counter=0x0005,
    )

    parsed = parse_gfdi_message(gfdi)
    assert parsed is not None
    assert parsed["crc_ok"] is True
    assert parsed["type_id"] == 0x8139
    assert parsed["body"] == bytes.fromhex("0500") + protobuf
    assert extract_compact_payload(gfdi) == protobuf


def test_runtime_run_input_is_not_wrapped_as_gfdi_protobuf_request() -> None:
    protobuf = build_activity_state_started_proto()
    gfdi = build_compact_eventsharing_message(protobuf, sequence=2, counter=0x0006)

    assert parse_protobuf_transport(gfdi) is None
    assert parse_gfdi_message(gfdi)["type_id"] == 0x8239


def test_compact_transport_frame_unwraps_embedded_smart_payload() -> None:
    gfdi = bytes.fromhex(
        "21002b820d00000000000d0000000d000000f2010a0a080a0208160a020817cf5d"
    )
    frame = parse_compact_frame(gfdi)

    assert frame == {
        "type_id": 0x822B,
        "sequence": 2,
        "kind": 0x2B,
        "counter": 0x000D,
        "payload": bytes.fromhex("f2010a0a080a0208160a020817"),
    }
    assert decode_smart_payload(frame["payload"]) == [
        {
            "kind": "subscribe_request",
            "data": {
                "alerts": [
                    {"type": 22, "type_name": "ACCESSORY_UTILITIES_ACTIVITY_STATE"},
                    {"type": 23, "type_name": "RUNNING_ALGORITHM_INPUT"},
                ]
            },
        }
    ]


def test_watch_core_feature_request_reuses_known_good_capture() -> None:
    assert (
        build_watch_core_feature_request().hex()
        == "1c003981e9016a12421010018201021000d20100ca0100da0100ea48"
    )


def test_watch_device_information_ack_reuses_known_good_capture() -> None:
    gfdi = build_watch_device_information_ack()

    assert gfdi.hex() == (
        "34008813a013009700b811a13309d85b08a00f0e66656e69782038202d2035316d6d"
        "0566656e69780838202d2035316d6d030fd7"
    )
    parsed = parse_gfdi_message(gfdi)
    assert parsed is not None
    assert parsed["crc_ok"] is True
    assert parsed["type_id"] == 5000
    assert b"fenix 8 - 51mm" in parsed["body"]
