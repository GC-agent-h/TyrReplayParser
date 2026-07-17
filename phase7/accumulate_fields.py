"""Step 1 (revised): accumulate the COMPLETE per-class FNetFieldExport field list by
scanning EVERY frame's export section across the replay, merging field-name records
per group. UE writes a fresh FNetFieldExport record (with the name) the first time a
field changes; progressive frames thus complete the table that frame-0 alone leaves
partial (WorldSettings shows 10/22 in frame 0).

We reuse the proven per-record format (flags byte; bExported -> handle + u32 checksum
+ name; bExportBlob -> len + blob) and merge by group path.
"""
import sys, os, struct
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase1'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase2'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase3'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase5'))
from replay_reader import ReplayReader
from decompress import extract_payloads
import frame_parser as fp


def parse_export_section(np):
    """Parse one export section starting at np.o. Return (groups, err) where groups
    is a list of (path, [(handle, name_or_None), ...]).

    Each group: pni=sip, we=sip, [if we: path=fstring, nec=sip], then `nec`
    FNetFieldExport records. Per record: flags=u8 (bit0=bExported, bit1=bExportBlob,
    bit2+=other flags with no payload); if bExported: handle=sip (== changemask bit
    index), skip u32 checksum, bhard=u8, [if bhard: NameIndex=sip else
    ExportName=fstring+int32]; if bExportBlob: ln=sip, skip ln bytes.

    NOTE: `nec` is the changemask BIT-capacity, not the record count. The stream
    writes only the dynamically-exported records (a subset). We therefore read
    exactly `nec` record slots but stop early if we run into the next group's
    header bytes (detected by an implausible run).
    """
    num = np.sip()
    if np.err or num <= 0 or num > 5000:
        return [], True
    out = []
    for _ in range(num):
        pni = np.sip(); we = np.sip()
        if np.err:
            return out, False
        path = None; nec = 0; fields = []
        if we:
            path = np.fstring(); nec = np.sip()
        for _j in range(nec if we else 0):
            if np.err:
                break
            flags = np.u8()
            if flags is None or np.err:
                break
            handle = None; nm = None
            if flags & 1:  # bExported
                handle = np.sip()  # changemask bit index
                np.o += 4  # u32 CompatibleChecksum
                bhard = np.u8()
                if bhard:
                    nm = 'HARD#%d' % np.sip()
                else:
                    nm = np.fstring(); np.o += 4  # + trailing int32 Number
            if flags & 2:  # bExportBlob
                ln = np.sip()
                if np.err or ln > (1 << 24) or np.o + ln > np.n:
                    np.err = True; break
                np.o += ln
            fields.append((handle, nm))
        if np.err:
            break
        out.append((path, fields))
    return out, False


def accumulate(replay_path, max_frames=None):
    r = ReplayReader(replay_path); r.parse_header(); r.parse_chunks()
    payload = None
    for name, c, raw, d, method in extract_payloads(r):
        if name == 'ReplayData':
            payload = d; break
    frames, ok, params = fp.parse_replaydata(payload)
    if max_frames:
        frames = frames[:max_frames]
    merged = {}  # path -> list of field names (merged, order preserved)
    seen_groups = 0
    for fr in frames:
        raw = payload[fr['start']:fr['end']]
        np = fp.NetReader(raw); np.o = params['export_start']
        if np.o >= np.n:
            continue
        groups, err = parse_export_section(np)
        if err or not groups:
            continue
        seen_groups += 1
        for path, fields in groups:
            if path is None:
                continue
            p = path.replace('\x00', '')
            if p not in merged:
                merged[p] = list(fields)
            else:
                # merge: fill None slots with names from this frame where present
                cur = merged[p]
                for i, nm in enumerate(fields):
                    if i < len(cur) and cur[i] is None and nm is not None:
                        cur[i] = nm
                    elif i >= len(cur):
                        cur.append(nm)
    return merged, seen_groups


def main():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    f = os.path.join(repo, 'Demos', 'TyrReplay1.replay')
    merged, seen = accumulate(f)
    print('frames-with-exports scanned: %d' % seen)
    print('groups merged: %d' % len(merged))
    # WorldSettings check: expect nec=22 fully named
    for p in merged:
        if 'WorldSettings' in p:
            print('\n[WorldSettings] nec=%d named=%d' % (
                len([x for x in merged[p] if x is not None]),
                len([x for x in merged[p] if x is not None])))
            print('  fields:', merged[p])
    # player-relevant
    for p in sorted(merged):
        if any(k in p for k in ('LobbyPlayerRecord', 'TyrPlayerState', 'TyrGameState',
                                 'CaptureZone', 'PlayerRecord')):
            fs = merged[p]
            named = [x for x in fs if x]
            print('\n[%s] records=%d named=%d' % (p, len(fs), len(named)))
            print('  fields:', fs)


if __name__ == '__main__':
    main()
