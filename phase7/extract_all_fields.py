"""Step 1: extract the FULL per-class FNetFieldExport field list from the replay's
frame-0 export (authoritative, replay-provided field names + FName-index handles).

Extends phase5.object_identity.extract_export_groups: that parser only captured the
FIRST field's name (first_field) and skipped the rest. Here we loop over all `nec`
FNetFieldExport records per group, recovering every field's name (inline string or
HARD#N FName-index) or blob-only marker. This is the authoritative property->name
map the replay itself carries; resolving HARD#N needs the engine FName table (exe).
"""
import sys, os, struct
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase1'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase2'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase3'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase5'))
from replay_reader import ReplayReader
from decompress import extract_payloads
import frame_parser as fp


def extract_all_fields(replay_path):
    r = ReplayReader(replay_path); r.parse_header(); r.parse_chunks()
    payload = None
    for name, c, raw, d, method in extract_payloads(r):
        if name == 'ReplayData':
            payload = d; break
    frames, ok, params = fp.parse_replaydata(payload)
    f0 = frames[0]; raw = payload[f0['start']:f0['end']]
    np = fp.NetReader(raw); np.o = params['export_start']
    num = np.sip()
    groups = []
    for _ in range(num):
        pni = np.sip(); we = np.sip()
        if np.err:
            break
        path = None; nec = 0; fields = []
        if we:
            path = np.fstring(); nec = np.sip()
        for _j in range(nec if we else 0):
            if np.err:
                break
            flags = np.u8()
            if flags is None or np.err:
                break
            nm = None
            if flags & 1:  # bExported
                handle = np.sip(); np.o += 4  # skip u32 checksum
                bhard = np.u8()
                if bhard:
                    nm = 'HARD#%d' % np.sip()
                else:
                    nm = np.fstring(); np.o += 4
            if flags & 2:  # bExportBlob
                ln = np.sip()
                if np.err or ln > (1 << 24) or np.o + ln > np.n:
                    np.err = True; break
                np.o += ln
            fields.append(nm)
        if np.err:
            break
        groups.append({'pni': pni, 'path': path, 'nec': nec, 'fields': fields})
    return groups, np.err


def main():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    f = os.path.join(repo, 'Demos', 'TyrReplay1.replay')
    groups, err = extract_all_fields(f)
    print('=== frame-0 FNetFieldExport (ALL fields) ===')
    print('groups parsed: %d  (reader_err=%s)' % (len(groups), err))
    for g in groups:
        cs = (g['path'] or '').replace('\x00', '').split('/')[-1]
        if any(k in cs for k in ('LobbyPlayerRecord', 'TyrPlayerState', 'TyrGameState',
                                  'CaptureZone', 'PlayerRecord', 'WorldSettings')):
            print('\n[%s] nec=%d records=%d' % (cs, g['nec'], len(g['fields'])))
            print('  fields:', g['fields'])
    # summary
    str_n = sum(1 for g in groups for x in g['fields'] if x and not x.startswith('HARD#'))
    hard_n = sum(1 for g in groups for x in g['fields'] if x and x.startswith('HARD#'))
    print('\n--- summary ---')
    print('total inline-string fields: %d' % str_n)
    print('total HARD#N (FName-index) fields: %d' % hard_n)
    print('groups with >=1 inline name: %d / %d' % (
        sum(1 for g in groups if any(x and not x.startswith('HARD#') for x in g['fields'])),
        len(groups)))


if __name__ == '__main__':
    main()
