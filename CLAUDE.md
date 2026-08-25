# CLAUDE.md — GarminHRM600

CLI (`hrm600`) für direkten BLE-Zugriff auf den Garmin HRM 600.
Python ≥3.12, bleak, garmin-fit-sdk. UI-Sprache: **Englisch**.
README + `docs/` sind englisch (public Repo); diese Datei bleibt deutsch.

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

- **Das Repo ist PUBLIC** (seit 2026-08-25, MIT). Niemals Gesundheitsdaten
  committen: `hr.csv`, `captures/`, `downloads/`, Exports sind gitignored
  und müssen es bleiben. Die History wurde per filter-repo von hr.csv
  bereinigt — nicht erneut verschmutzen. (Das frühere private Archiv-Repo
  wurde am 2026-08-25 gelöscht.)
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
  `docs/gfdi-filetransfer.md`.
- **V2-Listing am Gurt bestätigt** (2. Probe-Lauf,
  `captures/hrm600-probe-20260825T112101Z.jsonl`): FileListRequest als
  kompakter `0x..39`-Frame wird beantwortet — Dateityp
  `STORE_AND_FORWARD_HR_DATA_FIT` (code 0), je ~10247 B = der 24h-Puffer.
  Antworten > ~495 B kommen **gechunkt** als `0x2c`-Frames
  (`[counter][offset:4][total:4][chunk:4][data]`); ohne per-Chunk-ACK
  (ProtobufStatus-Format, alle 5 s Retransmit) schickt der Gurt Chunk 2 nie.
  Reassembly + ACK: `FileSyncV2.on_transport_chunk`.
- **Download + Decode end-to-end verifiziert** (2026-08-25): 26 Dateien
  `STORE_AND_FORWARD_HR_DATA_FIT`, daraus 82 685 HR-Samples (08.–25.08.).
- **Zeitmodell der Store-and-Forward-Dateien** (wichtig!): Die FIT-Dateien
  enthalten NUR `hr`-Messages (kein file_id, keine timestamp_correlation);
  `garmin-fit-sdk` mit `merge_heart_rates=False` lesen, sonst
  KeyError('record_mesgs'). Samples tragen `event_timestamp` (Geräte-Uhr
  seit Batterie-Einlage, 1/1024 s). Echtzeit-Anker: bei sauber
  finalisierten Dateien ist `id1>>32` der Garmin-Timestamp des LETZTEN
  Samples ⇒ `C = id_ts − ev_last` (über Wochen sub-sekunden-stabil).
  Ring-Buffer-Dateien tragen stattdessen Flush-Timestamps, und einzelne
  Slots haben komplett falsche id-Zeiten (Slot-Reuse) — deshalb wird C als
  größter Cluster über alle Dateien kalibriert
  (`fitdecode.calibrate_event_anchor`) und NIE der einzelne id-Timestamp
  einer Datei geglaubt. `id2 & 0xFFFFFFFF` = Dateigröße in Bytes.
- `decode_heart_rate` (Feld 1 in Feld-1013-Alerts) ist eine plausible, aber
  unverifizierte Annahme — Feld-1013-Payloads im Probe-Log zeigen Muster
  `08 <hr> 10 00 18 <varint>` (Feld 1 ≈ 78–80 bpm, plausibel).
