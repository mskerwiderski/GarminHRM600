# GFDI File Transfer — Protokollreferenz

> **Probe-Ergebnis HRM 600 (2026-08-25):** Der Gurt beantwortet `5002
> DOWNLOAD_REQUEST` und `5031 SUPPORTED_FILE_TYPES` mit Status
> `UNSUPPORTED` (0x02); `5030 SYSTEM_EVENT` wird geACKt. Der klassische
> Transfer (unten) gilt für den HRM 600 als widerlegt — er nutzt das
> **protobuf-basierte FileSyncService-Protokoll** (letzter Abschnitt).

Destilliert aus dem Gadgetbridge-Quellcode (AGPL — hier sind nur Protokoll-
Fakten reimplementiert, kein Code übernommen). Stand: Gadgetbridge main,
2026-08-25, `service/devices/garmin/`.

Alle Werte little-endian. Alle Messages stecken im GFDI-Envelope
`[len:2][msg_type:2][body][crc16:2]`, COBS-geframed im Multi-Link-Stream
(identisch zum Live-Pfad, siehe `hrm600/gfdi.py`).

## Message-IDs (Auszug)

| ID | Name | Richtung (Central-Sicht) |
|---|---|---|
| 5000 | RESPONSE (Status) | beide |
| 5002 | DOWNLOAD_REQUEST | TX |
| 5003 | UPLOAD_REQUEST | TX |
| 5004 | FILE_TRANSFER_DATA | RX (Download) |
| 5005 | CREATE_FILE | TX |
| 5007 | FILTER | TX |
| 5008 | SET_FILE_FLAG | TX |
| 5009 | FILE_AVAILABLE | RX |
| 5023 | BATTERY_STATUS | RX |
| 5030 | SYSTEM_EVENT | TX |
| 5031 | SUPPORTED_FILE_TYPES_REQUEST | TX |
| 5037 | SYNCHRONIZATION | RX |

Kompakte Frames (Bit 15 gesetzt) mappt Gadgetbridge auf `(type & 0xff) + 5000`.

## Download-Flow

```
Central                          Gerät
  |-- DOWNLOAD_REQUEST (5002) ---->|   fileIndex=0 => FIT-Directory
  |<-- RESPONSE (5000) ------------|   status, downloadStatus, maxFileSize
  |<-- FILE_TRANSFER_DATA (5004) --|   flags, crc, offset, data
  |-- RESPONSE (5000, ACK) ------->|   transferStatus=OK, nextOffset
  |<-- FILE_TRANSFER_DATA ---------|   ... bis offset+len == maxFileSize
  |-- RESPONSE (ACK) ------------->|
```

- **DOWNLOAD_REQUEST body:** `fileIndex:2, dataOffset:4, requestType:1
  (0=CONTINUE, 1=NEW), crcSeed:2, dataSize:4`. Neuer Download:
  `NEW, crcSeed=0, dataSize=0, offset=0`.
- **RESPONSE auf 5002:** `origType:2=5002, status:1, downloadStatus:1,
  maxFileSize:4`. `status`: 0=ACK, 1=NAK, 2=UNSUPPORTED. `downloadStatus`:
  0=OK, 1=INDEX_UNKNOWN, 2=INDEX_NOT_READABLE, 3=NO_SPACE_LEFT, 4=INVALID,
  5=NOT_READY, 6=CRC_INCORRECT.
- **FILE_TRANSFER_DATA (5004) body:** `flags:1, crc:2, dataOffset:4, data:N`.
  `crc` ist die **laufende** Garmin-CRC16 über alle bisherigen Daten
  (Seed = CRC des vorherigen Chunks).
- **ACK auf 5004:** RESPONSE-Body `origType:2=5004, status:1=0,
  transferStatus:1, dataOffset:4` mit `dataOffset` = nächste erwartete
  Position (= empfangener Offset + Datenlänge). `transferStatus`: 0=OK,
  1=RESEND, 2=ABORT, 3=CRC_MISMATCH, 4=OFFSET_MISMATCH, 5=SYNC_PAUSED.

## FIT-Directory (fileIndex 0)

Datei aus 16-Byte-Einträgen:

```
fileIndex:2  dataType:1  subType:1  fileNumber:2
specificFlags:1  fileFlags:1  fileSize:4  timestamp:4
```

