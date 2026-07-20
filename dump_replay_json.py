"""Dump the full verified pipeline for a single replay (TyrReplay4) to JSON.

Runs every phase that is confirmed working and writes a structured result to
<repo>/out/<ReplayName>.json. No labeled-field / typed-value decoding (those are
the unblocked Option B items) — only what is actually possible today.
"""
import sys, os, json, glob
sys.path.insert(0, 'phase1'); sys.path.insert(0, 'phase2'); sys.path.insert(0, 'phase3')
sys.path.insert(0, 'phase4'); sys.path.insert(0, 'phase5'); sys.path.insert(0, 'phase6')
import replay_reader as rr
import decompress as dc
import frame_parser as fp
import state_decoder as sd
from replay_reader import CHUNK_TYPES

F = sys.argv[1] if len(sys.argv) > 1 else 'Demos/TyrReplay4.replay'
OUT_DIR = 'out'

result = {'replay': os.path.basename(F), 'phases': {}}

# ---- Phase 1: container ----
r = rr.ReplayReader(F); r.parse_header(); r.parse_chunks()
info = r.info
types = {}
for c in r.chunks:
    t = CHUNK_TYPES.get(c['type'], c['type']); types[t] = types.get(t, 0) + 1
result['phases']['phase1_container'] = {
    'file_version': info.get('FileVersion'),
    'chunk_count': len(r.chunks),
    'chunk_types': types,
}

# ---- Phase 2: decompression ----
payloads = dc.extract_payloads(r)
methods = {}
for name, c, raw, d, method in payloads:
    methods[name] = methods.get(name, 0) + 1
result['phases']['phase2_decompression'] = {'methods': methods}

# ---- Phase 3: frame tiling ----
rd = None
for name, c, raw, d, method in payloads:
    if name == 'ReplayData':
        rd = d; break
frames, ok, params = fp.parse_replaydata(rd)
packets = [p for fr in frames for p in fr.get('packets', [])]
result['phases']['phase3_frames'] = {
    'replaydata_frames': len(frames),
    'tile_ok': ok,
    'total_packets': len(packets),
}

# ---- Phase 5: export groups / class inventory ----
groups, err, _ = sd.oi.extract_export_groups(F)
cm_sizes = [g[1] for g in groups if g[4] and g[1] > 0]
cm_to_paths = {}
for path, size, _, _, exp in groups:
    if exp and size > 0:
        cm_to_paths.setdefault(size, []).append(path.split('/')[-1].split('.')[-1].rstrip('\x00'))
result['phases']['phase5_class_inventory'] = {
    'export_groups': len(groups),
    'distinct_cm_sizes': sorted(set(cm_sizes)),
    'class_by_cm': {str(sz): cm_to_paths[sz] for sz in sorted(cm_to_paths)},
}

# ---- Phase 6: Iris decode + raw per-object state ----
objs = []
agg = dict(reads=0, cc={}, mode={'strict': 0, 'agnostic': 0}, atcm={}, skipped=0)
for pkt in packets:
    res = sd.decode_session(pkt, sorted(set(cm_sizes)), object_states=objs)
    if res is None:
        continue
    rd_, ccc, mm, ia, sk, atcm = res
    agg['reads'] += rd_; agg['skipped'] += sk
    for k, v in ccc.items(): agg['cc'][k] = agg['cc'].get(k, 0) + v
    for k, v in mm.items(): agg['mode'][k] += v
    for k, v in atcm.items(): agg['atcm'][k] = v

by_class = {}
for o in objs:
    cm = o['cm_size']; p = cm_to_paths.get(cm, ['cm%d' % cm])[0]
    by_class.setdefault(p, []).append(o)

class_states = {}
for label in sorted(by_class):
    os_ = by_class[label]
    dirty = [s for s in os_ if sum(s['cm_bits']) > 0]
    samples = []
    for s in sorted(dirty, key=lambda x: -sum(x['cm_bits']))[:5]:
        samples.append({
            'handle': s['handle'],
            'cm_size': s['cm_size'],
            'changemask': ''.join('1' if b else '0' for b in s['cm_bits']),
            'values': [{'bit': bi, 'float': fv, 'int': iv} for bi, fv, iv in s['values']],
        })
    class_states[label] = {
        'objects': len(os_),
        'dirty_objects': len(dirty),
        'samples': samples,
    }

