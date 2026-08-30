"""hrm600 - CLI for direct BLE access to the Garmin HRM 600."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bleak import BleakClient, BleakScanner

from .client import DEFAULT_NAME_REGEX, Hrm600Client, read_device_info, resolve_target
from .filesync import FileSyncV2
from .filetransfer import FileTransfer
from .gfdi import build_system_event

GARMIN_FE1F = "0000fe1f-0000-1000-8000-00805f9b34fb"


def default_out_path(prefix: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("captures") / f"{prefix}-{stamp}.jsonl"


async def cmd_scan(args: argparse.Namespace) -> None:
    print(f"# Scanning {args.scan_timeout:.1f}s...", file=sys.stderr)
    results = await BleakScanner.discover(timeout=args.scan_timeout, return_adv=True)
    rows = []
    for device, adv in results.values():
        name = device.name or adv.local_name or ""
        fe1f = adv.service_data.get(GARMIN_FE1F)
        is_hrm = "HRM" in name.upper() or fe1f is not None
        if not args.all and not is_hrm:
            continue
        rows.append((name, device.address, adv.rssi, fe1f))
    if not rows:
        print("No matching devices found. Is the strap worn/in pairing mode? Try --all.")
        return
    for name, address, rssi, fe1f in sorted(rows, key=lambda r: r[2], reverse=True):
        extra = f"  fe1f={fe1f.hex()}" if fe1f else ""
        print(f"{name or '(unnamed)':24s}  {address}  rssi={rssi}{extra}")


async def cmd_info(args: argparse.Namespace) -> None:
    target = await resolve_target(args.address, args.name_regex, args.scan_timeout)
    async with BleakClient(target, timeout=args.connect_timeout, pair=args.pair) as client:
        info = await read_device_info(client)
    for key, value in info.items():
        print(f"{key:22s} {value if value is not None else '-'}")


async def cmd_live(args: argparse.Namespace) -> None:
    out_path = args.out or default_out_path("hrm600-live")
    target = await resolve_target(args.address, args.name_regex, args.scan_timeout)
    print(f"# Output: {out_path}", file=sys.stderr)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    verbose, show_progress = console_mode(args)

    async with BleakClient(target, timeout=args.connect_timeout, pair=args.pair) as bleak_client:
        client = Hrm600Client(
            bleak_client,
            out_path,
            watch_init=not args.passive,
            feed_run_input=args.feed_run_input,
            speed_mps=args.speed_mps,
            grade_pct=args.grade_pct,
            run_input_count=args.run_input_count,
            run_input_period=args.run_input_period,
            quiet=not verbose,
        )

        status: dict[str, Any] = {}

        def on_event(event: dict[str, Any]) -> None:
            kind = event.get("kind")
            if kind in ("standard_hr", "realtime_hr") and event.get("heart_rate_bpm"):
                status["hr"] = event["heart_rate_bpm"]
            elif kind == "standard_rsc":
                status["speed"] = event.get("speed_mps")
                status["cadence"] = event.get("cadence_spm")
            elif kind == "battery":
                status["battery"] = event.get("level_pct")

        if show_progress:
            client.on_event = on_event

        await client.start()
        await client.register_multilink_services(args.services)
        if not args.passive:
            await client.send_initial_watch_events()

        print(f"# Listening {args.duration:.1f}s. Ctrl-C stops early.", file=sys.stderr)
        started = time.monotonic()
        end = started + args.duration
        last_action_at = 0.0
        try:
            while time.monotonic() < end and not stop.is_set():
                if not args.passive and time.monotonic() - last_action_at >= args.action_spacing:
                    await client.pump_pending_actions()
                    await client.pump_run_inputs()
                    last_action_at = time.monotonic()
                if show_progress:
                    spinner = PROGRESS_SPINNER[int(time.monotonic() * 5) % len(PROGRESS_SPINNER)]
                    parts = [f"HR {status.get('hr', '--')} bpm"]
                    if status.get("speed") is not None:
                        parts.append(f"{status['speed']:.2f} m/s")
                    if status.get("cadence") is not None:
                        parts.append(f"{status['cadence']:.0f} spm")
                    if status.get("battery") is not None:
                        parts.append(f"bat {status['battery']}%")
                    parts.append(f"events {sum(client.counts.values())}")
                    elapsed = time.monotonic() - started
                    render_status(
                        f"{spinner} live  " + "   ".join(parts)
                        + f"   {int(elapsed)}/{int(args.duration)}s"
                    )
                await asyncio.sleep(0.05)
        finally:
            if show_progress:
                clear_progress()
            await client.stop()

        print("# Counts:", file=sys.stderr)
        for kind, count in client.counts.most_common():
            print(f"#   {kind}: {count}", file=sys.stderr)


async def run_filetransfer(
    args: argparse.Namespace,
    download: bool,
    skip_existing: bool = False,
) -> None:
    out_path = args.out or default_out_path("hrm600-sync" if download else "hrm600-probe")
    verbose, show_progress = console_mode(args)
    target = await resolve_target(args.address, args.name_regex, args.scan_timeout)
    print(f"# Log: {out_path}", file=sys.stderr)

    def say(message: str, **kwargs: Any) -> None:
        if show_progress:
            clear_progress()
        print(message, **kwargs)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    async with BleakClient(target, timeout=args.connect_timeout, pair=args.pair) as bleak_client:
        client = Hrm600Client(bleak_client, out_path, watch_init=not args.no_bootstrap,
                              quiet=not verbose)

        def note(line: str) -> None:
            client.emit({"kind": "filesync", "note": line}, f"[FS] {line}")

        if args.classic:
            ft_classic = FileTransfer(client, log=note)
            client.on_gfdi_frame = ft_classic.on_gfdi_frame
            fs = None
        else:
            fs = FileSyncV2(client, log=note)
            client.on_gfdi_frame = fs.on_gfdi_frame
            client.on_register_ok = fs.on_register_ok
            client.on_service_payload = fs.on_service_payload
            client.on_service_close = fs.on_service_close

        await client.start()
        await client.register_multilink_services([6, 1])
        if not args.no_bootstrap:
            await client.send_initial_watch_events()

        if not show_progress:
            print(f"# Waiting {args.settle:.0f}s for bootstrap before file sync...", file=sys.stderr)
        started = time.monotonic()
        end = started + args.duration
        probe_at = started + args.settle
        probe_fired = False
        queued_total = 0
        listing_printed = False
        queued_downloads = False
        idle_since: float | None = None
        attempted: set[tuple[int, int]] = set()
        cached = {p.name for p in args.out_dir.glob("*.fit")} if skip_existing else set()
        listing_round = 0
        max_listing_rounds = 3
        relist_exhausted = False

        def queue_new_wanted() -> int:
            """Queue listed/notified files not yet attempted or cached."""
            nonlocal queued_total
            wanted = []
            for f in fs.files:
                key = (f.id1, f.id2)
                if key in attempted:
                    continue
                if args.type is not None and (f.type_name or "").lower() != args.type.lower():
                    continue
                if f.filename() in cached:
                    attempted.add(key)
                    continue
                attempted.add(key)
                wanted.append(f)
            if wanted:
                say(f"# Downloading {len(wanted)} file(s)...", file=sys.stderr)
                queued_total += len(wanted)
                fs.queue_downloads(wanted)
            return len(wanted)

        try:
            while time.monotonic() < end and not stop.is_set():
                await client.pump_pending_actions()
                now = time.monotonic()
                if not probe_fired and now >= probe_at:
                    probe_fired = True
                    if args.sync_ready:
                        client.enqueue_gfdi("SystemEvent SYNC_READY", build_system_event("SYNC_READY"))
                    if fs is not None:
                        fs.request_file_list(exclude_synced=not args.all_files)
                    else:
                        ft_classic.request_supported_file_types()
                        ft_classic.start_directory_download()

                if fs is not None:
                    fs.tick()
                    if fs.listing_complete and not listing_printed:
                        listing_printed = True
                        say(f"# File list: {len(fs.files)} file(s)")
                        # the full listing is the point of probe-files; for
                        # sync/export it is debug detail
                        if not download or verbose:
                            for f in fs.files:
                                print(f"  {f.describe()}")
                        if download:
                            queue_new_wanted()
                            queued_downloads = True
                    finished = (
                        fs.listing_complete
                        and listing_printed
                        and (not download or (queued_downloads and fs.idle))
                    )
                    if finished and download and probe_fired:
                        # files flushed during this session (notifications) or
                        # beyond the last listed page: queue them, then re-list
                        # until a round brings nothing new
                        if queue_new_wanted():
                            finished = False
                        elif listing_round + 1 < max_listing_rounds and not relist_exhausted:
                            listing_round += 1
                            if verbose:
                                print(f"# Re-listing (round {listing_round})...", file=sys.stderr)
                            fs.listing_complete = False
                            listing_printed = True  # don't reprint the full list
                            fs.request_file_list(
                                start_page_id=fs.next_page_id,
                                exclude_synced=not args.all_files,
                            )
                            finished = False
                        else:
                            relist_exhausted = True
                else:
                    if ft_classic.directory is not None and not listing_printed:
                        listing_printed = True
                        say(f"# FIT directory: {len(ft_classic.directory)} entries")
                        for entry in ft_classic.directory:
                            print(f"  {entry.describe()}")
                    finished = listing_printed

                if finished:
                    if idle_since is None:
                        idle_since = now
                    elif now - idle_since >= args.linger:
                        break
                else:
                    idle_since = None
                if show_progress:
                    if not probe_fired:
                        render_progress("bootstrapping watch emulation", now - started, args.duration)
                    elif fs is not None and download and queued_total:
                        done = len(fs.completed) + len(fs.failed)
                        render_progress("downloading files", done, queued_total, unit=" files")
                    elif fs is not None and not fs.listing_complete:
                        render_progress("listing files", now - started, args.duration)
                    else:
                        render_progress("waiting for the strap", now - started, args.duration)
                await asyncio.sleep(0.05)
        finally:
            if show_progress:
                clear_progress()
            await client.stop()

        if not listing_printed:
            print("# No file list received. See the JSONL log for raw frames.", file=sys.stderr)
        if fs is not None:
            for sync_file, reason in fs.failed:
                print(f"# FAILED {sync_file.describe()}: {reason}", file=sys.stderr)
            if download and fs.completed:
                out_dir = args.out_dir
                out_dir.mkdir(parents=True, exist_ok=True)
                for sync_file, data in fs.completed:
                    path = out_dir / sync_file.filename()
                    path.write_bytes(data)
                    if verbose:
                        print(f"# Saved {path} ({len(data)}B)")
                print(f"# Saved {len(fs.completed)} file(s) to {args.out_dir}/")
        if verbose:
            print("# Counts:", file=sys.stderr)
            for kind, count in client.counts.most_common():
                print(f"#   {kind}: {count}", file=sys.stderr)


PROGRESS_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def console_mode(args: argparse.Namespace) -> tuple[bool, bool]:
    """(verbose, show_progress) from --log-level and whether stderr is a TTY."""
    verbose = getattr(args, "log_level", "debug") == "debug"
    return verbose, not verbose and sys.stderr.isatty()


def render_status(text: str) -> None:
    sys.stderr.write(f"\r\x1b[K{text}")
    sys.stderr.flush()


def render_progress(phase: str, done: float, total: float, unit: str = "s") -> None:
    width = 20
    filled = int(min(1.0, done / total) * width) if total else 0
    spinner = PROGRESS_SPINNER[int(time.monotonic() * 5) % len(PROGRESS_SPINNER)]
    bar = "█" * filled + "░" * (width - filled)
    render_status(f"{spinner} {phase:<36s} [{bar}] {int(done):3d}/{int(total)}{unit} ")


def clear_progress() -> None:
    sys.stderr.write("\r\x1b[K")
    sys.stderr.flush()


def clock_deviation_line(source: str, garmin_ts: int, rx_unix: float) -> str | None:
    """Format one strap-clock measurement; None if the timestamp is implausible.

    Positive deviation = the strap's clock runs ahead of the system clock.
    """
    from .filetransfer import GARMIN_TIME_EPOCH

    if garmin_ts == 0:
        return None
    strap_unix = garmin_ts + GARMIN_TIME_EPOCH
    deviation = strap_unix - rx_unix
    if abs(deviation) > 86400:  # ring-buffer slot reuse yields bogus id times
        return None
    strap = datetime.fromtimestamp(strap_unix, tz=timezone.utc)
    system = datetime.fromtimestamp(rx_unix, tz=timezone.utc)
    return (
        f"strap {strap:%Y-%m-%d %H:%M:%S}Z  system {system:%H:%M:%S}Z"
        f"  ->  strap clock {deviation:+.1f}s  [{source}]"
    )


async def cmd_clock(args: argparse.Namespace) -> None:
    from .gfdi import build_current_time_request, parse_current_time_response

    out_path = args.out or default_out_path("hrm600-clock")
    target = await resolve_target(args.address, args.name_regex, args.scan_timeout)
    print(f"# Log: {out_path}", file=sys.stderr)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    # (source, garmin_ts, unix arrival time)
    measurements: list[tuple[str, int, float]] = []
    verbose, show_progress = console_mode(args)

    async with BleakClient(target, timeout=args.connect_timeout, pair=args.pair) as bleak_client:
        client = Hrm600Client(
            bleak_client, out_path, watch_init=not args.no_bootstrap, quiet=not verbose
        )

        def note(line: str) -> None:
            client.emit({"kind": "filesync", "note": line}, f"[FS] {line}")

        fs = FileSyncV2(client, log=note)

        def on_gfdi_frame(parsed: dict) -> None:
            if parsed["type_id"] == 5000:
                ct = parse_current_time_response(parsed["body"])
                if ct is not None:
                    if ct.get("garmin_ts"):
                        measurements.append(("CurrentTimeRequest answer", ct["garmin_ts"], time.time()))
                    else:
                        note(f"CurrentTimeRequest rejected: status={ct['status']}")
            fs.on_gfdi_frame(parsed)

        client.on_gfdi_frame = on_gfdi_frame
        client.on_register_ok = fs.on_register_ok
        client.on_service_payload = fs.on_service_payload
        client.on_service_close = fs.on_service_close

        await client.start()
        await client.register_multilink_services([6, 1])
        if not args.no_bootstrap:
            await client.send_initial_watch_events()

        if not show_progress:
            print(f"# Waiting {args.settle:.0f}s for bootstrap, then up to "
                  f"{args.duration:.0f}s for a fresh strap timestamp...", file=sys.stderr)
        started = time.monotonic()
        end = started + args.duration
        probe_at = started + args.settle
        probe_fired = False
        notified_seen = 0
        printed = 0
        try:
            while time.monotonic() < end and not stop.is_set():
                await client.pump_pending_actions()
                fs.tick()
                if not probe_fired and time.monotonic() >= probe_at:
                    probe_fired = True
                    client.enqueue_gfdi("CurrentTimeRequest", build_current_time_request())
                    if args.sync_ready:
                        client.enqueue_gfdi("SystemEvent SYNC_READY", build_system_event("SYNC_READY"))
                    fs.request_file_list()
                while notified_seen < len(fs.notified):
                    rx_unix, sync_file = fs.notified[notified_seen]
                    notified_seen += 1
                    measurements.append(("fresh file flush", sync_file.id1 >> 32, rx_unix))
                got_valid = False
                while printed < len(measurements):
                    line = clock_deviation_line(*measurements[printed])
                    printed += 1
                    if show_progress:
                        clear_progress()
                    if line is None:
                        print("# Implausible timestamp ignored "
                              f"({measurements[printed - 1][0]})", file=sys.stderr)
                    else:
                        print(line)
                        got_valid = True
                if got_valid:
                    break
                if show_progress:
                    phase = (
                        "waiting for a fresh strap timestamp" if probe_fired
                        else "bootstrapping watch emulation"
                    )
                    render_progress(phase, time.monotonic() - started, args.duration)
                await asyncio.sleep(0.05)
        finally:
            if show_progress:
                clear_progress()
            await client.stop()

    if not any(clock_deviation_line(*m) for m in measurements):
        from .filetransfer import garmin_ts_to_datetime

        newest = max((f.id1 >> 32 for f in fs.files), default=0)
        if newest:
            print(f"# Newest listed file timestamp: {garmin_ts_to_datetime(newest)} "
                  "(stale - not usable as a clock reading)")
        print("# No fresh strap timestamp observed. The strap only reveals its "
              "clock when it answers a CurrentTimeRequest or flushes a file; "
              "retry with a longer --duration or right after wearing the strap.")


async def cmd_probe_files(args: argparse.Namespace) -> None:
    await run_filetransfer(args, download=False)


async def cmd_sync(args: argparse.Namespace) -> None:
    await run_filetransfer(args, download=True)


async def cmd_export(args: argparse.Namespace) -> None:
    from .export import (
        compute_stats,
        export_filename,
        filter_window,
        format_stats,
        parse_window,
    )
    from .fitdecode import collect_store_and_forward, read_fit, write_hr_csv

    try:
        start, end = parse_window(args.window, local=args.local)
    except ValueError as e:
        raise SystemExit(str(e)) from None
    args.export_dir.mkdir(parents=True, exist_ok=True)

    if not args.offline:
        # full listing (past windows may already be flagged as synced by the
        # Garmin app), skip files already in the cache
        args.all_files = True
        args.classic = False
        args.type = None
        args.out_dir = args.cache_dir
        await run_filetransfer(args, download=True, skip_existing=True)

    paths = sorted(args.cache_dir.glob("store_and_forward*.fit"))
    if not paths:
        raise SystemExit(f"No buffer files in {args.cache_dir}; run without --offline first.")
    decoded = []
    for path in paths:
        try:
            messages, _ = read_fit(path)
        except Exception as e:
            print(f"# Skipping {path.name}: {e}", file=sys.stderr)
            continue
        decoded.append((path, messages))
    series, anchor = collect_store_and_forward(decoded)
    print(f"# Decoded {len(decoded)} buffer file(s), {len(series)} samples total "
          f"(anchor C={anchor:.1f})" if series else "# No HR samples decodable",
          file=sys.stderr)

    windowed = filter_window(series, start, end)
    stats = compute_stats(windowed, start, end)
    if not windowed:
        print(format_stats(stats, start, end))
        raise SystemExit("No samples in the requested window - no file written.")
    if args.local:
        windowed = [(ts.astimezone(start.tzinfo), hr) for ts, hr in windowed]
    out_path = args.export_dir / export_filename(start, end)
    write_hr_csv(windowed, out_path)
    print(f"# Wrote {out_path}")
    print(format_stats(stats, start, end))


async def cmd_decode(args: argparse.Namespace) -> None:
    from .fitdecode import (
        collect_store_and_forward,
        extract_hr_series,
        read_fit,
        write_hr_csv,
    )

    decoded = []
    series = []
    for path in args.files:
        try:
            messages, errors = read_fit(path)
        except Exception as e:
            print(f"# {path}\n  not decodable: {e}")
            continue
        print(f"# {path}")
        for key in sorted(messages):
            print(f"  {key:32s} {len(messages[key])}")
        if errors:
            print(f"  errors: {errors}")
        decoded.append((path, messages))
        series.extend(extract_hr_series(messages))

    saf_series, anchor = collect_store_and_forward(decoded)
    if saf_series:
        print(f"\n# Store-and-forward HR: {len(saf_series)} samples, "
              f"event-clock anchor C={anchor:.1f}")
        series.extend(saf_series)

    series.sort(key=lambda row: row[0])
    if not series:
        print("# No HR samples found")
        return
    first, last = series[0][0], series[-1][0]
    print(f"# Total: {len(series)} HR samples  ({first.isoformat()} .. {last.isoformat()})")
    if args.hr_csv:
        write_hr_csv(series, args.hr_csv)
        print(f"# Wrote {args.hr_csv}")


def parse_services(value: str) -> list[int]:
    out: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part, 0))
    return out


def add_connect_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--address", "--mac", default=None,
                        help="BLE address/UUID; on macOS scan-by-name is used instead")
    parser.add_argument("--name-regex", default=DEFAULT_NAME_REGEX, help="device name regex for scan mode")
    parser.add_argument("--scan-timeout", type=float, default=8.0)
    parser.add_argument("--connect-timeout", type=float, default=20.0)
    parser.add_argument("--pair", action="store_true", help="ask Bleak to pair if the backend supports it")
    parser.add_argument("--log-level", choices=["info", "debug"], default="info",
                        help="info: progress display and results only; debug: echo every "
                             "frame (the JSONL log always records everything)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hrm600", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="scan for the HRM 600 (and other Garmin FE1F devices)")
    p_scan.add_argument("--scan-timeout", type=float, default=8.0)
    p_scan.add_argument("--all", action="store_true", help="list every BLE device seen")
    p_scan.set_defaults(func=cmd_scan)

    p_info = sub.add_parser("info", help="read device information, battery, sensor location")
    add_connect_args(p_info)
    p_info.set_defaults(func=cmd_info)

    p_live = sub.add_parser("live", help="stream live HR and Running Dynamics")
    add_connect_args(p_live)
    p_live.add_argument("--duration", type=float, default=60.0, help="seconds to listen")
    p_live.add_argument("--out", type=Path, default=None,
                        help="JSONL output path; default captures/hrm600-live-<UTC>.jsonl")
    p_live.add_argument("--services", type=parse_services, default=[6, 1],
                        help="comma-separated Multi-Link service IDs to register; default 6,1")
    p_live.add_argument("--passive", action="store_true",
                        help="subscribe only; skip watch-emulation bootstrap (no Garmin streams)")
    p_live.add_argument("--feed-run-input", action="store_true",
                        help="after 22/23 subscription, send activity-start and type-23 running input")
    p_live.add_argument("--speed-mps", type=float, default=3.0)
    p_live.add_argument("--grade-pct", type=float, default=0.0)
    p_live.add_argument("--run-input-count", type=int, default=20)
    p_live.add_argument("--run-input-period", type=float, default=1.0)
    p_live.add_argument("--action-spacing", type=float, default=0.25,
                        help="seconds between queued GFDI writes")
    p_live.set_defaults(func=cmd_live)

    def add_filetransfer_args(p: argparse.ArgumentParser) -> None:
        add_connect_args(p)
        p.add_argument("--duration", type=float, default=120.0, help="overall time budget in seconds")
        p.add_argument("--settle", type=float, default=8.0,
                       help="seconds to let the watch-emulation bootstrap finish first")
        p.add_argument("--linger", type=float, default=3.0,
                       help="seconds to keep listening after the transfer looks finished")
        p.add_argument("--out", type=Path, default=None, help="JSONL log path")
        p.add_argument("--no-bootstrap", action="store_true",
                       help="skip the watch-emulation bootstrap before probing")
        p.add_argument("--no-sync-ready", dest="sync_ready", action="store_false",
                       help="do not send the SYNC_READY system event")
        p.add_argument("--all-files", action="store_true",
                       help="list already-synced files too (omit the 0xa5a5 exclusion flags)")
        p.add_argument("--classic", action="store_true",
                       help="use the classic GFDI file transfer (known UNSUPPORTED on HRM 600)")

    p_clock = sub.add_parser(
        "clock",
        help="measure how far the strap's clock deviates from the system clock",
    )
    add_connect_args(p_clock)
    p_clock.add_argument("--duration", type=float, default=180.0,
                         help="max seconds to wait for a fresh strap timestamp")
    p_clock.add_argument("--settle", type=float, default=8.0,
                         help="seconds to let the watch-emulation bootstrap finish first")
    p_clock.add_argument("--out", type=Path, default=None, help="JSONL log path")
    p_clock.add_argument("--no-bootstrap", action="store_true",
                         help="skip the watch-emulation bootstrap before probing")
    p_clock.add_argument("--no-sync-ready", dest="sync_ready", action="store_false",
                         help="do not send the SYNC_READY system event")
    p_clock.set_defaults(func=cmd_clock)

    p_probe = sub.add_parser("probe-files", help="list the strap's files via the protobuf file sync")
    add_filetransfer_args(p_probe)
    p_probe.set_defaults(func=cmd_probe_files)

    p_sync = sub.add_parser("sync", help="download the strap's files (24h HR buffer) as FIT")
    add_filetransfer_args(p_sync)
    p_sync.add_argument("--type", default=None,
                        help="only download files of this type name (e.g. monitor)")
    p_sync.add_argument("--out-dir", type=Path, default=Path("downloads"),
                        help="directory for downloaded .fit files")
    p_sync.set_defaults(func=cmd_sync)

    p_export = sub.add_parser(
        "export",
        help="download the buffer and export one time window as CSV with stats",
    )
    p_export.add_argument("window",
                          help="time window 'YYYY-MM-DD HHMM HHMM', e.g. '2026-08-23 0500 0900'; "
                               "UTC unless --local is given")
    p_export.add_argument("export_dir", type=Path, help="directory for the exported CSV")
    p_export.add_argument("--local", action="store_true",
                          help="interpret the window (and write CSV timestamps) in the system's "
                               "local timezone instead of UTC")
    add_filetransfer_args(p_export)
    p_export.add_argument("--cache-dir", type=Path, default=Path("downloads"),
                          help="cache for downloaded .fit buffer files (default: downloads/)")
    p_export.add_argument("--offline", action="store_true",
                          help="skip the BLE download and export from the cache only")
    p_export.set_defaults(func=cmd_export)

    p_decode = sub.add_parser("decode", help="decode downloaded FIT files (summary + HR series)")
    p_decode.add_argument("files", type=Path, nargs="+")
    p_decode.add_argument("--hr-csv", type=Path, default=None,
                          help="write the extracted HR time series to this CSV")
    p_decode.set_defaults(func=cmd_decode)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
