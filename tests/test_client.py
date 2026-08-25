"""Offline client behavior tests using a fake Bleak client."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from hrm600.client import Hrm600Client
from hrm600.gfdi import parse_compact_frame


class FakeBleakClient:
    async def write_gatt_char(self, *_args: object, **_kwargs: object) -> None:
        pass


def make_client(tmp_path: Path) -> Hrm600Client:
    client = Hrm600Client(
        FakeBleakClient(),
        out_path=tmp_path / "client.jsonl",
        watch_init=True,
        quiet=True,
    )
    client.handle_by_service[1] = 7
    return client


def sent_events(out_path: Path) -> list[dict]:
    return [
        event
        for event in (json.loads(line) for line in out_path.read_text().splitlines())
        if event["kind"] == "gfdi_tx"
    ]


def test_watch_bootstrap_after_8132_reuses_known_good_capture(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    gfdi = bytes.fromhex("160032810f00000000000400008010029402ba0115d3")
    frame = parse_compact_frame(gfdi)

    client.handle_compact_smart({"type_id": 0x8132}, frame)
    while client.pending_actions:
        asyncio.run(client.pump_pending_actions())
    client.recorder.close()

    sent = sent_events(client.recorder.out_path)
    assert [event["label"] for event in sent] == [
        "Watch compact bootstrap ack",
        "Watch compact session/core preamble",
        "Core FeatureCapabilitiesRequest compact",
    ]
    assert sent[-1]["gfdi_hex"] == (
        "1c003981e9016a12421010018201021000d20100ca0100da0100ea48"
    )


def test_watch_init_sends_device_information_ack_only(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    asyncio.run(client.send_initial_watch_events())
    client.recorder.close()

    labels = [event["label"] for event in sent_events(client.recorder.out_path)]
    assert labels == ["DeviceInformation ACK fenix8"]