`timestamp` in Garmin-Epoche (Unix − 631065600; 0 = „kein Datum").
Eintrag mit lauter Nullen ignorieren (Endlosschleifen-Schutz).

Relevante Dateitypen (`dataType/subType`):

| Typ | Bedeutung |
|---|---|
| 0/0 | DIRECTORY (virtuell) |
| 128/4 | ACTIVITY (FIT) |
| 128/15 | MONITOR_A (FIT) |
| 128/28 | MONITOR_DAILY (FIT) |
| 128/32 | MONITOR (FIT) |
| 128/31 | UNKNOWN_31 — „sent by HRM Pro Plus" |
| 8/255 | DEVICE_XML (fileIndex 0xFFFD) |

`dataType=128` ⇒ FIT-Datei. Der 24h-HR-Puffer des HRM 600 steckt vermutlich
in einem MONITOR*- oder ACTIVITY-Typ; welcher genau, zeigt der Probe-Lauf.

## Sync-Orchestrierung (wie Gadgetbridge sie fährt)

Bei Initialisierung: `SUPPORTED_FILE_TYPES_REQUEST` (5031, leerer Body) und
System-Event `SYNC_READY` (5030, Body `[8, 0]`).

- **Antwort auf 5031:** RESPONSE-Body `origType:2=5031, status:1, count:1`,
  dann je Typ `dataType:1, subType:1, name:string` (String = längenpräfixiert).
- Gerät kann `SYNCHRONIZATION` (5037) schicken: `type:1, size:1,
  bitmask:4|8` (Bit-Nummern = FileType-Ordinal, z. B. 5=ACTIVITIES,
  21=ACTIVITY_SUMMARY, 26=SLEEP). Gadgetbridge antwortet mit `FILTER`
  (5007, Body `[3]`), wartet den Filter-ACK ab und startet dann den
  Directory-Download.
- Fetch beginnt immer mit Directory-Download (fileIndex 0), auch um das
  Gerät zum „Flushen" der Monitor-Daten zu bewegen.
- Nach abgeschlossenem Sync: System-Event `SYNC_COMPLETE` (5030, Body `[0, 0]`).

## Neues Sync-Protokoll: FileSyncService (V2, HRM 600)

Quelle: Gadgetbridge `gdi_file_sync_service.proto`, `FileSyncServiceHandler.kt`,
`GarminSupport.downloadFileFromServiceV2`, `CommunicatorV2.startTransfer`.
Transport: Smart-Protobuf-Container, Feld **43** = `FileSyncService`, versendet
als kompakter Frame (`0x..39`, Body `[counter:2][protobuf]`). Antworten kommen
als kompakte `0x2b`/`0x2c`-Frames (Protobuf nach 14 Header-Bytes).

`FileSyncService`-Felder:

| Feld | Message |
|---|---|
| 1 | FileRequest `{file:File, unk2=24, unk3=0, unk4=0, unk5=15}` |
| 2 | FileResponse `{status:1 (0=OK, 3=Fehler), handle:3}` |
| 9 | FileListRequest `{cursor_id:1, start_page_id:2, flags1:4, flags2:5}` |
| 10 | FileListResponse `{cursor_id:2, next_page_id:3, file:4 (repeated)}` |
| 12 | NewFileNotification `{file:1 (repeated)}` |
| 15 | FileSetFlags `{file:1 FileId, flags:2 FileId}` — markiert „synced" |

- `File`: `{id:1 FileId, type:2 FileType, size:3, page_id:5}`;
  `FileId`: `{id1:1 fixed64, id2:2 fixed64}`;
  `FileType`: `{name:2 string, code:3}` (8=monitor, 9=sports; Name oft nur
  beim ersten Eintrag eines Typs gesetzt).
- `flags1/flags2` = FileId mit id1=id2=**42405** (0xa5a5): schließt bereits
  synchronisierte Dateien aus. Ohne Flags kommt die komplette Historie.
- Paginierung: `cursor_id` in der Response ⇒ sofort mit
  `FileListRequest{cursor_id}` fortsetzen (Seiten à ~100).

**Datei-Inhalt** kommt nicht über GFDI, sondern über einen eigenen
Multi-Link-Service:

1. `FileRequest` senden → `FileResponse` mit 16-bit-`handle`.
2. Multi-Link-Service **0x2018** registrieren (Fallbacks: 0x4018, 0x6018,
   0xa018, 0xc018, 0xe018), unreliable genügt.
3. Auf den neuen Service-Handle schreiben: `[00 00 <handle:2 LE> 00 00]`.
4. Erste Nachricht vom Gerät beginnt mit `00 00 00`, danach rohe
   **deflate-komprimierte** Chunks (zlib inflate).
5. Ende: Gerät schickt Handle-Management `CLOSE_HANDLE_RESP` (Typ 3):
   `[00][03][client_id:8][service:2][handle:1][status:1]`.
6. Optional `FileSetFlags` mit 0xa5a5 = „synced" markieren — **nicht tun**,
   wenn die Garmin-App die Daten auch noch bekommen soll.

## System-Event-Ordinals (5030)

`SYNC_COMPLETE=0, SYNC_FAIL=1, FACTORY_RESET=2, PAIR_START=3, PAIR_COMPLETE=4,
PAIR_FAIL=5, HOST_DID_ENTER_FOREGROUND=6, HOST_DID_ENTER_BACKGROUND=7,
SYNC_READY=8, NEW_DOWNLOAD_AVAILABLE=9, DEVICE_SOFTWARE_UPDATE=10,
DEVICE_DISCONNECT=11, TUTORIAL_COMPLETE=12, SETUP_WIZARD_START=13,
SETUP_WIZARD_COMPLETE=14, SETUP_WIZARD_SKIPPED=15, TIME_UPDATED=16`
