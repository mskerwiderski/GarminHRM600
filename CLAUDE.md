# CLAUDE.md — GarminHRM600

CLI (`hrm600`) for direct BLE access to the Garmin HRM 600.
Python ≥3.12, bleak, garmin-fit-sdk. Everything in this repo is English
(public repo), including this file.

## Architecture

```
hrm600/crc.py           Garmin CRC16 (also used by FIT)
hrm600/cobs.py          Garmin COBS framing (0x00-delimited)
hrm600/multilink.py     Multi-Link: UUIDs, register request/response
hrm600/gfdi.py          GFDI envelope, compact Smart frames (0x..39/0x..3a)
hrm600/protobuf.py      schema-less protobuf helpers
hrm600/eventsharing.py  subscribe/alert payloads + decoders (types 20/21/22/23)
hrm600/bootstrap.py     captured fenix 8 bootstrap frames (bytes are canonical!)
hrm600/client.py        Hrm600Client: bleak, notify pump, watch emulation
hrm600/filetransfer.py  classic download state machine (5002/5004/5000)
hrm600/filesync.py      FileSyncService V2: listing, grants, chunk ACKs, streams
hrm600/fitdecode.py     FIT → HR time series (event-clock anchoring)
hrm600/export.py        window parsing (UTC/local), filtering, statistics
hrm600/cli.py           argparse subcommands
```

Protocol knowledge: `docs/gfdi-filetransfer.md` (file transfer, distilled
from Gadgetbridge) and the upstream `protocol.md` in the openrd repo (live
path).

## Non-negotiable

- **This repo is PUBLIC** (since 2026-08-25, MIT). Never commit health
  data: `hr.csv`, `captures/`, `downloads/`, and exports are gitignored and
  must stay that way. The history was cleaned of hr.csv via filter-repo —
  do not pollute it again. (The former private archive repo was deleted on
  2026-08-25.)
- **Never "improve" the captured frames in `bootstrap.py`** — the exact
  bytes including counters are validated against the real strap. Response
  frames are `0x..3a` echoing the request counter, requests are `0x..39`;
  mixing them up yields "connects, but no type-21 subscription".
- Gadgetbridge is AGPL: take protocol facts only, **never code**.
- openrd is MIT: adapted code keeps the attribution notice in its header.

## Testing

```bash
.venv/bin/pytest -q          # offline (codecs, parsers, state machine)
.venv/bin/ruff check hrm600 tests
```

`hrm600 export "<window>" /tmp/x --offline` exercises the decode/export
path against the `downloads/` cache without BLE.

Live tests need the strap: worn, with no other central connected (watch and
phone app out of range or Bluetooth off). Order:
`hrm600 scan` → `info` → `live --duration 60` → `probe-files`.
Every run logs JSONL to `captures/` (gitignored) — for protocol questions
look there first (`gfdi_rx` events with `decoded_hex`).

## Open items / current knowledge

- **Live streams verified against the strap** (probe run 2026-08-25,
  `captures/hrm600-probe-20260825T110706Z.jsonl`): full bootstrap,
  type-20 HR, type-21 RD, realtime HR.
- **Classic GFDI file transfer disproven**: 5002/5031 → UNSUPPORTED
  (0x02), 5030 SYSTEM_EVENT → ACK. The HRM 600 uses the
  **FileSyncService V2 protocol** (Smart field 43 + raw file stream over
  ML service 0x2018), implemented in `hrm600/filesync.py`, documented in
  `docs/gfdi-filetransfer.md`.
- **V2 listing confirmed on the strap** (2nd probe run,
  `captures/hrm600-probe-20260825T112101Z.jsonl`): a FileListRequest sent
  as a compact `0x..39` frame is answered — file type
  `STORE_AND_FORWARD_HR_DATA_FIT` (code 0), ~10247 B each = the 24h
  buffer. Responses > ~495 B arrive **chunked** as `0x2c` frames
  (`[counter][offset:4][total:4][chunk:4][data]`); without a per-chunk ACK
  (ProtobufStatus format, 5 s retransmit) the strap never sends chunk 2.
  Reassembly + ACK: `FileSyncV2.on_transport_chunk`.
- **Download + decode verified end-to-end** (2026-08-25, re-verified
  2026-08-29 after the sync-robustness fixes): 94 cached files of
  `STORE_AND_FORWARD_HR_DATA_FIT`, 250,329 HR samples (Aug 08–29),
  including a same-day recording that the strap only flushed during the
  sync session. `export --local` reproduced a 2h15m ride window at 100%
  coverage.
- **File content streams are RAW, not deflate** (2026-08-29): inflate never
  succeeded on any live HRM 600 stream — the byte count always equals the
  listed file size. The Aug-25 downloads only worked through a
  keep-raw-bytes fallback. Gadgetbridge's deflate applies to watches;
  in `filesync.py` raw FIT is the primary path, inflate the fallback.
- **Time model of the store-and-forward files** (important!): the FIT files
  contain ONLY `hr` messages (no file_id, no timestamp_correlation); read
  with `garmin-fit-sdk` and `merge_heart_rates=False`, otherwise
  KeyError('record_mesgs'). Samples carry `event_timestamp` (device clock
  since battery insertion, 1/1024 s). Real-time anchor: for cleanly
  finalized files `id1>>32` is the Garmin timestamp of the LAST sample ⇒
  `C = id_ts − ev_last` (sub-second stable over weeks). Ring-buffer files
  carry flush timestamps instead, and some slots have entirely wrong id
  times (slot reuse) — therefore C is calibrated as the largest cluster
  across all files (`fitdecode.calibrate_event_anchor`) and the id
  timestamp of any single file is NEVER trusted.
  `id2 & 0xFFFFFFFF` = file size in bytes.
- **Sync robustness (findings 2026-08-29,
  `captures/hrm600-sync-20260829T175016Z.jsonl`)**: complete `0x2c` frames
  must be generically ACKed, otherwise the strap retransmits them (incl.
  grants) every ~5 s and order-based matching derails; FileSyncService
  field 5 = grant notification `{FileId, handle}` (authoritative
  handle→file binding, undocumented in Gadgetbridge); status=3 =
  file evicted from the round-robin buffer; the current recording is
  flushed into listable files during the session → re-list after the
  queue drains. All implemented in `filesync.py` + `cli.run_filetransfer`.
- `decode_heart_rate` (field 1 in field-1013 alerts) is a plausible but
  unverified assumption — field-1013 payloads in the probe log show the
  pattern `08 <hr> 10 00 18 <varint>` (field 1 ≈ 78–80 bpm, plausible).
