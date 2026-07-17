"""Phase 7 / Step 1: authoritative changemask-bit -> field-name map from the replay's
own FNetFieldExport table.

KEY DISCOVERY (empirically verified): each FNetFieldExport record carries a `handle`
which is exactly the changemask BIT INDEX it replicates. So (handle, name) pairs give
a direct, authoritative bit->name mapping for every field the replay exports.

The export section format (per group):
    SerializeIntPacked(pni)            # group index
    SerializeIntPacked(was_exported)   # 0/1
    if was_exported:
        FString path_name              # class path (sometimes tokenized -> no inline str)
        SerializeIntPacked(nec)        # changemask BIT capacity (NOT record count!)
        <nec-or-fewer FNetFieldExport records>
    FNetFieldExport record:
        uint8 flags  (bit0=bExported, bit1=bExportBlob, bit2+=no-payload flags)
        if bExported: SerializeIntPacked(handle)=BIT INDEX; u32 checksum;
                      bHardcoded:1byte; if hardcoded: SerializeIntPacked(nameIndex)
                                       else: FString name + int32 number
        if bExportBlob: SerializeIntPacked(len) + len bytes

Groups are written back-to-back; `nec` over-counts (only exported records are
written), so we bound each group's records by the next group's start. We anchor on
the first group's inline path (WorldSettings is always group 0) and chain forward.
"""
import sys, os, struct
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase1'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase2'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase3'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase5'))
from replay_reader import ReplayReader
from decompress import extract_payloads
import frame_parser as fp


def _read_group_header(np):
    """Read pni + was_exported. Returns (pni, we, err). Does not read path/nec."""
    pni = np.sip(); we = np.sip()
    return pni, we, np.err


def _parse_records(np, max_records, nec):
    """Parse FNetFieldExport records from np.o. Stops after max_records (nec+slack)
    or on error/drift. Returns list of (handle, name_or_None)."""
    recs = []
    for _ in range(max_records):
        if np.err or np.o + 1 > np.n:
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
                nm = np.fstring(); np.o += 4  # + int32 Number
        if flags & 2:  # bExportBlob
            ln = np.sip()
            if np.err or ln > (1 << 24) or np.o + ln > np.n:
                np.err = True; break
            np.o += ln
        # drift guard: a real changemask bit index is small (< a few hundred). A
        # wildly large handle means we've run into the next group's blob.
        if handle is not None and handle > 400:
            break
        recs.append((handle, nm))
    return recs


def _find_path_anchors(d, start, end):
    """Find class-path FStrings (int32 len + ascii with '/' and '.'). These mark
    group starts. Limited to `end` (caller passes the export-section bound)."""
    out = []
    o = start
    while o + 4 <= end:
        ln = struct.unpack_from('<i', d, o)[0]
        if 4 < ln < 400:
            s = d[o + 4: o + 4 + ln]
            try:
                s = s.decode('ascii')
            except Exception:
                o += 1; continue
            if '/' in s and '.' in s and s.startswith('/'):
                out.append((o, s.replace('\x00', ''), 4 + ln))
        o += 1
    return out


def extract_field_map(replay_path, export_bound=60000):
    """Return dict: class_path -> {nec, fields:[(bit_index, name_or_None)]}.
    Anchors on inline class-path FStrings within the export section; bounds each
    group's records by the next anchor; trims drift via the handle>nec guard."""
    r = ReplayReader(replay_path); r.parse_header(); r.parse_chunks()
    payload = None
    for name, c, raw, d, method in extract_payloads(r):
        if name == 'ReplayData':
            payload = d; break
    frames, ok, params = fp.parse_replaydata(payload)
    f0 = frames[0]; raw = payload[f0['start']:f0['end']]
    np = fp.NetReader(raw); np.o = params['export_start']
    num = np.sip()
    if np.err or num <= 0 or num > 5000:
        return {}
    scan_end = min(np.o + export_bound, np.n)
    anchors = _find_path_anchors(np.d, np.o, scan_end)
    result = {}
    for i, (poff, pstr, plen) in enumerate(anchors):
        # nec is SerializeIntPacked in the 1-2 bytes immediately before the 4-byte
        # path-length int32. Try backing up past the length first.
        nec = 0
        for back in (1, 2):
            t2 = fp.NetReader(np.d); t2.o = poff - 4 - back; t2.n = np.n
            v = t2.sip()
            if not t2.err and 1 <= v <= 256:
                nec = v; break
        end_o = anchors[i + 1][0] if i + 1 < len(anchors) else scan_end
        rec_np = fp.NetReader(np.d); rec_np.o = poff + plen; rec_np.n = np.n
        recs = _parse_records(rec_np, 400, nec)
        result[pstr] = {'nec': nec, 'fields': recs}
    return result


def main():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    f = os.path.join(repo, 'Demos', 'TyrReplay1.replay')
    m = extract_field_map(f)
    print('groups with field maps: %d' % len(m))
    # count resolved (named) bits across all classes
    total_named = sum(1 for g in m.values() for h, nm in g['fields']
                      if nm and not nm.startswith('HARD#'))
    total_hard = sum(1 for g in m.values() for h, nm in g['fields']
                     if nm and nm.startswith('HARD#'))
    print('named (inline) field bits: %d   HARD#N field bits: %d' % (total_named, total_hard))
    for p, g in m.items():
        if any(k in p for k in ('LobbyPlayerRecord', 'TyrPlayerState',
                                'CaptureZone', 'WorldSettings')):
            print('\n[%s] nec=%d' % (p, g['nec']))
            for h, nm in g['fields']:
                if h is not None or nm is not None:
                    print('  bit %s = %s' % (h, (nm or '').replace('\x00', '')))


if __name__ == '__main__':
    main()
