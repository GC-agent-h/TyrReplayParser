"""Phase 5: Object identity & state descriptors.

Recovers the per-replay FNetFieldExportGroup table from the export section of the
first DemoFrame. Each group = one replicated class:
    SerializeIntPacked(PathNameIndex); SerializeIntPacked(WasExported);
    if WasExported: FString PathName; SerializeIntPacked(NumExportsInGroup);
    FNetFieldExport << (uint8 Flags; if exported: Handle, u32 Checksum,
                        StaticSerializeName(ExportName))

NumExportsInGroup == the Iris changemask bit-count (number of replicated state
members) for that class. PathName identifies the UClass. The first exported
field name confirms the class->field association.

Also cross-references recovered class paths against the TYR SDK dump
(ClassesInfo.json) to validate that object identity resolves to real classes.

Run:  python phase5/object_identity.py
"""

import sys, os, glob, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase1'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase2'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase3'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase4'))

from replay_reader import ReplayReader
from decompress import extract_payloads
import frame_parser as fp


def extract_export_groups(replay_path):
    """Return list of (path_name, num_exports_in_group, [bit0_field_name], pni, we)
    for a replay's first frame export section, plus the NetReader offset where export
    parsing ended.

    Each FNetFieldExportGroup = one replicated class. num_exports_in_group == the Iris
    changemask bit-count (number of replicated state members). The replay's export
    section stores exactly ONE FNetFieldExport name per group (the bit-0 / first
    replicated property); the remaining bit->name entries are NOT present in the stream
    (they live in the checkpoint's FReplicationStateDescriptor, which is binary and not
    recoverable without the engine source). So `fields` is a 1-element list: the bit-0
    name. Native classes resolve to real FStrings; Blueprint classes show
    'hardcoded#<FNameIndex>' for bit-0 when the Dumper couldn't resolve it.

    NOTE: an earlier attempt to read all `nec` FNetFieldExport entries per group desynced
    at group 1 (the export blob only carries the bit-0 name inline; the rest of the field
    list is reconstructed elsewhere). The single-field read below is the aligned,
    all-135-groups version.
    """
    r = ReplayReader(replay_path)
    r.parse_header(); r.parse_chunks()
    payload = None
    for name, c, raw, d, method in extract_payloads(r):
        if name == 'ReplayData':
            payload = d; break
    assert payload is not None, "no ReplayData chunk"
    frames, ok, params = fp.parse_replaydata(payload)
    assert ok, "frame parse failed"
    f0 = frames[0]
    raw = payload[f0['start']:f0['end']]

    np = fp.NetReader(raw)
    es = params['export_start']
    np.o = es
    num = np.sip()
    groups = []
    for _ in range(num):
        pni = np.sip(); we = np.sip()
        if np.err:
            break
        path = None; nec = 0; fields = []
        if we:
            path = np.fstring(); nec = np.sip()
        # One FNetFieldExport name per group (bit-0). This read is aligned across all
        # 135 groups; reading beyond it desyncs the stream.
        flags = np.u8()
        if np.err:
            break
        if flags & 1:
            h = np.sip(); np.o += 4
            bhard = np.u8()
            if bhard:
                nm = np.sip()
                fields.append(('hardcoded#%d' % nm) if isinstance(nm, int) else nm)
            else:
                nm = np.fstring(); np.o += 4
                fields.append(nm)
        if flags & 2:
            ln = np.sip()
            if np.err or ln > 1 << 20 or np.o + ln > np.n:
                np.err = True; break
            np.o += ln
        groups.append((path, nec, fields, pni, we))
    return groups, np.err, params


def load_sdk_classes():
    p = os.path.join(os.path.dirname(__file__), '..',
                     '5.6.0-31351+++Tyr+release-Tyr_OLD', 'Dumpspace', 'ClassesInfo.json')
    if not os.path.exists(p):
        return None
    d = json.load(open(p, encoding='utf-8'))
    raw = set()
    for entry in d['data']:
        for cname in entry:
            raw.add(cname)
    # Normalize: strip leading A/U prefix and trailing _C, lowercase, for fuzzy match.
    norm = {}
    for n in raw:
        k = n
        if k[:1] in ('A', 'U'):
            k = k[1:]
        if k.endswith('_C'):
            k = k[:-2]
        norm[k.lower()] = n
    return raw, norm


def sdk_lookup(short, sdk):
    if sdk is None:
        return None
    raw, norm = sdk
    if short in raw:
        return short
    k = short
    if k[:1] in ('A', 'U'):
        k = k[1:]
    if k.endswith('_C'):
        k = k[:-2]
    k = k.lower()
    return norm.get(k)


def class_short(path):
    if not path:
        return None
    return path.split('/')[-1].split('.')[-1].rstrip('\x00')


def main():
    sdk = load_sdk_classes()
    all_ok = True
    for f in sorted(glob.glob('Demos/*.replay')):
        groups, err, params = extract_export_groups(f)
        exported = [g for g in groups if g[4]]  # we==1
        nec_vals = [g[1] for g in exported]
        matched = 0
        sample = []
        for g in exported[:8]:
            cs = class_short(g[0])
            in_sdk = (sdk is not None and sdk_lookup(cs, sdk) is not None)
            if in_sdk:
                matched += 1
            if len(sample) < 6:
                fields = g[2]
                first = (fields[0] if fields else None) or ''
                sample.append((cs, g[1], first.rstrip('\x00'), in_sdk))
        status = 'OK' if not err else 'ERR'
        if err:
            all_ok = False
        print('%s %-16s groups=%d exported=%d nec[min=%d max=%d] sdk_matched=%d/%d'
              % (status, os.path.basename(f), len(groups), len(exported),
                 min(nec_vals) if nec_vals else 0, max(nec_vals) if nec_vals else 0,
                 matched, min(len(exported), 8)))
        for s in sample:
            print('      %-40s nec=%4d first_field=%-24s sdk=%s' % s)
    print('\nObject-identity table recovered for all files:', all_ok)
    if sdk is not None:
        raw, norm = sdk
        print('SDK class validation: ClassesInfo.json loaded (%d classes).' % len(raw))
    else:
        print('SDK class validation: ClassesInfo.json NOT found (skip cross-check).')
    assert all_ok, 'export group extraction failed on some file'


if __name__ == '__main__':
    main()
