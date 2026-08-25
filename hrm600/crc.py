"""Garmin CRC16 as used by GFDI frames and FIT files.

Adapted from openrd-ble-running-dynamics (MIT, Sam Dumont).
"""

from __future__ import annotations

_CRC_TABLE = [
    0x0000, 0xCC01, 0xD801, 0x1400,
    0xF001, 0x3C00, 0x2800, 0xE401,
    0xA001, 0x6C00, 0x7800, 0xB401,
    0x5000, 0x9C01, 0x8801, 0x4400,
]


def garmin_crc(data: bytes, initial: int = 0) -> int:
    crc = initial
    for b in data:
        crc = (((crc >> 4) & 0xFFF) ^ _CRC_TABLE[crc & 0xF]) ^ _CRC_TABLE[b & 0xF]
        crc = (((crc >> 4) & 0xFFF) ^ _CRC_TABLE[crc & 0xF]) ^ _CRC_TABLE[(b >> 4) & 0xF]
    return crc & 0xFFFF
