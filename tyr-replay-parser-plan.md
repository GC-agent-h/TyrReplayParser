# Tyr Replay Parser — Standalone Parser Development Plan

Based on recon findings: stock UE `.replay` container, Oodle-compressed chunks (zlib
fallback possible per chunk), no encryption, Iris replication (with legacy fallback to
rule out at runtime).

This plan sequences the **standalone parser** build (Path B): a from-scratch reader
validated phase-by-phase, ideally cross-checked against a real-engine playback harness
(Path A) where possible.

---

## Phase 1 — Container & Header

**Goal:** Parse file header + chunk index only. No decompression yet.

1. In the UE5.6 source, locate the stock local-file replay format:
   `Engine/Source/Runtime/NetworkReplayStreaming/LocalFileNetworkReplayStreaming/Private/LocalFileNetworkReplayStreaming.cpp`
   and its header. Find the struct/serialization function guarding the magic
   `0x1CA2E27F`, and the chunk-table entry struct.
2. Implement a minimal reader: header fields + ordered chunk list (type, size, offset).
   Run against all 10 sample files — chunk counts and total sizes should sum sanely
   against file size.
3. Copy the chunk-type enum **verbatim** from source (don't guess values). Tag every
   chunk in all 10 files by type; dump a summary table (counts/sizes per type) as a
   reusable fixture for later phases.

**✅ Checkpoint:** For all 10 replays, print an ordered chunk list (type/size/offset)
with no decompression involved.

---

## Phase 2 — Decompression

**Goal:** Decompress every chunk correctly and consistently.

1. Find the actual compression call site — search
   `LocalFileNetworkReplayStreaming.cpp` and
   `Engine/Source/Runtime/Engine/Private/Net/ReplayHelper.cpp` for
   `FCompression::CompressMemory` / `UncompressMemory` calls around chunk read/write.
   Confirm whether compression method is truly per-chunk-selectable or fixed by a CVar
   — don't assume a per-chunk method byte without confirming it in source.
2. Distinguish Oodle products: `OodleLZ_Compress/Decompress` symbols = **Oodle Data**
   (general block compression, used by `FCompression`'s Oodle codec — this is what
   matters here). `OodleNetwork*` symbols = a **different** product for live packet
   compression — not relevant to replay chunk decompression. Confirm by checking which
   symbol the Oodle `FCompression` codec module actually calls.
3. **Use the bundled SDK, not the shipping exe.** `Engine/Source/ThirdParty/Oodle`
   should contain the redistributable Oodle Data libraries matching this engine
   version — link against those in a standalone tool rather than signature-scanning
   the binary.
4. Decompress one chunk (start with a checkpoint chunk — most self-contained). Sanity
   check: plausible structured bytes and consistent uncompressed size, not garbage.
   If wrong, suspect: wrong codec (zlib vs Oodle), wrong size fields, or an
   unaccounted-for chunk sub-header.

**✅ Checkpoint:** Every chunk in all 10 files decompresses to consistent, plausible
uncompressed sizes.

---

## Phase 3 — Post-Decompression Framing

**Goal:** Slice decompressed chunk bytes into discrete saved packets.

1. Decompressed bytes are still wrapped in DemoNetDriver/DemoNetConnection framing
   (packet boundaries, sequence numbers, playback timing) — not raw Iris payload yet.
   Read the **writer** side in
   `Engine/Source/Runtime/Engine/Private/DemoNetDriver.cpp`
   (`WriteDemoFrame` or equivalent) — reader and writer are symmetric, so the writer
   is often the faster way to understand expected layout.
2. Identify: per-packet size prefix, timestamp/tick fields, and where the actual
   bitstream payload begins. This layer is generic UE machinery, not Iris-specific.

**✅ Checkpoint:** A decompressed chunk can be sliced into discrete "saved packets,"
each with a byte range believed to be the raw serialized payload.

---

## Phase 4 — Iris Bitstream Reader

**Goal:** Correctly bit-read packet framing using Iris-native primitives.

1. **Do not reuse legacy `FNetBitReader`/`FBitReader` logic.** Iris has its own
   bitstream primitives — check the `NetCore` module, e.g.
   `Engine/Source/Runtime/Net/Core/Private/Net/Core/NetBitStreamReader.cpp`
   (`FNetBitStreamReader`/`FNetBitStreamWriter`). Confirm word size, endianness, and
   packed int/float encoding match this class — this is the most common source of
   "looks like UE but doesn't decode" bugs.
2. Find `FNetSerializationContext` and trace how a top-level packet payload is
   dispatched (NetToken batches, replication data batches, RPCs, etc.) — this maps
   out the outer contents of the byte stream.

**✅ Checkpoint:** A saved packet's outer header/framing bit-reads cleanly, even
before decoding any replicated properties.

---

## Phase 5 — Object Identity & State Descriptors

**Goal:** Predict the exact wire layout of a class from source + dump, before
decoding any real packets.

1. **`FNetRefHandle`, not `NetGUID`.** Iris replaces the classic GUID cache with
   handle assignment via `UObjectReplicationBridge`. Find where handles get
   created/exported in the stream (likely tied to NetToken batches for names/paths).
   Don't port over classic GUID-cache-export logic — the structure differs.
2. Find `FReplicationStateDescriptorBuilder` (or equivalent) in
   `Engine/Source/Runtime/IrisCore`. Read how it walks a class's properties at
   runtime: enumeration order, which flags qualify a property (`Replicated`,
   `ReplicatedUsing`, `NetSerializer` overrides), and special handling for fast-array
   properties (`FIrisFastArraySerializer` is present in the dump, so at least one
   class uses it).
3. **Cross-reference against the Dumper-7 dump.** For each replicated class of
   interest, extract its property list in declaration order with replication flags.
   This is a *predicted* descriptor layout — verify it against the descriptor
   builder's actual enumeration order (which may not match raw declaration order;
   could be size-sorted, flag-sorted, etc.).

