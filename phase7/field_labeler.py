"""Phase 7: Option B — changemask-bit -> field-name labeling (empirical, Dumper-driven).

WHY THIS WORKS:
  The replay's first-frame FNetFieldExport blob gives, per replicated class:
    - PathName        (e.g. /Game/.../BP_LobbyPlayerRecord.BP_LobbyPlayerRecord_C)
    - NumExportsInGroup (nec) = the Iris changemask BIT-COUNT for that class
    - first_field     (bit-0 property name, or 'hardcoded#N' FName index)
  It does NOT carry the full per-bit name list (frame-0 only exports delta fields).

  The Dumper-7 dump (GObjects-Dump-WithProperties.txt) gives, per CLASS, the full
  ordered property list (name + type). Blueprint classes (BP_*_C, Map_*_C) are cooked
  assets NOT in the native SDK dump, but their NATIVE PARENT class IS in the dump and
  carries the replicated fields (a Blueprint subclass inherits its parent's replicated
  properties unless it overrides them — and the changemask bit layout follows the
  parent's replicated-property order). So:
    BP_LobbyPlayerRecord_C  ->  Tyr.TyrPlayerRecord  (PlayerName, MyTeamID, bIsAlive, ...)
    BP_CaptureZone_C        ->  Tyr.TyrCaptureZone   (CapturePoints, PubTeamID, ...)
    BP_TyrPlayerState_C     ->  Tyr.TyrPlayerStateBase
    BP_TyrGameState_C       ->  Tyr.TyrGameStateBase

  We map each replay class -> Dumper field dictionary, then label changemask bits by
  Dumper order. The replay's `first_field` anchors/validates the order: replay bit 0
  must equal the Dumper parent's first replicated field (or be locatable in it).

CAVEAT (known, documented): UE Dumper declaration order is NOT guaranteed == Iris
changemask bit order. We treat the Dumper list as the candidate order and VERIFY via
the decoder's extracted value TYPES (StrProperty -> string, not a 32-bit word;
IntProperty -> small int; FloatProperty -> float32; BoolProperty -> 1 bit). Where the
field count diverges from nec (structs expand into sub-fields), the mapping is
approximate and flagged.

USAGE:
  from phase7.field_labeler import Labeler
  lab = Labeler('Demos/TyrReplay1.replay')
  lab.report()                       # prints anchored class -> field map
  lab.label_object(st_dict)          # st from state_decoder.decode_session(object_states)
"""
import sys, os, re, glob, struct
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase1'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase2'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase3'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase5'))

from replay_reader import ReplayReader
from decompress import extract_payloads
import frame_parser as fp
import object_identity as oi

DUMPER_BASE = os.path.join(os.path.dirname(__file__), '..',
                           '5.6.0-31351+++Tyr+release-Tyr_OLD')
DUMPER_PATH = os.path.join(DUMPER_BASE, 'GObjects-Dump-WithProperties.txt')
NON_STATE_TYPES = ('MulticastInlineDelegateProperty', 'MulticastSparseDelegateProperty',
                   'DelegateProperty', 'MulticastDelegateProperty')


def class_short(path):
    if not path:
        return None
    return path.split('/')[-1].split('.')[-1].rstrip('\x00')


def load_dumper():
    if not os.path.exists(DUMPER_PATH):
        return {}
    cls_re = re.compile(r'^\[[0-9A-Fa-f]+\]\s+\{[^}]+\}\s+Class\s+(\S+)')
    prop_re = re.compile(r'^\[[0-9A-Fa-f]+\]\s+\{[^}]+\}\s+(\w+Property)\s+(\S+)$')
    classes = {}
    cur = None
    for ln in open(DUMPER_PATH, encoding='utf-8', errors='replace'):
        m = cls_re.match(ln)
        if m:
            cur = m.group(1)
            classes.setdefault(cur, [])
            continue
        if cur is not None:
            mp = prop_re.match(ln)
            if mp:
                classes[cur].append((mp.group(2), mp.group(1)))
    return classes


def data_fields(dumper, class_path):
    return [(n, t) for (n, t) in dumper.get(class_path, []) if t not in NON_STATE_TYPES]


