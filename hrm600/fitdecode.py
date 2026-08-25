"""Decode downloaded FIT files: summary and HR time series extraction."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def read_fit(path: Path) -> tuple[dict[str, list[dict[str, Any]]], list[Any]]:
    from garmin_fit_sdk import Decoder, Stream

    stream = Stream.from_file(str(path))
    decoder = Decoder(stream)
    messages, errors = decoder.read()
    return messages, errors


def resolve_timestamp_16(last_full: datetime | None, ts16: int) -> datetime | None:
    """Standard FIT timestamp_16 resolution against the last full timestamp."""
    if last_full is None:
        return None
    last_secs = int(last_full.timestamp())
    delta = (ts16 - (last_secs & 0xFFFF)) & 0xFFFF
    return last_full + timedelta(seconds=delta)


def extract_hr_series(messages: dict[str, list[dict[str, Any]]]) -> list[tuple[datetime, int]]:
    series: list[tuple[datetime, int]] = []
    for key in ("record_mesgs", "monitoring_mesgs"):
        last_full: datetime | None = None
        for msg in messages.get(key, []):
            ts = msg.get("timestamp")
            if isinstance(ts, datetime):
                last_full = ts
            elif "timestamp_16" in msg:
                ts = resolve_timestamp_16(last_full, msg["timestamp_16"])
            hr = msg.get("heart_rate")
            if isinstance(ts, datetime) and isinstance(hr, int) and hr > 0:
                series.append((ts, hr))
    series.sort(key=lambda row: row[0])
    return series


def write_hr_csv(series: list[tuple[datetime, int]], out_path: Path) -> None:
    with out_path.open("w", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["timestamp", "heart_rate_bpm"])
        for ts, hr in series:
            writer.writerow([ts.isoformat(), hr])
