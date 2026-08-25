"""Garmin-flavoured COBS framing for the GFDI byte stream.

Frames are delimited by 0x00 at both ends; chunk codes cap at 0xFE.
Adapted from openrd-ble-running-dynamics (MIT, Sam Dumont).
"""

from __future__ import annotations


def cobs_encode_garmin(data: bytes) -> bytes:
    out = bytearray([0x00])
    i = 0
    last_zero = False
    while i < len(data):
        j = i
        while j < len(data) and data[j] != 0 and j - i < 0xFE:
            j += 1
        chunk = data[i:j]
        if j - i >= 0xFE:
            out.append(0xFF)
            out.extend(chunk)
            i = j
            last_zero = False
            continue
        out.append(len(chunk) + 1)
        out.extend(chunk)
        if j < len(data) and data[j] == 0:
            i = j + 1
            last_zero = True
        else:
            i = j
            last_zero = False
    if last_zero:
        out.append(0x01)
    out.append(0x00)
    return bytes(out)


def cobs_decode_garmin(frame: bytes) -> bytes | None:
    if len(frame) < 4 or frame[0] != 0x00 or frame[-1] != 0x00:
        return None
    out = bytearray()
    i = 1
    end = len(frame) - 1
    while i < end:
        code = frame[i]
        i += 1
        if code == 0:
            break
        for _ in range(code - 1):
            if i >= end:
                break
            out.append(frame[i])
            i += 1
        if code != 0xFF and i < end:
            out.append(0)
    return bytes(out)
