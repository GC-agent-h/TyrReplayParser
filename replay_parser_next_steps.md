# Tyr Replay Parser — Naming Layer & Priority Class Decoding

> **Revision note:** Updated after agent diagnostics showed that some priority classes
> (`BP_TyrGameState_C`, `WorldSettings`, `BP_TyrPlayerState_C`) currently produce **zero**
> decoded objects from the delta (`ReplayData`) stream — not just unlabeled ones — and that
> the 10 `Checkpoint` chunks are completely untouched by the current pipeline. This version
> reorders the plan: diagnose the zero-decode problem and switch to checkpoint-based full-state
> extraction *before* investing further in delta-stream naming/decoding. See "Suggested execution
> order" at the bottom for the full revised sequence.

## 0. Diagnose the Zero-Decode Classes (do this first)

Before building anything else, confirm *why* `BP_TyrGameState_C`, `WorldSettings`, and
`BP_TyrPlayerState_C` yield zero objects in `phase6_iris_decode` despite being present in
`class_by_cm`. This is a decoder gap, not a naming gap — no amount of `FNetFieldExport`
resolution will help if the object walker never emits these objects in the first place.

Likely causes to check, in order of likelihood:
- **Batch/op skip logic**: the `skipped: 2831` counter (59% of all reads) suggests the walker
  is bailing out on packet types or op-codes it doesn't recognize. Log the op-code/packet type
  for every skipped read and check whether these three classes cluster on a specific
  unhandled op (e.g. a "full replication" op distinct from the "delta update" op your walker
  currently handles).
- **Singleton/rare-update objects**: `WorldSettings` and `BP_TyrGameState_C` likely have exactly
  one instance each and update infrequently — if your walker only samples a fixed window of
  frames, or dedupes by handle in a way that drops rarely-touched handles, these could be
  falling through silently.
- **Mode mismatch**: `phase6_iris_decode.mode_counts` shows `strict: 9` vs `agnostic: 31` —
  check whether these three classes require strict-mode field descriptors your decoder doesn't
  have loaded, and are being silently dropped rather than falling back to agnostic mode.

