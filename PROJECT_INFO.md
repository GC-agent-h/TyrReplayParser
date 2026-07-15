# Tyr Replay Parser — Project Info

Reverse-engineering the UE5.6 network replay format for **Tyr** (`5.6.0-31351+++Tyr+release`).
Evidence sources: Dumper-7 dump (`5.6.0-31351+++Tyr+release-Tyr_OLD`), the shipping
binary (`Tyr_Playtest/Tyr/Binaries/Win64/TyrClient-Win64-Shipping.exe`), and the 10 sample
`.replay` files in `Demos/`.

## Key findings (summary)

### 1. NetDriver / Streamer — STOCK, no custom format
- Only game-specific NetDriver is `Tyr.TyrNetDriver` (live gameplay net driver, not replay).
- Replay path uses stock **`Engine.DemoNetDriver`** + **`LocalFileNetworkReplayStreaming`**.
- **No** custom `INetworkReplayStreamer` subclass and **no** proprietary streamer found.
- Game replay classes (`TyrReplayController`, `TyrReplaySubsystem`, etc.) are UI/controller
  logic only — not serialization.
- **Implication:** parse the standard UE `.replay` container; nothing proprietary to reverse.

### 2. Compression — OODLE (statically linked) + zlib fallback
- No `oo2core.dll`/`oo2net.dll` shipped, but Oodle is **statically linked** into the exe
  (symbols `OodleLZ_Compress`, `OodleLZ_Decompress`, `OodleNetwork`, `OodleLZB` present).
- `zlib` is also present (`zlib_new/read/write/flush`, `deflate`).
- **Implication:** chunks must be decompressed. Need a compatible Oodle decompressor
  (extract the export from the exe, or obtain the matching Oodle SDK for this engine era).
  Check the per-chunk compression-method byte — some chunks may be zlib.

### 3. Encryption — NONE
- Header magic is stock `0x1CA2E27F` on all 10 files (header is cleartext).
- Body Shannon entropy ≈ 5.0–5.5 (mid-file 5.5, tail 5.0), far below ~8.0 expected for AES.
- `bcrypt.dll` + `AES-256` string exist in the exe, but **no** replay-encryption symbols
  (`bEncryptedReplays`, `AesKey`, `EncryptReplay`) — AES is for general net/cooker use.
- **Implication:** no decryption key needed. Header + chunk table are readable in cleartext;
  body is compressed, not encrypted.

### 4. Replication model — TARGET IRIS
- Full Iris stack compiled in: `IrisCore.ReplicationSystem` (UReplicationSystem),
  `ObjectReplicationBridge`, `IrisFastArraySerializer`, `ReplicationStateDescriptorConfig`,
  `DataStream`/`ChunkedDataStream`, plus `Engine.EngineReplicationBridge`.
- Engine switch present: `Engine.GameNetDriverReplicationSystem` (enum `NetCore.EReplicationSystem`)
  selects Legacy vs Iris per net driver.
- Legacy system (`NetCore.FastArraySerializer`, `FRepLayout`) is also present but is the
  non-default path for a UE5.6 shipping title.
- **Implication:** build the parser around **Iris state descriptors / FNetSerializers**,
  NOT classic `FRepLayout` + `PackageMapClient`. Add a runtime detector on the first
  replication packet to confirm Iris vs Legacy before committing.

## Bottom line for the parser
1. Use the standard UE `.replay` container reader (stock DemoNetDriver streamer).
2. Decompress chunks via Oodle (with zlib fallback per chunk method byte).
3. No encryption — read header/chunks directly.
4. Decode replication as **Iris**, with a first-packet legacy/Iris autodetect.

## Recommended next step
Scaffold a minimal replay header + chunk-table reader, confirm the per-chunk compression
method byte, and validate Oodle decompression on one chunk before building the Iris packet
decoder.
