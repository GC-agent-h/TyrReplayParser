"""Step 2.1: isolate + name BP_TyrPlayerState (cm64) per-player objects.

The phase6 strict decoder rejects cm64 objects (64-bit changemask with
UObject/struct fields don't fit its 32-bit-word state model), so BP_TyrPlayerState
is never isolated. We MONKEYPATCH _read_one_object to FORCE-CAPTURE any object
whose changemask decodes to size 64 (or any size) by greedily reading one 32-bit
word per dirty bit. This isolates the objects and names every dirty bit via the
engine-format export table (phase7/parse_export_full.py).

VALUE CAVEAT: without per-field TYPE/SIZE (only in the binary checkpoint's
FReplicationStateDescriptor), the 32-bit words are POSITIONAL GUESSES, not typed
values. Field NAMES + dirty-bit presence ARE reliable; integer/float VALUES are
not yet trustworthy for variable-width fields. This is the documented Step 2.1
limitation — the next sub-step is extracting field types from the checkpoint
descriptor (engine-source-driven).

Run: python phase7/decode_player_stats.py [replay]
"""
import sys, os, glob, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase6'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase1'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase2'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase3'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase4'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase5'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase7'))

import struct
import state_decoder as sd
import parse_export_full as pef
from replay_reader import ReplayReader
from decompress import extract_payloads
import frame_parser as fp


def _read_one_forced(br, handle, is_subobj, obj_end_pos, cm_sizes,
                     class_counts, mode_counts, initial_archetypes, archetype_to_cm,
                     object_states=None):
    """Like sd._read_one_object but force-captures objects the strict decoder skips."""
    return_pos = br.tell_bits()
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
            br.read_bits(2)
        archetype_id = sd.read_creation_header(br)
    state_start = br.tell_bits()
    res = sd.decode_object_state(br, obj_end_pos, cm_sizes, b_is_initial, archetype_id)
    if res is not None:
        cm_size, cm, floats, mode = res
    else:
        # forced: find which cm_size changemask validates here.
        # Try LARGEST sizes first so a real cm64 object is captured as cm64
        # (not mis-captured as a smaller cm that coincidentally fits).
        br.err = False
        br.seek_bits(state_start)
        cm_size = None
        for cand in sorted(set(cm_sizes), reverse=True):
            br.seek_bits(state_start)
            cm = sd.read_changemask(br, cand)
            if cm is None or br.err:
                br.err = False
                continue
            # greedily read one 32-bit word per set bit; require no error and
            # not wildly overshooting the object end (allow slack for variable
            # field widths / trailing padding).
            pos = br.tell_bits()
            ok = True
            for bit in cm:
                if not bit:
                    continue
                if br.bits_left() < 32:
                    ok = False
                    break
                br.read_bits(32)
            if ok and br.tell_bits() <= obj_end_pos + 256:
                cm_size = cand
                break
        if cm_size is None:
            br.err = False
            br.seek_bits(obj_end_pos)
            return None
        br.seek_bits(state_start)
        cm = sd.read_changemask(br, cm_size)
        mode = 'forced'
    key = ('arch%d' % archetype_id) if (b_is_initial and archetype_id is not None) else ('cm%d' % cm_size)
    class_counts[key] = class_counts.get(key, 0) + 1
    mode_counts[mode] = mode_counts.get(mode, 0) + 1
    if b_is_initial and archetype_id is not None:
        initial_archetypes.setdefault(archetype_id, 0)
        initial_archetypes[archetype_id] += 1
        archetype_to_cm.setdefault(archetype_id, cm_size)
    if object_states is not None:
        vals = sd.extract_object_values(br, state_start, cm)
        if vals is None:
            vals = []
        object_states.append({'handle': handle, 'class_key': key, 'cm_size': cm_size,
                              'cm_bits': cm, 'values': vals, 'mode': mode})
    br.seek_bits(obj_end_pos)
    return (cm_size, mode)


def decode_player_stats(replay_path):
    export_groups, err, _ = pef.parse_export_groups(replay_path)
    if err:
        print('WARN export parse error:', err)

    # patch
    sd._read_one_object = _read_one_forced

    cm_sizes, _ = sd.build_cm_sizes(replay_path)
    r = ReplayReader(replay_path); r.parse_header(); r.parse_chunks()
    payload = next(d for name, c, raw, d, method in extract_payloads(r) if name == 'ReplayData')
    frames, ok, params = fp.parse_replaydata(payload)
    packets = [p for fr in frames for p in fr.get('packets', [])]
    object_states = []
    for pkt in packets:
        sd.decode_session(pkt, cm_sizes, object_states=object_states)

    # field name lookup per cm_size
    fields_by_cm = {}
    for g in export_groups:
        sz = g['nec']
        flds = [f.rstrip('\x00') if isinstance(f, str) else f for f in g['fields']]
        fields_by_cm.setdefault(sz, flds)

    # cm64 = BP_TyrPlayerState (and BP_TyrLobbyPlayerState)
    pstate = [st for st in object_states if st['cm_size'] == 64]
    players = {}
    for st in pstate:
        h = st['handle']
        flds = fields_by_cm.get(64, [])
        named = []
        for bit_idx, fval, ival in st['values']:
            nm = flds[bit_idx] if bit_idx < len(flds) else None
            if nm:
                named.append((nm, ival, fval))
        players.setdefault(h, []).append({'dirty': sum(st['cm_bits']), 'fields': named, 'mode': st['mode']})

    from collections import Counter
    cc = Counter(st['cm_size'] for st in object_states)
    return {
        'replay': os.path.basename(replay_path),
        'total_objects': len(object_states),
        'cm_histogram': dict(cc),
        'bp_tyrplayerstate_objects': len(pstate),
        'distinct_player_handles': len(players),
        'players': players,
        'fields_64': fields_by_cm.get(64, []),
    }


if __name__ == '__main__':
    replay = sys.argv[1] if len(sys.argv) > 1 else 'Demos/TyrReplay4.replay'
    out = decode_player_stats(replay)
    print('replay:', out['replay'])
    print('total decoded objects (forced):', out['total_objects'])
    print('cm histogram:', out['cm_histogram'])
    print('BP_TyrPlayerState objects:', out['bp_tyrplayerstate_objects'])
    print('distinct player handles:', out['distinct_player_handles'])
    print('\nBP_TyrPlayerState field names (bit order, %d fields):' % len(out['fields_64']))
    for i, f in enumerate(out['fields_64']):
        print('  bit%-3d %s' % (i, f))
    print('\nNOTE: object isolation + bit naming are reliable. Per-field VALUES require')
    print('per-field TYPE/SIZE from the binary checkpoint FReplicationStateDescriptor')
    print('(not yet decoded) — integer/float words shown below are positional guesses.')
    shown = 0
    for h, snaps in out['players'].items():
        if shown >= 3:
            break
        best = max(snaps, key=lambda s: s['dirty'])
        nz = [(fn, iv) for fn, iv, fv in best['fields'] if iv != 0]
        if nz:
            print('\nplayer handle %s  (mode=%s, %d snapshots, %d non-zero bits):' % (h, best['mode'], len(snaps), len(nz)))
            for fn, iv in nz[:15]:
                print('   %-30s i=%d' % (fn, iv))
            shown += 1
