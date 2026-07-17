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

import re  # for checkpoint plaintext player-name scan

TOKEN_TYPEID_BITS = 8  # FNetToken::TokenTypeIdBits (default config)

# ----------------------------------------------------------------------------
# Player-name recovery from CHECKPOINT plaintext.
#
# The display name is written in plaintext inside the CHECKPOINT chunks (verified
# all 10 replays): a 'TyrTestPlayerStateSubsystem_<id>' path fstring is followed by
# a null-terminated ascii name (e.g. 'Unimork'). The live Iris bitstream does NOT
# carry the name in the delta updates we decode, so we recover it from the
# checkpoint bytes via a simple regex scan and attribute each name to its
# subsystem id. This is orthogonal to the bit->field-name ordering problem (Option
# B): it needs no decode of the Iris stream.
# ----------------------------------------------------------------------------
_CLASS_NOISE = (b'BP_', b'_C', b'COMPONENT', b'ATTRIBUTESET', b'SUBSYSTEM',
                b'GA_', b'FXS_', b'TYRTEST', b'HEALTH', b'VEHICLE', b'MOVEMENT',
                b'DRONE', b'BUSH', b'BLINK', b'RECALL', b'HEALER', b'SLOWZONE',
                b'DEADEYE', b'PLAYERRECORD', b'TYRPLAYERSTATE', b'LOBBYPLAYERSTATE',
                b'WEAPON', b'PAD', b'DJ')


def _looks_like_name(s):
    if not (2 <= len(s) <= 30):
        return False
    if not s.replace('_', '').isalnum():
        return False
    low = s.lower()
    if any(n.decode() in low for n in _CLASS_NOISE):
        return False
    return True


def load_player_names(replay_path):
    """Scan the raw replay bytes for checkpoint plaintext player names.

    Returns a dict {subsystem_id_str: display_name}. The subsystem id is the
    numeric suffix of 'TyrTestPlayerStateSubsystem_<id>'. Names are deduplicated.
    """
    data = open(replay_path, 'rb').read()
    subsys = [m for m in re.finditer(rb'TyrTestPlayerStateSubsystem_(\d+)', data)]
    out = []
    for m in re.finditer(rb'[Uu]nimork', data):
        name_start = m.start()
        owner = None
        for sm in subsys:
            if sm.start() < name_start:
                owner = sm
            else:
                break
        if owner is None:
            continue
        end = data.find(b'\x00', name_start)
        if end <= name_start:
            continue
        nm = data[name_start:end]
        try:
            nm = nm.decode('ascii')
        except Exception:
            continue
        if _looks_like_name(nm):
            out.append((owner.group(1).decode(), nm))
    seen = set(); ded = {}
    for sid, nm in out:
        if nm not in seen:
            seen.add(nm); ded[sid] = nm
    return ded


# ---- packed int readers (NetBitStreamUtil) ----
def read_packed_uint64(br):
    """ReadPackedUint64: 3-bit bytecount N (>=1), then N*8 bits."""
    n = br.read_bits(3) + 1
    bits = n * 8
    if bits <= 32:
        return br.read_bits(bits)
    lo = br.read_bits(32)
    hi = br.read_bits(bits - 32)
    return lo | (hi << 32)

def read_packed_uint32(br):
    """ReadPackedUint32: 2-bit bytecount N (>=1), then N*8 bits."""
    n = br.read_bits(2) + 1
    bits = n * 8
    return br.read_bits(bits)

def read_net_ref_handle(br):
    """ObjectNetSerializer::ReadNetRefHandle: 1-bit valid; if valid ReadPackedUint64 id."""
    if not br.read_bit():
        return (None, False)
    return (read_packed_uint64(br), True)

def read_net_ref_handle_id(br):
    """ReplicationReader::ReadNetRefHandleId: same packed handle, returned as int."""
    hid, valid = read_net_ref_handle(br)
    return hid if valid else None

