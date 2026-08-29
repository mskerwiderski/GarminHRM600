"""Windowed CSV export of the strap's HR buffer: parsing, filtering, stats."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

WINDOW_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+(\d{2})(\d{2})\s+(\d{2})(\d{2})$"
)

GAP_THRESHOLD_S = 60.0


def parse_window(window: str, local: bool = False) -> tuple[datetime, datetime]:
    """Parse '2026-08-23 0500 0900' into (start, end); end is exclusive.

    Times are UTC by default; with local=True they are interpreted in the
    system's local timezone. The returned datetimes are timezone-aware (in
    the input timezone), so downstream comparison, filtering, and filename
    formatting all stay in the timezone the user typed.
    """
    match = WINDOW_RE.match(window.strip())
    if match is None:
        raise ValueError(
            f"invalid window {window!r}; expected 'YYYY-MM-DD HHMM HHMM'"
        )
    date_str, sh, sm, eh, em = match.groups()
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"invalid date in window {window!r}: {e}") from None
    if not (0 <= int(sh) <= 23 and 0 <= int(eh) <= 23):
        raise ValueError(f"invalid hour in window {window!r}")
    if not (0 <= int(sm) <= 59 and 0 <= int(em) <= 59):
        raise ValueError(f"invalid minute in window {window!r}")
    start = day + timedelta(hours=int(sh), minutes=int(sm))
    end = day + timedelta(hours=int(eh), minutes=int(em))
    if end <= start:
        raise ValueError(f"window end must be after start: {window!r}")
    if local:
        # naive .astimezone() attaches the system's local timezone
        return start.astimezone(), end.astimezone()
    return start.replace(tzinfo=timezone.utc), end.replace(tzinfo=timezone.utc)


def export_filename(start: datetime, end: datetime) -> str:
    return f"HRM600_{start:%Y%m%d}_{start:%H%M}_{end:%H%M}.csv"


def filter_window(
    series: list[tuple[datetime, int]],
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, int]]:
    """Samples with start <= t < end, sorted by time."""
    return sorted(
        ((ts, hr) for ts, hr in series if start <= ts < end),
        key=lambda row: row[0],
    )


def compute_stats(
    series: list[tuple[datetime, int]],
    start: datetime,
    end: datetime,
) -> dict:
    """Coverage and HR statistics for an already window-filtered series."""
    window_s = (end - start).total_seconds()
    if not series:
        return {"samples": 0, "window_s": window_s, "coverage_pct": 0.0}
    hrs = [hr for _, hr in series]
    times = [ts for ts, _ in series]
    covered = 0.0
    largest_gap = 0.0
    edges = [start] + times + [end]
    for prev, cur in zip(edges, edges[1:]):
        delta = (cur - prev).total_seconds()
        if delta <= GAP_THRESHOLD_S:
            covered += delta
        else:
            largest_gap = max(largest_gap, delta)
    return {
        "samples": len(series),
        "window_s": window_s,
        "first": times[0],
        "last": times[-1],
        "coverage_pct": 100.0 * covered / window_s if window_s else 0.0,
        "largest_gap_s": largest_gap,
        "hr_min": min(hrs),
        "hr_max": max(hrs),
        "hr_avg": sum(hrs) / len(hrs),
    }


def _fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def format_stats(stats: dict, start: datetime, end: datetime) -> str:
    tz_label = start.tzname() or "UTC"
    lines = [
        f"  window ({tz_label}): {start:%Y-%m-%d %H:%M} .. {end:%H:%M}"
        f"  ({_fmt_duration(stats['window_s'])})",
        f"  samples:       {stats['samples']}",
    ]
    if stats["samples"]:
        gap = stats["largest_gap_s"]
        gap_note = f", largest gap {_fmt_duration(gap)}" if gap else ""
        first = stats["first"].astimezone(start.tzinfo)
        last = stats["last"].astimezone(start.tzinfo)
        lines.append(
            f"  coverage:      {stats['coverage_pct']:.1f}%"
            f"  (first {first:%H:%M:%S}, last {last:%H:%M:%S}{gap_note})"
        )
        lines.append(
            f"  heart rate:    min {stats['hr_min']}"
            f"  avg {stats['hr_avg']:.1f}  max {stats['hr_max']} bpm"
        )
    else:
        lines.append("  coverage:      0.0%  (no samples in window)")
    return "\n".join(lines)