**✅ Checkpoint:** For one known, simple class (e.g. a projectile or pickup — not the
player pawn), the exact wire layout (field order + `NetSerializer` per field) can be
predicted purely from source + dump, with zero packets decoded yet.

---

## Phase 6 — Decode & Validate

**Goal:** Decode real packet data and confirm correctness.

1. Implement state-descriptor-driven decode for the one simple class chosen in
   Phase 5. Run it against a checkpoint chunk (full-state snapshot — easiest target)
   from one sample file.
2. Validate against anything externally checkable: plausible position/rotation values
   (in map bounds, smooth deltas over time), or cross-reference against any
   in-game/UI-exposed data for that replay.
3. Expand to the full class set only after one class round-trips cleanly. Then move
   to delta/incremental packets between checkpoints — harder than checkpoints, since
   it requires correct fast-array delta semantics, not just full-state decode.

**✅ Checkpoint:** One class decodes correctly and consistently across all 10 sample
files, from both checkpoint and delta data.

---

## Sequencing Notes

- **Phases 1–3** are generic UE mechanics, low-risk given confirmed findings (stock
  container, no encryption). Should move fast.
- **Phases 4–6** are where reverse-engineering effort concentrates — Iris is newer
  and has less public prior art than the legacy replication system. Budget most
  project time here.
- **Real milestone:** successfully decoding one simple class from one checkpoint —
  not "parsing the header." Treat that as the point the rest of the project is
  de-risked.

## Open Follow-Ups (for later phases)
- NetToken / name-interning mechanism — how strings/paths are batched and referenced.
- Fast-array delta encoding in Iris — needed for Phase 6's incremental-packet stage.

---

## Status Log (working session)

### Phase 1 — ✅ DONE (validated on all 10 files)
- `phase1/replay_reader.py`: header + chunk index. Magic `0x1CA2E27F`, FileVersion=7,
  FString friendly name (length -257 → 257 UTF-16 chars), no Encrypted field. Chunks
  tile `[0x242, EOF)` exactly on all 10 files. Chunk types: 0=Header, 1=ReplayData,
  2=Checkpoint, 0xFFFFFFFF=Unknown(cleared). Real DemoHeader chunk at data_off 0x675213
  (size 264, magic `0x2CF5A13D`, version 19); the type-0 chunk at 0x24a is size 0 (cleared).

### Phase 2 — ✅ DONE (validated on all 10 files)
- `phase2/decompress.py`: all container payloads are RAW (SupportsCompression=false in
  LocalFile streamer). No zlib/Oodle wrapper at container level. Per-packet Oodle pending.

### Phase 3 — ✅ DONE (validated on all 10 files, 2026-07-16)
- `phase3/frame_parser.py`: `parse_replaydata(payload)` tiles every ReplayData chunk
  EXACTLY (offset==len) for all 10 samples. Frame counts: TyrReplay1=3781, 2=5179,
  3=4555, 4=2406, 5=4945, 6=3866, 7=2181, 8=3927, 9=4839, 10=5058.
- **CORRECTION (supersedes earlier "bit-packed" finding):** the ReplayData stream is
  BYTE-ALIGNED, written through `FLocalFileStreamFArchive` (a byte FArchive, NOT
  FBitWriter). The earlier byte-parse failures were caused by wrong SUB-SECTION models,
  not bit-packing. Root causes fixed:
  1. `SerializeIntPacked` = bit0 more-flag, payload=byte>>1 (UE Archive.cpp, confirmed).
  2. `TArray<uint8> GUIDData` = RAW int32 length (not varint).
  3. `FNetworkGUID::operator<<` = `SerializeIntPacked64` (uint64, up to 9 bytes).
  4. `FNetFieldExport` blob: 1-byte Flags + (if bExported) SerializeIntPacked(Handle)+
     u32 checksum + StaticSerializeName (1 byte bHardcoded; if not hardcoded: FString +
     int32 Number) + (if bExportBlob) SafeNetSerializeTArray.
  5. Streaming levels ALWAYS read (format differs by HasStreamingFixes).
  6. Frame-0-only 16-byte prefix between TimeSeconds end (0x08) and export start (0x18).
- Winning params (all 10 files): order=EG (exports then GUIDs), sf=False, gsf=False,
  frame-0 export_start=0x18. `parse_replaydata` auto-detects by trying combos.
- Frame layout: [i32 LevelIndex][f32 TimeSeconds][export section][streaming levels]
  [external data][game-specific if flag][packets][EndCount]. Packets = [int32 Count]
  [Count bytes], Count==0 terminates.

### Phase 4 — IN PROGRESS (Iris bitstream reader)
- `parse_frame` returns per-frame packet byte buffers (`pkts` list). Phase 4 = parse each
  packet's bytes as Iris replication data (FBitReader over the packet buffer) to extract
  actor/property state. NOTE: packet PAYLOADS (the replicated bunch data) ARE bit-serialized
  at the Iris/bunch level — that is the genuine bit-packed layer, distinct from frame framing.
- `phase3/bit_reader.py` has a BitReader stub (LSB-first SerializeIntPacked) for this.
- Per-packet Oodle decompression still pending (PROJECT_INFO: Oodle per-packet).