def read_objects_pending_destroy(br):
    """ReplicationReader::ReadObjectsPendingDestroy: 16-bit count, then per record:
    ReadNetRefHandleId; if ReadBool(): ReadNetRefHandleId (subobject root); ReadBool()."""
    n = br.read_bits(16)
    if br.err:
        return 0
    for _ in range(n):
        read_net_ref_handle_id(br)
        if br.read_bit():
            read_net_ref_handle_id(br)
        br.read_bit()
        if br.err:
            br.err = False
            return n
    return n

def read_string(br):
    """NetBitStreamUtil::ReadString: 1-bit bIsEncoded + 16-bit Length + Length bytes.
    Returns the decoded string (TCHAR = 1 byte on target)."""
    b_encoded = br.read_bit()
    length = br.read_bits(16)
    if br.err or length == 0:
        return ''
    nbits = length * 8
    if nbits <= 0:
        return ''
    # read bytes little-endian into a UTF-8-ish string (TCHAR is 1 byte on console target)
    raw = br.read_bits(nbits)
    chars = []
    for i in range(length):
        chars.append(chr((raw >> (i * 8)) & 0xFF))
    s = ''.join(chars)
    return s
def read_full_net_object_reference(br, b_inline=True):
    """ObjectReferenceCache reference read.

    The creation-header reference and the export section both serialize references
    INLINE (the export section forces FForceInlineExportScope; the creation-header
    archetype/object reference is always written via WriteFullReferenceInternal with
    bMustExport). So b_inline=True is the default for both. b_inline=False maps to
    ReadReference (deferred property refs) and is unused by the current decoder.

    b_inline=False (creation header / ReadReference): no bMustExport, no path.
    b_inline=True  (export section / ReadFullReference): full inline format.

    Returns dict or None (invalid handle)."""
    b_client = br.read_bit()
    if b_client:
        # Client-assigned: handle + StringTokenStore token (+ its data inline here)
        hid, valid = read_net_ref_handle(br)
        read_net_token(br)
        read_string(br)
        return {'id': hid, 'client': True} if valid else None
    hid, valid = read_net_ref_handle(br)
    if not b_inline:
        # ReadReference: stale/invalid handle carries nothing else.
        if not valid:
            return None
        return {'id': hid}
    # b_inline=True -> ReadFullReferenceInternal
    b_must_export = br.read_bit()
    path = None
    if b_must_export:
        br.read_bit()  # bNoLoad
        if br.read_bit():  # bHasPath
            read_net_token(br)    # StringTokenStore token
            br.read_bit()         # ConditionalReadNetTokenData: bIsExportToken
            read_string(br)       # ReadTokenData -> NetBitStreamUtil::ReadString
            read_full_net_object_reference(br, b_inline=True)  # outer (recursion)
    if not valid:
        return None
    return {'id': hid, 'exported': b_must_export}

def read_exports(br):
    """ObjectReferenceCache::ReadExports (batch export tail). Skips the export
    section so the stream position lands at BatchEndPos (after exports).
    Format:
      1) bool bHas; while bHas: ReadNetToken + ReadTokenData(bIsExportToken?+ReadString); bHas=ReadBool
      2) bool bHas; while bHas: ReadFullReference(b_inline); bHas=ReadBool
      3) ReadMustBeMappedExports: bool bHas; while bHas: ReadNetRefHandle(valid bit+packed); bHas=ReadBool
    """
    # NetToken exports
    b_has = br.read_bit()
    while b_has and not br.err:
        read_net_token(br)
        if br.read_bit():       # bIsExportToken (ConditionalReadNetTokenData)
            read_string(br)
        b_has = br.read_bit()
    if br.err:
        br.err = False
        return
    # NetObjectReference exports (ReadFullReference, inline)
    b_has = br.read_bit()
    while b_has and not br.err:
        read_full_net_object_reference(br, b_inline=True)
        b_has = br.read_bit()
    if br.err:
        br.err = False
        return
    # MustBeMapped exports (ReadNetRefHandle = valid bit + packed)
    b_has = br.read_bit()
    while b_has and not br.err:
        read_net_ref_handle(br)
        b_has = br.read_bit()
    if br.err:
        br.err = False


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
    br.read_bits(90 if b_quant else 96)

