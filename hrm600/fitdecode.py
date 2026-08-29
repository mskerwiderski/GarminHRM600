"""Decode downloaded FIT files: summary and HR time series extraction.

The HRM 600's STORE_AND_FORWARD_HR_DATA_FIT files contain only `hr` messages
with filtered_bpm samples on the device's event clock (seconds since battery
insert). Real time is recovered via the file id: for cleanly finalized files
id1>>32 is the Garmin timestamp of the LAST sample, so
C = id_ts - ev_last maps the event clock to Garmin time. C is stable to
sub-second over weeks; files still in the ring buffer carry a flush timestamp
instead, so C is calibrated from the largest cluster across all files.

Consecutive files overlap in the ring buffer, so merging them yields the
same beats twice; samples closer together than MIN_BEAT_INTERVAL_S are
dropped.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .filetransfer import GARMIN_TIME_EPOCH

ID_RE = re.compile(r"_(\d+)_(\d+)\.fit$")

ANCHOR_CLUSTER_TOLERANCE_S = 60.0

# Consecutive ring-buffer files overlap, so the same beats arrive twice.
# 0.25 s is 240 bpm: below that no two samples can be distinct beats.
MIN_BEAT_INTERVAL_S = 0.25


def read_fit(path: Path) -> tuple[dict[str, list[dict[str, Any]]], list[Any]]:
    from garmin_fit_sdk import Decoder, Stream

    stream = Stream.from_file(str(path))
    decoder = Decoder(stream)
    # merge_heart_rates would try to merge hr mesgs into (absent) record mesgs
    messages, errors = decoder.read(merge_heart_rates=False)
    return messages, errors


def resolve_timestamp_16(last_full: datetime | None, ts16: int) -> datetime | None:
    """Standard FIT timestamp_16 resolution against the last full timestamp."""
    if last_full is None:
        return None
    last_secs = int(last_full.timestamp())
    delta = (ts16 - (last_secs & 0xFFFF)) & 0xFFFF
    return last_full + timedelta(seconds=delta)


def extract_hr_series(messages: dict[str, list[dict[str, Any]]]) -> list[tuple[datetime, int]]:
    """HR series from record/monitoring messages (absolute timestamps)."""
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


def extract_hr_event_samples(messages: dict[str, list[dict[str, Any]]]) -> list[tuple[float, int]]:
    """(event_timestamp_seconds, bpm) pairs from `hr` messages."""
    samples: list[tuple[float, int]] = []
    for msg in messages.get("hr_mesgs", []):
        evs = msg.get("event_timestamp")
        bpms = msg.get("filtered_bpm")
        if evs is None or bpms is None:
            continue
        if not isinstance(evs, list):
            evs = [evs]
        if not isinstance(bpms, list):
            bpms = [bpms]
        for ev, bpm in zip(evs, bpms):
            if isinstance(ev, (int, float)) and isinstance(bpm, int) and bpm > 0:
                samples.append((float(ev), bpm))
    return samples


def file_id_from_name(path: Path) -> tuple[int, int] | None:
    match = ID_RE.search(path.name)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def calibrate_event_anchor(anchors: list[float]) -> float | None:
    """Pick the event->Garmin-time offset C from per-file candidates.

    Finalized files agree on C to sub-second; ring-buffer files carry a flush
    timestamp and scatter. Take the largest cluster (then its median).
    """
    if not anchors:
        return None
    best: list[float] = []
    for center in anchors:
        cluster = [a for a in anchors if abs(a - center) <= ANCHOR_CLUSTER_TOLERANCE_S]
        if len(cluster) > len(best):
            best = cluster
    best.sort()
    return best[len(best) // 2]


def collect_store_and_forward(
    decoded: list[tuple[Path, dict[str, list[dict[str, Any]]]]],
) -> tuple[list[tuple[datetime, int]], float | None]:
    """Combine hr-message samples of many files into one absolute time series."""
    per_file: list[tuple[list[tuple[float, int]], int | None]] = []
    anchors: list[float] = []
    for path, messages in decoded:
        samples = extract_hr_event_samples(messages)
        if not samples:
            continue
        ids = file_id_from_name(path)
        garmin_ts = ids[0] >> 32 if ids else None
        if garmin_ts is not None:
            ev_last = max(ev for ev, _ in samples)
            anchors.append(garmin_ts - ev_last)
        per_file.append((samples, garmin_ts))

    anchor = calibrate_event_anchor(anchors)
    if anchor is None:
        return [], None
    merged: list[tuple[datetime, int]] = []
    for samples, _ in per_file:
        for ev, bpm in samples:
            unix = ev + anchor + GARMIN_TIME_EPOCH
            merged.append((datetime.fromtimestamp(unix, tz=timezone.utc), bpm))
    merged.sort(key=lambda row: row[0])
    series: list[tuple[datetime, int]] = []
    for row in merged:
        if series and (row[0] - series[-1][0]).total_seconds() < MIN_BEAT_INTERVAL_S:
            continue
        series.append(row)
    return series, anchor


def write_hr_csv(series: list[tuple[datetime, int]], out_path: Path) -> None:
    with out_path.open("w", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["timestamp", "heart_rate_bpm"])
        for ts, hr in series:
            writer.writerow([ts.isoformat(), hr])
