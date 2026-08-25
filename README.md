# GarminHRM600

CLI für direkten BLE-Zugriff auf den Garmin HRM 600 — ohne Uhr, ohne
Garmin-Connect-App. Live-Herzfrequenz und Running Dynamics streamen sowie
(experimentell) den internen HR-Speicher („24h-Puffer") als FIT-Dateien
herunterladen.

## Installation

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
```

## Kommandos

```bash
hrm600 scan                    # Gurt finden (Name + Garmin-FE1F-Advertising)
hrm600 info                    # Modell, Seriennummer, Firmware, Batterie
hrm600 live --duration 60      # Live-HR + Running Dynamics (JSONL-Log)
hrm600 probe-files             # R&D: Supported File Types + FIT-Directory
hrm600 sync                    # FIT-Directory + alle FIT-Dateien laden
hrm600 decode downloads/*.fit --hr-csv hr.csv   # FIT → HR-Zeitreihe
```

Der Gurt muss getragen (aktiviert) und darf mit keinem anderen Central
(Uhr, Garmin-Connect-App) verbunden sein. Beim ersten Verbinden ggf.
`--pair` mitgeben.

## Wie es funktioniert

Der Gurt spricht neben Standard-BLE (HR `2A37`, RSC `2A53`) das proprietäre
Garmin-Protokoll: Multi-Link (Multiplexer) → COBS-geframte GFDI-Messages →
Smart/EventSharing-Protobufs. Die CLI emuliert die kompakte
Watch-Bootstrap-Sequenz einer fenix 8 (DeviceInformation-ACK →
FeatureCapabilities → EventSharing-Subscribe Typ 20/21) und liest dann:

- **Typ 20 / Feld 1013**: App-Layer-Herzfrequenz
- **Typ 21 / Feld 1014**: Running Dynamics (Kadenz, Bodenkontaktzeit,
  vertikale Oszillation, Balance, Pace/Distanz)
- **Realtime-HR** (Multi-Link-Service 6)

`probe-files`/`sync` nutzen zusätzlich den GFDI-File-Transfer
(DownloadRequest 5002 / FileTransferData 5004), dokumentiert in
[docs/gfdi-filetransfer.md](docs/gfdi-filetransfer.md). Ob der HRM 600 den
Directory-Download beantwortet, ist Gegenstand des Probe-Laufs (M4) —
siehe Status unten.

## Status

- ✅ Transport, Watch-Bootstrap, Live-Streams — **am echten Gurt verifiziert**
  (2026-08-25): HR-Alerts Typ 20, Running Measurement Typ 21, Realtime-HR
- ✅ Erkenntnis aus dem Probe-Lauf: klassischer GFDI-File-Transfer (5002)
  wird vom HRM 600 mit UNSUPPORTED abgelehnt; der Gurt nutzt das
  protobuf-basierte **FileSyncService**-Protokoll (V2)
- 🧪 `probe-files`/`sync` implementieren jetzt den V2-Weg (FileListRequest →
  FileRequest → deflate-Stream über ML-Service 0x2018) — Live-Verifikation
  am Gurt ausstehend
- `sync` markiert Dateien **nicht** als „synced" — die Garmin-App bekommt
  den Puffer weiterhin

## Quellen & Attribution

- Protokoll-Grundlage und Referenz-Client:
  [openrd-ble-running-dynamics](https://codeberg.org/samdumont/openrd-ble-running-dynamics)
  von Sam Dumont (MIT) — der Transport-/EventSharing-Code in `hrm600/` ist
  daraus adaptiert.
- File-Transfer-Protokollfakten: reimplementiert nach dem
  [Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge)-Quellcode
  (AGPL; kein Code übernommen), siehe `docs/gfdi-filetransfer.md`.
