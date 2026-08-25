"""hrm600 - CLI for direct BLE access to the Garmin HRM 600."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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
        )
        await client.start()
        await client.register_multilink_services(args.services)
        if not args.passive:
            await client.send_initial_watch_events()

        print(f"# Listening {args.duration:.1f}s. Ctrl-C stops early.", file=sys.stderr)
        end = time.monotonic() + args.duration
        last_action_at = 0.0
        try:
            while time.monotonic() < end and not stop.is_set():
                if not args.passive and time.monotonic() - last_action_at >= args.action_spacing:
                    await client.pump_pending_actions()
                    await client.pump_run_inputs()
                    last_action_at = time.monotonic()
                await asyncio.sleep(0.05)
        finally:
            await client.stop()

        print("# Counts:", file=sys.stderr)
        for kind, count in client.counts.most_common():
            print(f"#   {kind}: {count}", file=sys.stderr)


async def run_filetransfer(args: argparse.Namespace, download: bool) -> None:
    out_path = args.out or default_out_path("hrm600-sync" if download else "hrm600-probe")
    target = await resolve_target(args.address, args.name_regex, args.scan_timeout)
    print(f"# Log: {out_path}", file=sys.stderr)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    async with BleakClient(target, timeout=args.connect_timeout, pair=args.pair) as bleak_client:
        client = Hrm600Client(bleak_client, out_path, watch_init=not args.no_bootstrap)

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

        print(f"# Waiting {args.settle:.0f}s for bootstrap before file sync...", file=sys.stderr)
        end = time.monotonic() + args.duration
        probe_at = time.monotonic() + args.settle
        probe_fired = False
        listing_printed = False
        queued_downloads = False
        idle_since: float | None = None
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
                    if fs.listing_complete and not listing_printed:
                        listing_printed = True
                        print(f"\n# File list: {len(fs.files)} file(s)")
                        for f in fs.files:
                            print(f"  {f.describe()}")
                        print()
                        if download:
                            wanted = [
                                f for f in fs.files
                                if args.type is None
                                or (f.type_name or "").lower() == args.type.lower()
                            ]
                            print(f"# Downloading {len(wanted)} file(s)...", file=sys.stderr)
                            fs.queue_downloads(wanted)
                            queued_downloads = True
                    finished = listing_printed and (not download or (queued_downloads and fs.idle))
                else:
                    if ft_classic.directory is not None and not listing_printed:
                        listing_printed = True
                        print(f"\n# FIT directory: {len(ft_classic.directory)} entries")
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
                await asyncio.sleep(0.05)
        finally:
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
                    print(f"# Saved {path} ({len(data)}B)")
        print("# Counts:", file=sys.stderr)
        for kind, count in client.counts.most_common():
            print(f"#   {kind}: {count}", file=sys.stderr)


async def cmd_probe_files(args: argparse.Namespace) -> None:
    await run_filetransfer(args, download=False)


async def cmd_sync(args: argparse.Namespace) -> None:
    await run_filetransfer(args, download=True)


async def cmd_decode(args: argparse.Namespace) -> None:
    from .fitdecode import extract_hr_series, read_fit, write_hr_csv

    for path in args.files:
        messages, errors = read_fit(path)
        print(f"# {path}")
        for key in sorted(messages):
            print(f"  {key:32s} {len(messages[key])}")
        if errors:
            print(f"  errors: {errors}")
        series = extract_hr_series(messages)
        if series:
            first, last = series[0][0], series[-1][0]
            print(f"  HR samples: {len(series)}  ({first.isoformat()} .. {last.isoformat()})")
            if args.hr_csv:
                write_hr_csv(series, args.hr_csv)
                print(f"  wrote {args.hr_csv}")
        else:
            print("  no HR samples found")


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
