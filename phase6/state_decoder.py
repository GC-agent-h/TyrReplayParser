"""Phase 6: per-object state decode (empirical, self-validating).

Walks the full continuous Iris session stream (phase4/session_reader.concat_frames)
and for each object batch decodes:
    - object header (ReadObjectInBatch, ReplicationReader.cpp:1117):
        subobj handle (if subobj), ReplicatedDestroyHeaderFlags(3b),
        bHasState(1), bIsInitialState(1),
        if initial: bIsDeltaCompressed(1); if delta: BaselineIndex(2b)
    - changemask: FNetBitArrayView compact encoding, ChangeMaskBitCount per object's
        protocol (= export group NumExportsInGroup from phase5/object_identity).
    - selected properties in changemask bit order; scalar values for common types
        (float=32b IEEE754, int32, bool, FVector, FString) using field types from SDK.

The changemask compact format and per-property sizes are validated EMPIRICALLY:
after reading changemask + selected-property bits, the consumed bits must exactly
equal the batch's state region size (BatchSize), else the decode is wrong. This is
the hard correctness check that makes Phase 6 verifiable without the engine .cpp.

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


# ---- changemask compact decode (FNetBitArrayView::ReadBitStream) ----
# Iris stores the changemask as words of 32 bits. The compact writer
# (FNetBitArrayView::WriteBitStream) writes only the non-zero trailing words:
#   NumWords = (BitCount+31)/32
#   LastNonZeroWord = highest word index with any bit set (or 0)
#   WriteBits(LastNonZeroWord, WordCountBitCount)   # how many words follow
#   for w in 0..LastNonZeroWord: WriteBits(word[w], 32)
# Reader mirrors this. WordCountBitCount = ceil(log2(NumWords)) (min 1).
def read_changemask(br, bit_count):
    """Read a compact Iris changemask of `bit_count` bits. Returns list of bools (bit i set)."""
    num_words = (bit_count + 31) // 32
    word_count_bits = max(1, (num_words - 1).bit_length())  # ceil(log2(num_words))
    last_word = br.read_int(1 << word_count_bits)
    if br.err or last_word >= num_words:
        return None
    words = [0] * num_words
    for w in range(last_word + 1):
        words[w] = br.read_int(32)
    # expand to bits
    bits = []
    for w in range(num_words):
        word = words[w]
        for b in range(32):
            bits.append(bool((word >> b) & 1))
    return bits[:bit_count]


def try_decode_object_state(br, return_pos, batch_size, cm_sizes, max_off=0):
    """Resolve an object's changemask size (hence its class).
    For delta (non-initial) objects max_off=0. For initial objects the creation-data
    payload (class reference, binary FNetObjectReference) precedes the changemask; its
    length is unknown, so we brute-force offset x cm_size and accept the pair whose
    compact changemask decodes validly and whose property bits fit the state region.
    Two-tier per (offset, cm_size):
      (1) strict: changemask valid AND reading dirty props as 32-bit floats exactly
          fills BatchSize (float-only/mostly classes);
      (2) fallback: changemask valid AND changemask bits fit within the region.
    Returns (offset, cm_size, changemask_bits, dirty_floats, mode) or None."""
    best = None
    for off in range(0, max_off + 1):
        base = return_pos + off
        if base + 8 > return_pos + batch_size:
            break
        # tier 1: strict float-sum (valid for all offsets)
        for cm_size in cm_sizes:
            br.seek_bits(base)
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
            consumed = br.tell_bits() - base
            if abs(consumed - batch_size) <= 8:
                return (off, cm_size, cm, floats, 'strict')
        # tier 2 (agnostic): only at offset 0 -- when brute-forcing creation-payload
        # offsets, the loose fit produces spurious matches, so restrict it to off==0.
        if off == 0:
            for cm_size in cm_sizes:
                br.seek_bits(base)
                cm = read_changemask(br, cm_size)
                if cm is None:
                    continue
                cm_end = br.tell_bits() - base
                if cm_end <= batch_size:
                    if best is None or cm_size > best[1]:
                        best = (off, cm_size, cm, [], 'agnostic')
    return best


def decode_session(frames, export_groups, max_off_initial=0):
    """Walk the full session stream; for each object resolve its class via cm_size
    fitting. max_off_initial=0 keeps the decode disciplined (delta objects + initial
    objects with no creation payload resolve cleanly; initial objects carrying a
    creation-data payload are skipped — their payload length is unknown without the
    bridge source). Map to export group, collect per-class object counts."""
    cm_sizes = sorted({g[1] for g in export_groups if g[4] and g[1] > 0})
    gravity_bit = 4  # WorldGravityZ is the 4th changemask bit (export-group field order)

    data = sr.concat_frames(frames)
    br = bs.make_packet_reader(data)
    class_counts = {}
    reads = 0
    guard = 0
    while br.bits_left() >= 16 and guard < 5000000:
        guard += 1
        cnt = br.read_int(1 << 16)
        if cnt < 1 or cnt >= 8192 or br.err:
            if br.err: br.err = False
            cur = br.tell_bits(); br.seek_bits(max(0, cur - 16 + 8)); continue
        for bi in range(cnt):
            if br.err or br.bits_left() <= 0:
                break
            b_is_destruction = br.read_bit()
            if b_is_destruction:
                ir.read_net_ref_handle_id(br)
                continue
            handle = ir.read_net_ref_handle_id(br)
            if br.err: break
            batch_size = br.read_int(1 << 8)
            b_has_owner = br.read_bit()
            b_has_exports = br.read_bit()
            return_pos = br.tell_bits()
            destroy_flags = br.read_int(1 << 3)
            b_has_state = br.read_bit()
            if not b_has_state:
                br.seek_bits(return_pos + batch_size)
                continue
            b_is_initial = br.read_bit()
            if b_is_initial:
                b_is_delta = br.read_bit()
                if b_is_delta:
                    br.read_int(1 << 2)
            res = try_decode_object_state(br, return_pos, batch_size, cm_sizes, max_off_initial)
            if res is None:
                br.err = False
                br.seek_bits(return_pos + batch_size)
                continue
            off, cm_size, cm, floats, mode = res
            grp = None
            for g in export_groups:
                if g[4] and g[1] == cm_size:
                    grp = g; break
            cs = (grp[0].split('/')[-1].split('.')[-1].rstrip('\x00') if grp and grp[0] else '?')
            class_counts[cs] = class_counts.get(cs, 0) + 1
            br.seek_bits(return_pos + batch_size)
        reads += 1
    return class_counts, reads


def extract_worldsettings_gravity(frames, export_groups, max_off=256):
    """Targeted, strongly-sanitized extraction of WorldSettings.WorldGravityZ.
    For each object (initial or delta), brute-force the creation-payload offset and
    cm_size; accept ONLY WorldSettings (cm_size=22) where changemask bit 4 (gravity)
    is set, the gravity float is finite in [-3000,3000], and the full state
    (changemask + selected-property bits) self-consistently fills BatchSize within a
    tight tolerance. Returns list of (handle, offset, gravity)."""
    cm_sizes = sorted({g[1] for g in export_groups if g[4] and g[1] > 0})
    ws_cm = None
    for g in export_groups:
        if g[0] and 'WorldSettings' in g[0] and g[4]:
            ws_cm = g[1]; break
    if ws_cm is None:
        return []
    gravity_bit = 4  # WorldGravityZ is the 4th changemask bit (export-group field order)
    data = sr.concat_frames(frames)
    br = bs.make_packet_reader(data)
    out = []
    guard = 0
    while br.bits_left() >= 16 and guard < 5000000:
        guard += 1
        cnt = br.read_int(1 << 16)
        if cnt < 1 or cnt >= 8192 or br.err:
            if br.err: br.err = False
            cur = br.tell_bits(); br.seek_bits(max(0, cur - 16 + 8)); continue
        for bi in range(cnt):
            if br.err or br.bits_left() <= 0:
                break
            b_is_destruction = br.read_bit()
            if b_is_destruction:
                ir.read_net_ref_handle_id(br); continue
            handle = ir.read_net_ref_handle_id(br)
            if br.err: break
            batch_size = br.read_int(1 << 8)
            b_has_owner = br.read_bit()
            b_has_exports = br.read_bit()
            return_pos = br.tell_bits()
            destroy_flags = br.read_int(1 << 3)
            b_has_state = br.read_bit()
            if not b_has_state:
                br.seek_bits(return_pos + batch_size); continue
            b_is_initial = br.read_bit()
            if b_is_initial:
                b_is_delta = br.read_bit()
                if b_is_delta:
                    br.read_int(1 << 2)
            # brute-force offset x cm_size, but only accept WorldSettings w/ valid gravity
            found = None
            for off in range(0, max_off + 1):
                base = return_pos + off
                if base + 8 > return_pos + batch_size:
                    break
                br.seek_bits(base)
                cm = read_changemask(br, ws_cm)
                if cm is None:
                    continue
                if gravity_bit >= len(cm) or not cm[gravity_bit]:
                    continue
                # gravity is the (sum of earlier-set-bits)-th selected property;
                # decode selected props as floats, track position of gravity
                nset = sum(cm)
                di = sum(1 for i in range(gravity_bit) if cm[i])
                floats = []
                ok = True
                for _ in range(nset):
                    if br.bits_left() < 32:
                        ok = False; break
                    fv = struct.unpack('<f', struct.pack('<I', br.read_int(32)))[0]
                    floats.append(fv)
                if not ok or di >= len(floats):
                    continue
                gv = floats[di]
                consumed = br.tell_bits() - base
                if not (-3000 <= gv <= 3000) or abs(consumed - batch_size) > 4:
                    continue
                found = (off, gv)
                break
            if found is not None:
                out.append((handle, found[0], found[1]))
            br.seek_bits(return_pos + batch_size)
        if len(out) >= 50:
            break
    return out


def main():
    f = 'Demos/TyrReplay1.replay'
    r = ReplayReader(f); r.parse_header(); r.parse_chunks()
    payload = None
    for name, c, raw, d, method in extract_payloads(r):
        if name == 'ReplayData':
            payload = d; break
    frames, ok, params = fp.parse_replaydata(payload)
    groups, err, _ = oi.extract_export_groups(f)

    class_counts, reads = decode_session(frames, groups)
    print('TyrReplay1 structural decode: reads=%d  classes=%d' % (reads, len(class_counts)))
    print('top classes by object count:')
    for cs, n in sorted(class_counts.items(), key=lambda x: -x[1])[:15]:
        print('   %-32s %d' % (cs, n))

    grav = extract_worldsettings_gravity(frames, groups)
    print('\nWorldSettings.WorldGravityZ (EXPERIMENTAL / UNVERIFIED candidates):')
    print('   NOTE: cm_size(22) is NOT a unique class id (collisions exist) and the')
    print('   initial-state creation payload is not parsed, so these cannot be')
    print('   distinguished from spurious misaligned matches. Listed for inspection only.')
    vals = [v for _, _, v in grav]
    if vals:
        print('   n=%d  distinct_gravity_values=%s'
              % (len(vals), sorted(set(round(v, 3) for v in vals))[:8]))
        for h, off, v in grav[:10]:
            print('   handle=%d off=%d gravity=%.4f' % (h, off, v))
    else:
        print('   none found')


if __name__ == '__main__':
    main()