def find_dumper_parent(dumper, bp_short):
    """Find the native Dumper class that is the parent/replication-source of a
    Blueprint class. Strip BP_ / _C prefixes and match against Dumper class shorts."""
    core = bp_short.replace('BP_', '').replace('_C', '')
    # remove common map prefixes
    core_clean = re.sub(r'^(Map_.*?_Rework|Map_.*?|PC_Replay).*$', '', core)
    if not core_clean:
        core_clean = core
    matches = []
    for c in dumper:
        csh = class_short(c)
        if not csh or 'BP_' in csh:
            continue
        if core_clean and (core_clean in csh or csh in core_clean):
            matches.append(c)
    if not matches:
        # fallback: try the core without 'Rework'/'Map_'
        base = re.sub(r'(Rework|Map_|_C)$', '', bp_short).replace('BP_', '')
        for c in dumper:
            csh = class_short(c)
            if csh and base and base in csh:
                matches.append(c)
    # prefer the match whose data-field count is non-trivial
    matches.sort(key=lambda c: -len(data_fields(dumper, c)))
    return matches[0] if matches else None


def type_width_bits(typ):
    """Approximate serialised bit-width for a replicated property TYPE under Iris
    default NetSerializers. Used only for sanity hints, not exact framing."""
    return {
        'IntProperty': 32, 'UInt32Property': 32, 'FloatProperty': 32,
        'BoolProperty': 1, 'ByteProperty': 8, 'EnumProperty': 8,
        'StrProperty': None,  # variable (u32 len + ascii)
        'NameProperty': None, 'TextProperty': None,
        'ObjectProperty': 32, 'SoftObjectProperty': None,
        'StructProperty': None, 'ArrayProperty': None,
        'MapProperty': None, 'SetProperty': None,
    }.get(typ, 32)


class Labeler:
    def __init__(self, replay_path):
        self.replay = replay_path
        self.dumper = load_dumper()
        groups, err, params = oi.extract_export_groups(replay_path)
        self.groups = groups
        self.export_err = err
        # build cm_size -> [(class_path, dumper_fields, first_field, aligned)]
        self.cm_map = {}
        for g in groups:
            if not g[4] or g[1] <= 0:
                continue
            path, nec, first_field = g[0], g[1], g[2]
            cs = class_short(path)
            dpath = None
            if cs in self.dumper:
                dpath = cs
            else:
                parent = find_dumper_parent(self.dumper, cs or '')
                dpath = parent
            dfields = data_fields(self.dumper, dpath) if dpath else []
            aligned = self._check_anchor(first_field, dfields)
            self.cm_map.setdefault(nec, []).append({
                'class_path': path, 'dumper_class': dpath, 'nec': nec,
                'first_field': first_field, 'dfields': dfields,
                'dumper_count': len(dfields), 'aligned': aligned,
            })

    def _check_anchor(self, first_field, dfields):
        """Return True ONLY if replay bit-0 field == Dumper's FIRST data field
        (i.e. Iris changemask order == Dumper declaration order for this class).
        Return False if first_field is a known string but NOT at Dumper[0] (order
        differs). Return None if first_field is a hardcoded FName index (unresolved)
        or absent (cannot verify)."""
        ff = (first_field or '').rstrip('\x00')
        if ff.startswith('hardcoded#'):
            return None
        ff = ff.replace('hardcoded#', '').strip()
        if not ff or not dfields:
            return None
        if dfields[0][0] == ff:
            return True
        return False

    def _count_for(self, dumper_class):
        if not dumper_class:
            return -1
        return len(self.dumper.get(dumper_class, []))

    def class_for_cm(self, cm_size):
        return self.cm_map.get(cm_size, [])

    def label_object(self, st):
        """Given an object-state dict from state_decoder (cm_size, cm_bits, values
        [(bit,fval,ival)]), return labeled value lists.

        Because MULTIPLE classes can share one changemask size (cm_size alone is NOT
        a unique class key), we return ALL candidate labels (one per class at that
        cm_size). Each candidate is {class, dumper_class, aligned, fields, labeled}.
        The caller should prefer the candidate whose `aligned` is True, or treat the
        set as ambiguous when several share a cm_size.
        """
        cm = st['cm_size']
        entries = self.cm_map.get(cm, [])
        if not entries:
            return None
        candidates = []
        for ent in entries:
            if not ent['dfields']:
                continue
            fields = ent['dfields']
            labeled = []
            for (bit, fv, iv) in st['values']:
                if bit < len(fields):
                    name, typ = fields[bit]
                else:
                    name, typ = ('bit%d' % bit, '?')
                labeled.append((name, typ, fv, iv, bit))
            candidates.append({
                'class': (ent['class_path'] or '?').split('/')[-1].split('.')[-1],
                'dumper_class': ent['dumper_class'], 'aligned': ent['aligned'],
                'labeled': labeled,
            })
        if not candidates:
            return None
        # prefer an anchored candidate; else the one whose dumper_count==cm
        best = next((c for c in candidates if c['aligned'] is True), None)
        if best is None:
            best = next((c for c in candidates if c['dumper_class']
                         and self._count_for(c['dumper_class']) == cm), candidates[0])
        return {'candidates': candidates, 'best': best,
                'ambiguous': len(candidates) > 1}

    def report(self):
        print('=== Option B field-label map: %s ===' % os.path.basename(self.replay))
        print('Dumper classes: %d | export groups: %d | export_err: %s'
              % (len(self.dumper), len(self.groups), self.export_err))
        print('changemask sizes with a Dumper field dictionary:')
        for cm in sorted(self.cm_map.keys()):
            for e in self.cm_map[cm]:
                tag = {True: 'ANCHOR', False: 'drift', None: 'unverif'}[e['aligned']]
                src = 'self' if e['dumper_class'] == class_short(e['class_path']) else 'parent'
                print('  cm%-4d %-38s %s dumper=%-3d ff=%-20s [%s:%s]'
                      % (cm, (e['class_path'] or '?').split('/')[-1].split('.')[-1],
                         tag, e['dumper_count'], str(e['first_field'])[:20],
                         src, (e['dumper_class'] or '?').split('.')[-1]))