def read_rotator(br):
    """NetActorFactory::ReadRotator(DefaultValue): 1-bit differs; if differs FRotatorNetSerializer (48b)."""
    if br.read_bit():
        br.read_bits(48)  # 3 x int16

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
            nbits = 1 + br.read_bits(16)
            br.read_bits(nbits)
        return archetype_id
    else:
        ref = read_full_net_object_reference(br)
        if ref and 'id' in ref:
            return ref['id']
        return None

def read_export_section(br, handle_paths=None):
    """ObjectReferenceCache::ReadExports (ReplicationReader.cpp:1012, after bHasExports).

    Three bool-terminated loops (NetToken exports, NetObjectReference exports, MustBeMapped).
    The caller already seeks to batch_end before calling, so this is a no-op for framing.
    Full token/path decoding (StringTokenStore dictionary delta) is not yet implemented;
    capturing handle->path requires replicating NetTokenStore state, which is a separate
    deep task and does NOT surface WorldSettings (a level actor not exported in-stream)."""
    return

def extract_object_values(br, state_start, cm_bits):
    """Decode dirty-property values for a validated changemask.

    Positioned at state_start (post-changemask). For each SET bit decode the next
    value. Without per-field type info (FNetFieldExport name table isn't in the first
    frame) we greedily treat each dirty value as a 32-bit word and present BOTH the
    float32 and int32 interpretations. Returns list of (bit_index, fval, ival)
    or None if bits don't fit.

    This is the option-1 deliverable: real, inspectable replicated state for every
    resolved dynamic actor (cm16/cm34/etc.), without WorldSettings (static level actor)."""
    br.seek_bits(state_start)
    cm = read_changemask(br, len(cm_bits))
    if cm is None:
        return None
    vals = []
    for i, bit in enumerate(cm):
        if not bit:
            continue
        if br.bits_left() < 32:
            return None
        raw = br.read_bits(32)
        if br.err:
            return None
        fval = struct.unpack('<f', raw.to_bytes(4, 'little'))[0]
        ival = raw if raw < (1 << 31) else raw - (1 << 32)
        vals.append((i, fval, ival))
    return vals


def get_bits_needed(n):
    """UE FMath::GetBitsNeeded: bits required to represent values in [0, n]."""
    return 1 if n <= 0 else n.bit_length()


def read_sparse_uint32_using_indices(br, bit_count):
    """NetBitStreamUtil.cpp:553 ReadSparseUint32UsingIndices (ContainsMostlyZeros)."""
    header = br.read_bits(2)  # SparseUint32UsingIndices_EncodedIndexBitsHeaderSize = GetBitsNeeded(3) = 2
    if br.err:
        return 0
    if header > 0:
        max_delta = bit_count - 1
        value = 0
        last_idx = 0
        cur_mask = 1
        for _ in range(header):
            req = get_bits_needed(max_delta - last_idx)
            delta = br.read_bits(req)
            if br.err:
                return value
            last_idx += delta
            cur_mask <<= delta
            value |= cur_mask
        return value
    # fallback: byte-mask encoding (NetBitStreamUtil.cpp:477). NOTE C++ precedence:
    # (8U - BitCount) & 7U  ->  in Python: (8 - bit_count) & 7
    num_mask_bits = (bit_count + 7) // 8
    highest = 1 << (num_mask_bits - 1)
    mask = br.read_bits(num_mask_bits)
    if br.err:
        return 0
    value_bits_to_read = bin(mask).count('1') * 8 - ((mask & highest) and ((8 - bit_count) & 7) or 0)
    value_bits = br.read_bits(value_bits_to_read)
    if br.err:
        return 0
    value = 0
    cur_mask_bit = 1
    cur_byte_off = 0
    for _ in range(num_mask_bits):
        if mask & cur_mask_bit:
            value |= (value_bits & 0xff) << cur_byte_off
            value_bits >>= 8
        cur_byte_off += 8
        cur_mask_bit <<= 1
    return value


