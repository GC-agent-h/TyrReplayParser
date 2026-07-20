"""Extract the FULL ordered bit->name field table from the replay's frame-0 export
section. Format taken from engine source (verified against TyrReplay4):

  UPackageMapClient::AppendNetFieldExports (PackageMapClient.cpp:1737)
    SerializeIntPacked(NetFieldCount)            // total field exports
    for each FieldExport (uint64 = PathNameIndex<<32 | Handle):
        SerializeIntPacked(PathNameIndex)
        SerializeIntPacked(NeedsExport)          // =1 if path not yet sent
        if NeedsExport:
            Ar << PathName                        // FString
            SerializeIntPacked(NumExports)        // group field count (nec)
        Ar << NetFieldExports[Handle]             // ONE FNetFieldExport (PackageMapClient.cpp:4310)
            uint8 Flags (bExported=bit0, bExportBlob=bit1)
            if bExported:
                SerializeIntPacked(Handle)
                uint32 CompatibleChecksum
                StaticSerializeName (CoreNet.cpp:299): uint8 bHardcoded
                    if bHardcoded: SerializeIntPacked(NameIndex) -> EName(NameIndex)
                    else:           FString + int32 Number
                if bExportBlob: TArray<uint8> Blob (i32 len + bytes)

So it's a FLAT list of field exports. A new group starts when NeedsExport=1
(path + nec precede the field). Consecutive we=0 fields belong to the current
group. nec = total bits for that class; field order = bit order.
Hardcoded name indices resolve via UnrealNames.inl (EName enum).
"""
import sys, re, json
sys.path.insert(0, 'phase1'); sys.path.insert(0, 'phase2')
sys.path.insert(0, 'phase3'); sys.path.insert(0, 'phase5')
import replay_reader as rr, decompress as dc, frame_parser as fp

UNREAL_NAMES = {}
for ln in open(r'C:/UnrealEngine/Engine/Source/Runtime/Core/Public/UObject/UnrealNames.inl',
               encoding='utf-8', errors='replace'):
    m = re.search(r'REGISTER_NAME\s*\(\s*(\d+)\s*,\s*(\w+)\s*\)', ln)
    if m:
        UNREAL_NAMES[int(m.group(1))] = m.group(2)


def parse_export_groups(replay_path):
    r = rr.ReplayReader(replay_path); r.parse_header(); r.parse_chunks()
    payloads = dc.extract_payloads(r)
    payload = next(d for name, c, raw, d, method in payloads if name == 'ReplayData')
    frames, ok, params = fp.parse_replaydata(payload)
    f0 = frames[0]
    raw = payload[f0['start']:f0['end']]
    np = fp.NetReader(raw)
    np.seek(params['export_start'])
    count = np.sip()
    groups = []          # list of (path, path_index, nec, [field_names])
    cur = None
    for k in range(count):
        pni = np.sip()
        we = np.sip()
        if we:
            path = np.fstring()
            nec = np.sip()
            cur = {'path': path, 'pni': pni, 'nec': nec, 'fields': []}
            groups.append(cur)
        # read ONE FNetFieldExport
        flags = np.u8()
        if flags & 1:  # bExported
            np.sip()             # Handle
            np.i32()            # CompatibleChecksum
            bhard = np.u8()     # bHardcoded (byte-aligned archive)
            if bhard:
                idx = np.sip()  # NameIndex
                name = UNREAL_NAMES.get(idx, 'EName#%d' % idx)
            else:
                s = np.fstring()
                if s.endswith('\x00'):
                    s = s[:-1]
                num = np.i32()  # InNumber
                name = s + (str(num) if num else '')
            if flags & 2:  # bExportBlob
                blen = np.i32()
                np.seek(np.o + blen)
        else:
            name = None
        if cur is not None:
            cur['fields'].append(name)
    return groups, np.err, params


if __name__ == '__main__':
    groups, err, params = parse_export_groups('Demos/TyrReplay4.replay')
    print('err:', err, 'groups:', len(groups))
    out = [{'path': g['path'], 'pni': g['pni'], 'nec': g['nec'], 'fields': g['fields']} for g in groups]
    json.dump(out, open('out/export_field_table.json', 'w'), indent=1)
    wanted = ('TyrPlayerState', 'TyrGameState', 'WorldSettings', 'TyrTeamPublicInfo', 'BP_LobbyPlayerRecord')
    for g in out:
        if g['path'] and any(w in g['path'] for w in wanted):
            print('\n', g['path'], 'nec=%d' % g['nec'])
            for i, f in enumerate(g['fields']):
                print('  bit%-3d %s' % (i, f))