result['phases']['phase6_iris_decode'] = {
    'reads': agg['reads'],
    'skipped': agg['skipped'],
    'objects_captured': len(objs),
    'mode_counts': agg['mode'],
    'top_classes_by_object_count': sorted(agg['cc'].items(), key=lambda kv: -kv[1]),
    'archetype_to_class': [
        {'archetype_handle': str(a), 'cm_size': cm, 'class': cm_to_paths.get(cm, ['?'])[0]}
        for a, cm in sorted(agg['atcm'].items(), key=lambda kv: str(kv[0]))
    ],
    'per_class_state': class_states,
}

# ---- Username (checkpoint plaintext) ----
names = sd.load_player_names(F)
result['phases']['username_checkpoint_plaintext'] = {
    'players': sorted(set(names.values())) if names else [],
    'subsystem_ids': list(names.keys()) if names else [],
}

# ---- Player subsystem / component roster (ReplayData plaintext blocks) ----
sys.path.insert(0, 'phase7')
import parse_subsystems as ps_mod
subsystem_blocks = ps_mod.extract_subsystems(F)
result['phases']['player_subsystem_roster'] = {
    'block_count': len(subsystem_blocks),
    'blocks': [{'subsystem_id': sid, 'components': comps} for sid, comps in subsystem_blocks],
}

# ---- Phase 5 field schema (bit-0 name + cm_size per exported class) ----
sys.path.insert(0, 'phase5')
import build_field_schema as bfs_mod
schema, schema_err = bfs_mod.build_schema(F)
result['phases']['phase5_field_schema'] = {
    'export_parse_error': schema_err,
    'classes': schema,
}

# ---- Phase: best-effort labeled fields (option c) ----
sys.path.insert(0, 'phase7')
import label_fields as lf_mod
dumper = lf_mod.parse_dumper()
cbm = result['phases']['phase5_class_inventory']['class_by_cm']
labeled = {'verified': False, 'note': 'positional hypothesis: bit i ~= Dumper property i (UNVERIFIED)', 'classes': []}
for cm_key in ('1', '12', '83'):
    names = cbm.get(cm_key, [])
    if not names:
        labeled['classes'].append({'cm': cm_key, 'note': 'class not recovered'})
        continue
    for cn in names:
        labeled['classes'].append(lf_mod.label_class(cm_key, cn, dumper))
result['phases']['phase_labeled_fields'] = labeled

# ---- Phase: authoritative bit->name field table (engine-format export parse) ----
# Reverse-engineered from UPackageMapClient::AppendNetFieldExports
# (PackageMapClient.cpp:1737) + FNetFieldExport::operator<< (:4310) +
# StaticSerializeName (CoreNet.cpp:299). Flat per-field export list; grouped by
# NeedsExport=1 boundaries. Resolves hardcoded EName indices via UnrealNames.inl.
import parse_export_full as pef_mod
groups, pef_err, pef_params = pef_mod.parse_export_groups(F)
result['phases']['phase_export_field_table'] = {
    'parse_error': pef_err,
    'group_count': len(groups),
    'groups': [{'path': g['path'].rstrip('\x00'), 'pni': g['pni'], 'nec': g['nec'],
                'fields': [f.rstrip('\x00') if isinstance(f, str) else f for f in g['fields']]}
               for g in groups],
}

# ---- Phase: BP_TyrPlayerState isolation + naming (Step 2.1) ----
# Monkeypatches phase6 to force-capture cm64 objects; names every dirty bit via
# the export table. Object isolation + bit naming are reliable; per-field VALUES
# need type/size from the binary checkpoint descriptor (see decode_player_stats.py).
import decode_player_stats as dps_mod
dps = dps_mod.decode_player_stats(F)
result['phases']['phase_player_stats'] = {
    'bp_tyrplayerstate_objects': dps['bp_tyrplayerstate_objects'],
    'distinct_player_handles': dps['distinct_player_handles'],
    'fields_64': dps['fields_64'],
    'value_typing': 'UNVERIFIED - positional 32-bit words; needs checkpoint FReplicationStateDescriptor',
}

# ---- write ----
os.makedirs(OUT_DIR, exist_ok=True)
out_path = os.path.join(OUT_DIR, os.path.splitext(os.path.basename(F))[0] + '.json')
with open(out_path, 'w') as f:
    json.dump(result, f, indent=2)
print('wrote', os.path.abspath(out_path))
print('replay=%s frames=%d packets=%d objects=%d players=%s subsystems=%d schema_classes=%d labeled=%d' % (
    result['replay'], len(frames), len(packets), len(objs),
    result['phases']['username_checkpoint_plaintext']['players'], len(subsystem_blocks),
    len(schema), len(labeled['classes'])))
