"""Build a JSON file with everything the existing TyrReplayParser can produce right now.

Runs the committed Phase 6 per-packet decoder + Phase 7 field-label tables across all
10 sample replays, and serializes:
  - per-replay: structural decode stats, export-group class inventory, archetype->class map
  - per-replay: captured per-object replicated state (raw 32-bit float/int words)
  - per-replay: Dumper-driven field-label TABLE (names+types per cm_size) with the
    anchor-validation status (whether replay bit-0 == Dumper[0], i.e. order proven)
  - global: known blockers / what is NOT yet achievable

Output: results_current.json (written next to this script).
"""
import sys, os, glob, json, struct, datetime

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, '..', 'phase1'))
sys.path.insert(0, os.path.join(HERE, '..', 'phase2'))
sys.path.insert(0, os.path.join(HERE, '..', 'phase3'))
sys.path.insert(0, os.path.join(HERE, '..', 'phase4'))
sys.path.insert(0, os.path.join(HERE, '..', 'phase5'))
sys.path.insert(0, os.path.join(HERE, '..', 'phase6'))

from replay_reader import ReplayReader
from decompress import extract_payloads
import frame_parser as fp
import state_decoder as sd
import object_identity as oi

try:
    import field_labeler as fl
    HAVE_LABELER = True
except Exception as e:
    HAVE_LABELER = False
    LABELER_ERR = repr(e)


def cm_bits_to_int(bits):
    v = 0
    for i, b in enumerate(bits):
        if b:
            v |= (1 << i)
    return v


