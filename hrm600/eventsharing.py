"""Garmin Smart/EventSharing payloads: subscriptions, alerts, decoders.

Alert types: 20 HR, 21 Running Measurement (strap->watch); 22 activity state,
23 running algorithm input (watch->strap).
Adapted from openrd-ble-running-dynamics (MIT, Sam Dumont).
"""

from __future__ import annotations

from typing import Any

from .protobuf import (
    decode_packed_varints,
    field_len,
    field_sfixed32,
    field_varint,
    parse_fields,
    zigzag_decode,
)

ALERT_TYPES = {
    20: "HEART_RATE_MEASUREMENT",
    21: "RUNNING_MEASUREMENT",
    22: "ACCESSORY_UTILITIES_ACTIVITY_STATE",
    23: "RUNNING_ALGORITHM_INPUT",
}

FIELD_MARKERS = {
    "field1013_heart_rate": bytes([0xAA, 0x3F]),
    "field1014_running_measurement": bytes([0xB2, 0x3F]),
    "field1015_type22_data": bytes([0xBA, 0x3F]),
    "field1016_algorithm_input": bytes([0xC2, 0x3F]),
}

FEATURE_EXTENSIONS = {
    16: "extension_16",
    25: "running",
    26: "accessory_utilities",
}


def build_eventsharing_subscribe_response(alert_types: list[int]) -> bytes:
    alert_statuses = bytearray()
    for alert_type in alert_types:
        if not 0 <= alert_type <= 0x7F:
            raise ValueError(f"alert type {alert_type} needs multi-byte varint support")
        status = b"\x08\x00" + b"\x10" + bytes([alert_type])
        alert_statuses.extend(b"\x0a" + bytes([len(status)]) + status)

    eventsharing_body = b"\x12" + bytes([len(alert_statuses)]) + bytes(alert_statuses)
    return b"\xf2\x01" + bytes([len(eventsharing_body)]) + eventsharing_body


def smart_eventsharing(eventsharing_body: bytes) -> bytes:
    return field_len(30, eventsharing_body)


def eventsharing_alert(alert_body: bytes) -> bytes:
    return smart_eventsharing(field_len(3, alert_body))


def build_eventsharing_subscribe_request(alert_types: list[int]) -> bytes:
    alerts = bytearray()
    for alert_type in alert_types:
        alerts.extend(field_len(1, field_varint(1, alert_type)))
    return smart_eventsharing(field_len(1, bytes(alerts)))


def build_compact_eventsharing_subscribe_response(alert_types: list[int]) -> bytes:
    alert_statuses = bytearray()
    for alert_type in alert_types:
        status = field_varint(1, 0) + field_len(2, field_varint(1, alert_type))
        alert_statuses.extend(field_len(1, status))
    return smart_eventsharing(field_len(2, bytes(alert_statuses)))


def build_activity_state_started_proto(sport_fit_type: int = 1, sub_sport: int = 0) -> bytes:
    activity_state = (
        field_varint(1, sport_fit_type)
        + field_varint(2, sub_sport)
        + field_varint(3, 0)
    )
    alert = (
        field_varint(1, 22)
        + field_len(1015, activity_state)
    )
    return eventsharing_alert(alert)


def build_running_algorithm_input_proto(speed_mps: float = 3.0, grade_pct: float = 0.0) -> bytes:
    gps_speed_mm_s = max(0, int(round(speed_mps * 1000.0)))
    horizontal_speed_1_256ths_ms = max(0, int(round(speed_mps * 256.0)))
    grade_1_100_pct = int(round(grade_pct * 100.0))
    speed_distance_inputs = (
        field_sfixed32(1, grade_1_100_pct)
        + field_varint(2, gps_speed_mm_s)
    )
    algorithm_input = (
        field_varint(1, horizontal_speed_1_256ths_ms)
        + field_len(2, speed_distance_inputs)
    )
    alert = (
        field_varint(1, 23)
        + field_len(1016, algorithm_input)
    )
    return eventsharing_alert(alert)


