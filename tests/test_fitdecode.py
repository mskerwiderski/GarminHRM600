"""Offline tests for store-and-forward HR decoding."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hrm600.filetransfer import GARMIN_TIME_EPOCH
from hrm600.fitdecode import (
    calibrate_event_anchor,
    collect_store_and_forward,
    extract_hr_event_samples,
    file_id_from_name,
)


def test_extract_hr_event_samples_handles_scalars_and_lists() -> None:
    messages = {
        "hr_mesgs": [
            {"event_timestamp": 100.0, "filtered_bpm": 80},
            {"event_timestamp": [101.0, 102.0], "filtered_bpm": [81, 0]},
            {"filtered_bpm": 99},  # no event timestamp -> skipped
        ]
    }
    assert extract_hr_event_samples(messages) == [(100.0, 80), (101.0, 81)]


def test_file_id_from_name() -> None:
    path = Path("downloads/store_and_forward_hr_data_fit_123_456.fit")
    assert file_id_from_name(path) == (123, 456)
    assert file_id_from_name(Path("foo.fit")) is None


def test_calibrate_event_anchor_picks_largest_cluster() -> None:
    # three finalized files agree, two flush-stamped files scatter
    anchors = [1000.0, 1000.5, 999.8, 500000.0, 780000.0]
    assert calibrate_event_anchor(anchors) == 1000.0
    assert calibrate_event_anchor([]) is None


def test_collect_store_and_forward_maps_event_clock_to_real_time() -> None:
    anchor = 1_153_433_598
    # finalized file: id timestamp == anchor + last event second
    ev_a = [1000.0, 1001.0]
    id1_a = (anchor + 1001) << 32 | 7
    path_a = Path(f"downloads/store_and_forward_hr_data_fit_{id1_a}_1.fit")
    msgs_a = {"hr_mesgs": [{"event_timestamp": ev_a, "filtered_bpm": [70, 71]}]}
    # ring-buffer file: flush timestamp way off, samples still anchored via C
    ev_b = [2000.0]
    id1_b = (anchor + 999_999) << 32 | 8
    path_b = Path(f"downloads/store_and_forward_hr_data_fit_{id1_b}_1.fit")
    msgs_b = {"hr_mesgs": [{"event_timestamp": ev_b, "filtered_bpm": [90]}]}

    series, c = collect_store_and_forward([(path_a, msgs_a), (path_b, msgs_b)])

    assert c == anchor
    expected_first = datetime.fromtimestamp(1000 + anchor + GARMIN_TIME_EPOCH, tz=timezone.utc)
    expected_last = datetime.fromtimestamp(2000 + anchor + GARMIN_TIME_EPOCH, tz=timezone.utc)
    assert series[0] == (expected_first, 70)
    assert series[-1] == (expected_last, 90)
    assert len(series) == 3
