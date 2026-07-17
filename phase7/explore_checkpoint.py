"""Step 1 exploration: extract the per-class FNetFieldExport property list from the
replay's frame-0 export (authoritative, replay-provided field names), and report how
many are inline-string vs HARD#N (FName-index) vs blob-only. This quantifies exactly
how much of the bit->name map is directly recoverable vs needs the exe FName table.
"""
import sys, os, struct
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase1'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase2'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase3'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase5'))

from replay_reader import ReplayReader
from decompress import extract_payloads
import frame_parser as fp


def parse_frame0_exports(replay_path):
    r = ReplayReader(replay_path); r.parse_header(); r.parse_chunks()
    payload = None
    for name, c, raw, d, method in extract_payloads(r):
        if name == 'ReplayData':
            payload = d; break
    frames, ok, params = fp.parse_replaydata(payload)
    f0 = frames[0]; raw = payload[f0['start']:f0['end']]
    np = fp.NetReader(raw); np.o = params['export_start']
    num = np.sip()
    print('[dbg] payload_len=%d num_groups=%d err=%s' % (len(payload), num, np.err), file=sys.stderr)
    groups = []
    for i in range(num):
        pni = np.sip(); we = np.sip()
        if i == 0:
            print('[dbg] grp0 pni=%s we=%s err=%s o=%d' % (pni, we, np.err, np.o), file=sys.stderr)
        if np.err: break
        path = None; nec = 0; flds = []
        if we:
            path = np.fstring(); nec = np.sip()
        for j in range(nec if we else 0):
            fl = np.u8()
            nm = None
            if fl & 1:
                np.sip(); np.o += 4; bh = np.u8()
                if bh:
                    nm = 'HARD#%d' % np.sip()
                else:
                    nm = np.fstring(); np.o += 4
            if fl & 2:
                ln = np.sip(); np.o += ln
            if np.err: break
            flds.append(nm)
        if np.err: break
        groups.append({'pni': pni, 'path': path, 'nec': nec, 'fields': flds})
    print('[dbg] groups built: %d' % len(groups), file=sys.stderr)
    return groups


def main():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    f = os.path.join(repo, 'Demos', 'TyrReplay1.replay')
    groups = parse_frame0_exports(f)
    print('=== frame-0 FNetFieldExport per class (%s) ===' % os.path.basename(f))
    print('total groups: %d' % len(groups))
    str_total = hard_total = blob_only = 0
    for g in groups:
        named = [x for x in g['fields'] if x and not x.startswith('HARD#')]
        hard = [x for x in g['fields'] if x and x.startswith('HARD#')]
        str_total += len(named); hard_total += len(hard)
        if not named and not hard:
            blob_only += 1
        cs = (g['path'] or '').split('/')[-1].split('.')[-1]
        # focus on player-relevant classes
        if any(k in cs for k in ('LobbyPlayerRecord', 'TyrPlayerState', 'TyrGameState',
                                  'CaptureZone', 'PlayerRecord')):
            print('\n[%s] nec=%d records=%d' % (cs, g['nec'], len(g['fields'])))
            print('   fields:', g['fields'])
    print('\n--- summary ---')
    print('groups with >=1 inline string name: %d' % sum(1 for g in groups
          if any(x and not x.startswith('HARD#') for x in g['fields'])))
    print('total inline-string fields: %d' % str_total)
    print('total HARD#N (FName-index) fields: %d' % hard_total)
    print('groups with ONLY blob/empty records: %d' % blob_only)


if __name__ == '__main__':
    main()