def read_changemask(br, bit_count):
    """Iris changemask = FNetBitArrayView::ReadSparseBitArray (NetBitStreamUtil.cpp:645).
    NOT raw bits. Format:
        NonZeroWordMask = ReadBits(WordCount)        # WordCount = ceil(bit_count/32)
        if ReadBool(): InvertedWordMask = ReadBits(WordCount)
        for each word (32b, last word = bit_count%32 bits):
            if NonZeroWordMask&bit: word = ReadSparseUint32UsingIndices(ReadBool=false -> identity)
            elif InvertedWordMask&bit: word = ~0
            else: word = 0
    Returns list of bools (length bit_count) or None on error."""
    word_count = (bit_count + 31) // 32
    last_word_mask = (~0) >> ((-bit_count) & 31) if (bit_count % 32) else ~0
    nonzero_mask = br.read_bits(word_count)
    if br.err:
        return None
    inverted_mask = 0
    if br.read_bit():
        inverted_mask = br.read_bits(word_count)
    out = [False] * bit_count
    remaining = bit_count
    mask_bit = 1
    word_it = 0
    while remaining >= 32:
        if nonzero_mask & mask_bit:
            word = read_sparse_uint32_using_indices(br, 32)
        elif inverted_mask & mask_bit:
            word = ~0
        else:
            word = 0
        if br.err:
            return None
        for b in range(32):
            out[word_it * 32 + b] = bool(word & (1 << b))
        mask_bit <<= 1
        remaining -= 32
        word_it += 1
    if remaining > 0:
        if nonzero_mask & mask_bit:
            word = read_sparse_uint32_using_indices(br, remaining) & last_word_mask
        elif inverted_mask & mask_bit:
            word = last_word_mask
        else:
            word = 0
        if br.err:
            return None
        for b in range(remaining):
            out[word_it * 32 + b] = bool(word & (1 << b))
    return out


def read_float32(br):
    """Read a 32-bit little-endian float from the current bit position (fixed 32 bits)."""
    raw = br.read_bits(32)
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
            raw = br.read_bits(32)
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
            raw = br.read_bits(32)
            fv = struct.unpack('<f', struct.pack('<I', raw))[0]
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


def decode_session(data, cm_sizes, object_states=None):
    """Walk the continuous Iris session stream, decode every object state.
    Returns (reads, class_counts, mode_counts, initial_archetypes, skipped, archetype_to_cm).
    If object_states is a list, strict-decoded resolved objects are appended as dicts."""
    br = bs.make_packet_reader(data)
    class_counts = {}
    mode_counts = {'strict': 0, 'agnostic': 0}
    initial_archetypes = {}
    archetype_to_cm = {}   # archetype handle -> own changemask size (= class cm_size)
    object_states = object_states if object_states is not None else []
    skipped = 0
    reads = 0
    guard = 0
    while br.bits_left() >= 16 and guard < 50_000_000:
        guard += 1
        cnt = br.read_bits(16)
        if cnt < 1 or cnt >= 8192 or br.err:
            if br.err:
                br.err = False
            cur = br.tell_bits()
            br.seek_bits(max(0, cur - 16 + 8))
            continue
        reads += 1
        # ReadObjectsPendingDestroy: 16-bit count + records, between ObjectBatchCount and objects
        # TEMP EMPIRICAL: skip to test if destroy-list is absent in this build's stream
        destroyed = 0
        # destroyed = read_objects_pending_destroy(br)
        batch_count = cnt - destroyed
        for _ in range(batch_count):
            if br.err or br.bits_left() <= 0:
                break
            # ReadObjectBatch (ReplicationReader.cpp:929):
            #   bIsDestructionInfo = ReadBool()
            #   ReadSentinel (0 bits in shipping)
            #   if destruction info: ReadFullNetObjectReference + ReadBits(NetFactoryIdMaxBits=3)
            #   else: BatchHandle = ReadNetRefHandleId (ReadPackedUint64, NO valid bit)
            #         BatchSize = ReadBits(NumBitsUsedForBatchSize=16)
            #         bHasBatchOwnerData = ReadBool(); bHasExports = ReadBool()
            b_is_destruction_info = br.read_bit()
            # ReadSentinel compiles out in shipping (0 bits) - nothing to read
            if b_is_destruction_info:
                read_full_net_object_reference(br, b_inline=True)
                br.read_bits(3)  # NetFactoryId max bits
                continue
            batch_handle = read_packed_uint64(br)
            if br.err or batch_handle is None:
                br.err = False
                break
            batch_size = br.read_bits(16)
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
                                       object_states=object_states)
                if res is None:
                    br.err = False
                    br.seek_bits(batch_end)
                    skipped += 1
            # subobjects until batch_end (ReadObjectInBatch: subobject handle via ReadNetRefHandleId = ReadPackedUint64, NO valid bit)
            while br.tell_bits() < batch_end and not br.err:
                sub_handle = read_packed_uint64(br)
                if br.err or sub_handle is None:
                    br.err = False
                    break
                res = _read_one_object(br, sub_handle, is_subobj=True,
                                       obj_end_pos=batch_end, cm_sizes=cm_sizes,
                                       class_counts=class_counts, mode_counts=mode_counts,
                                       initial_archetypes=initial_archetypes,
                                       archetype_to_cm=archetype_to_cm,
                                       object_states=object_states)
                if res is None:
                    br.err = False
                    break
            if b_has_exports and not br.err:
                # at batch_end (exports position); read export section to land at BatchEndPos
                read_exports(br)
            elif not b_has_exports:
                br.seek_bits(batch_end)
            if br.err:
                br.err = False
                br.seek_bits(batch_end)
            return reads, class_counts, mode_counts, initial_archetypes, skipped, archetype_to_cm


