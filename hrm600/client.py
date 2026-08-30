"""Bleak-based central client for the HRM 600.

Connects, registers Multi-Link services, replays the watch-emulation bootstrap
and decodes standard BLE plus Garmin GFDI/EventSharing streams.
Adapted from openrd-ble-running-dynamics (MIT, Sam Dumont).
"""

from __future__ import annotations

import asyncio
import json
import platform
import re
import struct
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bleak import BleakClient, BleakScanner

from .bootstrap import (
    build_watch_compact_bootstrap_frames,
    build_watch_core_feature_response_ack_frames,
    build_watch_device_information_ack,
)
from .cobs import cobs_decode_garmin, cobs_encode_garmin
from .eventsharing import (
    FIELD_MARKERS,
    build_activity_state_started_proto,
    build_compact_eventsharing_subscribe_response,
    build_eventsharing_subscribe_request,
    build_eventsharing_subscribe_response,
    build_running_algorithm_input_proto,
    decode_smart_payload,
)
from .gfdi import (
    build_compact_eventsharing_message,
    build_compact_smart_message,
    build_protobuf_message,
    build_protobuf_status_ack,
    parse_compact_frame,
    parse_gfdi_message,
    parse_protobuf_transport,
)
from .multilink import (
    CTRL_2820,
    MULTILINK_SERVICES,
    NOTIFY_2810,
    build_register_request,
    parse_register_response,
)

DEFAULT_NAME_REGEX = r"^(HRM\s*600|HRM600)"

STD_HR = "00002a37-0000-1000-8000-00805f9b34fb"
STD_RSC = "00002a53-0000-1000-8000-00805f9b34fb"
STD_BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"

DIS_CHARS = {
    "manufacturer": "00002a29-0000-1000-8000-00805f9b34fb",
    "model": "00002a24-0000-1000-8000-00805f9b34fb",
    "serial": "00002a25-0000-1000-8000-00805f9b34fb",
    "hardware_rev": "00002a27-0000-1000-8000-00805f9b34fb",
    "firmware_rev": "00002a26-0000-1000-8000-00805f9b34fb",
    "software_rev": "00002a28-0000-1000-8000-00805f9b34fb",
}
BODY_SENSOR_LOCATION = "00002a38-0000-1000-8000-00805f9b34fb"


def describe_standard_hr(payload: bytes) -> dict[str, Any]:
    if not payload:
        return {"raw_hex": payload.hex()}
    flags = payload[0]
    if flags & 0x01:
        hr = struct.unpack("<H", payload[1:3])[0] if len(payload) >= 3 else None
    else:
        hr = payload[1] if len(payload) >= 2 else None
    return {"heart_rate_bpm": hr, "flags": flags, "raw_hex": payload.hex()}


def describe_standard_rsc(payload: bytes) -> dict[str, Any]:
    if len(payload) < 4:
        return {"raw_hex": payload.hex()}
    flags = payload[0]
    speed = struct.unpack("<H", payload[1:3])[0] / 256.0
    cadence = payload[3]
    offset = 4
    out: dict[str, Any] = {
        "speed_mps": speed,
        "cadence_spm": cadence,
        "flags": flags,
        "raw_hex": payload.hex(),
    }
    if flags & 0x01 and len(payload) >= offset + 2:
        out["stride_length_m"] = struct.unpack("<H", payload[offset:offset + 2])[0] / 100.0
        offset += 2
    if flags & 0x02 and len(payload) >= offset + 4:
        out["total_distance_m"] = struct.unpack("<I", payload[offset:offset + 4])[0] / 10.0
    return out


@dataclass
class PendingGfdiAction:
    label: str
    request_id: int
    gfdi_bytes: bytes
    completes_request: bool = False


class JsonlRecorder:
    def __init__(self, out_path: Path) -> None:
        self.out_path = out_path
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = out_path.open("w", buffering=1)

    def write(self, event: dict[str, Any]) -> None:
        self.fp.write(json.dumps(event, separators=(",", ":"), default=str) + "\n")

    def close(self) -> None:
        self.fp.close()


