"""Step 1 integration: label decoded player-record objects with authoritative field
names using the replay's own FNetFieldExport table (field_map.py).

Pipeline:
  1. field_map.extract_field_map(replay) -> path -> {nec, fields[(bit,name)]}
  2. decoder build_cm_sizes(replay)      -> cm_size -> [(path, nec, first_field)]
  3. link cm_size -> path -> field_map   (nec == cm_size == changemask width)
  4. decode all packets; for each object with dirty bits, label bit -> field name
  5. focus output on player-record classes (BP_LobbyPlayerRecord / BP_TyrPlayerState)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase1'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase2'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase3'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase4'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase5'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase6'))
sys.path.insert(0, os.path.dirname(__file__))

from replay_reader import ReplayReader
from decompress import extract_payloads
import frame_parser as fp
import state_decoder as sd
from field_map import extract_field_map
from object_identity import extract_export_groups


PLAYER_CLASSES = ('LobbyPlayerRecord', 'TyrPlayerState', 'PlayerRecord')


def build_label_index(replay_path):
    """Return (cm_to_fields, path_to_fields) for a replay."""
    field_map = extract_field_map(replay_path)
    # path -> fields
    path_to_fields = {p: g['fields'] for p, g in field_map.items()}
    # cm_size -> path : use the export groups (correct nec == cm_size)
    groups, err, params = extract_export_groups(replay_path)
    cm_to_path = {}
    for path, nec, first_field, pni, we in groups:
        if path:
            p = path.replace('\x00', '')
            cm_to_path[nec] = p
    # cm_size -> fields (via path)
    cm_to_fields = {}
    for cm, p in cm_to_path.items():
        if p in path_to_fields:
            cm_to_fields[cm] = path_to_fields[p]
    return cm_to_fields, path_to_fields, cm_to_path


def decode_and_label(replay_path, max_packets=None):
    r = ReplayReader(replay_path); r.parse_header(); r.parse_chunks()
    payload = None
    for name, c, raw, d, method in extract_payloads(r):
        if name == 'ReplayData':
            payload = d; break
    frames, ok, params = fp.parse_replaydata(payload)
    cm_sizes, groups = sd.build_cm_sizes(replay_path)
    cm_to_fields, path_to_fields, cm_to_path = build_label_index(replay_path)
    packets = [p for fr in frames for p in fr.get('packets', [])]
    if max_packets:
        packets = packets[:max_packets]
    objs = []
    for p in packets:
        try:
            sd.decode_session(p, cm_sizes, object_states=objs)
        except Exception:
            pass
    labeled = []
    for st in objs:
        cm = st['cm_size']
        if cm not in cm_to_fields:
            continue
        fields = cm_to_fields[cm]
        # fields is list of (bit_index, name); build bit->name lookup
        bit_to_name = {}
        for bit, nm in fields:
            if bit is not None:
                bit_to_name[bit] = (nm or '').replace('\x00', '')
        recs = []
        for bit, fval, ival in st['values']:
            recs.append((bit, bit_to_name.get(bit, '?'), fval, ival))
        if recs:
            labeled.append((cm, cm_to_path.get(cm, '?'), recs))
    return labeled


def main():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for demo in ['TyrReplay1.replay', 'TyrReplay10.replay']:
        f = os.path.join(repo, 'Demos', demo)
        print('\n===== %s =====' % demo)
        labeled = decode_and_label(f, max_packets=20000)
        seen = set()
        for cm, path, recs in labeled:
            pshort = path.split('/')[-1]
            if not any(k in pshort for k in PLAYER_CLASSES):
                continue
            key = (pshort, tuple(r[1] for r in recs))
            if key in seen:
                continue
            seen.add(key)
            print('\n[%s] cm=%d' % (pshort, cm))
            for bit, name, fval, ival in recs:
                print('  bit %2d %-16s = i%d' % (bit, name, ival))


if __name__ == '__main__':
    main()
