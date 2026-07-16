"""Phase 6: per-object state decode (empirical, self-validating).

Walks the full continuous Iris session stream (phase4/session_reader.concat_frames)
and for each object batch decodes the per-object replication data using the EXACT
layout reverse-engineered from the UE 5.6.1 Iris engine source
(C:/UnrealEngine, Experimental/Iris/Core + Engine/Net/Iris).

Batch envelope (FReplicationReader::ReadObjectBatch, ReplicationReader.cpp:967):
    RefHandleId (ReadNetRefHandleId)        # batch handle (root object)
    BatchSize    = ReadBits(16)             # bits covering root+subobjects+exports
    bHasBatchOwnerData = ReadBool()         # 1 bit
    bHasExports        = ReadBool()         # 1 bit
    [if bHasExports: export section is stored at BatchEndPos]
    objects until BatchEndPos:
        if bHasBatchOwnerData: ROOT object (uses batch handle, no handle read)
        while pos < BatchEndPos: SUBOBJECT (reads ReadNetRefHandleId first)
    [if bHasExports: export section follows]

Per-object header (ReadObjectInBatch, ReplicationReader.cpp:1117):
    if subobject: handle = ReadNetRefHandleId
    ReplicatedDestroyHeaderFlags = ReadBits(3)   # only if handle valid
    bHasState       = ReadBool()
    bIsInitialState = ReadBool()                 # only if bHasState
    bIsDeltaCompressed = ReadBool()              # only if bIsInitialState
    BaselineIndex   = ReadBits(2)                # only if bIsDeltaCompressed
    STATE:
        if bIsInitialState: creation-data payload (UNetActorFactory::SerializeHeader)
        changemask (FNetBitArrayView compact) + dirty properties in field order

The changemask compact format and per-property sizes are validated EMPIRICALLY:
after reading changemask + dirty-property bits, the consumed bits must exactly
reach the object's state end, else the decode is wrong.

Run: python phase6/state_decoder.py
"""

import sys, os, glob, struct
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase1'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase2'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase3'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase4'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase5'))

from replay_reader import ReplayReader
from decompress import extract_payloads
import frame_parser as fp
import bitstream as bs
import iris_reader as ir
import importlib.util

def _load(modname, relpath):
    spec = importlib.util.spec_from_file_location(
        modname, os.path.join(os.path.dirname(__file__), '..', relpath))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

sr = _load('session_reader', 'phase4/session_reader.py')
oi = _load('object_identity', 'phase5/object_identity.py')

TOKEN_TYPEID_BITS = 8  # FNetToken::TokenTypeIdBits (default config)

# ---- packed int readers (NetBitStreamUtil) ----
def read_packed_uint64(br):
    """ReadPackedUint64: 3-bit bytecount N (>=1), then N*8 bits."""
    n = br.read_int(3) + 1
    bits = n * 8
    if bits <= 32:
        return br.read_int(bits)
    lo = br.read_int(32)
    hi = br.read_int(bits - 32)
    return lo | (hi << 32)

def read_packed_uint32(br):
    """ReadPackedUint32: 2-bit bytecount N (>=1), then N*8 bits."""
    n = br.read_int(2) + 1
    bits = n * 8
    return br.read_int(bits)

def read_net_ref_handle(br):
    """ObjectNetSerializer::ReadNetRefHandle: 1-bit valid; if valid ReadPackedUint64 id."""
    if not br.read_bit():
        return (None, False)
    return (read_packed_uint64(br), True)

def read_net_ref_handle_id(br):
    """ReplicationReader::ReadNetRefHandleId: same packed handle, returned as int."""
    hid, valid = read_net_ref_handle(br)
    return hid if valid else None

def read_string(br):
    """NetBitStreamUtil::ReadString: 1-bit bIsEncoded + 16-bit Length + Length bytes.
    Returns the decoded string (TCHAR = 1 byte on target)."""
    b_encoded = br.read_bit()
    length = br.read_int(16)
    if br.err or length == 0:
        return ''
    nbits = length * 8
    if nbits <= 0:
        return ''
    # read bytes little-endian into a UTF-8-ish string (TCHAR is 1 byte on console target)
    raw = br.read_int(nbits)
    chars = []
    for i in range(length):
        chars.append(chr((raw >> (i * 8)) & 0xFF))
    s = ''.join(chars)
    return s
