# GFDI File Transfer — Protocol Reference

> **HRM 600 probe result (2026-08-25):** The strap answers `5002
> DOWNLOAD_REQUEST` and `5031 SUPPORTED_FILE_TYPES` with status
> `UNSUPPORTED` (0x02); `5030 SYSTEM_EVENT` is ACKed. The classic transfer
> (below) is therefore considered disproven for the HRM 600 — it uses the
> **protobuf-based FileSyncService protocol** instead (last section).

Distilled from the Gadgetbridge source code (AGPL — only protocol facts are
re-implemented here, no code was copied). As of Gadgetbridge main,
2026-08-25, `service/devices/garmin/`.

All values little-endian. Every message sits in the GFDI envelope
`[len:2][msg_type:2][body][crc16:2]`, COBS-framed inside the Multi-Link
stream (identical to the live path, see `hrm600/gfdi.py`).

## Message IDs (excerpt)

| ID | Name | Direction (central's view) |
|---|---|---|
| 5000 | RESPONSE (status) | both |
| 5002 | DOWNLOAD_REQUEST | TX |
| 5003 | UPLOAD_REQUEST | TX |
| 5004 | FILE_TRANSFER_DATA | RX (download) |
| 5005 | CREATE_FILE | TX |
| 5007 | FILTER | TX |
| 5008 | SET_FILE_FLAG | TX |
| 5009 | FILE_AVAILABLE | RX |
| 5023 | BATTERY_STATUS | RX |
| 5030 | SYSTEM_EVENT | TX |
| 5031 | SUPPORTED_FILE_TYPES_REQUEST | TX |
| 5037 | SYNCHRONIZATION | RX |

Compact frames (bit 15 set) are mapped by Gadgetbridge to
`(type & 0xff) + 5000`.

## Download flow

```
Central                          Device
  |-- DOWNLOAD_REQUEST (5002) ---->|   fileIndex=0 => FIT directory
  |<-- RESPONSE (5000) ------------|   status, downloadStatus, maxFileSize
  |<-- FILE_TRANSFER_DATA (5004) --|   flags, crc, offset, data
  |-- RESPONSE (5000, ACK) ------->|   transferStatus=OK, nextOffset
  |<-- FILE_TRANSFER_DATA ---------|   ... until offset+len == maxFileSize
  |-- RESPONSE (ACK) ------------->|
```

- **DOWNLOAD_REQUEST body:** `fileIndex:2, dataOffset:4, requestType:1
  (0=CONTINUE, 1=NEW), crcSeed:2, dataSize:4`. Fresh download:
  `NEW, crcSeed=0, dataSize=0, offset=0`.
- **RESPONSE to 5002:** `origType:2=5002, status:1, downloadStatus:1,
  maxFileSize:4`. `status`: 0=ACK, 1=NAK, 2=UNSUPPORTED. `downloadStatus`:
  0=OK, 1=INDEX_UNKNOWN, 2=INDEX_NOT_READABLE, 3=NO_SPACE_LEFT, 4=INVALID,
  5=NOT_READY, 6=CRC_INCORRECT.
- **FILE_TRANSFER_DATA (5004) body:** `flags:1, crc:2, dataOffset:4, data:N`.
  `crc` is the **running** Garmin CRC16 over all data so far
  (seed = CRC of the previous chunk).
- **ACK to 5004:** RESPONSE body `origType:2=5004, status:1=0,
  transferStatus:1, dataOffset:4` where `dataOffset` = next expected
  position (= received offset + data length). `transferStatus`: 0=OK,
  1=RESEND, 2=ABORT, 3=CRC_MISMATCH, 4=OFFSET_MISMATCH, 5=SYNC_PAUSED.

## FIT directory (fileIndex 0)

A file of 16-byte entries:

```
fileIndex:2  dataType:1  subType:1  fileNumber:2
specificFlags:1  fileFlags:1  fileSize:4  timestamp:4
```

`timestamp` in the Garmin epoch (Unix − 631065600; 0 = "no date").
Skip all-zero entries (endless-loop protection).

Relevant file types (`dataType/subType`):

| Type | Meaning |
|---|---|
| 0/0 | DIRECTORY (virtual) |
| 128/4 | ACTIVITY (FIT) |
| 128/15 | MONITOR_A (FIT) |
| 128/28 | MONITOR_DAILY (FIT) |
| 128/32 | MONITOR (FIT) |
| 128/31 | UNKNOWN_31 — "sent by HRM Pro Plus" |
| 8/255 | DEVICE_XML (fileIndex 0xFFFD) |

`dataType=128` ⇒ FIT file.

## Sync orchestration (as Gadgetbridge drives it)

On initialization: `SUPPORTED_FILE_TYPES_REQUEST` (5031, empty body) and
system event `SYNC_READY` (5030, body `[8, 0]`).

- **Response to 5031:** RESPONSE body `origType:2=5031, status:1, count:1`,
  then per type `dataType:1, subType:1, name:string` (length-prefixed).
- The device may send `SYNCHRONIZATION` (5037): `type:1, size:1,
  bitmask:4|8` (bit numbers = FileType ordinal, e.g. 5=ACTIVITIES,
  21=ACTIVITY_SUMMARY, 26=SLEEP). Gadgetbridge replies with `FILTER`
  (5007, body `[3]`), waits for the filter ACK, then starts the directory
  download.
- Fetching always starts with the directory download (fileIndex 0), which
  also nudges the device into flushing its monitor data.
- After a completed sync: system event `SYNC_COMPLETE` (5030, body `[0, 0]`).

## New sync protocol: FileSyncService (V2, HRM 600)

Source: Gadgetbridge `gdi_file_sync_service.proto`,
`FileSyncServiceHandler.kt`, `GarminSupport.downloadFileFromServiceV2`,
`CommunicatorV2.startTransfer`.
Transport: Smart protobuf container, field **43** = `FileSyncService`, sent
as a compact frame (`0x..39`, body `[counter:2][protobuf]`). Responses
arrive as compact `0x2b`/`0x2c` frames (protobuf after 14 header bytes).

`FileSyncService` fields:

| Field | Message |
|---|---|
| 1 | FileRequest `{file:File, unk2=24, unk3=0, unk4=0, unk5=15}` |
| 2 | FileResponse `{status:1 (0=OK, 3=error), handle:3}` |
| 9 | FileListRequest `{cursor_id:1, start_page_id:2, flags1:4, flags2:5}` |
| 10 | FileListResponse `{cursor_id:2, next_page_id:3, file:4 (repeated)}` |
| 12 | NewFileNotification `{file:1 (repeated)}` |
| 15 | FileSetFlags `{file:1 FileId, flags:2 FileId}` — marks "synced" |

- `File`: `{id:1 FileId, type:2 FileType, size:3, page_id:5}`;
  `FileId`: `{id1:1 fixed64, id2:2 fixed64}`;
  `FileType`: `{name:2 string, code:3}` (8=monitor, 9=sports; the name is
  often only set on the first entry of a type).
- `flags1/flags2` = FileId with id1=id2=**42405** (0xa5a5): excludes
  already-synced files. Without the flags, the full history is returned.
- Pagination: `cursor_id` in the response ⇒ continue immediately with
  `FileListRequest{cursor_id}` (pages of ~100).

**HRM 600 findings** (probe runs 2026-08-25, logs in `captures/`):

- File type observed: `STORE_AND_FORWARD_HR_DATA_FIT` (code 0), ~10247
  bytes per file = one buffer chunk (~32 min of HR at ~0.5 s intervals).
- Responses larger than ~495 bytes are **chunked** across compact `0x2c`
  frames: body `[counter:2][data_offset:4][total_len:4][chunk_len:4][data]`.
  Every partial chunk must be ACKed with a compact ProtobufStatus response
  (`[orig_type:2=5044][ACK=0][counter:2][data_offset:4][kept=0][no_error=0]`
  in a kind-0x00 compact frame), otherwise the strap retransmits the same
  chunk every ~5 s and never sends the next one. Reassembly + ACK:
  `FileSyncV2.on_transport_chunk`.
- The file id encodes metadata: `id1 >> 32` is a Garmin timestamp (see
  "time model" in the README — trustworthy only for finalized files),
  `id2 & 0xFFFFFFFF` is the file size in bytes.

**File content** is not delivered over GFDI but over a dedicated
Multi-Link service:

1. Send `FileRequest` → `FileResponse` with a 16-bit `handle`.
2. Register Multi-Link service **0x2018** (fallbacks: 0x4018, 0x6018,
   0xa018, 0xc018, 0xe018); unreliable is sufficient.
3. Write to the new service handle: `[00 00 <handle:2 LE> 00 00]`.
4. The device's first message starts with `00 00 00`, followed by raw
   **deflate**-compressed chunks (zlib inflate).
5. End: the device sends handle management `CLOSE_HANDLE_RESP` (type 3):
   `[00][03][client_id:8][service:2][handle:1][status:1]`.
6. Optionally mark the file as synced via `FileSetFlags` with 0xa5a5 —
   **don't** do this if the Garmin app should still receive the data.

## System event ordinals (5030)

`SYNC_COMPLETE=0, SYNC_FAIL=1, FACTORY_RESET=2, PAIR_START=3, PAIR_COMPLETE=4,
PAIR_FAIL=5, HOST_DID_ENTER_FOREGROUND=6, HOST_DID_ENTER_BACKGROUND=7,
SYNC_READY=8, NEW_DOWNLOAD_AVAILABLE=9, DEVICE_SOFTWARE_UPDATE=10,
DEVICE_DISCONNECT=11, TUTORIAL_COMPLETE=12, SETUP_WIZARD_START=13,
SETUP_WIZARD_COMPLETE=14, SETUP_WIZARD_SKIPPED=15, TIME_UPDATED=16`