def decode_speed_distance(payload: bytes) -> dict[str, float]:
    out: dict[str, float] = {}
    for field, wire, value in parse_fields(payload):
        if wire != 0 or not isinstance(value, int):
            continue
        if field == 1:
            out["speed_mps"] = value / 256.0
        elif field == 2:
            out["distance_m"] = value / 16.0
    return out


def decode_step_speed_loss(payload: bytes) -> dict[str, int]:
    out: dict[str, int] = {}
    for field, wire, value in parse_fields(payload):
        if wire != 0 or not isinstance(value, int):
            continue
        if field == 1:
            out["step_forward_velocity_trough_mm_per_s"] = zigzag_decode(value)
        elif field == 2:
            out["step_forward_velocity_peak_mm_per_s"] = zigzag_decode(value)
        elif field == 3:
            out["step_forward_velocity_mean_mm_per_s"] = zigzag_decode(value)
        elif field == 4:
            out["step_vertical_velocity_at_trough_mm_per_s"] = zigzag_decode(value)
        elif field == 5:
            out["time_to_trough_ms"] = value
    return out


def decode_dynamics(payload: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field, wire, value in parse_fields(payload):
        if wire != 0:
            if field == 11 and wire == 2 and isinstance(value, bytes):
                out["step_speed_loss"] = decode_step_speed_loss(value)
                out["step_speed_loss_data_hex"] = value.hex()
            continue
        assert isinstance(value, int)
        if field == 1:
            out["vertical_oscillation_mm"] = value / 4.0
        elif field == 2:
            out["ground_contact_time_ms"] = value
        elif field == 3:
            out["stance_time_pct"] = value / 4.0
        elif field == 4:
            out["ground_contact_balance_pct"] = value / 32.0
        elif field == 5:
            out["vertical_ratio_pct"] = value / 32.0
        elif field == 6:
            out["step_length_mm"] = value
        elif field == 7:
            out["is_module_right_side_up"] = bool(value)
        elif field == 8:
            out["cadence_strides_per_min"] = value / 32.0
        elif field == 9:
            out["step_count"] = value
        elif field == 10:
            out["is_walking"] = bool(value)
    return out


def decode_measurement(payload: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field, wire, value in parse_fields(payload):
        if wire != 2 or not isinstance(value, bytes):
            continue
        if field == 1:
            out["dynamics"] = decode_dynamics(value)
        elif field == 2:
            out["speed_distance"] = decode_speed_distance(value)
    return out


def decode_algorithm_input(payload: bytes) -> dict[str, float]:
    out: dict[str, float] = {}
    for field, wire, value in parse_fields(payload):
        if field == 1 and wire == 0 and isinstance(value, int):
            out["horizontal_speed_mps"] = value / 256.0
        elif field == 2 and wire == 2 and isinstance(value, bytes):
            for sf, sw, sv in parse_fields(value):
                if sf == 1 and sw == 0 and isinstance(sv, int):
                    out["grade_pct"] = zigzag_decode(sv) / 100.0
                elif sf == 2 and sw == 0 and isinstance(sv, int):
                    out["gps_speed_mps"] = sv / 1000.0
    return out


def decode_alert_message(payload: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field, wire, value in parse_fields(payload):
        if field == 1 and wire == 0 and isinstance(value, int):
            out["type"] = value
            out["type_name"] = ALERT_TYPES.get(value, f"UNKNOWN_{value}")
        elif field == 2 and wire == 0 and isinstance(value, int):
            out["interval"] = value
    return out


def decode_subscribe_request(payload: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {"alerts": []}
    for field, wire, value in parse_fields(payload):
        if field == 1 and wire == 2 and isinstance(value, bytes):
            out["alerts"] = [*out["alerts"], decode_alert_message(value)]
        elif field == 2 and wire == 0 and isinstance(value, int):
            out["target_distance"] = value
    return out


def decode_subscribe_response(payload: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {"alert_status": []}
    for field, wire, value in parse_fields(payload):
        if field != 1 or wire != 2 or not isinstance(value, bytes):
            continue
        status: dict[str, Any] = {}
        for sf, sw, sv in parse_fields(value):
            if sf == 1 and sw == 0 and isinstance(sv, int):
                status["status"] = sv
                status["status_name"] = "SUCCESS" if sv == 0 else f"UNKNOWN_{sv}"
            elif sf == 2 and sw == 2 and isinstance(sv, bytes):
                status["type"] = decode_alert_message(sv)
        out["alert_status"] = [*out["alert_status"], status]
    return out


def decode_eventsharing(payload: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for field, wire, value in parse_fields(payload):
        if wire != 2 or not isinstance(value, bytes):
            continue
        if field == 1:
            events.append({"kind": "subscribe_request", "data": decode_subscribe_request(value)})
        elif field == 2:
            events.append({"kind": "subscribe_response", "data": decode_subscribe_response(value)})
        elif field == 3:
            alert: dict[str, Any] = {"types": []}
            for af, aw, av in parse_fields(value):
                if af == 1 and aw == 0 and isinstance(av, int):
                    alert["types"] = [*alert["types"], av]
                elif af == 1 and aw == 2 and isinstance(av, bytes):
                    alert["types"] = [*alert["types"], *decode_packed_varints(av)]
                elif af == 1013 and aw == 2 and isinstance(av, bytes):
                    alert["heart_rate"] = decode_heart_rate(av)
                elif af == 1014 and aw == 2 and isinstance(av, bytes):
                    alert["measurement"] = decode_measurement(av)
                elif af == 1016 and aw == 2 and isinstance(av, bytes):
                    alert["algorithm_input"] = decode_algorithm_input(av)
                elif aw == 2 and isinstance(av, bytes):
                    alert[f"extension_{af}_hex"] = av.hex()
            events.append({"kind": "alert_notification", "data": alert})
        elif field == 4:
            events.append({"kind": "support_request", "data": {}})
        elif field == 5:
            events.append({"kind": "support_response", "data": {"raw_hex": value.hex()}})
    return events


def decode_heart_rate(payload: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {"raw_hex": payload.hex()}
    for field, wire, value in parse_fields(payload):
        if wire == 0 and isinstance(value, int) and field == 1:
            out["heart_rate_bpm"] = value
    return out


def decode_running_capabilities_response(payload: bytes) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field, wire, value in parse_fields(payload):
        if wire != 2 or not isinstance(value, bytes):
            continue
        if field == 1:
            for sf, sw, sv in parse_fields(value):
                if sf == 1 and sw == 0 and isinstance(sv, int):
                    out["speed_distance_auto_calibration"] = bool(sv)
        elif field == 2:
            for sf, sw, sv in parse_fields(value):
                if sf == 1 and sw == 0 and isinstance(sv, int):
                    out["dynamics_step_speed_loss"] = bool(sv)
    return out


def decode_feature_capabilities(payload: bytes, is_response: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"extensions": []}
    for field, wire, value in parse_fields(payload):
        if wire == 0 and isinstance(value, int):
            if field == 1 and is_response:
                out["guid_status"] = value
            elif field == 2:
                out["version" if is_response else "client_version"] = value
        elif wire == 2 and isinstance(value, bytes):
            name = FEATURE_EXTENSIONS.get(field, f"extension_{field}")
            out["extensions"] = [*out["extensions"], name]
            if is_response and field == 25:
                out["running"] = decode_running_capabilities_response(value)
    return out


def decode_core_service(payload: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for field, wire, value in parse_fields(payload):
        if wire != 2 or not isinstance(value, bytes):
            continue
        if field == 8:
            events.append({
                "kind": "feature_capabilities_request",
                "data": decode_feature_capabilities(value, is_response=False),
            })
        elif field == 9:
            events.append({
                "kind": "feature_capabilities_response",
                "data": decode_feature_capabilities(value, is_response=True),
            })
        else:
            events.append({"kind": "core_service", "data": {"raw_hex": value.hex()}})
    return events


def decode_smart_payload(payload: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for field, wire, value in parse_fields(payload):
        if field == 30 and wire == 2 and isinstance(value, bytes):
            events.extend(decode_eventsharing(value))
        elif field == 13 and wire == 2 and isinstance(value, bytes):
            events.extend(decode_core_service(value))
    return events
