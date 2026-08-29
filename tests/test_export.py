"""Offline tests for the windowed export helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hrm600.export import (
    compute_stats,
    export_filename,
    filter_window,
    format_stats,
    parse_window,
)


def dt(h: int, m: int = 0, s: int = 0) -> datetime:
    return datetime(2026, 8, 23, h, m, s, tzinfo=timezone.utc)


def test_parse_window_happy_path() -> None:
    start, end = parse_window("2026-08-23 0500 0900")
    assert start == dt(5)
    assert end == dt(9)


@pytest.mark.parametrize("bad", [
    "2026-08-23 0500",          # missing end
    "23.08.2026 0500 0900",     # wrong date format
    "2026-08-23 0900 0500",     # end before start
    "2026-08-23 0500 0500",     # empty window
    "2026-08-23 2500 2600",     # invalid hour
    "2026-08-23 0560 0900",     # invalid minute
    "2026-02-30 0500 0900",     # invalid date
])
def test_parse_window_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_window(bad)


def test_export_filename_matches_spec() -> None:
    start, end = parse_window("2026-08-23 0500 0900")
    assert export_filename(start, end) == "HRM600_20260823_0500_0900.csv"


def test_filter_window_is_start_inclusive_end_exclusive() -> None:
    series = [
        (dt(4, 59, 59), 70),
        (dt(5, 0, 0), 71),
        (dt(7, 30), 72),
        (dt(8, 59, 59), 73),
        (dt(9, 0, 0), 74),
    ]
    assert filter_window(series, dt(5), dt(9)) == [
        (dt(5, 0, 0), 71),
        (dt(7, 30), 72),
        (dt(8, 59, 59), 73),
    ]


def test_compute_stats_full_coverage() -> None:
    series = [(dt(5) + timedelta(seconds=i * 30), 80 + (i % 3)) for i in range(480)]
    stats = compute_stats(series, dt(5), dt(9))

    assert stats["samples"] == 480
    assert stats["coverage_pct"] > 99.0
    assert stats["largest_gap_s"] == 0.0
    assert stats["hr_min"] == 80
    assert stats["hr_max"] == 82


def test_compute_stats_detects_gap() -> None:
    # samples only in the first and last hour of a 4h window
    series = (
        [(dt(5) + timedelta(seconds=i * 30), 80) for i in range(120)]
        + [(dt(8) + timedelta(seconds=i * 30), 90) for i in range(120)]
    )
    stats = compute_stats(series, dt(5), dt(9))

    assert stats["samples"] == 240
    assert 45.0 < stats["coverage_pct"] < 55.0
    # gap from 05:59:30 to 08:00:00
    assert 7200 < stats["largest_gap_s"] < 7260
    assert stats["hr_avg"] == 85.0


def test_compute_stats_empty_series() -> None:
    stats = compute_stats([], dt(5), dt(9))
    assert stats["samples"] == 0
    assert stats["coverage_pct"] == 0.0
    assert "no samples" in format_stats(stats, dt(5), dt(9))


def test_format_stats_renders_all_lines() -> None:
    series = [(dt(5, 0, 12), 60), (dt(8, 59, 58), 140)]
    stats = compute_stats(series, dt(5), dt(9))
    text = format_stats(stats, dt(5), dt(9))

    assert "2026-08-23 05:00 .. 09:00" in text
    assert "4h00m" in text
    assert "samples:       2" in text
    assert "min 60" in text and "max 140" in text


def test_parse_window_local_converts_to_system_timezone(monkeypatch) -> None:
    import time as time_mod

    monkeypatch.setenv("TZ", "Europe/Berlin")
    time_mod.tzset()
    try:
        start, end = parse_window("2026-08-29 1455 1710", local=True)
        # CEST = UTC+2
        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)
        assert start_utc == datetime(2026, 8, 29, 12, 55, tzinfo=timezone.utc)
        assert end_utc == datetime(2026, 8, 29, 15, 10, tzinfo=timezone.utc)
        # filename keeps the local times as typed
        assert export_filename(start, end) == "HRM600_20260829_1455_1710.csv"
        # filtering against UTC series works across timezones
        inside = datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc)
        outside = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        assert filter_window([(inside, 80), (outside, 70)], start, end) == [(inside, 80)]
        # stats label and sample times render in the local timezone
        text = format_stats(compute_stats([(inside, 80)], start, end), start, end)
        assert "window (CEST)" in text
        assert "first 15:00:00" in text
    finally:
        monkeypatch.delenv("TZ")
        time_mod.tzset()