class Hrm600Client:
    def __init__(
        self,
        client: BleakClient,
        out_path: Path,
        watch_init: bool = True,
        feed_run_input: bool = False,
        speed_mps: float = 3.0,
        grade_pct: float = 0.0,
        run_input_count: int = 20,
        run_input_period: float = 1.0,
        quiet: bool = False,
    ) -> None:
        self.client = client
        self.watch_init = watch_init
        self.feed_run_input = feed_run_input
        self.speed_mps = speed_mps
        self.grade_pct = grade_pct
        self.run_input_count = run_input_count
        self.run_input_period = run_input_period
        self.quiet = quiet
        self.recorder = JsonlRecorder(out_path)
        self.t0 = time.monotonic()
        self.handle_by_service: dict[int, int] = {}
        self.service_by_handle: dict[int, int] = {}
        self.gfdi_buffer = bytearray()
        self.pending_actions: list[PendingGfdiAction] = []
        self.sent_status_for: set[int] = set()
        self.completed_requests: set[int] = set()
        self.counts: Counter[str] = Counter()
        self.sent_activity_state = False
        self.running_input_sent = 0
        self.next_run_input_at = 0.0
        self.next_compact_counter = 0x01EF
        self.next_compact_sequence = 0x0A
        self.sent_watch_compact_bootstrap = False
        self.sent_core_feature_response_ack = False
        self.answered_watch_side_subscribe = False
        self.sent_strap_side_subscribe_requests = False
        # optional observers (file transfer / file sync / status displays)
        self.on_event: Any = None
        self.on_gfdi_frame: Any = None
        self.on_register_ok: Any = None
        self.on_service_payload: Any = None
        self.on_service_close: Any = None
        self.pending_raw: list[tuple[str, bytes]] = []

    def now(self) -> float:
        return round(time.monotonic() - self.t0, 3)

    def emit(self, event: dict[str, Any], line: str | None = None) -> None:
        event.setdefault("t", self.now())
        self.recorder.write(event)
        self.counts[event.get("kind", "unknown")] += 1
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                pass
        if line is not None and not self.quiet:
            print(f"{event['t']:7.2f}s  {line}", flush=True)

    async def start(self) -> None:
        await self.client.start_notify(NOTIFY_2810, self.on_multilink_notify)
        self.log("[SUB] Garmin Multi-Link 2810")
        await self.try_standard_subscribe(STD_HR, "standard_hr", self.on_standard_hr)
        await self.try_standard_subscribe(STD_RSC, "standard_rsc", self.on_standard_rsc)
        await self.try_standard_subscribe(STD_BATTERY, "battery", self.on_battery)

    async def stop(self) -> None:
        for uuid in (NOTIFY_2810, STD_HR, STD_RSC, STD_BATTERY):
            try:
                await self.client.stop_notify(uuid)
            except Exception:
                pass
        self.recorder.close()

    def log(self, line: str) -> None:
        if not self.quiet:
            print(f"  {line}", flush=True)

    async def try_standard_subscribe(self, uuid: str, name: str, callback: Any) -> None:
        try:
            await self.client.start_notify(uuid, callback)
            self.log(f"[SUB] {name} {uuid}")
        except Exception as e:
            self.log(f"[SUBFAIL] {name}: {e}")

    async def register_multilink_services(self, services: list[int]) -> None:
        for service_id in services:
            name = MULTILINK_SERVICES.get(service_id, f"svc{service_id}")
            msg = build_register_request(service_id)
            self.log(f"[REG] {name} ({service_id}) <- {msg.hex()}")
            await self.client.write_gatt_char(CTRL_2820, msg, response=True)
            await asyncio.sleep(0.8)

    async def send_initial_watch_events(self) -> None:
        gfdi_handle = self.handle_by_service.get(1)
        if gfdi_handle is None:
            self.log("[WARN] no GFDI handle; cannot send watch init")
            return
        await self.send_to_gfdi(
            gfdi_handle,
            build_watch_device_information_ack(),
            "DeviceInformation ACK fenix8",
        )

    async def pump_pending_actions(self) -> None:
        if self.pending_raw:
            label, data = self.pending_raw.pop(0)
            self.log(f"[RAW TX] {label}: {data.hex()}")
            try:
                await self.client.write_gatt_char(CTRL_2820, data, response=True)
                self.emit({"kind": "raw_tx", "label": label, "raw_hex": data.hex()})
            except Exception as e:
                self.emit({"kind": "raw_tx_error", "label": label, "error": str(e)},
                          f"[TXFAIL] {label}: {e}")
            return
        gfdi_handle = self.handle_by_service.get(1)
        if gfdi_handle is None or not self.pending_actions:
            return
        action = self.pending_actions.pop(0)
        if action.label.startswith("status-ack") and action.request_id in self.sent_status_for:
            return
        if action.completes_request and action.request_id in self.completed_requests:
            return
        ok = await self.send_to_gfdi(gfdi_handle, action.gfdi_bytes, action.label)
        if not ok:
            return
        if action.label.startswith("status-ack"):
            self.sent_status_for.add(action.request_id)
        if action.completes_request:
            self.completed_requests.add(action.request_id)

    async def pump_run_inputs(self) -> None:
        if not self.feed_run_input or not self.completed_requests:
            return
        gfdi_handle = self.handle_by_service.get(1)
        if gfdi_handle is None:
            return
        now = time.monotonic()
        if not self.sent_activity_state:
            proto = build_activity_state_started_proto()
            counter, sequence = self.next_compact_ids()
            ok = await self.send_to_gfdi(
                gfdi_handle,
                build_compact_eventsharing_message(proto, sequence=sequence, counter=counter),
                f"ActivityState START compact seq={sequence} counter=0x{counter:04x}",
            )
            if ok:
                self.sent_activity_state = True
                self.next_run_input_at = now + 0.5
            return
        if self.running_input_sent >= self.run_input_count or now < self.next_run_input_at:
            return
        proto = build_running_algorithm_input_proto(speed_mps=self.speed_mps, grade_pct=self.grade_pct)
        counter, sequence = self.next_compact_ids()
        ok = await self.send_to_gfdi(
            gfdi_handle,
            build_compact_eventsharing_message(proto, sequence=sequence, counter=counter),
            f"RunningAlgorithmInput compact seq={sequence} counter=0x{counter:04x}",
        )
        if ok:
            self.running_input_sent += 1
            self.next_run_input_at = time.monotonic() + self.run_input_period

    def next_compact_ids(self) -> tuple[int, int]:
        counter = self.next_compact_counter
        sequence = self.next_compact_sequence
        self.next_compact_counter = (self.next_compact_counter + 1) & 0xFFFF
        self.next_compact_sequence = (self.next_compact_sequence + 1) & 0x7F
        if self.next_compact_sequence == 0:
            self.next_compact_sequence = 1
        return counter, sequence

    def enqueue_gfdi(self, label: str, gfdi_bytes: bytes) -> None:
        """Queue an arbitrary GFDI frame for transmission (used by probe/sync)."""
        self.pending_actions.append(
            PendingGfdiAction(label=label, request_id=-1, gfdi_bytes=gfdi_bytes)
        )

    def enqueue_raw(self, label: str, data: bytes) -> None:
        """Queue a raw write to the Multi-Link control char (register, stream open)."""
        self.pending_raw.append((label, data))

    async def send_to_gfdi(self, gfdi_handle: int, gfdi_msg: bytes, label: str) -> bool:
        encoded = cobs_encode_garmin(gfdi_msg)
        chunks = [encoded[i:i + 19] for i in range(0, len(encoded), 19)]
        self.log(
            f"[GFDI TX] {label}: gfdi={len(gfdi_msg)}B cobs={len(encoded)}B "
            f"chunks={[len(chunk) + 1 for chunk in chunks]}"
        )
        try:
            for chunk in chunks:
                await self.client.write_gatt_char(CTRL_2820, bytes([gfdi_handle]) + chunk, response=True)
                await asyncio.sleep(0.03)
            self.emit({
                "kind": "gfdi_tx",
                "label": label,
                "handle": gfdi_handle,
                "gfdi_hex": gfdi_msg.hex(),
                "cobs_hex": encoded.hex(),
            })
            return True
        except Exception as e:
            self.emit({"kind": "gfdi_tx_error", "label": label, "error": str(e)}, f"[TXFAIL] {label}: {e}")
            return False

    def on_standard_hr(self, _sender: Any, data: bytearray) -> None:
        payload = bytes(data)
        decoded = describe_standard_hr(payload)
        hr = decoded.get("heart_rate_bpm")
        self.emit({"kind": "standard_hr", **decoded}, f"STD HR {hr} bpm raw={payload.hex()}")

    def on_standard_rsc(self, _sender: Any, data: bytearray) -> None:
        payload = bytes(data)
        decoded = describe_standard_rsc(payload)
        speed = decoded.get("speed_mps")
        cadence = decoded.get("cadence_spm")
        self.emit(
            {"kind": "standard_rsc", **decoded},
            f"STD RSC speed={speed} m/s cadence={cadence} spm raw={payload.hex()}",
        )

    def on_battery(self, _sender: Any, data: bytearray) -> None:
        payload = bytes(data)
        level = payload[0] if payload else None
        self.emit({"kind": "battery", "level_pct": level, "raw_hex": payload.hex()}, f"BAT {level}%")

    def on_multilink_notify(self, _sender: Any, data: bytearray) -> None:
        raw = bytes(data)
        if len(raw) < 2:
            self.emit({"kind": "multilink_tiny", "raw_hex": raw.hex()}, f"ML tiny {raw.hex()}")
            return
        handle = raw[0]
        if handle == 0x00:
            self.handle_management(raw)
            return
        payload = raw[1:]
        service_id = self.service_by_handle.get(handle)
        if service_id == 6:
            self.decode_realtime_hr(handle, payload)
        elif service_id == 1:
            self.decode_gfdi_payload(handle, payload)
        else:
            service_name = MULTILINK_SERVICES.get(service_id, f"svc{service_id}")
            self.emit(
                {
                    "kind": "multilink_payload",
                    "handle": handle,
                    "service_id": service_id,
                    "service_name": service_name,
                    "payload_hex": payload.hex()[:80],
                    "payload_len": len(payload),
                },
                f"ML handle={handle} {service_name} len={len(payload)}",
            )
            if self.on_service_payload is not None and service_id is not None:
                try:
                    self.on_service_payload(service_id, handle, payload)
                except Exception as e:
                    self.emit({"kind": "service_hook_error", "error": str(e)}, f"[HOOKFAIL] {e}")

    def handle_management(self, raw: bytes) -> None:
        # CLOSE_HANDLE_RESP: [0x00][0x03][client_id:8][service:2][handle:1][status:1]
        if len(raw) >= 14 and raw[1] == 0x03:
            service_id = struct.unpack("<H", raw[10:12])[0]
            ml_handle = raw[12]
            status = raw[13]
            self.service_by_handle.pop(ml_handle, None)
            self.handle_by_service.pop(service_id, None)
            self.emit(
                {"kind": "service_close", "service_id": service_id, "handle": ml_handle,
                 "status": status},
                f"SERVICE CLOSE svc=0x{service_id:04x} handle={ml_handle} status={status}",
            )
            if self.on_service_close is not None:
                try:
                    self.on_service_close(service_id, ml_handle, status)
                except Exception as e:
                    self.emit({"kind": "service_hook_error", "error": str(e)}, f"[HOOKFAIL] {e}")
            return
        parsed = parse_register_response(raw)
        if parsed is None:
            self.emit({"kind": "multilink_mgmt", "raw_hex": raw.hex()}, f"ML mgmt raw={raw.hex()}")
            return
        service_id = parsed["service_id"]
        service_name = MULTILINK_SERVICES.get(service_id, f"svc{service_id}")
        if parsed["status"] == 0:
            handle = parsed["handle"]
            self.handle_by_service[service_id] = handle
            self.service_by_handle[handle] = service_id
            self.emit(
                {"kind": "register_ok", "service_name": service_name, **parsed},
                f"REGISTER OK {service_name} svc={service_id} handle={handle}",
            )
            if self.on_register_ok is not None:
                try:
                    self.on_register_ok(service_id, handle)
                except Exception as e:
                    self.emit({"kind": "service_hook_error", "error": str(e)}, f"[HOOKFAIL] {e}")
        else:
            self.emit(
                {"kind": "register_fail", "service_name": service_name, **parsed},
                f"REGISTER FAIL {service_name} svc={service_id} status={parsed['status']}",
            )

    def decode_realtime_hr(self, handle: int, payload: bytes) -> None:
        event: dict[str, Any] = {
            "kind": "realtime_hr",
            "handle": handle,
            "raw_hex": payload.hex(),
        }
        if len(payload) >= 3:
            event.update({"sample_type": payload[0], "heart_rate_bpm": payload[1], "resting_bpm": payload[2]})
        self.emit(event, f"REALTIME_HR hr={event.get('heart_rate_bpm')} raw={payload.hex()}")

    def decode_gfdi_payload(self, handle: int, payload: bytes) -> None:
        self.gfdi_buffer.extend(payload)
        for decoded in self.consume_gfdi_stream():
            parsed = parse_gfdi_message(decoded)
            if parsed is None:
                continue
            type_id = parsed["type_id"]
            type_name = parsed["type_name"]
            base = {
                "kind": "gfdi_rx",
                "handle": handle,
                "type_id": type_id,
                "type_name": type_name,
                "crc_ok": parsed["crc_ok"],
                "decoded_hex": decoded.hex(),
            }
            if self.on_gfdi_frame is not None:
                try:
                    self.on_gfdi_frame(parsed)
                except Exception as e:
                    self.emit({"kind": "gfdi_hook_error", "error": str(e)}, f"[HOOKFAIL] {e}")
            if type_id == 5024:
                self.emit(base, "GFDI DEVICE_INFORMATION")
            elif type_id in (5043, 5044):
                self.handle_protobuf_transport(base, decoded)
            elif type_id == 5011:
                self.emit(base, f"GFDI FIT_DEFINITION {decoded.hex()[:120]}")
            elif type_id == 5012:
                self.emit(base, f"GFDI FIT_DATA {decoded.hex()[:120]}")
            elif type_id == 5000:
                self.emit(base, f"GFDI RESPONSE {decoded.hex()}")
            else:
                compact = parse_compact_frame(decoded)
                if compact is not None:
                    self.handle_compact_smart(base, compact)
                else:
                    self.emit(base, f"GFDI {type_name} type={type_id} hex={decoded.hex()[:160]}")

    def consume_gfdi_stream(self) -> list[bytes]:
        out: list[bytes] = []
        while True:
            try:
                start = self.gfdi_buffer.index(0x00)
            except ValueError:
                self.gfdi_buffer.clear()
                return out
            try:
                end = self.gfdi_buffer.index(0x00, start + 1)
            except ValueError:
                del self.gfdi_buffer[:start]
                return out
            frame = bytes(self.gfdi_buffer[start:end + 1])
            del self.gfdi_buffer[:end + 1]
            self.gfdi_buffer.insert(0, 0x00)
            decoded = cobs_decode_garmin(frame)
            if decoded is not None:
                out.append(decoded)

    def handle_protobuf_transport(self, base: dict[str, Any], decoded: bytes) -> None:
        parsed = parse_protobuf_transport(decoded)
        if parsed is None:
            self.emit(base, f"GFDI {base['type_name']} malformed hex={decoded.hex()}")
            return
        protobuf = parsed["protobuf"]
        events = decode_smart_payload(protobuf)
        marker_hits = [name for name, marker in FIELD_MARKERS.items() if marker in protobuf]
        event = {
            **base,
            "request_id": parsed["request_id"],
            "data_offset": parsed["data_offset"],
            "total_len": parsed["total_len"],
            "chunk_len": parsed["chunk_len"],
            "protobuf_hex": protobuf.hex(),
            "decoded_events": events,
            "field_markers": marker_hits,
        }
        label = (
            f"{parsed['msg_name']} req={parsed['request_id']} "
            f"events={[e['kind'] for e in events]} markers={marker_hits}"
        )
        self.emit(event, label)
        if self.watch_init and parsed["msg_type"] == 5043:
            self.queue_watch_reply(parsed["request_id"], events)

    def queue_watch_reply(self, request_id: int, events: list[dict[str, Any]]) -> None:
        if request_id in self.completed_requests:
            return
        self.pending_actions.append(
            PendingGfdiAction(
                label=f"status-ack req#{request_id}",
                request_id=request_id,
                gfdi_bytes=build_protobuf_status_ack(request_id, kept=True, error_code=0),
            )
        )
        wants_eventsharing_subscribe = any(event["kind"] == "subscribe_request" for event in events)
        if wants_eventsharing_subscribe:
            response = build_eventsharing_subscribe_response([22, 23])
            self.pending_actions.append(
                PendingGfdiAction(
                    label=f"EventSharing SubscribeResponse req#{request_id}",
                    request_id=request_id,
                    gfdi_bytes=build_protobuf_message(5044, request_id, response),
                    completes_request=True,
                )
            )

    def queue_compact_watch_reply(self, label: str, gfdi_bytes: bytes, request_id: int = -1,
                                  completes_request: bool = False) -> None:
        self.pending_actions.append(
            PendingGfdiAction(
                label=label,
                request_id=request_id,
                gfdi_bytes=gfdi_bytes,
                completes_request=completes_request,
            )
        )

    def queue_watch_compact_bootstrap(self) -> None:
        if self.sent_watch_compact_bootstrap:
            return
        self.sent_watch_compact_bootstrap = True
        for label, gfdi_bytes in build_watch_compact_bootstrap_frames():
            self.queue_compact_watch_reply(label, gfdi_bytes)

    def queue_core_feature_response_ack(self) -> None:
        if self.sent_core_feature_response_ack:
            return
        self.sent_core_feature_response_ack = True
        for label, gfdi_bytes in build_watch_core_feature_response_ack_frames():
            self.queue_compact_watch_reply(label, gfdi_bytes)

    def queue_strap_side_subscribe_requests(self) -> None:
        if self.sent_strap_side_subscribe_requests:
            return
        self.sent_strap_side_subscribe_requests = True
        counter, sequence = self.next_compact_ids()
        self.queue_compact_watch_reply(
            "Watch SubscribeRequest HR type20 compact",
            build_compact_eventsharing_message(
                build_eventsharing_subscribe_request([20]),
                sequence=sequence,
                counter=counter,
            ),
        )
        counter, sequence = self.next_compact_ids()
        self.queue_compact_watch_reply(
            "Watch SubscribeRequest HR+RD type20+21 compact",
            build_compact_eventsharing_message(
                build_eventsharing_subscribe_request([20, 21]),
                sequence=sequence,
                counter=counter,
            ),
        )

    def queue_watch_side_subscribe_response(self, counter: int) -> None:
        if self.answered_watch_side_subscribe:
            return
        self.answered_watch_side_subscribe = True
        self.queue_strap_side_subscribe_requests()
        _, sequence = self.next_compact_ids()
        self.queue_compact_watch_reply(
            "EventSharing SubscribeResponse 22/23 compact",
            build_compact_smart_message(
                build_compact_eventsharing_subscribe_response([22, 23]),
                frame_kind=0x3A,
                sequence=sequence,
                counter=counter,
            ),
            request_id=counter,
            completes_request=True,
        )

    def handle_compact_smart(self, base: dict[str, Any], frame: dict[str, Any]) -> None:
        protobuf = frame["payload"]
        events = decode_smart_payload(protobuf)
        marker_hits = [name for name, marker in FIELD_MARKERS.items() if marker in protobuf]
        self.emit(
            {
                **base,
                "kind": "gfdi_compact_smart",
                "compact_kind": frame["kind"],
                "compact_counter": frame["counter"],
                "protobuf_hex": protobuf.hex(),
                "decoded_events": events,
                "field_markers": marker_hits,
            },
            f"GFDI compact events={[e['kind'] for e in events]} markers={marker_hits}",
        )
        if not self.watch_init:
            return
        if frame["kind"] == 0x32:
            self.queue_watch_compact_bootstrap()
        if any(event["kind"] == "feature_capabilities_response" for event in events):
            self.queue_core_feature_response_ack()
        wants_watch_side_inputs = False
        for event in events:
            if event["kind"] != "subscribe_request":
                continue
            alert_types = {
                alert["type"]
                for alert in event["data"].get("alerts", [])
                if "type" in alert
            }
            if {22, 23}.issubset(alert_types):
                wants_watch_side_inputs = True
        if wants_watch_side_inputs:
            self.queue_watch_side_subscribe_response(frame["counter"])