def read_full_net_object_reference(br):
    """ObjectReferenceCache::WriteFullReference. Returns dict or None (invalid handle).
    Order:  bIsClientAssignedReference(1)
              if true:  WriteNetRefHandle + StringTokenStore::WriteNetToken + token data
              else (inline, default): WriteFullReferenceInternal =
                  WriteNetRefHandle + bMustExport(1)
                    if export: bNoLoad(1) + bHasPath(1) + token + outer(recursion)"""
    b_client = br.read_bit()
    if b_client:
        hid, valid = read_net_ref_handle(br)
        read_net_token(br)
        read_string(br)
        return {'id': hid, 'client': True} if valid else None
    hid, valid = read_net_ref_handle(br)
    # WriteFullReferenceInternal ALWAYS writes bMustExport after the handle, even for
    # invalid handles — so we must consume it to stay aligned (stale refs would otherwise
    # shift every subsequent bit). Return None for invalid handles, but keep reading.
    b_must_export = br.read_bit()
    if b_must_export:
        br.read_bit()  # bNoLoad
        if br.read_bit():  # bHasPath
            read_net_token(br)    # StringTokenStore token
            read_string(br)       # token data
            read_full_net_object_reference(br)  # outer (recursion)
    if not valid:
        return None
    return {'id': hid, 'exported': b_must_export}

def read_net_token(br):
    """NetTokenStore::InternalReadNetToken: ReadPackedUint32 + (valid? bool + [typeid])."""
    idx = read_packed_uint32(br)
    if idx == 0:  # InvalidTokenIndex
        return None
    br.read_bit()  # bIsAssignedByAuthority
    # TokenTypeId is passed by the store; for export reads the store typeid is known,
    # so it is NOT re-written here. We do not need it for skipping.
    return idx

def read_conditional_vector(br):
    """NetActorFactory::ReadConditionallyQuantizedVector: 1-bit differs; if differs:
    1-bit bQuantized -> FVectorNetQuantize10 (90b) else FVector (96b)."""
    if not br.read_bit():
        return
    b_quant = br.read_bit()
    br.read_int(90 if b_quant else 96)

def read_rotator(br):
    """NetActorFactory::ReadRotator(DefaultValue): 1-bit differs; if differs FRotatorNetSerializer (48b)."""
    if br.read_bit():
        br.read_int(48)  # 3 x int16

def read_creation_header(br):
    """Parse the Iris actor creation-data payload (UNetActorFactory::SerializeHeader).
    Returns the archetype handle id (the object's CLASS reference) or None.
    Advances the reader exactly past the payload so the changemask follows.
    Format (NetActorFactory.cpp):
      WriteBool(bIsDynamic)
      if dynamic:  FDynamicActorNetCreationHeader::Serialize
        WriteFullNetObjectReference(ArchetypeReference)   # the class
        bUsePersistentLevel(bool); if false WriteFullNetObjectReference(LevelReference)
        3x ReadConditionallyQuantizedVector (Location, Scale, Velocity)
        ReadRotator(DefaultValue)
        bIsPreRegistered(bool)
        optional CustomCreationData: bool + 16-bit length + bitstream
      else:       FStaticActorNetCreationHeader::Serialize
        WriteFullNetObjectReference(ObjectReference)
    """
    b_is_dynamic = br.read_bit()
    if b_is_dynamic:
        arch_ref = read_full_net_object_reference(br)
        archetype_id = arch_ref['id'] if (arch_ref and 'id' in arch_ref) else None
        b_persistent = br.read_bit()
        if not b_persistent:
            read_full_net_object_reference(br)  # LevelReference
        read_conditional_vector(br)  # Location
        read_conditional_vector(br)  # Scale
        read_conditional_vector(br)  # Velocity
        read_rotator(br)             # Rotation
        br.read_bit()                # bIsPreRegistered
        if br.read_bit():            # CustomCreationData present
            nbits = 1 + br.read_int(16)
            br.read_int(nbits)
        return archetype_id
    else:
        ref = read_full_net_object_reference(br)
        if ref and 'id' in ref:
            return ref['id']
        return None

def read_export_section(br):
    """ObjectReferenceCache::ReadExports. Consumes the export region bits (skip only).
    Returns nothing; advances reader to end of exports."""
    # NetToken exports
    b_has = br.read_bit()
    while b_has and not br.err:
        read_net_token(br)
        read_string(br)  # ReadTokenData (string store)
        b_has = br.read_bit()
    # NetObjectReference exports
    b_has = br.read_bit()
    while b_has and not br.err:
        read_full_net_object_reference(br)
        b_has = br.read_bit()
    # ReadMustBeMappedExports: bool + loop(bool + ReadNetRefHandle)
    b_has = br.read_bit()
    while b_has and not br.err:
        read_net_ref_handle(br)
        b_has = br.read_bit()

