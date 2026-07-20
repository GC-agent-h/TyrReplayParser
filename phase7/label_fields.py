"""Best-effort field labeling for decoded classes (next-steps Step 2, option c).

The Tyr replay stream carries only bit-0 field names per class (proven: export groups
store exactly ONE inline FNetFieldExport name; full bit->name lives in engine
FReplicationStateDescriptor / StringTokenStore, both unrecoverable). The Dumper-7
GObjects dump lists each class's PROPERTIES in UPROPERTY order. UE builds the Iris
replication state from that same UPROPERTY order, so bit i ~= Dumper property i is a
reasonable POSITIONAL HYPOTHESIS for native/replicated classes.

This module:
  - parses Dumper per-class ordered property lists
  - maps replay cm_size -> class name (from phase5 class_by_cm)
  - emits bit->name labeling: bit i -> Dumper property i, flagged UNVERIFIED
Native classes we actually decode (cm1, cm12, cm83) get what the Dumper provides.
ClassNetCache classes resolve via their PARENT class's replicated props.

OUTPUT IS A HYPOTHESIS, not byte-verified. Use for eyeballing, not ground truth.
"""
import re, json, os

DUMPER = r'C:/TyrReplayParser/5.6.0-31351+++Tyr+release-Tyr_OLD/GObjects-Dump-WithProperties.txt'

_CLASS_RE = re.compile(r'^\[[0-9A-F]+\] \{0x[0-9a-f]+\}\s+Class\s+(\S+)\s*$')
# property lines share the [hex] {0x...} prefix, then "<TypeProperty> <Name>"
_PROP_RE = re.compile(r'^\[[0-9A-F]+\] \{0x[0-9a-f]+\}\s+(\w+Property)\s+(\w+)\s*$')


def parse_dumper(path=DUMPER):
    """Return {class_fullname: [prop_name, ...]} in file order."""
    out = {}
    cur = None
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            m = _CLASS_RE.match(line.rstrip('\n'))
            if m:
                cur = m.group(1)
                out.setdefault(cur, [])
                continue
            if cur is not None:
                p = _PROP_RE.match(line.rstrip('\n'))
                if p:
                    out[cur].append(p.group(2))
    return out


def class_props(classname, dumper):
    """Find property list for a class, trying exact then parent (strip _ClassNetCache / module)."""
    if classname in dumper:
        return dumper[classname]
    # ClassNetCache -> parent class (remove _ClassNetCache suffix)
    base = re.sub(r'_ClassNetCache$', '', classname)
    if base in dumper:
        return dumper[base]
    # try with common module prefixes (Tyr., PsRealVehiclePlugin., etc.)
    short = base.split('.')[-1]
    for cand in (short, 'Tyr.' + short, 'Tyr.Tyr' + short):
        if cand in dumper:
            return dumper[cand]
    # last resort: any key whose final segment matches short
    for k, v in dumper.items():
        if k.split('.')[-1] == short:
            return v
    return None


def label_class(cm_key, classname, dumper):
    """Return {cm, class, bit0: name_or_None, fields: [(bit, prop_name_or_'?')], verified: False}."""
    props = class_props(classname, dumper)
    if props is None:
        return {'cm': cm_key, 'class': classname, 'fields': [], 'verified': False,
                'note': 'no Dumper entry'}
    # bit0 is the replay-exported name if known; here we just align positionally
    return {'cm': cm_key, 'class': classname, 'field_count': len(props),
            'fields': [(i, p) for i, p in enumerate(props)],
            'verified': False,
            'note': 'positional hypothesis: bit i ~= Dumper property i'}


if __name__ == '__main__':
    dumper = parse_dumper()
    # load replay inventory
    d = json.load(open('out/TyrReplay4.json'))
    cbm = d['phases']['phase5_class_inventory']['class_by_cm']
    # cm1/cm83/cm12 are the decoded classes
    targets = {'1': cbm.get('1', []), '12': cbm.get('12', []), '83': cbm.get('83', [])}
    result = {'verified': False, 'classes': []}
    for cm, names in targets.items():
        if not names:
            result['classes'].append({'cm': cm, 'note': 'class not recovered'})
            continue
        for cn in names:
            result['classes'].append(label_class(cm, cn, dumper))
    print(json.dumps(result, indent=2))
