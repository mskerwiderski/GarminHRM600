"""Garmin Multi-Link service: UUIDs, service registration, handle management.

Adapted from openrd-ble-running-dynamics (MIT, Sam Dumont).
"""

from __future__ import annotations

import struct

MULTILINK_SERVICE = "6a4e2800-667b-11e3-949a-0800200c9a66"
CTRL_2820 = "6a4e2820-667b-11e3-949a-0800200c9a66"
NOTIFY_2810 = "6a4e2810-667b-11e3-949a-0800200c9a66"
NOTIFY_2811 = "6a4e2811-667b-11e3-949a-0800200c9a66"
NOTIFY_2812 = "6a4e2812-667b-11e3-949a-0800200c9a66"

CLIENT_ID = 2

MULTILINK_SERVICES = {
    1: "GFDI",
    6: "REALTIME_HR",
    7: "REALTIME_STEPS",
    10: "REALTIME_INTENSITY",
    12: "REALTIME_HRV",
    16: "REALTIME_ACCEL",
    20: "REALTIME_BODY_BATTERY",
}


def build_register_request(service_id: int, client_id: int = CLIENT_ID, reliable: bool = False) -> bytes:
    return (
        b"\x00"
        + b"\x00"
        + struct.pack("<Q", client_id)
        + struct.pack("<H", service_id)
        + bytes([0x02 if reliable else 0x00])
    )


def parse_register_response(data: bytes) -> dict[str, int] | None:
    if len(data) < 15 or data[0] != 0x00 or data[1] != 0x01:
        return None
    return {
        "client_id": struct.unpack("<Q", data[2:10])[0],
        "service_id": struct.unpack("<H", data[10:12])[0],
        "status": data[12],
        "handle": data[13],
        "reliable": data[14],
    }
