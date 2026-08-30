"""GFDI frame envelope: [len:2][msg_type:2][body][crc16:2], all little-endian.

Rides COBS-framed inside Garmin Multi-Link chunks. Compact Smart frames set
bit 15 of msg_type; low byte 0x39 = request/notification, 0x3a = response.
Adapted from openrd-ble-running-dynamics (MIT, Sam Dumont).
"""

from __future__ import annotations

import struct
from typing import Any

from .crc import garmin_crc

GFDI_MSG_NAMES = {
    5000: "RESPONSE",
    5011: "FIT_DEFINITION",
    5012: "FIT_DATA",
    5024: "DEVICE_INFORMATION",
    5030: "SYSTEM_EVENT",
    5043: "PROTOBUF_REQUEST",
    5044: "PROTOBUF_RESPONSE",
    5052: "CURRENT_TIME_REQUEST",
}

SYSTEM_EVENT_ORDINALS = {
    "SYNC_READY": 8,
    "PAIR_COMPLETE": 4,
    "SETUP_WIZARD_COMPLETE": 14,
}

GFDI_STATUS_NAMES = {
    0x00: "ACK",
    0x01: "NAK",
    0x02: "UNKNOWN_OR_NOT_SUPPORTED",
}


def build_gfdi_message(type_id: int, body: bytes) -> bytes:
    length = 2 + 2 + len(body) + 2
    header = struct.pack("<HH", length, type_id)
    crc = garmin_crc(header + body)
    return header + body + struct.pack("<H", crc)


def build_system_event(name: str, value: int = 0) -> bytes:
    return build_gfdi_message(5030, bytes([SYSTEM_EVENT_ORDINALS[name], value & 0xFF]))


def build_current_time_request(reference_id: int = 1) -> bytes:
    """CURRENT_TIME_REQUEST (5052), body = referenceID:4.

    In Gadgetbridge the *device* asks the phone for the time; whether the
    HRM 600 answers the reverse direction is untested (probe experiment).
    """
    return build_gfdi_message(5052, struct.pack("<I", reference_id))


def parse_current_time_response(body: bytes) -> dict[str, Any] | None:
    """RESPONSE (5000) body for a CURRENT_TIME_REQUEST.

    Layout (from Gadgetbridge's central-side response): [orig_type:2=5052]
    [status:1][reference_id:4][garmin_ts:4][tz_offset_s:4][...transitions].
    Returns None if the response belongs to another message type.
    """
    if len(body) < 3 or struct.unpack("<H", body[:2])[0] != 5052:
        return None
    out: dict[str, Any] = {"status": body[2]}
    if len(body) >= 11:
        out["reference_id"] = struct.unpack("<I", body[3:7])[0]
        out["garmin_ts"] = struct.unpack("<I", body[7:11])[0]
    if len(body) >= 15:
        out["tz_offset_s"] = struct.unpack("<i", body[11:15])[0]
    return out


def parse_gfdi_message(decoded: bytes) -> dict[str, Any] | None:
    if len(decoded) < 6:
        return None
    length = struct.unpack("<H", decoded[:2])[0]
    type_id = struct.unpack("<H", decoded[2:4])[0]
    body = decoded[4:-2]
    got_crc = struct.unpack("<H", decoded[-2:])[0]
    want_crc = garmin_crc(decoded[:-2])
    return {
        "length": length,
        "type_id": type_id,
        "type_name": GFDI_MSG_NAMES.get(type_id, f"UNKNOWN_{type_id}"),
        "body": body,
        "crc_ok": length == len(decoded) and got_crc == want_crc,
    }


def build_protobuf_status_ack(
    request_id: int,
    kept: bool = True,
    error_code: int = 0,
    orig_type: int = 5043,
) -> bytes:
    body = (
        struct.pack("<H", orig_type)
        + b"\x00"
        + struct.pack("<H", request_id)
        + struct.pack("<I", 0)
        + bytes([0 if kept else 1])
        + bytes([error_code])
    )
    return build_gfdi_message(5000, body)