async def resolve_target(address: str | None, name_regex: str, scan_timeout: float) -> Any:
    if address:
        if platform.system() == "Darwin" and re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", address):
            print(
                "# macOS/CoreBluetooth does not expose BLE MACs; scanning by name instead.",
                file=sys.stderr,
            )
        else:
            return address

    pattern = re.compile(name_regex, re.IGNORECASE)
    print(f"# Scanning {scan_timeout:.1f}s for name regex {name_regex!r}...", file=sys.stderr)
    devices = await BleakScanner.discover(timeout=scan_timeout)
    matches = [d for d in devices if d.name and pattern.search(d.name)]
    if not matches:
        seen = ", ".join(sorted(d.name for d in devices if d.name)[:20])
        raise SystemExit(f"No HRM 600 match found. Named devices seen: {seen or '(none)'}")
    if len(matches) > 1:
        print("# Multiple matches:", file=sys.stderr)
        for idx, device in enumerate(matches):
            print(f"#   [{idx}] {device.name} {device.address}", file=sys.stderr)
    target = matches[0]
    print(f"# Using {target.name} {target.address}", file=sys.stderr)
    return target


async def read_device_info(client: BleakClient) -> dict[str, Any]:
    info: dict[str, Any] = {}
    for key, uuid in DIS_CHARS.items():
        try:
            info[key] = (await client.read_gatt_char(uuid)).decode(errors="replace")
        except Exception:
            info[key] = None
    try:
        info["battery_pct"] = (await client.read_gatt_char(STD_BATTERY))[0]
    except Exception:
        info["battery_pct"] = None
    try:
        loc = (await client.read_gatt_char(BODY_SENSOR_LOCATION))[0]
        info["body_sensor_location"] = {1: "chest"}.get(loc, str(loc))
    except Exception:
        info["body_sensor_location"] = None
    return info