def decode_replay(path):
    """Run the full per-packet decode; return a dict of everything recoverable."""
    name = os.path.basename(path)
    out = {'replay': name, 'errors': []}

    # ---- export groups (Phase 5): class inventory + changemask sizes ----
    try:
        groups, err, params = oi.extract_export_groups(path)
    except Exception as e:
        out['errors'].append('export_groups: %s' % e)
        return out
    out['export_err'] = bool(err)
    out['export_group_count'] = len(groups)
    cm_sizes = sorted({g[1] for g in groups if g[4] and g[1] > 0})
    out['distinct_changemask_sizes'] = cm_sizes

    # class inventory: cm_size -> list of class paths + first_field (bit-0 name)
    inv = {}
    for (p, nec, ff, pni, we) in groups:
        if not we or nec <= 0:
            continue
        inv.setdefault(nec, []).append({
            'class_path': (p or '').rstrip('\x00'),
            'first_field': (ff or '').rstrip('\x00'),
        })
    out['class_inventory'] = {str(k): v for k, v in sorted(inv.items())}

    # ---- per-packet Iris decode (Phase 6) ----
    r = ReplayReader(path)
    r.parse_header(); r.parse_chunks()
    payload = None
    for nm, c, raw, d, method in extract_payloads(r):
        if nm == 'ReplayData':
            payload = d
            break
    if payload is None:
        out['errors'].append('no ReplayData chunk')
        return out
    frames, ok, params2 = fp.parse_replaydata(payload)
    if not ok:
        out['errors'].append('frame parse failed')
        return out

    packets = [p for fr in frames for p in fr.get('packets', [])]
    object_states = []
    agg = dict(reads=0, class_counts={}, mode_counts={'strict': 0, 'agnostic': 0},
               initial_archetypes={}, archetype_to_cm={}, skipped=0)
    for pkt in packets:
        res = sd.decode_session(pkt, cm_sizes, object_states=object_states)
        if res is None:
            continue
        reads, class_counts, mode_counts, initial_archetypes, skipped, arch_to_cm = res
        agg['reads'] += reads
        agg['skipped'] += skipped
        for k, v in class_counts.items():
            agg['class_counts'][k] = agg['class_counts'].get(k, 0) + v
        for k, v in mode_counts.items():
            agg['mode_counts'][k] += v
        for k, v in initial_archetypes.items():
            agg['initial_archetypes'][k] = agg['initial_archetypes'].get(k, 0) + v
        for k, v in arch_to_cm.items():
            agg['archetype_to_cm'][k] = v

    out['structural_decode'] = {
        'frame_count': len(frames),
        'packet_count': len(packets),
        'iris_reads': agg['reads'],
        'objects_decoded': len(object_states),
        'skipped_batches': agg['skipped'],
        'decode_mode_counts': agg['mode_counts'],
        'class_counts_by_key': agg['class_counts'],
        'initial_archetype_handle_histogram': agg['initial_archetypes'],
    }

    # archetype handle -> class path (via export groups cm_size -> path)
    cm_to_paths = {}
    for p, nec, ff, pni, we in groups:
        if we and nec > 0:
            cm_to_paths.setdefault(nec, []).append((p or '').rstrip('\x00'))
    arch_map = {}
    for aid, cm in agg['archetype_to_cm'].items():
        paths = cm_to_paths.get(cm, [])
        arch_map[str(aid)] = {
            'changemask_size': cm,
            'class_path': paths[0] if paths else None,
            'all_candidate_paths': paths,
        }
    out['archetype_to_class'] = arch_map

    # ---- captured per-object replicated state (option 1: raw float/int words) ----
    objs = []
    for st in object_states:
        cm = st['cm_bits']
        ndirty = sum(cm)
        if ndirty == 0:
            continue  # only meaningful state carries dirty values
        vals = [{'bit': b, 'float32': round(fv, 6), 'int32': iv} for (b, fv, iv) in st['values']]
        paths = cm_to_paths.get(st['cm_size'], [])
        label = paths[0].split('/')[-1].split('.')[-1] if len(paths) == 1 else ('cm%d' % st['cm_size'])
        objs.append({
            'handle': str(st['handle']),
            'class_key': st['class_key'],
            'changemask_size': st['cm_size'],
            'resolved_class': label,
            'changemask_bits_set': ndirty,
            'changemask': cm_bits_to_int(cm),
            'values': vals,
        })
    out['object_states_with_dirty_values'] = objs
    out['object_states_dirty_count'] = len(objs)

    # ---- Phase 7 field-label TABLE (Dumper-driven) + anchor validation ----
    if HAVE_LABELER:
        try:
            lab = fl.Labeler(path)
            table = {}
            for nec, entries in lab.cm_map.items():
                rows = []
                for ent in entries:
                    rows.append({
                        'class_path': (ent['class_path'] or '').rstrip('\x00'),
                        'dumper_class': ent['dumper_class'],
                        'first_field': (ent['first_field'] or '').rstrip('\x00'),
                        # aligned: True==replay bit0 == Dumper[0] (order PROVEN);
                        #          False==order differs; None==hardcoded FName (unresolved)
                        'order_anchor_verified': ent['aligned'],
                        'dumper_field_count': ent['dumper_count'],
                        'fields': [{'name': n, 'type': t} for (n, t) in ent['dfields']],
                    })
                table[str(nec)] = rows
            out['field_label_table'] = table
            # summary of anchor verification
            verified = sum(1 for rows in table.values() for e in rows
                           if e['order_anchor_verified'] is True)
            diff = sum(1 for rows in table.values() for e in rows
                       if e['order_anchor_verified'] is False)
            unresolved = sum(1 for rows in table.values() for e in rows
                             if e['order_anchor_verified'] is None)
            out['field_label_summary'] = {
                'classes_mapped': sum(len(rows) for rows in table.values()),
                'order_anchor_verified': verified,
                'order_anchor_differs': diff,
                'order_anchor_unresolved_hardcoded': unresolved,
                'note': ('Dumper declaration order != Iris changemask bit order in this dataset; '
                         'field NAMES are available as a candidate set but bit->name ORDER is '
                         'not yet proven for any class (blocked on Checkpoint NetExport / exe FName table).'),
            }
        except Exception as e:
            out['errors'].append('labeler: %s' % e)
    else:
        out['errors'].append('labeler unavailable: %s' % LABELER_ERR)

    return out