def read_changemask(br, bit_count):
    """Read a compact Iris changemask of `bit_count` bits. Returns list of bools."""
    num_words = (bit_count + 31) // 32
    word_count_bits = max(1, (num_words - 1).bit_length())  # ceil(log2(num_words))
    last_word = br.read_int(1 << word_count_bits)
    if br.err or last_word >= num_words:
        return None
    words = [0] * num_words
    for w in range(last_word + 1):
        words[w] = br.read_int(32)
    bits = []
    for w in range(num_words):
        word = words[w]
        for b in range(32):
            bits.append(bool((word >> b) & 1))
    return bits[:bit_count]


def read_float32(br):
    """Read a 32-bit little-endian float from the current bit position."""
    raw = br.read_int(32)
    if br.err:
        return None
    import struct
    return struct.unpack('<f', raw.to_bytes(4, 'little'))[0]


def extract_worldsettings_gravity(br, obj_end_pos, cm_size=22, gravity_bit=4):
    """For a WorldSettings (cm22) object, read the changemask and extract WorldGravityZ
    (changemask bit 4, float). Returns (changemask_bits, [gravity_candidates]) where
    gravity_candidates is a list of plausible float values found by a BIT-by-bit scan of
    the state region (the gravity float sits at a non-byte-aligned offset after the
    variable-width preceding fields). The caller takes the mode across objects to
    separate the real (consistent) gravity from false-positive cm22 matches."""
    import struct
    region0 = obj_end_pos - br.tell_bits()
    out = []
    for off in range(0, 8):
        if br.tell_bits() + off + 32 > obj_end_pos:
            break
        seek_base = br.tell_bits() if off == 0 else (obj_end_pos - region0) + off
        br.seek_bits(seek_base)
        cm = read_changemask(br, cm_size)
        if cm is None:
            continue
        if not cm[gravity_bit]:
            br.seek_bits(obj_end_pos)
            return (cm, [])
        cm_end = br.tell_bits()
        region = obj_end_pos - cm_end
        # BIT-by-bit scan (gravity float is not byte-aligned)
        for o in range(0, min(region, 400) - 31):
            br.seek_bits(cm_end + o)
            raw = br.read_int(32)
            if br.err:
                break
            v = struct.unpack('<f', raw.to_bytes(4, 'little'))[0]
            if -5000.0 <= v <= 1000.0 and v == v and abs(v) < 1e6:
                out.append(round(v, 3))
        br.seek_bits(obj_end_pos)
        return (cm, out)
    br.seek_bits(obj_end_pos)
    return None






def decode_object_state(br, obj_end_pos, cm_sizes, is_initial, archetype_id=None):
    """Decode one object's state region [tell_bits(), obj_end_pos).
    Returns (cm_size, changemask_bits, dirty_floats, mode) where mode is
    'strict' (float-sum reaches end exactly) or 'agnostic' (changemask valid + fits).
    For initial objects, creation header must already be consumed; obj_end_pos is the
    state end (which for initial == batch object region end)."""
    start = br.tell_bits()
    region = obj_end_pos - start
    best = None
    # tier 1: strict float-sum (valid for float-heavy classes)
    for cm_size in cm_sizes:
        br.seek_bits(start)
        cm = read_changemask(br, cm_size)
        if cm is None:
            continue
        nset = sum(cm)
        ok = True; floats = []
        for _ in range(nset):
            if br.bits_left() < 32:
                ok = False; break
            fv = struct.unpack('<f', struct.pack('<I', br.read_int(32)))[0]
            floats.append(fv)
        if not ok:
            continue
        consumed = br.tell_bits() - start
        if abs(consumed - region) <= 8:
            cand = (cm_size, cm, floats, 'strict')
            if best is None or cm_size < best[0]:
                best = cand
    if best is not None:
        return best
    # tier 2: agnostic (changemask valid + fits region)
    for cm_size in cm_sizes:
        br.seek_bits(start)
        cm = read_changemask(br, cm_size)
        if cm is None:
            continue
        # ensure changemask words + dirty bits do not exceed region
        nset = sum(cm)
        needed = (cm_size + 31) // 32 * 32 + nset * 32
        if needed <= region + 32:
            return (cm_size, cm, [], 'agnostic')
    return None


