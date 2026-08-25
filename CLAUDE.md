# CLAUDE.md — GarminHRM600

CLI (`hrm600`) für direkten BLE-Zugriff auf den Garmin HRM 600.
Python ≥3.12, bleak, garmin-fit-sdk. UI-Sprache: **Englisch**, Doku Deutsch.

## Architektur

```
hrm600/crc.py           Garmin CRC16 (auch FIT)
hrm600/cobs.py          Garmin-COBS-Framing (0x00-delimitiert)
hrm600/multilink.py     Multi-Link: UUIDs, Register-Request/Response
hrm600/gfdi.py          GFDI-Envelope, kompakte Smart-Frames (0x..39/0x..3a)
hrm600/protobuf.py      Schema-lose Protobuf-Helfer
hrm600/eventsharing.py  Subscribe/Alert-Payloads + Decoder (Typ 20/21/22/23)
hrm600/bootstrap.py     Captured fenix-8-Bootstrap-Frames (Bytes sind kanonisch!)
hrm600/client.py        Hrm600Client: bleak, Notify-Pump, Watch-Emulation
hrm600/filetransfer.py  Download-Zustandsautomat (5002/5004/5000)
hrm600/fitdecode.py     FIT → HR-Zeitreihe (timestamp_16-Auflösung)
hrm600/cli.py           argparse-Subcommands
```

Protokollwissen: `docs/gfdi-filetransfer.md` (File-Transfer, aus Gadgetbridge
destilliert) und upstream `protocol.md` im openrd-Repo (Live-Pfad).

## Nicht verhandelbar

- Die **captured Frames in `bootstrap.py` niemals „verbessern"** — exakte
  Bytes inkl. Counter sind gegen den echten Gurt validiert. Response-Frames
  sind `0x..3a` mit gespiegeltem Request-Counter, Requests `0x..39`;
  Verwechslung führt zu „verbindet, aber keine Typ-21-Subscription".
- Gadgetbridge ist AGPL: nur Protokollfakten übernehmen, **keinen Code**.
- openrd ist MIT: adaptierter Code behält den Attributionshinweis im Header.

## Testen

```bash
.venv/bin/pytest -q          # offline (Codecs, Parser, Zustandsautomat)
.venv/bin/ruff check hrm600 tests
```

Live-Tests brauchen den Gurt: getragen, kein anderes Central verbunden
(Uhr/Handy-App außer Reichweite oder BT aus). Reihenfolge:
`hrm600 scan` → `info` → `live --duration 60` → `probe-files`.
Alle Läufe loggen JSONL nach `captures/` (gitignored) — bei Protokollfragen
zuerst dort nachsehen (`gfdi_rx`-Events mit `decoded_hex`).

## Offene Punkte / Wissensstand

- **Live-Streams am Gurt verifiziert** (Probe-Lauf 2026-08-25,
  `captures/hrm600-probe-20260825T110706Z.jsonl`): kompletter Bootstrap,
  Typ-20-HR, Typ-21-RD, Realtime-HR.
- **Klassischer GFDI-File-Transfer widerlegt**: 5002/5031 → UNSUPPORTED
  (0x02), 5030 SYSTEM_EVENT → ACK. Der HRM 600 nutzt das
  **FileSyncService-V2-Protokoll** (Smart-Feld 43 + deflate-Stream über
  ML-Service 0x2018), implementiert in `hrm600/filesync.py`, dokumentiert in
  `docs/gfdi-filetransfer.md`. **V2 am Gurt noch unverifiziert** — nächster
  Schritt: `hrm600 probe-files` erneut laufen lassen.
- Falls V2 nicht antwortet: Garmin-Connect-Sync per iOS-BT-Logging-Profil +
  PacketLogger mitschneiden und Ablauf vergleichen. Unklar ist v. a., ob die
  FileList-Anfrage als kompakter `0x..39`-Frame korrekt geframt ist oder ein
  anderes Framing (z. B. `0x2b` mit Transport-Header) braucht.
- `decode_heart_rate` (Feld 1 in Feld-1013-Alerts) ist eine plausible, aber
  unverifizierte Annahme — Feld-1013-Payloads im Probe-Log zeigen Muster
  `08 <hr> 10 00 18 <varint>` (Feld 1 ≈ 78–80 bpm, plausibel).