Output of this step should be a short log/report: for each of the three classes, which specific
op-code or condition causes the walker to skip it. This determines whether the fix belongs in
the delta-stream walker (if fixable cheaply) or whether it reinforces the case for going
checkpoint-first (if the objects simply aren't emitted via delta ops at all for these classes).

## 1. Naming/Resolution Layer + Full-State Extraction (single combined pass)

The goal of this phase: for every export group (archetype/class), produce a table of
`bit_position → property_name → type`, AND capture the complete per-object state each
checkpoint carries — sourced entirely from data already inside the replay file, no exe
reverse-engineering.

**Important change from the original plan:** these were originally sequenced as two steps
(build schema, then decode state). Since checkpoints are full-state snapshots and likely carry
both the object data and the name/field metadata together in the same chunk, parse each
checkpoint **once** and extract both in the same pass:

- (a) every object state block in full (this is the agent's gap #1 — currently completely
  unread; 10 Checkpoint chunks are opened by nothing in the pipeline today)
- (b) any `FNetFieldExport`-style name table sitten alongside them (this is the naming layer)

Only fall back to the delta (`ReplayData`) stream for objects that genuinely never appear in
any checkpoint (spawned/destroyed entirely between two checkpoints) — this should be a small
minority of cases.

Also fold in the **`TyrTestPlayerStateSubsystem_<id>` blocks** as an explicit target in this
pass — the agent's diagnostics found 21 of these (one per player), and the current pipeline
extracts only the username field from each, ignoring the rest of the block. These are likely
cheap, high-value wins: fully decode each block, not just the username.

### 1.1 Locate the Checkpoint's NetFieldExport data

Unreal replicated-object serialization (both classic `NetConnection` replication and Iris) writes a **NetFieldExportGroup** table into checkpoints. Each group corresponds to one class/archetype and contains, for every replicated property in bit order:

- `Handle` — the field index used inline in the bitstream (this is your `bit`/field position)
- `CompatibleChecksum` — a hash used for schema validation across versions
- `Name` — the actual `FString` property name (e.g. `"Kills"`, `"TeamID"`)
- `Type` — often implicit from the property's `NetSerialize` behavior, but sometimes present as a string hint

Your parser already decompresses the `Checkpoint` chunks (10 of them per `phase1_container`). The NetFieldExportGroup table is typically serialized once per checkpoint, near the start, as an array:

```
i32 NumExportGroups
for each group:
    FString PathName          // e.g. "/Game/.../BP_TyrPlayerState_C" — this IS your archetype→class key
    i32 GroupHandle
    i32 NumNetFieldExports
    for each field:
        i32 Handle             // bit/field index
        u32 CompatibleChecksum
        FString Name           // <-- the string you need
        FString Type           // may be empty; not always populated
```

Exact field order/presence varies slightly by engine version (5.6) and whether the project uses classic replication or Iris replication — Iris tends to wrap this in `FNetBlobIrisParams`/`FReplicationStateDescriptor` name tables instead of the classic `NetFieldExportGroup`, so you may find TWO different structures depending on which system a given class uses. Given your `phase6_iris_decode` output already assumes Iris, look for Iris's **class descriptor names** table first — this is usually a flat array of `(NetHandle/ArchetypeHandle, string PathName)` plus, per class, an array of `(StateID, string PropertyName)`.

### 1.2 Practical extraction approach

Rather than guessing the exact struct layout blind, do this:

1. **String-scan the decompressed checkpoint bytes.** Since `"Unimork"` already surfaced as plaintext, run the same scan but collect *every* printable ASCII/UTF-16 run ≥ 3 chars. Filter to ones that look like UE identifiers (`PascalCase`, contains `_C`, matches known prefixes like `BP_`, `Tyr`, `Kills`, `Score`, `Team`, `Health`, `Ammo`).
2. **Record the byte offset of each string hit.** For each hit, walk backward a fixed window (e.g. 64 bytes) and forward a fixed window, looking for a repeating pattern of `(int32, string_length, string_bytes)` — this is the signature of a serialized array element. Once you find that pattern once, you can derive the exact stride and replicate it to walk the whole array mechanically instead of scanning strings repeatedly.
3. **Cluster strings by proximity.** NetFieldExportGroup entries are serialized contiguously — a class's ~10-30 property names will sit in a tight byte range, immediately preceded or followed by the class path name (`BP_TyrPlayerState_C`). This clustering is what lets you assign properties to a class even before you've fully reverse-engineered the header struct.
4. **Cross-validate against `cm_size`.** Once you have a candidate ordered list of N property names for a class, check whether N is consistent with the `cm_size` (changemask size) you already recorded in `class_by_cm`. A cm_size of 64 bits should correspond to roughly 64 (or fewer, if some fields are multi-bit) property entries. Mismatches tell you you've mis-clustered.

### 1.3 Build the archetype→class map fully

You currently have 1 of 135 groups resolved (`archetype_handle 48 → TyrTeamPublicInfo_ClassNetCache`). This mapping is written once per object, near where the object is first introduced into the replay (bunch with `bNetInitial` set, or the Iris "add object" op). Practically:

- Every time your decoder sees a new `handle` for the first time (not yet in your archetype map), capture the surrounding bytes — the object-creation record typically carries the class path string right next to the handle assignment.
- Build this as a running dictionary as you stream through `ReplayData` chunks (not just checkpoints) — new objects can be introduced mid-match, not only at checkpoint time.

### 1.4 Output artifact

Produce a single resolved schema file, e.g. `field_schema.json`:

```json
{
  "BP_TyrPlayerState_C": {
    "cm_size": 64,
    "fields": {
      "0": {"name": "PlayerName", "type": "FString"},
      "5": {"name": "Kills", "type": "uint16"},
      "6": {"name": "Deaths", "type": "uint16"}
    }
  }
}
```

Everything downstream (step 2) just becomes "look up bit N in this table" instead of raw-guessing.

---

## 2. Priority Class Decoding

Once step 1 produces `field_schema.json` entries for these classes, decode in this order — each is scoped narrowly so you get a working win before moving to the next.

### 2.1 `BP_TyrPlayerState_C` / `BP_TyrLobbyPlayerState_C` (cm64)

**Why first:** this is the per-player row your entire output schema hinges on.

**What to look for once fields are named:**
- Identity fields: `PlayerName`/`PlayerNamePrivate` (FString), `UniqueId`/`PlatformId` (should match the `subsystem_ids` you already extracted — `"2147239908"` — giving you a hard join key between the plaintext username table and the bit-level PlayerState object)
- Stat counters: likely `uint8`/`uint16`/`int32` fields near each other in bit order — `Kills`, `Deaths`, `Assists`, `Score`
- Team/role: small enum-backed fields (2-4 bits) — `TeamID`, `ClassType`/`Role`
- Connection meta: `Ping`, `bIsBot`, `bIsSpectator` — usually single-bit flags

**Decoding approach:** for each `handle` seen with class `BP_TyrPlayerState_C`, take the LAST sample per checkpoint interval (stat counters are monotonic/cumulative — you want final-state, not every intermediate delta) rather than trying to replay every change. This gets you an end-of-match scoreboard fastest; you can add time-series later if you want kill-by-kill timelines.

**Gotcha:** `BP_TyrLobbyPlayerState_C` is a *different* class from `BP_TyrPlayerState_C` (separate cm64 entries per your class list) — likely lobby-phase vs in-match phase. Don't merge them; decode both but keep them as separate schemas since their field sets probably differ despite sharing cm_size by coincidence.

### 2.2 `BP_TyrGameState_C` (cm43)

**Why second:** gives you the context to normalize per-player stats (match duration, which round, final outcome) and is a single object per match, so it's cheap to fully decode.

**What to look for:**
- Match clock: `RemainingTime`/`ElapsedTime` (likely float or int32, changes almost every frame — easy to spot because it'll be the highest-frequency dirty bit in this class)
- Match phase/state enum: `MatchState` (small bitfield, changes rarely — maybe 3-5 times total in a match: warmup→live→postmatch)
- Team scores: `TeamScores` might be a fixed-size array (2 entries for 2 teams) rather than a flat scalar — watch for a field whose bit width doesn't match a single value cleanly

**Decoding approach:** since there's only one `BP_TyrGameState_C` object, just log every dirty-bit sample across the whole replay in order — this single object's time series doubles as your match timeline/log, useful for later timestamping player events (e.g. "kill happened when MatchState.ElapsedTime was X").

### 2.3 `TyrAbilitySystemComponent` (cm83)

**Why third:** highest complexity, lowest immediate payoff — GAS (Gameplay Ability System) typically doesn't replicate clean "damage dealt" scalars; it replicates ability activation events, tags, and attribute *changes* (current health/resource values), not derived stats like "total damage."

**What to look for:**
- `GameplayTagCountContainer` or similar tag-replication — bursts of dirty bits correlated with ability use, but tag names themselves are often FGameplayTag (its own separate name table, potentially yet another indirection beyond FNetFieldExport — check if your checkpoint scan surfaces tag-looking strings like `Ability.Cooldown.X` or `State.Stunned`)
- `AttributeSet` replicated floats — current/max health, resource (mana/energy/ammo-charge) — these ARE useful proxies: you can diff consecutive samples to infer "damage taken this frame" even without an explicit damage-dealt field
- Cooldown/activation booleans — useful for "abilities used" counts, less useful for damage stats

**Decoding approach:** treat this as lower priority than 2.1/2.2. If you want damage stats specifically, diffing the `AttributeSet.Health` float on the *target's* PlayerState/Pawn (whichever owns the AttributeSet) between consecutive dirty samples is more reliable than trying to parse ability-activation payloads. Flag this as "phase 2" work — get 2.1 and 2.2 fully working and useful first, since AbilitySystemComponent decoding for a full damage breakdown is a project in itself (GAS replication is notoriously verbose and versioned per-project).

### 2.4 `TyrAmmunitionComponent` / `TyrAmmunitionComponent_ClassNetCache`

**Why fourth:** nice-to-have accuracy/shots-fired stats, small field count (cm_size 16 / cm_size 1), cheap to decode once schema exists but not essential for a first "stats per player" deliverable.

**What to look for:**
- `CurrentAmmo`/`MaxAmmo` (int) — diffable for "shots fired" if you also have a reload event to reset the baseline
- The `_ClassNetCache` variant (cm_size 1) is almost certainly just a dirty-flag/field-resolution marker like `TyrTeamPublicInfo_ClassNetCache` — expect it to carry no direct stat value, just a "go re-check ammo component" signal

**Decoding approach:** defer until 2.1/2.2 are solid. This is a good "phase 2 polish" item — shots-fired/accuracy is a nice bonus stat, not core.

### 2.5 `BP_LobbyPlayerRecord_C` (cm34)

**Why fifth (but do it in parallel with 2.1, low cost):** this is your cleanest identity source — pre-match roster records tend to have minimal churn (mostly written once at lobby time, rarely updated), so it's cheap to fully decode and gives you a verified player list to sanity-check your PlayerState join logic against.

**What to look for:**
- `PlayerName`, `UniqueNetId`/`PlatformId`, maybe `TeamAssignment`, `LoadoutId`/`SelectedClass`
- Check whether this object persists and gets reused for a final scoreboard, or whether it's purely pre-match (Dumper-7 showed this class's *Blueprint scratch* properties, not real ones — remember none of that old Dumper data applies here; you're resolving fresh from the checkpoint now)

**Decoding approach:** since object count is small (one per player, ~small lobby size) and dirty-bit frequency is low, just fully decode every field for every object — no need to prioritize sub-fields here like you would for GameState's high-frequency clock field.

---

## Suggested execution order (revised)

1. **Diagnose the zero-decode classes** (Section 0) — determine exactly why
   `BP_TyrGameState_C`, `WorldSettings`, and `BP_TyrPlayerState_C` emit zero objects from the
   current delta-stream walker. This determines whether checkpoint-first is a nice-to-have or
   mandatory for these classes.
2. **Build the single-pass checkpoint decoder** (Section 1) — full object state extraction +
   `FNetFieldExport`/name-table extraction together, in one parse of the 10 Checkpoint chunks.
   This is the highest-value unblocked step and matches the agent's recommended next action.
3. Decode `TyrTestPlayerStateSubsystem_<id>` (21 instances, currently only username extracted)
   and `BP_LobbyPlayerRecord_C` (2.5) — cheapest, validates the schema and gives a verified
   player roster to check joins against.
4. Decode `BP_TyrPlayerState_C` / `BP_TyrLobbyPlayerState_C` (2.1) — core per-player stats.
5. Decode `BP_TyrGameState_C` (2.2) — match context/timeline.
6. **Diff consecutive checkpoints** for time-series stats (e.g. kills over time) rather than
   fixing the delta-stream (`ReplayData`) walker — checkpoint-to-checkpoint diffing gets correct
   end-state results with far less decoder complexity. Only revisit the delta walker later if
   sub-checkpoint-interval time resolution is needed (e.g. exact kill timestamps between
   checkpoints).
7. Defer `TyrAbilitySystemComponent` (2.3), `TyrAmmunitionComponent` (2.4), and any remaining
   delta-stream walker repairs to a second pass, once the checkpoint-based core pipeline is
   validated end-to-end on real match data.
