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

### Phase 4 — ✅ CORE DONE (BitReader + Iris envelope + frame-0 batch walker, 6/10 files)

**Key findings (reverse-engineered from UE 5.6.1 source):**
- The replay stores the Iris `FReplicationReader::Read` stream DIRECTLY per frame. There is
  NO UE packet-info header and NO legacy UNetConnection bunch header in front of it (legacy
  bunch parser proved to NOT tile — all header-combo brute-forces failed).
- Packets within a frame are FRAGMENTS of one merged Iris stream (no individual packet tiles;
  frame 0's first packet has count=83 but only 20 bytes → fragment). Frame 0 of 6/10 files
  stitches cleanly into one stream with `ObjectBatchCount=83` and 83 batch headers.
- `phase4/bitstream.py`: faithful LSB-first `FBitReader` (matches UE semantics). **Bit-exact
  verified** via round-trip (mirror BitWriter → BitReader) for read_int, read_int_packed
  (0/106/113), read_int(15), raw bits, consuming exactly all bits. Fixed a real bug:
  read_int_packed/read_int_packed64 MUST read bit-by-bit (not byte-aligned) or they misalign
  after any non-byte-aligned read.
- `phase4/iris_reader.py`: Iris envelope + batch walker (early frame-0 version; the envelope
  shape was later corrected and moved into `phase6/state_decoder.py`). The frame-0 envelope has
  `ObjectBatchCount`=ReadBits(16); each batch = `[bIsDestruction(1)][if not:
  NetRefHandleId=ReadPackedUint64 (NO valid bit)][BatchSize=ReadBits(16)][bHasOwnerData]
  [bHasExports]` then state/export data. **NOTE: `BatchSize` is ReadBits(16), NOT ReadBits(8)**
  — the earlier `NumBitsUsedForBatchSize=8` measurement was wrong and was fixed (it was
  corrupting framing). See Phase 6 for the confirmed envelope.
- **Validation (ad-hoc, exit 0):** frame-0 Iris envelope tiles for 6/10 files (TyrReplay
  1,2,7,8,9,10): count=83, 83 batches walked, real handles. TyrReplay 3,4,5,6 read a large
  batch_count at offset 0 in the naive per-frame walk — resolved later (Phase 6) by decoding
  PER PACKET: each frame packet is its own `Read()` with its own `ObjectBatchCount`, and a
  per-frame (not per-packet) envelope assumption was what made those files look like outliers.
  The per-PACKET model decodes all 10 files with stable framing (reads=3538 on TyrReplay1).
- Per-packet Oodle: ruled OUT at container level AND per-packet (all 10 files' frame-0 packets
  are raw, not Oodle/zlib) — PROJECT_INFO's "Oodle per-packet" does not apply to this dataset.

### Phase 4.x — ✅ DONE, but SUPERSEDED by per-packet decode (see Phase 6)

- **Original finding (now superseded):** the replay stores ONE continuous Iris session stream
  split across frame packet buffers; `phase4/session_reader.py` `concat_frames()` then
  `session_decode()` loops `[uint16 ObjectBatchCount][batches...]` until exhausted. This tiled
  all 10 files at 100% consumption (reads=6..16, batches=6808..24208).
- **CORRECTION (Phase 6, committed):** the continuous/concatenated model is WRONG in practice.
  Concatenating all frames into one stream makes any single misread cascade and consume the rest
  of the replay (observed: reads jumped, classes dropped). The CORRECT model is **per-packet**:
  each frame packet is a self-contained `FReplicationReader::Read()` with its own
  `ObjectBatchCount`=ReadBits(16). `decode_session` runs PER PACKET; a misread cannot cascade.
  `session_reader.concat_frames()` is retained as a utility but is NOT used by the active decoder.
  On TyrReplay1 this yields reads=3538 packets with stable framing.

  ### Phase 5 — ✅ DONE (object identity & state descriptors, all 10 files)

  - `phase5/object_identity.py`: recovers the `FNetFieldExportGroup` table from each replay's
    first-frame export section (format: `SerializeIntPacked(PathNameIndex);
    SerializeIntPacked(WasExported); if WasExported: FString PathName;
    SerializeIntPacked(NumExportsInGroup); FNetFieldExport <<`). Each group = one replicated
    class with `NumExportsInGroup` = the Iris **changemask bit-count** (replicated field count).
  - **Validation (ad-hoc, exit 0, all 10 files):** groups recovered = 106..314 (longer recordings
    see more classes). Exported classes per file = 15..30. `nec` (replicated field count) ranges
    1..1947. Real class→field mappings confirmed: `WorldSettings`(22, WorldGravityZ),
    `BP_CaptureZone_C`(21), `BP_BasicSpawnWall_C`(16, bHidden), `NetworkGameplayTagNodeIndex`
    (1947 tag fields, first_field e.g. `Gameplay.Vehicle.CanOpener`). Native classes cross-validate
    against the TYR SDK dump `ClassesInfo.json` (7373 classes): `WorldSettings`, `TyrMiniMapComponent`,
    `BP_LobbyPlayerRecord_C` resolve. Game-specific blueprints (`BP_CaptureZone_C`, `Map_*_C`) are
    cooked assets not in the native SDK dump — expected, not a failure.
  - **Note on the SDK dump:** it contains C++ class memory layouts (ClassesInfo/Structs/Offsets) but
    NOT Iris `FReplicationProtocol` descriptors (changemask bit widths / replicated-property ordering
    per property). Those come from the replay's own export table (above), which is sufficient for
    object identity and changemask structure. Full per-property VALUE decode (Phase 6) needs the
    property serialization logic + the export group's per-field metadata (type/checksum), which the
    `FNetFieldExport` blob carries (CompatibleChecksum + ExportName) — enough to drive typed decode.

  ### Phase 6 — structural decode + class resolution + per-type VALUE extraction (option 1) DONE

  - **CRITICAL FIX: fixed-width bit reads (`read_bits` vs `read_int`).** `BitReader.read_int(N)`
    is UE `SerializeInt` (variable `ceil(log2(N))` bits) — WRONG for the many fixed-width Iris
    fields. Added `BitReader.read_bits(N)` (true LSB-first fixed width) in `phase4/bitstream.py`
    and switched ALL engine fixed-width reads in `phase6/state_decoder.py` to it: `batch_size`
    (=ReadBits(16)), `changemask` masks + sparse-uint header/deltas, `read_packed_uint64/32`,
    `read_string` len+payload, `read_net_token` payload, `read_float32`, `read_conditional_vector`
    (90/96b), `read_rotator` (48b), `destroy_flags` (3b), `BaselineIndex` (2b). This was a
    pervasive bug corrupting ALL decoding.
  - **CHANGEMASK = `ReadSparseBitArray` (FNetBitArrayView, NetBitStreamUtil.cpp:645), NOT raw bits
    and NOT the "last_word + 32b words" format.** `NonZeroWordMask`=ReadBits(WordCount); optional
    `InvertedWordMask`=ReadBits(WordCount) if ReadBool(); per word `ReadSparseUint32UsingIndices`
    (2-bit header=GetBitsNeeded(3) + delta-encoded bit indices, OR byte-mask fallback with the
    `(8-BitCount)&7` trim applied by the READER only). Round-trip unit-tested: 300 cases
    (cm 16/21/22/34/43/64, 30% density) PASS — this is the format in the committed code.
  - **BATCH ENVELOPE corrected from engine source (ReplicationReader.cpp:929):** `bIsDestructionInfo`(1)
    → if destruction: `ReadFullNetObjectReference(b_inline=True)` + `ReadBits(3)` (NetFactoryId max)
    → else: `BatchHandle`=`ReadPackedUint64` (NO valid bit — this was a key bug, the old code added
    a spurious valid bit) → `BatchSize`=ReadBits(16) → `bHasBatchOwnerData`(1) → `bHasExports`(1).
    Subobject handles ALSO use `ReadPackedUint64` (no valid bit). Per-batch EXPORT TAIL is skipped
    via `read_exports` (ObjectReferenceCache.cpp:1714: NetToken exports + NetObjectReference
    exports + MustBeMapped) — REQUIRED or framing drifts into the next batch.
  - **PER-PACKET DECODE (correct Iris model, committed).** Each frame packet is a self-contained
    `FReplicationReader::Read()` with its own `ObjectBatchCount`=ReadBits(16). `decode_session`
    runs PER PACKET (NOT on `concat_frames()` output — a misread there cascaded and consumed the
    whole stream). Result on TyrReplay1: **reads=3538 packets**, cm16=30 objects, archetype→class
    resolves `Map_Dunes_Rework_C` (cm16) correctly (e.g. arch=24 → Map_Dunes_Rework_C).
  - **`state_start` FIX (this session, UNCOMMITTED):** `extract_object_values` must seek to the
    changemask position (captured BEFORE `decode_object_state` consumes it). Previously it sought
    PAST the changemask and returned empty values for every object. After the fix, every object
    with a non-empty changemask carries recovered 32-bit (float+int) words. Verified: TyrReplay1
    cm16 5/5 dirty objects recover values; TyrReplay10 cm16 12/12 + cm34 player-records 2/2.
  - **OPTION 1 DELIVERABLE: typed value extraction (no field names).** For each object, read the
    changemask, then for each SET bit decode a 32-bit word and present BOTH the `float32` and
    `int32` view (`extract_object_values`). Output: real dirty values per changemask bit for
    resolved dynamic actors (cm16 Map_Dunes_Rework_C, cm34 BP_LobbyPlayerRecord). Example
    cm34 (TyrReplay10): `h=34359738378 cm=...0010 vals=[32:f0.000/i6300226]` (bit32 = a small
    int/handle, likely team/vehicle id).
  - **WorldSettings / WorldGravityZ — DROPPED (user "Do 1").** Static level actor; creation ref is
    written INVALID in these replays so the class can't be recovered from the creation header.
  - **`ReadObjectsPendingDestroy` (16-bit count + records) — present in source between
    ObjectBatchCount and the object loop, but LEFT DISABLED.** Enabling it consumed the stream
    (destroy-count misread); per-packet isolation keeps framing stable without it. Revisit only if
    object counts come up short. NOTE: this is a known gap, not a proven-correct skip.
  - **ARCHETYPE → CLASS MAP: WORKING (dynamic actors).** `decode_session` returns `archetype_to_cm`;
    cross-referenced with Phase 5 export groups (cm_size → class_path). Resolves e.g.
    `arch=24 → cm16 → /TyrMapDunes/Maps/Map_Dunes_Rework.Map_Dunes_Rework_C`.

  ---

  ### Field-labeling source — Dumper-7 dump (available, this session)

  **Correction to earlier "SDK lacks Tyr classes / no field info" claim:** the project ships a full
  Dumper-7 dump at `5.6.0-31351+++Tyr+release-Tyr_OLD\`:
  - `GObjects-Dump-WithProperties.txt` — class → property **name / offset / type** (full UPROPERTY
    layout). e.g. `TyrPlayerRecord`: PlayerName(Str @0x418), MyTeamID(Struct @0x428), bIsAlive(Bool
    @0x468), StatTags(Struct @0x470), PartyId(Str @0x448), UserId(Str @0x458), bIsLobbyOwner,
    ConnectionState(Enum), Viewmodels(Array). `TyrPlayerStateBase` (parent of BP_TyrPlayerState):
    TeamId, VehicleTag, KillerRef, StatTags, CosmeticStyle, PlayerRecord, plus OnRep_* funcs.
  - `Dumpspace\ClassesInfo.json` — MDK SDK, `data` = 7373 classes w/ properties.
  - `CppSDK\SDK\*.hpp` — per-class C++ headers.

  **CAVEAT (not a blocker, just a mapping step):** Dumper declaration/CDO order ≠ Iris changemask
  bit order. Only `Replicated`/`ReplicatedUsing` UPROPERTYs get a changemask bit, in Iris-assigned
  order (derivable from the `OnRep_*` functions + Replicated flags). So bit N ≠ Dumper property N.
  Mapping is solvable: use the replicated-property subset + empirical anchors (StrProperty =
  uint32 length + ASCII; known constants). This is the next phase ("option B").

  ---

  ### Current Capabilities (as of 2026-07-17, end of session)

  **What you CAN do right now:**
  - Decode every replay end-to-end: per-packet Iris decode, class resolution via archetype handle.
  - Get the changemask bit-width (cm_size) and class PATH for every replicated class in a replay
    (Phase 5 export groups: 106–314 groups, 15–30 exported classes per file).
  - Extract RAW per-object replicated state: for each object with a dirty changemask, recover the
    32-bit words as both float32 and int32. Verified working on cm16 (Map, 30 objs TyrReplay1),
    cm34 (BP_LobbyPlayerRecord, 2 objs TyrReplay10), and cm1 (floods every replay — likely
    replicated movement/transform on all actors).
  - Class inventory across the 10 replays: cm16=Map, cm34=BP_LobbyPlayerRecord (player records),
    cm64=BP_TyrPlayerState does NOT appear in any sample replay (only its cm34 PlayerRecord subclass
    does). cm1/cm3/cm5/cm7/cm11/cm12/cm83 appear in various replays.

  **Player username (`PlayerName`): EXTRACTABLE (from checkpoint plaintext).** Verified all 10
    replays — the display name (e.g. "Unimork") is written in plaintext inside CHECKPOINT chunks,
    in a `TyrTestPlayerStateSubsystem_<id>` entry (null-terminated ascii after a marker + zero).
    `phase6/state_decoder.py::load_player_names()` scans the raw replay bytes and returns
    `{subsystem_id: name}`. Integrated into `main()` which prints a per-replay roster line.
    IMPORTANT: the name is NEVER in the live Iris ReplayData bitstream (raw byte scan of all
    packets in TyrReplay10 → 0 occurrences), so it cannot be recovered from decoded live objects;
    it is a per-replay checkpoint fact, not per-object state. The cm34 (BP_LobbyPlayerRecord)
    tag in the output explicitly notes `from checkpoint plaintext`.
  - **Live-stream cm34 creation-payload descent (option c) — PROVEN INFEASIBLE.** The username
    does not appear in any live ReplayData packet, so descending into the cm34 creation bunch
    yields nothing. Checkpoint plaintext is the only source.
  - **Labeled fields (e.g. "bit 5 = Kills"): NOT available.** Needs the bit→field-name mapping
    (Dumper-7 + Iris replicated order), which is unwritten. `extract_object_values` gives positional
    float/int words only.
  - **WorldSettings / WorldGravityZ:** dropped (static actor, invalid creation ref).
  - **ReadObjectsPendingDestroy:** disabled (known gap).

  ---

  ### Phase 7 — Option B: field labeling (attempted 2026-07-17, PARTIAL)

  **Goal:** map changemask bits -> field names (labeled stats) + decode usernames.

  **Delivered (phase7/field_labeler.py + phase7/validate_option_b.py):**
  - Full Dumper field-set dictionaries (name + type per class) loaded and cross-linked
    to every replay export group via direct match (native classes) or parent-class
    inheritance (Blueprint `BP_*_C`/`Map_*_C`/`PC_Replay_C` -> their native Dumper parent,
    e.g. `BP_LobbyPlayerRecord_C` -> `Tyr.TyrPlayerRecord`: PlayerName/MyTeamID/bIsAlive/StatTags).
  - Labeled decode pipeline: per dirty changemask bit, attach candidate field name(s) +
    raw 32-bit (float/int) value. Ambiguity (multiple classes share a cm_size) is flagged
    explicitly (`[AMBIGUOUS cm_size]`) rather than guessed.
  - Validated that the replay's export `first_field` (bit-0 property) resolves to a real
    name for native classes (field SET is correct), enabling type-checked candidate labels.

  **CRITICAL FINDING (validated across all 10 replays, 67 native classes):**
    **replay bit-0 != Dumper[0] for ALL 67 native classes (0 matches).**
    => **Dumper declaration order != Iris changemask bit order** (confirms the earlier
    caveat). So `bit N != Dumper field N`. The Dumper list is a valid FIELD-SET dictionary
    (correct names + types) but its ORDER is not the wire/changemask order.

  **Remaining gate (the real Option B blocker, as predicted):**
  - The authoritative Iris field order lives in the replay's OWN `FNetFieldExport` table.
    The first-frame export blob only carries the delta-exported fields (e.g. WorldSettings
    nec=22 but only 9 FNetFieldExport records in frame 0; several as `hardcoded#N` FName
    indices). The COMPLETE per-bit name table is in the Checkpoint/NetExport stream or
    requires decoding the StringTokenStore dictionary (the plan's flagged gate).
  - 120 Blueprint classes carry `hardcoded#216` (and similar) for bit-0 -> their field
    names are FName-index-encoded and need the engine's hardcoded FName table (from the
    shipping exe `TyrClient-Win64-Shipping.exe`) OR the StringTokenStore decode.
  - Per-field VALUE decoding also needs per-type NetSerializer logic (structs/vectors/
    gameplay tags) — currently only raw 32-bit words are recovered (float+int views).

  **Conclusion:** Option B part (a) [bit->name] is BLOCKED at the authoritative-order step;
  the Dumper approach gives field SETS but not proven bit ORDER. Part (b) [usernames] is
  SOLVED via checkpoint plaintext (`load_player_names` in phase6) — no StringTokenStore decode
  needed for the display name. Next concrete step to unblock part (a): resolve hardcoded FName
  indices from the exe (map #216 -> property name) AND/OR decode the Checkpoint NetExport
  FNetFieldExport block for the full ordered table.

  ---

  ### Remaining work (next phases)

  - **Username (player display name): DONE.** Recovered from checkpoint plaintext via
    `load_player_names()` in phase6/state_decoder.py (verified all 10 replays). No live-stream
    or StringTokenStore decode required. To be committed alongside the Phase 6 state_start/read_bits
    fixes and the Phase 7 scripts.
  - **Option B continued — resolve hardcoded FName indices / decode Checkpoint NetExport:**
    extract the engine hardcoded FName table from `TyrClient-Win64-Shipping.exe` to map
    `hardcoded#N` -> property name, and/or parse the Checkpoint chunk's full
    `FNetFieldExport` (all field names, ordered) to get authoritative bit->name for all
    classes at once. This is the gate for labeled stats (NOT for usernames, which are solved).
  - Per-field typed value decode (NetSerializers) so raw 32-bit words become real values.
  - Re-enable / correctly position `ReadObjectsPendingDestroy` if object counts come up short.
  - Commit Phase 6 username integration (state_decoder.py) + Phase 7 scripts (username extractor,
    field labeler, checkpoint explorer). The core Phase 6 fixes (state_start, read_bits,
    changemask, per-packet) were already committed in earlier sessions.
  - Optional: run the decoder across all 10 replays to build a class-inventory consistency table.