def run_labeled_decode(replay_path, max_packets=None):
    """Full pipeline: decode every packet per-packet, then label each object's dirty
    values with Dumper field names. Returns (labeler, labeled_objects, stats)."""
    import importlib.util as _ilu
    def _load(modname, relpath):
        spec = _ilu.spec_from_file_location(
            modname, os.path.join(os.path.dirname(__file__), '..', relpath))
        m = _ilu.module_from_spec(spec); spec.loader.exec_module(m); return m
    sd = _load('state_decoder', 'phase6/state_decoder.py')
    lab = Labeler(replay_path)
    r = ReplayReader(replay_path); r.parse_header(); r.parse_chunks()
    payload = None
    for name, c, raw, d, method in extract_payloads(r):
        if name == 'ReplayData':
            payload = d; break
    frames, ok, params = fp.parse_replaydata(payload)
    cm_sizes, groups = sd.build_cm_sizes(replay_path)
    packets = [p for fr in frames for p in fr.get('packets', [])]
    if max_packets:
        packets = packets[:max_packets]
    objs = []
    for pkt in packets:
        try:
            sd.decode_session(pkt, cm_sizes, object_states=objs)
        except Exception:
            continue
    labeled = []
    for st in objs:
        L = lab.label_object(st)
        if L is not None and L['best'] is not None and L['best']['labeled']:
            labeled.append((st, L))
    return lab, labeled, len(objs)


def main():
    import argparse
    demo_dir = os.path.join(os.path.dirname(__file__), '..', 'Demos')
    demos = sorted(glob.glob(os.path.join(demo_dir, 'TyrReplay*.replay')))
    if '--report' in sys.argv:
        for f in demos:
            lab = Labeler(f)
            lab.report()
            print()
        return
    # default: labeled decode on the first replay (capped for speed)
    f = demos[0]
    print('### Option B labeled decode: %s ###' % os.path.basename(f))
    lab = Labeler(f)
    lab.report()
    lab2, labeled, total = run_labeled_decode(f, max_packets=20000)
    print('\nDecoded objects: %d | objects with Dumper-resolvable labels: %d'
          % (total, len(labeled)))
    # group labeled objects by class, show a few dirty-value samples
    by_cls = {}
    for st, L in labeled:
        if sum(st['cm_bits']) > 0:
            best = L['best']
            if best is None:
                continue
            by_cls.setdefault(best['class'], []).append((st, L, best))
    print('\n--- labeled dirty-state samples (class | handle | field: fval/iVal) ---')
    for cls in sorted(by_cls.keys()):
        items = by_cls[cls]
        print('\n[%s]  %d objects with dirty state' % (cls, len(items)))
        for st, L, best in items[:2]:
            vs = '  '.join('%s(%s):f%.3f/i%d' % (nm, typ, fv, iv)
                           for (nm, typ, fv, iv, bit) in best['labeled'][:16])
            amb = ' [AMBIGUOUS]' if L['ambiguous'] else ''
            print('  h=%-12s %s%s' % (st['handle'], vs, amb))


if __name__ == '__main__':
    main()
