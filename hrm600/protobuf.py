"""Minimal protobuf wire-format helpers (no schema, hand-rolled).

Adapted from openrd-ble-running-dynamics (MIT, Sam Dumont).
"""

from __future__ import annotations

import struct


def varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint value must be non-negative")
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def field_varint(field: int, value: int) -> bytes:
    return varint((field << 3) | 0) + varint(value)


def field_len(field: int, payload: bytes) -> bytes:
    return varint((field << 3) | 2) + varint(len(payload)) + payload


def field_sfixed32(field: int, value: int) -> bytes:
    return varint((field << 3) | 5) + struct.pack("<i", value)


def read_varint(data: bytes, offset: int) -> tuple[int | None, int]:
    value = 0
    shift = 0
    i = offset
    while i < len(data) and shift < 64:
        b = data[i]
        i += 1
        value |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return value, i - offset
        shift += 7
    return None, 0


def parse_fields(data: bytes) -> list[tuple[int, int, int | bytes]]:
    fields: list[tuple[int, int, int | bytes]] = []
    i = 0
    while i < len(data):
        tag, n = read_varint(data, i)
        if tag is None or n == 0:
            break
        i += n
        field = tag >> 3
        wire = tag & 7
        if wire == 0:
            value, n = read_varint(data, i)
            if value is None:
                break
            i += n
            fields.append((field, wire, value))
        elif wire == 2:
            length, n = read_varint(data, i)
            if length is None or i + n + length > len(data):
                break
            i += n
            fields.append((field, wire, data[i:i + length]))
            i += length
        elif wire == 5:
            if i + 4 > len(data):
                break
            fields.append((field, wire, struct.unpack("<I", data[i:i + 4])[0]))
            i += 4
        elif wire == 1:
            if i + 8 > len(data):
                break
            fields.append((field, wire, data[i:i + 8]))
            i += 8
        else:
            break
    return fields


def decode_packed_varints(payload: bytes) -> list[int]:
    values: list[int] = []
    i = 0
    while i < len(payload):
        value, n = read_varint(payload, i)
        if value is None or n == 0:
            break
        values.append(value)
        i += n
    return values


def zigzag_decode(value: int) -> int:
    return (value >> 1) ^ -(value & 1)