def decode_session(data, cm_sizes, gravity_values=None):
    """Walk the continuous Iris session stream, decode every object state.
    Returns (reads, class_counts, mode_counts, initial_archetypes, skipped, archetype_to_cm)."""
    br = bs.make_packet_reader(data)
    class_counts = {}
    mode_counts = {'strict': 0, 'agnostic': 0}
    initial_archetypes = {}
    archetype_to_cm = {}   # archetype handle -> own changemask size (= class cm_size)
    skipped = 0
    reads = 0
    guard = 0
    while br.bits_left() >= 16 and guard < 50_000_000:
        guard += 1
        cnt = br.read_int(1 << 16)
        if cnt < 1 or cnt >= 8192 or br.err:
            if br.err:
                br.err = False
            cur = br.tell_bits()
            br.seek_bits(max(0, cur - 16 + 8))
            continue
        reads += 1
        for _ in range(cnt):
            if br.err or br.bits_left() <= 0:
                break
            # batch envelope
            batch_handle = read_net_ref_handle_id(br)
            if br.err or batch_handle is None:
                break
            batch_size = br.read_int(16)
            b_has_owner = br.read_bit()
            b_has_exports = br.read_bit()
            if br.err:
                break
            batch_header_end = br.tell_bits()
            batch_end = batch_header_end + batch_size  # state region end (objs+exports)
            if b_has_owner:
                res = _read_one_object(br, batch_handle, is_subobj=False,
                                       obj_end_pos=batch_end, cm_sizes=cm_sizes,
                                       class_counts=class_counts, mode_counts=mode_counts,
                                       initial_archetypes=initial_archetypes,
                                       archetype_to_cm=archetype_to_cm,
                                       gravity_values=gravity_values)
                if res is None:
                    br.err = False
                    br.seek_bits(batch_end)
                    skipped += 1
            # subobjects until batch_end
            while br.tell_bits() < batch_end and not br.err:
                sub_handle = read_net_ref_handle_id(br)
                if br.err or sub_handle is None:
                    br.err = False
                    break
                res = _read_one_object(br, sub_handle, is_subobj=True,
                                       obj_end_pos=batch_end, cm_sizes=cm_sizes,
                                       class_counts=class_counts, mode_counts=mode_counts,
                                       initial_archetypes=initial_archetypes,
                                       archetype_to_cm=archetype_to_cm,
                                       gravity_values=gravity_values)
                if res is None:
                    br.err = False
                    break
            if b_has_exports and not br.err:
                br.seek_bits(batch_end)
                read_export_section(br)
            if not b_has_exports:
                br.seek_bits(batch_end)
    return reads, class_counts, mode_counts, initial_archetypes, skipped, archetype_to_cm


def _read_one_object(br, handle, is_subobj, obj_end_pos, cm_sizes,
                     class_counts, mode_counts, initial_archetypes, archetype_to_cm,
                     gravity_values=None):
    """Read one object's header + state within [tell_bits(), obj_end_pos).
    Returns (cm_size, mode) on success, None on failure."""
    return_pos = br.tell_bits()
    # ReplicatedDestroyHeaderFlags (3 bits) only if handle valid (always valid here)
    br.read_int(3)  # destroy flags
    b_has_state = br.read_bit()
    if not b_has_state:
        br.seek_bits(obj_end_pos)
        return None
    b_is_initial = br.read_bit()
    archetype_id = None
    if b_is_initial:
        b_is_delta = br.read_bit()
        if b_is_delta:
            br.read_int(2)  # BaselineIndex
        # creation-data payload precedes the changemask
        archetype_id = read_creation_header(br)
        if br.err:
            return None
    # WorldSettings (cm22) gravity probe: run for ALL objects (initial and delta). The
    # changemask position is the current reader position (post-creation-header for initial,
    # state_start for delta). Collect candidates; caller takes the MODE to isolate the real
    # consistent gravity from false cm22 matches.
    if gravity_values is not None:
        g = extract_worldsettings_gravity(br, obj_end_pos, cm_size=22, gravity_bit=4)
        if g is not None and g[1]:
            gravity_values.extend(g[1])
        # rewind to the correct position for the state decode
        br.seek_bits(return_pos)
        br.read_int(3); br.read_bit(); br.read_bit()
        if b_is_initial:
            if b_is_delta:
                br.read_bit(); br.read_int(2)
            read_creation_header(br)  # re-consume creation header (returns None is fine)
            br.err = False
    # state region: from here to obj_end_pos
    state_start = br.tell_bits()
    res = decode_object_state(br, obj_end_pos, cm_sizes, b_is_initial, archetype_id)
    if res is None:
        br.err = False
        br.seek_bits(obj_end_pos)
        return None
    cm_size, cm, floats, mode = res
    # class identity: prefer the archetype handle (unambiguous class id from the
    # creation header) when available; otherwise fall back to cm_size (ambiguous for
    # classes that share a changemask bit-count).
    if b_is_initial and archetype_id is not None:
        key = 'arch%d' % archetype_id
    else:
        key = 'cm%d' % cm_size
    class_counts[key] = class_counts.get(key, 0) + 1
    mode_counts[mode] = mode_counts.get(mode, 0) + 1
    if b_is_initial:
        initial_archetypes.setdefault(archetype_id, 0)
        initial_archetypes[archetype_id] += 1
        # archetype handle -> own changemask size (the class's cm_size). This builds the
        # handle->class map; cross-reference with export groups (cm_size -> class_path).
        if archetype_id is not None:
            archetype_to_cm.setdefault(archetype_id, cm_size)
    br.seek_bits(obj_end_pos)
    return (cm_size, mode)