def main():
    import argparse
    demo_dir = os.path.join(HERE, '..', 'Demos')
    ap = argparse.ArgumentParser(description='Export current decode results to JSON.')
    ap.add_argument('replay', nargs='?', default=None,
                    help='Single TyrReplayN.replay to process (default: all 10).')
    args = ap.parse_args()

    if args.replay:
        if not os.path.isabs(args.replay):
            f = os.path.join(demo_dir, args.replay)
        else:
            f = args.replay
        if not os.path.exists(f):
            print('replay not found:', f)
            sys.exit(1)
        demos = [f]
    else:
        demos = sorted(glob.glob(os.path.join(demo_dir, 'TyrReplay*.replay')))

    results = []
    for f in demos:
        print('decoding', os.path.basename(f), flush=True)
        results.append(decode_replay(f))

    # global blockers / capability summary
    global_blockers = {
        'player_usernames': {
            'status': 'BLOCKED',
            'reason': ('PlayerName is not plaintext in the live stream; it is StringTokenStore-'
                       'encoded. Decoder only catches small cm34 DELTA updates (never the '
                       'creation that carries PlayerName). Raw FString scan across all packet '
                       'bytes found 0 hits; 291 ASCII fragments are string-table entries, not names.'),
        },
        'labeled_fields_bit_to_name': {
            'status': 'BLOCKED',
            'reason': ('Replay bit-0 != Dumper[0] for all 67 native classes (0 matches). '
                       'Dumper gives the field SET (names+types) but not the wire bit ORDER. '
                       'Authoritative order lives in the replay own FNetFieldExport / Checkpoint '
                       'NetExport stream or the shipping exe hardcoded FName table (#216 -> name), '
                       'neither yet decoded.'),
        },
        'typed_value_decode': {
            'status': 'PARTIAL',
            'reason': ('Only raw 32-bit words recovered (float32 + int32 views). Per-type '
                       'NetSerializer decode (vectors, gameplay tags, structs) not implemented; '
                       'value interpretation beyond "float or int" is not done.'),
        },
        'read_objects_pending_destroy': {
            'status': 'DISABLED',
            'reason': ('Left disabled: enabling it misread the destroy-count and consumed the '
                       'stream. Per-packet isolation keeps framing stable without it. Known gap; '
                       'revisit only if object counts come up short.'),
        },
        'worldsettings_worldgravityz': {
            'status': 'DROPPED',
            'reason': ('Static level actor; creation ref written INVALID in these replays so the '
                       'class cannot be recovered from the creation header.'),
        },
    }

    doc = {
        'generated_utc': datetime.datetime.utcnow().isoformat() + 'Z',
        'generator': 'phase7/export_results_json.py',
        'method': 'per-packet Iris decode (FReplicationReader::Read) per frame packet; option-1 raw value extraction',
        'replays_analyzed': len(results),
        'replay_results': results,
        'global_blockers': global_blockers,
        'capability_summary': {
            'can': [
                'End-to-end decode of every replay (per-packet Iris, class resolution via archetype handle).',
                'Changemask bit-width (cm_size) + class PATH for every replicated class (106-314 groups/file).',
                'Raw per-object replicated state: for each object with a dirty changemask, recover 32-bit words as float32 AND int32.',
                'Archetype handle -> class path mapping for dynamic actors (e.g. cm16 Map, cm34 BP_LobbyPlayerRecord).',
                'Dumper-driven field-name SETS per class (names + types) as candidate label tables.',
            ],
            'cannot_yet': [
                'Player usernames (StringTokenStore-encoded).',
                'Labeled stats: bit N -> field name (order unproven; 0/67 native classes match at bit-0).',
                'Typed value decode beyond raw 32-bit words (vectors/tags/structs need NetSerializers).',
                'WorldSettings/WorldGravityZ (static actor, invalid creation ref).',
                'ReadObjectsPendingDestroy (disabled).',
            ],
        },
    }

    out_path = os.path.join(HERE, 'results_current.json')
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
    print('wrote', out_path)
    # quick console summary
    for res in results:
        sd_dec = res.get('structural_decode', {})
        print('  %-18s reads=%-5d objs=%-4d dirty=%-4d groups=%-4d' % (
            res['replay'], sd_dec.get('iris_reads', 0),
            sd_dec.get('objects_decoded', 0), res.get('object_states_dirty_count', 0),
            res.get('export_group_count', 0)))


if __name__ == '__main__':
    main()
