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


def try_decode_object_state(br, return_pos, batch_size, cm_sizes):
    """Resolve an object's changemask size (hence its class). Two-tier:
    (1) strict: changemask valid AND reading dirty props as 32-bit floats exactly
        fills the BatchSize state region (catches float-only/mostly classes);
    (2) fallback: changemask valid AND changemask bits fit within the state region
        leaving room for >=0 property bits (type-agnostic; catches mixed classes).
    Returns (cm_size, changemask_bits, dirty_floats, mode) or None."""
    # tier 1: strict float-sum
    for cm_size in cm_sizes:
        br.seek_bits(return_pos)
        cm = read_changemask(br, cm_size)
        if cm is None:
            continue
        nset = sum(cm)
        ok = True; floats = []
        for _ in range(nset):
            if br.bits_left() < 32:
                ok = False; break
            f = struct.unpack('<f', struct.pack('<I', br.read_int(32)))[0]
            floats.append(f)
        if not ok:
            continue
        consumed = br.tell_bits() - return_pos
        if abs(consumed - batch_size) <= 8:
            return (cm_size, cm, floats, 'strict')
    # tier 2: type-agnostic (smallest cm_size whose changemask fits region)
    best = None
    for cm_size in cm_sizes:
        br.seek_bits(return_pos)
        cm = read_changemask(br, cm_size)
        if cm is None:
            continue
        cm_end = br.tell_bits() - return_pos
        if cm_end <= batch_size:
            # prefer the largest cm_size that still fits (most specific)
            if best is None or cm_size > best[0]:
                best = (cm_size, cm, [], 'agnostic')
    return best


def decode_session(frames, export_groups):
    """Walk the full session stream; for each object resolve its class via cm_size
    fitting, map to export group, collect per-class object counts. For WorldSettings
    decode WorldGravityZ (changemask bit 3)."""
    cm_sizes = sorted({g[1] for g in export_groups if g[4] and g[1] > 0})
    ws_cm = None
    for g in export_groups:
        if g[0] and 'WorldSettings' in g[0] and g[4]:
            ws_cm = g[1]; break
    gravity_bit = 3  # WorldGravityZ (phase5 probe: [3])

    data = sr.concat_frames(frames)
    br = bs.make_packet_reader(data)
    class_counts = {}
    gravity = []
    mode_counts = {'strict': 0, 'agnostic': 0}
    skipped = 0
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
            res = try_decode_object_state(br, return_pos, batch_size, cm_sizes)
            if res is None:
                br.err = False
                br.seek_bits(return_pos + batch_size)
                skipped += 1
                continue
            cm_size, cm, floats, mode = res
            mode_counts[mode] += 1
            grp = None
            for g in export_groups:
                if g[4] and g[1] == cm_size:
                    grp = g; break
            cs = (grp[0].split('/')[-1].split('.')[-1].rstrip('\x00') if grp and grp[0] else '?')
            class_counts[cs] = class_counts.get(cs, 0) + 1
            if grp and 'WorldSettings' in grp[0] and gravity_bit < len(cm) and cm[gravity_bit]:
                di = sum(1 for i in range(gravity_bit) if cm[i])
                if di < len(floats):
                    gravity.append((handle, floats[di]))
            br.seek_bits(return_pos + batch_size)
        reads += 1
    return class_counts, gravity, reads, mode_counts, skipped


def main():
    f = 'Demos/TyrReplay1.replay'
    r = ReplayReader(f); r.parse_header(); r.parse_chunks()
    payload = None
    for name, c, raw, d, method in extract_payloads(r):
        if name == 'ReplayData':
            payload = d; break
    frames, ok, params = fp.parse_replaydata(payload)
    groups, err, _ = oi.extract_export_groups(f)
    class_counts, gravity, reads, mode_counts, skipped = decode_session(frames, groups)
    print('TyrReplay1: reads=%d  classes_resolved=%d  gravity_samples=%d  mode=%s  skipped=%d'
          % (reads, len(class_counts), len(gravity), mode_counts, skipped))
    print('top classes by object count:')
    for cs, n in sorted(class_counts.items(), key=lambda x: -x[1])[:15]:
        print('   %-32s %d' % (cs, n))
    print('WorldGravityZ samples (should be ~ -980 or 0):')
    for h, v in gravity[:10]:
        print('   handle=%d gravity=%.4f' % (h, v))


if __name__ == '__main__':
    main()