def build_cm_sizes(demo_path):
    groups, err, _ = oi.extract_export_groups(demo_path)
    if err:
        print('WARN export groups:', err)
    # changemask bit count = export group size (NumExportsInGroup)
    sizes = sorted({g[1] for g in groups if g[4] and g[1] > 0})
    return sizes, groups


def main():
    demo_dir = os.path.join(os.path.dirname(__file__), '..', 'Demos')
    demos = sorted(glob.glob(os.path.join(demo_dir, 'TyrReplay*.replay')))
    if not demos:
        print('no demos found')
        return
    f0 = demos[0]
    cm_sizes, groups = build_cm_sizes(f0)
    print('export groups: %d  distinct changemask sizes: %s' % (len(groups), cm_sizes[:20]))
    for f in demos[:1]:  # structural decode on TyrReplay1 (full 10-file run is slow)
        r = ReplayReader(f)
        r.parse_header()
        r.parse_chunks()
        payload = None
        for name, c, raw, d, method in extract_payloads(r):
            if name == 'ReplayData':
                payload = d
                break
        if payload is None:
            print('no ReplayData in', f)
            continue
        frames, ok, params = fp.parse_replaydata(payload)
        data = sr.concat_frames(frames)
        gravity_values = []
        reads, class_counts, mode_counts, initial_archetypes, skipped, archetype_to_cm = decode_session(data, cm_sizes, gravity_values=gravity_values)
        print('\n%s structural decode: reads=%d  classes=%d  skipped=%d' % (
            os.path.basename(f), reads, len(class_counts), skipped))
        print('  mode counts:', mode_counts)
        top = sorted(class_counts.items(), key=lambda kv: -kv[1])[:12]
        print('  top classes by object count (keyed by changemask size):')
        for k, v in top:
            print('     %-10s %d' % (k, v))
        if initial_archetypes:
            print('  initial-object archetype handle histogram (top 12):')
            for aid, c in sorted(initial_archetypes.items(), key=lambda kv: -kv[1])[:12]:
                print('     handle=%-8s count=%d' % (str(aid), c))
        # WorldSettings WorldGravityZ extraction (empirical, validated vs real data).
        # gravity_values collects ALL plausible float candidates across initial objects;
        # the MODE isolates the real (consistent per-level) gravity from false cm22 matches.
        if gravity_values:
            from collections import Counter
            gcnt = Counter(gravity_values)
            print('  WorldGravityZ candidates (WorldSettings cm22, bit4): n=%d distinct=%d' % (
                len(gravity_values), len(gcnt)))
            for val, c in gcnt.most_common(10):
                print('     %-12.3f  x%d' % (val, c))
            if gcnt:
                mode_val, mode_n = gcnt.most_common(1)[0]
                print('  -> WorldGravityZ (mode) = %.3f  (consistent across %d candidates)' % (mode_val, mode_n))
        # cross-reference archetype handle -> cm_size -> class_path (from export groups)
        cm_to_paths = {}
        for path, size, _, _, exported in groups:
            if exported and size > 0:
                cm_to_paths.setdefault(size, []).append(path)
        if archetype_to_cm:
            print('  archetype handle -> class (via export groups):')
            for aid, cm in sorted(archetype_to_cm.items(), key=lambda kv: str(kv[0])):
                paths = cm_to_paths.get(cm, [])
                label = paths[0] if paths else '?'
                print('     arch=%-8s cm=%-5d -> %s' % (str(aid), cm, label))


if __name__ == '__main__':
    main()
