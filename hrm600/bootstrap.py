"""Watch-emulation bootstrap frames, captured from a validated fenix 8 flow.

These exact bytes make the strap open the Garmin app-layer streams toward a
central that is not a Garmin watch. Order matters; see docs in protocol.md of
openrd-ble-running-dynamics (MIT, Sam Dumont), section "Real-HRM Client Proof".
"""

from __future__ import annotations

CAPTURED_WATCH_DEVICE_INFORMATION_ACK = bytes.fromhex(
    "34008813a013009700b811a13309d85b08a00f0e66656e69782038202d2035316d6d"
    "0566656e69780838202d2035316d6d030fd7"
)
CAPTURED_WATCH_COMPACT_BOOTSTRAP_ACK = bytes.fromhex("09000081ba13009de9")
CAPTURED_WATCH_COMPACT_SESSION_CORE = bytes.fromhex(
    "170032801000000000000000000000028400b80000076e"
)
CAPTURED_WATCH_CORE_FEATURE_REQUEST = bytes.fromhex(
    "1c003981e9016a12421010018201021000d20100ca0100da0100ea48"
)
CAPTURED_WATCH_CORE_FEATURE_RESPONSE_ACK = bytes.fromhex("0d000082c21300e901000036e6")
CAPTURED_WATCH_CORE_READY_TRIGGER = bytes.fromhex("08001e820800a088")


def build_watch_device_information_ack() -> bytes:
    return CAPTURED_WATCH_DEVICE_INFORMATION_ACK


def build_watch_core_feature_request() -> bytes:
    return CAPTURED_WATCH_CORE_FEATURE_REQUEST


def build_watch_compact_bootstrap_frames() -> list[tuple[str, bytes]]:
    return [
        ("Watch compact bootstrap ack", CAPTURED_WATCH_COMPACT_BOOTSTRAP_ACK),
        ("Watch compact session/core preamble", CAPTURED_WATCH_COMPACT_SESSION_CORE),
        ("Core FeatureCapabilitiesRequest compact", CAPTURED_WATCH_CORE_FEATURE_REQUEST),
    ]


def build_watch_core_feature_response_ack_frames() -> list[tuple[str, bytes]]:
    return [
        ("Core FeatureCapabilitiesResponse status ack", CAPTURED_WATCH_CORE_FEATURE_RESPONSE_ACK),
        ("Core ready trigger 0x821e", CAPTURED_WATCH_CORE_READY_TRIGGER),
    ]
