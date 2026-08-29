# GarminHRM600

[![CI](https://github.com/mskerwiderski/GarminHRM600/actions/workflows/ci.yml/badge.svg)](https://github.com/mskerwiderski/GarminHRM600/actions/workflows/ci.yml)

A command-line tool for talking to the **Garmin HRM 600** heart rate strap
directly over Bluetooth LE — no watch, no Garmin Connect app required.

It speaks the strap's proprietary Garmin protocol stack (Multi-Link, GFDI,
Smart/EventSharing protobufs) by emulating the compact bootstrap sequence of a
Garmin watch, and it can:

- **Stream live data**: heart rate (standard BLE *and* Garmin app-layer),
  Running Dynamics (cadence, ground contact time, vertical oscillation,
  L/R balance, pace/distance), and Garmin realtime HR
- **Download the strap's internal HR memory** (the "store and forward"
  buffer that the strap records while you wear it without any receiver)
  as FIT files, using the protobuf-based FileSyncService (V2) protocol
- **Reconstruct an absolute HR time series** from those FIT files
  (they only carry a device-internal event clock; see
  [Time model](#time-model))
- **Export any time window as CSV** with coverage and heart rate statistics
  in a single command

Everything below has been verified against a real HRM 600 (firmware 5.20.0).

> **Disclaimer:** This is an independent open-source project based on
> publicly available protocol research. It is not affiliated with, endorsed
> by, or supported by Garmin. Use at your own risk. *Garmin* and *HRM 600*
> are trademarks of Garmin Ltd.

## Installation

Requirements: Python ≥ 3.12 and a BLE-capable machine. Developed and tested
on macOS (CoreBluetooth via [bleak](https://github.com/hbldh/bleak)); Linux
(BlueZ) should work, with the difference that you can pass a real
`--address` instead of scanning by name.

```bash
git clone https://github.com/mskerwiderski/GarminHRM600.git
cd GarminHRM600
python3.12 -m venv .venv
.venv/bin/pip install -e .

.venv/bin/hrm600 --help
```

For development: `pip install -e . --group dev`, then `pytest` and
`ruff check hrm600 tests` (the test suite is fully offline — codecs,
parsers, and the sync state machine run against captured frames).

## Usage

The strap must be **worn** (it only advertises when it detects a heartbeat)
and must **not be connected to any other central** — put your watch and
phone out of range or turn their Bluetooth off. On the very first
connection, add `--pair` to bond.

| Command | What it does |
|---|---|
| `hrm600 scan` | Find the strap (by name and Garmin FE1F advertising). `--all` lists every BLE device seen. |
| `hrm600 info` | Read model, serial, firmware, battery, sensor location. |
| `hrm600 live` | Stream live HR + Running Dynamics to the terminal and a JSONL log. |
| `hrm600 sync` | List and download the strap's FIT files into `downloads/`. |
| `hrm600 decode` | Decode downloaded FIT files into an absolute HR time series / CSV. |
| `hrm600 export` | One-shot: download buffer → cut a UTC time window → CSV + statistics. |
| `hrm600 probe-files` | R&D command: probe the file transfer surface, log every raw frame. |

### Examples

```bash
# find and inspect the strap
hrm600 scan
hrm600 info

# 60 seconds of live heart rate and Running Dynamics
hrm600 live --duration 60

# download the whole buffer (incremental; already-downloaded files are skipped)
hrm600 sync

# turn the downloaded files into one CSV time series
hrm600 decode downloads/*.fit --hr-csv hr.csv

# everything in one step: buffer download + window extraction + stats
hrm600 export "2026-08-23 0500 0900" ./exports
```

`export` takes a time window (`"YYYY-MM-DD HHMM HHMM"`, start inclusive, end
exclusive; UTC by default, or the system's local timezone with `--local`),
downloads new buffer files into a local cache (`downloads/` by default,
`--cache-dir` to change, `--offline` to skip BLE entirely), writes
`HRM600_<date>_<from>_<to>.csv` into the export directory and prints a
summary:

```text
# Wrote exports/HRM600_20260823_0500_0900.csv
  window (UTC):  2026-08-23 05:00 .. 09:00  (4h00m)
  samples:       16292
  coverage:      56.3%  (first 06:12:44, last 08:59:59, largest gap 1h12m)
  heart rate:    min 97  avg 120.8  max 130 bpm
```

Note: `sync`/`export` intentionally do **not** mark files as synced on the
strap, so the Garmin Connect app will still receive the full buffer later.

## How it works

The strap exposes standard BLE services (Heart Rate `2A37`, RSC `2A53`,
Battery) plus Garmin's proprietary stack:

```
BLE GATT (Multi-Link service 6a4e2800-...)
  └─ Multi-Link multiplexer (register service → handle)
       ├─ service 6:      realtime HR
       ├─ service 1:      GFDI — COBS-framed [len][msg_type][body][crc16]
       │                   └─ compact Smart frames (protobuf):
       │                       Core, EventSharing (HR type 20, RD type 21),
       │                       FileSyncService (field 43)
       └─ service 0x2018: raw file stream for content (V2 download)
```

A passive subscriber only gets the standard BLE characteristics. The Garmin
streams start after the central registers Multi-Link services and replays a
watch's compact bootstrap: DeviceInformation ACK → session/core preamble →
FeatureCapabilities → EventSharing subscribe for alert types 20/21.

For the stored HR buffer, the HRM 600 rejects the classic GFDI file
transfer (`DOWNLOAD_REQUEST` 5002 → UNSUPPORTED) and instead uses the newer
protobuf **FileSyncService**: `FileListRequest` → paginated, chunked
`FileListResponse` (each chunk must be ACKed or the strap stalls and
retransmits) → per file `FileRequest` → 16-bit handle → open a dedicated
Multi-Link service (`0x2018`) → receive the file as a **raw byte
stream** (uncompressed on the HRM 600; watches use deflate here). Details, message layouts, and byte-level findings from the probe
runs are documented in
[docs/gfdi-filetransfer.md](docs/gfdi-filetransfer.md).

### Time model

The buffer FIT files contain only `hr` messages: `filtered_bpm` samples
stamped with a device event clock (1/1024 s since battery insertion) — no
`file_id`, no `timestamp_correlation`. Real time is recovered from the
sync-protocol file ids: for cleanly finalized files, `id1 >> 32` is the
Garmin timestamp of the file's **last** sample, so `C = id_ts − ev_last`
maps the event clock to absolute time (observed stable to sub-seconds over
weeks). Ring-buffer files carry flush timestamps instead, and reused slots
can carry entirely wrong ids, so `C` is calibrated as the largest consistent
cluster across all files rather than trusted per file.

## Project layout

```
hrm600/
├── crc.py           Garmin CRC16 (GFDI + FIT)
├── cobs.py          Garmin COBS framing (0x00-delimited)
├── multilink.py     Multi-Link UUIDs, service registration
├── gfdi.py          GFDI envelope, compact Smart frames, chunk ACKs
├── protobuf.py      schema-less protobuf helpers
├── eventsharing.py  subscribe/alert payloads + decoders (types 20/21/22/23)
├── bootstrap.py     captured watch bootstrap frames (byte-exact)
├── client.py        bleak client: notify pump, watch emulation
├── filesync.py      FileSyncService (V2): listing, chunk reassembly, streams
├── filetransfer.py  classic GFDI file transfer (rejected by the HRM 600)
├── fitdecode.py     FIT → absolute HR time series (event-clock anchoring)
├── export.py        window parsing, filtering, statistics
└── cli.py           argparse subcommands
```

## Sources and attribution

This project stands on prior reverse-engineering work:

- **[openrd-ble-running-dynamics](https://codeberg.org/samdumont/openrd-ble-running-dynamics)**
  by Sam Dumont (MIT) — the definitive HRM 600 protocol reference
  (`protocol.md`) and a working Python central client. The transport and
  EventSharing code in `hrm600/` is adapted from it; see also the author's
  [blog post](https://dropbars.be/blog/reverse-engineering-garmin-hrm600-running-dynamics/).
- **[Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge)**
  (AGPL) — the Garmin GFDI/FileSyncService protocol knowledge
  (message ids, protobuf schemas, V2 download flow) was re-implemented from
  scratch based on its source; no Gadgetbridge code was copied. See also the
  [Gadgetbridge Garmin protocol notes](https://gadgetbridge.org/internals/specifics/garmin-protocol/).
- **[Garmin FIT SDK](https://developer.garmin.com/fit/overview/)**
  (`garmin-fit-sdk`) — FIT file decoding.

## License

[MIT](LICENSE). Adapted portions remain under the original MIT terms of
openrd-ble-running-dynamics (attribution headers in the affected files).