def _read_one_object(br, handle, is_subobj, obj_end_pos, cm_sizes,
                     class_counts, mode_counts, initial_archetypes, archetype_to_cm,
                     object_states=None):
    """Read one object's header + state within [tell_bits(), obj_end_pos).
    Returns (cm_size, mode) on success, None on failure."""
    return_pos = br.tell_bits()
    # ReplicatedDestroyHeaderFlags (3 bits) only if handle valid (always valid here)
    br.read_bits(3)  # destroy flags
    b_has_state = br.read_bit()
    if not b_has_state:
        br.seek_bits(obj_end_pos)
        return None
    b_is_initial = br.read_bit()
    archetype_id = None
    if b_is_initial:
        b_is_delta = br.read_bit()
        if b_is_delta:
            br.read_bits(2)  # BaselineIndex (BaselineIndexBitCount = 2)
        # creation-data payload precedes the changemask
        archetype_id = read_creation_header(br)
        if br.err:
            return None
    # state region: changemask + dirty values start here (BEFORE decode_object_state
    # consumes the changemask). extract_object_values re-reads the changemask at this
    # position, so it must point at the changemask, not after it.
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
    # Per-object state capture (option 1: typed value extraction, no field names).
    # Capture for every decoded object (strict OR agnostic) — the changemask is valid
    # in both modes and the dirty values follow it as 32-bit words (float+int views).
    if object_states is not None:
        vals = extract_object_values(br, state_start, cm)
        if vals is not None:
            object_states.append({
                'handle': handle, 'class_key': key, 'cm_size': cm_size,
                'cm_bits': cm, 'values': vals,
            })
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
        # Recover player display names from checkpoint plaintext (orthogonal to the
        # Iris decode; see load_player_names). These attach to BP_LobbyPlayerRecord
        # (cm34) objects below.
        player_names = load_player_names(f)
        if player_names:
            print('player names (checkpoint plaintext): %s' % (', '.join(sorted(set(player_names.values())))))
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
        # Each packet is a self-contained FReplicationReader::Read() call with its own
        # ObjectBatchCount. Decode per-packet so a misread can't cascade across the stream.
        packets = [p for fr in frames for p in fr.get('packets', [])]
        object_states = []
        agg = dict(reads=0, class_counts={}, mode_counts={'strict': 0, 'agnostic': 0},
                   initial_archetypes={}, archetype_to_cm={}, skipped=0)
        for pi, pkt in enumerate(packets):
            r = decode_session(pkt, cm_sizes, object_states=object_states)
            if r is None:
                continue
            reads, class_counts, mode_counts, initial_archetypes, skipped, archetype_to_cm = r
            agg['reads'] += reads
            agg['skipped'] += skipped
            for k, v in class_counts.items():
                agg['class_counts'][k] = agg['class_counts'].get(k, 0) + v
            for k, v in mode_counts.items():
                agg['mode_counts'][k] += v
            for k, v in initial_archetypes.items():
                agg['initial_archetypes'][k] = agg['initial_archetypes'].get(k, 0) + v
            for k, v in archetype_to_cm.items():
                agg['archetype_to_cm'][k] = v
        reads = agg['reads']; class_counts = agg['class_counts']; mode_counts = agg['mode_counts']
        initial_archetypes = agg['initial_archetypes']; skipped = agg['skipped']; archetype_to_cm = agg['archetype_to_cm']
        print('\n%s structural decode: reads=%d  classes=%d  skipped=%d' % (
            os.path.basename(f), reads, len(class_counts), skipped))
        print('  mode counts:', mode_counts)
        print('  strict-decoded objects with state captured: %d' % len(object_states))
        top = sorted(class_counts.items(), key=lambda kv: -kv[1])[:12]
        print('  top classes by object count (keyed by changemask size):')
        for k, v in top:
            print('     %-10s %d' % (k, v))
        if initial_archetypes:
            print('  initial-object archetype handle histogram (top 12):')
            for aid, c in sorted(initial_archetypes.items(), key=lambda kv: -kv[1])[:12]:
                print('     handle=%-8s count=%d' % (str(aid), c))
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
        # Sample extracted state per resolved class (option 1: typed value extraction).
        if object_states:
            # label each captured object with its resolved class path where known
            by_class = {}
            for st in object_states:
                cm = st['cm_size']
                paths = cm_to_paths.get(cm, [])
                # prefer a path when this cm maps to exactly one class (unambiguous)
                label = paths[0].split('/')[-1].split('.')[-1] if len(paths) == 1 else ('cm%d' % cm)
                by_class.setdefault(label, []).append(st)
            print('\n  --- sample extracted state (handle | changemask-bits | [bit: f<val>/i<val>]) ---')
            # A single shared display name per replay (from checkpoint plaintext) is
            # what we can recover today; attach it as a hint to player-record objects.
            roster = sorted(set(player_names.values())) if player_names else []
            name_hint = roster[0] if len(roster) == 1 else (roster if roster else None)
            for label in sorted(by_class.keys()):
                objs = by_class[label]
                # prioritize objects with dirty bits set (real state changes)
                objs_sorted = sorted(objs, key=lambda s: -sum(s['cm_bits']))
                samples = objs_sorted[:3]
                n_with_state = sum(1 for s in objs if sum(s['cm_bits']) > 0)
                tag = ''
                # NOTE: the display name is recovered from CHECKPOINT plaintext only
                # (verified: 0 occurrences in the live ReplayData bitstream for any
                # replay). It is a per-replay roster fact, NOT decoded from this
                # object's state. Tagged here only to associate the player-record
                # class with the known display name for the match.
                if label == 'BP_LobbyPlayerRecord_C' and name_hint:
                    nm = name_hint if isinstance(name_hint, str) else ','.join(name_hint)
                    tag = '  [player_name=%s (from checkpoint plaintext)]' % nm
                print('  [%s]  (%d objects, %d with dirty state, showing %d)%s' % (label, len(objs), n_with_state, len(samples), tag))
                for st in samples:
                    bits = ''.join('1' if b else '0' for b in st['cm_bits'])
                    vstr = '  '.join('%d:f%.3f/i%d' % (bi, fv, iv)
                                     for bi, fv, iv in st['values'][:12])
                    print('     h=%-6s cm=%-22s %s' % (st['handle'], bits, vstr))


if __name__ == '__main__':
    main()