def build_protobuf_message(
    msg_type: int,
    request_id: int,
    protobuf: bytes,
    data_offset: int = 0,
) -> bytes:
    body = (
        struct.pack("<H", request_id)
        + struct.pack("<I", data_offset)
        + struct.pack("<I", len(protobuf))
        + struct.pack("<I", len(protobuf))
        + protobuf
    )
    return build_gfdi_message(msg_type, body)


def build_compact_smart_message(
    protobuf: bytes,
    frame_kind: int,
    sequence: int,
    counter: int,
) -> bytes:
    if not 1 <= sequence <= 0x7F:
        raise ValueError("compact frame sequence must be 1..127")
    if not 0 <= frame_kind <= 0xFF:
        raise ValueError("compact frame kind must fit in one byte")
    if not 0 <= counter <= 0xFFFF:
        raise ValueError("compact frame counter must fit in two bytes")
    msg_type = 0x8000 | (sequence << 8) | frame_kind
    return build_gfdi_message(msg_type, struct.pack("<H", counter) + protobuf)


def build_compact_eventsharing_message(
    protobuf: bytes,
    sequence: int,
    counter: int,
) -> bytes:
    return build_compact_smart_message(
        protobuf=protobuf,
        frame_kind=0x39,
        sequence=sequence,
        counter=counter,
    )


def build_compact_status_ack(orig_kind: int, sequence: int) -> bytes:
    """Compact RESPONSE (kind 0x00) generic ACK: body = [orig_type:2][status=ACK].

    orig_type is the 5000-mapped id of the acked compact frame's kind.
    Matches the captured watch ack 09000081ba13009de9 (ack for kind 0x32).
    """
    msg_type = 0x8000 | (sequence << 8)
    return build_gfdi_message(msg_type, struct.pack("<H", 5000 + orig_kind) + b"\x00")


def build_compact_protobuf_status_ack(
    orig_kind: int,
    counter: int,
    data_offset: int,
    sequence: int,
) -> bytes:
    """Compact per-chunk ACK for a partial protobuf transport frame.

    Body layout follows the GFDI ProtobufStatusMessage:
    [orig_type:2][status=ACK][request_id:2][data_offset:4][kept=0][no_error=0]
    """
    body = (
        struct.pack("<H", 5000 + orig_kind)
        + b"\x00"
        + struct.pack("<H", counter)
        + struct.pack("<I", data_offset)
        + b"\x00\x00"
    )
    msg_type = 0x8000 | (sequence << 8)
    return build_gfdi_message(msg_type, body)


def parse_protobuf_transport(decoded: bytes) -> dict[str, Any] | None:
    gfdi = parse_gfdi_message(decoded)
    if gfdi is None or gfdi["type_id"] not in (5043, 5044):
        return None
    body = gfdi["body"]
    if len(body) < 14:
        return None
    chunk_len = struct.unpack("<I", body[10:14])[0]
    return {
        "msg_type": gfdi["type_id"],
        "msg_name": gfdi["type_name"],
        "request_id": struct.unpack("<H", body[0:2])[0],
        "data_offset": struct.unpack("<I", body[2:6])[0],
        "total_len": struct.unpack("<I", body[6:10])[0],
        "chunk_len": chunk_len,
        "protobuf": body[14:14 + chunk_len],
    }


def parse_compact_frame(decoded: bytes) -> dict[str, Any] | None:
    parsed = parse_gfdi_message(decoded)
    if parsed is None or not parsed["crc_ok"]:
        return None
    type_id = parsed["type_id"]
    if (type_id & 0x8000) == 0:
        return None
    body = parsed["body"]
    if len(body) < 2:
        return None
    kind = type_id & 0xFF
    payload = body[14:] if kind == 0x2B and len(body) >= 14 else body[2:]
    return {
        "type_id": type_id,
        "sequence": (type_id >> 8) & 0x7F,
        "kind": kind,
        "counter": struct.unpack("<H", body[:2])[0],
        "payload": payload,
    }


def extract_compact_payload(decoded: bytes) -> bytes | None:
    frame = parse_compact_frame(decoded)
    if frame is None:
        return None
    return frame["payload"]
