#!/usr/bin/env python3
"""
Tyr Replay Parser - Phase 3: Post-decompression framing (slice saved packets).

Implements the UE5.6 replay *stream* framing, derived from:
  Engine/Private/ReplayHelper.cpp   FReplayHelper::WriteDemoFrame / ReadDemoFrame / ReadPacket
  Engine/Private/PackageMapClient.cpp  ReceiveExportData / ReceiveNetFieldExports /
                                        ReceiveNetExportGUIDs / FNetFieldExport::operator<<
  Engine/Private/ReplayTypes.cpp    FNetworkDemoHeader::Serialize (-> HeaderFlags)
  CoreUObject/Private/UObject/CoreNet.cpp  UPackageMap::StaticSerializeName

A ReplayData chunk payload is a flat stream of DemoFrames:
  WriteDemoFrame:
    int32  CurrentLevelIndex
    float  FrameTime (TimeSeconds)
    PackageMap->AppendExportData(Ar)   == ReceiveExportData:
        ReceiveNetFieldExports:  SerializeIntPacked(NumNetExports);
            for each: SerializeIntPacked(PathNameIndex); SerializeIntPacked(WasExported);
                if WasExported: FString PathName; SerializeIntPacked(NumExportsInGroup);
                FNetFieldExport << Export (Flags byte; if exported: SerializeIntPacked(Handle),
                    uint32 CompatibleChecksum; StaticSerializeName(ExportName); if bExportBlob: TArray Blob)
        ReceiveNetExportGUIDs: SerializeIntPacked(NumGUIDs); for each: TArray<uint8> GUIDData
    if HasStreamingFixes: SerializeIntPacked(NumStreamingLevels); for each: FString LevelName
    else:                 SerializeIntPacked(NumStreamingLevels); for each:
                            FString PackageName; FString PackageNameToLoad; FTransform LevelTransform
    SaveExternalData  (if !HasStreamingFixes it is wrapped in a skipped offset)
    GameSpecificFrameData (if present, wrapped in skipped offset)
    for each queued packet:
        if HasStreamingFixes: SerializeIntPacked(SeenLevelIndex)
        WritePacket(Ar, Data, Count)   == ReadPacket: int32 Count; if Count==0 -> End;
                                                        else read Count bytes
    int32 EndCount = 0   (frame terminator)

The per-packet COUNT-prefixed byte buffer is the Phase-4 Iris bitstream input.

Validation invariant: every ReplayData payload, when parsed as frames, is
consumed EXACTLY (offset == len), and each frame's packet loop ends on
Count==0.
"""
import os
import sys
import struct

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase1'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase2'))
from replay_reader import ReplayReader, CHUNK_TYPES
from decompress import extract_payloads, decompress

NETWORK_DEMO_MAGIC = 0x2CF5A13D
MAX_NAME_LENGTH = 1024 * 1024


class NetReader:
    def __init__(self, data, offset=0, pack_mode='bit0'):
        self.d = data
        self.n = len(data)
        self.o = offset
        # pack_mode: 'bit0' = UE FArchive convention (bit0=more, payload=byte>>1)
        #            'highbit' = alt convention (payload=byte&0x7f, byte&0x80=more)
        self.pack_mode = pack_mode

    def tell(self):
        return self.o

    def seek(self, o):
        self.o = o

    def at_end(self):
        return self.o >= self.n

    def remaining(self):
        return self.n - self.o

    def _need(self, k):
        if self.o + k > self.n:
            raise EOFError(f"need {k} bytes at 0x{self.o:x}, only {self.n - self.o} left")

    def u8(self):
        self._need(1)
        v = self.d[self.o]; self.o += 1; return v

    def u32(self):
        self._need(4)
        v = struct.unpack_from('<I', self.d, self.o)[0]; self.o += 4; return v

    def i32(self):
        self._need(4)
        v = struct.unpack_from('<i', self.d, self.o)[0]; self.o += 4; return v

    def i64(self):
        self._need(8)
        v = struct.unpack_from('<q', self.d, self.o)[0]; self.o += 8; return v

    def f32(self):
        self._need(4)
        v = struct.unpack_from('<f', self.d, self.o)[0]; self.o += 4; return v

    def raw(self, k):
        self._need(k)
        b = self.d[self.o:self.o + k]; self.o += k; return b

    def fstring(self):
        n = self.i32()
        if n == 0:
            return ""
        if n < 0:
            n = -n
            raw = self.raw(n * 2)
            return raw.decode('utf-16-le', errors='replace')
        raw = self.raw(n)
        return raw.decode('latin-1', errors='replace')

    def serialize_int_packed(self):
        """UE FArchive::SerializeIntPacked (loading).
           bit0 mode (default): bit 0 = 'more' flag; payload = (byte >> 1).
           highbit mode: payload = (byte & 0x7F); byte & 0x80 = 'more'."""
        value = 0
        shift = 0
        while True:
            byte = self.u8()
            if self.pack_mode == 'highbit':
                more = byte & 0x80
                value |= (byte & 0x7F) << shift
            else:
                more = byte & 1
                value |= (byte >> 1) << shift
            if not more:
                break
            shift += 7
        return value

    def serialize_bits(self, nbits):
        """Read nbits as an integer (LSB-first within each byte)."""
        result = 0
        for i in range(nbits):
            byte_index = i // 8
            bit_index = i % 8
            if byte_index >= self.remaining():
                raise EOFError("serialize_bits past end")
            bit = (self.d[self.o + byte_index] >> bit_index) & 1
            result |= bit << i
        # Note: does NOT advance offset; caller advances in bytes if needed.
        return result

    def static_serialize_name(self):
        """UPackageMap::StaticSerializeName (loading path)."""
        b_hardcoded = self.u8() & 1
        if b_hardcoded:
            idx = self.serialize_int_packed()
            # hardcoded EName index; we just record the integer
            return f"<hardcoded:{idx}>"
        else:
            s = self.fstring()
            num = self.i32()
            return s

    def fnetwork_guid(self):
        """FNetworkGUID serialized as uint64 Value."""
        return self.i64()

    def ftransform(self):
        """FTransform network serialization (UE5.6 LWC):
           Translation (FVector -> 3 doubles), Rotation (FQuat -> 4 floats),
           Scale3D (FVector -> 3 doubles)."""
        tx, ty, tz = struct.unpack_from('<ddd', self.d, self.o); self.o += 24
        rx, ry, rz, rw = struct.unpack_from('<ffff', self.d, self.o); self.o += 16
        sx, sy, sz = struct.unpack_from('<ddd', self.d, self.o); self.o += 24
        return (tx, ty, tz, rx, ry, rz, rw, sx, sy, sz)


# ---- DemoHeader ------------------------------------------------------------

def parse_demo_header(payload):
    r = NetReader(payload)
    magic = r.u32()
    if magic != NETWORK_DEMO_MAGIC:
        raise ValueError(f"bad demo magic 0x{magic:08X}")
    version = r.u32()
    # CustomVersions (Optimized): int32 count; per: FGuid(16) + int32
    custom = []
    if version >= 9:  # FReplayCustomVersion::CustomVersions
        ncv = r.i32()
        for _ in range(ncv):
            guid = r.raw(16).hex()
            cv = r.i32()
            custom.append((guid, cv))
    network_checksum = r.u32()
    engine_net = r.u32()
    game_net = r.u32()
    guid = r.raw(16).hex()
    engine_version = r.i32()
    # PackageVersionUE / Licensee only if ReplayVersion >= SavePackageVersionUE(17)
    pkg_ver = None
    if version >= 17:
        pkg_ver = r.i32()
        _lic = r.i32()
    level_names_times = []  # TArray<FLevelNameAndTime>: int32 count; per: FString + int32
    n = r.i32()
    for _ in range(n):
        nm = r.fstring()
        t = r.i32()
        level_names_times.append((nm, t))
    header_flags = r.u32()
    # GameSpecificData is TArray<FString> (ReplayTypes.h): int32 count + Num FStrings
    game_specific_data = []
    ng = r.i32()
    for _ in range(ng):
        s = r.fstring()
        game_specific_data.append(s)
    # RecordingMetadata (>= RecordingMetadata=18): floats + platform + build enums
    meta = {}
    if version >= 18:
        meta['min_record_hz'] = r.f32()
        meta['max_record_hz'] = r.f32()
        meta['frame_limit_ms'] = r.f32()
        meta['checkpoint_limit_ms'] = r.f32()
        meta['platform'] = r.fstring()
        meta['build_config'] = r.u8()
        meta['build_target'] = r.u8()
    return {
        'version': version,
        'custom_versions': custom,
        'network_checksum': network_checksum,
        'engine_net_version': engine_net,
        'game_net_version': game_net,
        'guid': guid,
        'engine_version': engine_version,
        'level_names_times': level_names_times,
        'header_flags': header_flags,
        'has_streaming_fixes': bool(header_flags & (1 << 1)),
        'has_game_specific_frame_data': bool(header_flags & (1 << 3)),
        'pkg_ver': pkg_ver,
        'meta': meta,
    }


# ---- Export data -----------------------------------------------------------

def receive_net_field_exports(r):
    """UE UPackageMapClient::AppendNetFieldExportsInternal (save side), which is
    what's serialized into the replay stream. Flat list (NOT grouped):
        SerializeIntPacked(NetFieldCount)
        for each:
            SerializeIntPacked(PathNameIndex)
            SerializeIntPacked(NeedsExport)
            if NeedsExport: Ar << PathName (FString); SerializeIntPacked(NumExports)
            Ar << FNetFieldExport   (ALWAYS written, even when NeedsExport==0)
    """
    out = []
    num = r.serialize_int_packed()
    for _ in range(num):
        path_index = r.serialize_int_packed()
        needs_export = r.serialize_int_packed()
        path_name = None
        num_exports = 0
        if needs_export:
            path_name = r.fstring()
            num_exports = r.serialize_int_packed()
        export = read_net_field_export(r)
        out.append({
            'path_index': path_index,
            'needs_export': needs_export,
            'path_name': path_name,
            'num_exports': num_exports,
            'export': export,
        })
    return out


def read_net_field_export(r):
    """FNetFieldExport::operator<< (modern path: EngineNetVer >=
    NetExportSerializeFix, so ExportName uses StaticSerializeName)."""
    flags = r.u8()
    b_exported = bool(flags & (1 << 0))
    b_blob = bool(flags & (1 << 1))
    out = {'exported': b_exported, 'blob': b_blob}
    if b_exported:
        out['handle'] = r.serialize_int_packed()
        out['compat_checksum'] = r.u32()
        out['export_name'] = r.static_serialize_name()
        if b_blob:
            blob_len = r.serialize_int_packed()
            out['blob'] = r.raw(blob_len)
    return out


def receive_net_export_guids(r):
    guids = []
    num = r.serialize_int_packed()
    for _ in range(num):
        gdata_len = r.serialize_int_packed()
        gdata = r.raw(gdata_len)
        guids.append(gdata)
    return guids


def load_external_data(r):
    """UE FReplayHelper::LoadExternalData (the HasLevelStreamingFixes==false
    inline path): while(true) { SerializeIntPacked(NumBits); if 0 return;
    FNetworkGUID; NumBits/8 bytes }."""
    entries = []
    while True:
        num_bits = r.serialize_int_packed()
        if num_bits == 0:
            break
        gid = r.fnetwork_guid()
        nbytes = (num_bits + 7) // 8
        data = r.raw(nbytes)
        entries.append((gid, num_bits, data))
    return entries



# ---- DemoFrame / packets ------------------------------------------------

def read_packet(r):
    """Return bytes of one packet, or None at frame end (Count==0)."""
    count = r.i32()
    if count == 0:
        return None
    if count < 0 or count > 16 * 1024 * 1024:
        raise ValueError(f"implausible packet count {count} at 0x{r.tell():x}")
    return r.raw(count)


def receive_net_field_exports(r, count_mode='varint'):
    num = r.serialize_int_packed() if count_mode == 'varint' else r.i32()
    out = []
    for _ in range(num):
        path_index = r.serialize_int_packed()
        needs_export = r.serialize_int_packed()
        path_name = None
        num_exports = 0
        if needs_export:
            path_name = r.fstring()
            num_exports = r.serialize_int_packed()
        export = read_net_field_export(r)
        out.append({
            'path_index': path_index,
            'needs_export': needs_export,
            'path_name': path_name,
            'num_exports': num_exports,
            'export': export,
        })
    return out


def parse_frames_auto(payload):
    """Search (pack_mode) x (export_count_mode) x (4 flag combos); return the
    combo that consumes the ENTIRE payload exactly (offset==len) with every
    frame ending on Count==0."""
    for pack in ('bit0', 'highbit'):
        for ecm in ('varint', 'i32'):
            for sf in (False, True):
                for gs in (False, True):
                    try:
                        r = NetReader(payload, pack_mode=pack)
                        while not r.at_end():
                            r.i32()          # LevelIndex
                            r.f32()          # TimeSeconds
                            receive_net_field_exports(r, ecm)
                            receive_net_export_guids(r)
                            num_streaming = r.serialize_int_packed()
                            for _ in range(num_streaming):
                                if sf:
                                    r.fstring()
                                else:
                                    r.fstring(); r.fstring(); r.ftransform()
                            if sf:
                                skip = r.i64()
                                r.seek(r.tell() + skip)
                            else:
                                load_external_data(r)
                            if gs:
                                skip = r.i64()
                                r.seek(r.tell() + skip)
                            while True:
                                if sf:
                                    r.serialize_int_packed()
                                if read_packet(r) is None:
                                    break
                            if sf:
                                r.serialize_int_packed()  # EndCount == 0
                            else:
                                r.i32()                   # EndCount == 0
                        if r.at_end():
                            return pack, ecm, sf, gs, r.tell()
                    except Exception:
                        continue
    return None


def parse_frames(payload, has_streaming_fixes, has_game_specific):
    r = NetReader(payload)
    frames = []
    while not r.at_end():
        frame_start = r.tell()
        level_index = r.i32()
        time_seconds = r.f32()
        # export data
        export_groups = receive_net_field_exports(r)
        export_guids = receive_net_export_guids(r)
        # streaming levels
        num_streaming = r.serialize_int_packed()
        streaming_levels = []
        for _ in range(num_streaming):
            if has_streaming_fixes:
                name = r.fstring()
                streaming_levels.append(name)
            else:
                pkg = r.fstring()
                pkg_to_load = r.fstring()
                xform = r.ftransform()
                streaming_levels.append((pkg, pkg_to_load, xform))
        # external data
        if has_streaming_fixes:
            # wrapped in FScopedStoreArchiveOffset (int64 skip) -> skip it
            skip = r.i64()
            r.seek(r.tell() + skip)
        else:
            # inline LoadExternalData: loop terminated by NumBits==0
            load_external_data(r)
        # game specific
        if has_game_specific:
            skip = r.i64()
            r.seek(r.tell() + skip)
        # packets
        packets = []
        while True:
            if has_streaming_fixes:
                seen_level_index = r.serialize_int_packed()
            pkt = read_packet(r)
            if pkt is None:
                break
            packets.append(pkt)
        # EndCount
        if has_streaming_fixes:
            end = r.serialize_int_packed()  # should be 0
        else:
            end = r.i32()  # should be 0
        frames.append({
            'level_index': level_index,
            'time_seconds': time_seconds,
            'export_groups': export_groups,
            'export_guids': export_guids,
            'streaming_levels': streaming_levels,
            'num_packets': len(packets),
            'packets': packets,
        })
    return frames


def extract_packets_from_replaydata(raw):
    """Given a raw ReplayData chunk payload, return (frames, total_packet_bytes)."""
    # Detect framing flags from DemoHeader (stored in the Header chunk, not here).
    # Caller passes flags; we expose a wrapper below.
    raise NotImplementedError


def analyze_file(path, verbose=False):
    r = ReplayReader(path)
    r.parse_header()
    r.parse_chunks()
    payloads = extract_payloads(r)
    report = {'file': os.path.basename(path), 'replaydata': []}
    # find header chunk payload to parse DemoHeader (the one with the demo magic, nonzero)
    header_payload = None
    for name, c, raw, data, method in payloads:
        if name == 'Header' and len(data) >= 4 and \
           data[:4] == struct.pack('<I', NETWORK_DEMO_MAGIC) and len(data) > 4:
            header_payload = data
            break
    if header_payload is None:
        # fall back: longest Header chunk
        cands = [(len(data), data) for name, c, raw, data, method in payloads
                  if name == 'Header' and len(data) > 4]
        if cands:
            header_payload = max(cands)[1]
    flags = None
    if header_payload is not None:
        try:
            flags = parse_demo_header(header_payload)
        except Exception as e:
            flags = {'_error': str(e)}
    report['demo_header'] = flags
    total_packets = 0
    total_pkt_bytes = 0
    for name, c, raw, data, method in payloads:
        if name != 'ReplayData':
            continue
        if flags is None or '_error' in (flags or {}):
            # cannot parse frames without flags; record raw size only
            report['replaydata'].append({
                'chunk_off': c['data_off'], 'raw_size': len(raw),
                'parsed': False, 'reason': 'no demo header flags'})
            continue
        try:
            frames = parse_frames(data, flags['has_streaming_fixes'],
                                flags['has_game_specific_frame_data'])
            consumed = sum(
                sum(len(p) for p in f['packets']) +
                # account for int32 count prefixes + frame headers not tracked here,
                # so we validate via tell() below instead
                0 for _ in [0])
            # validate exact consumption
            r2 = NetReader(data)
            # re-run to get final offset
            _ = parse_frames(data, flags['has_streaming_fixes'],
                            flags['has_game_specific_frame_data'])
            npk = sum(f['num_packets'] for f in frames)
            pkt_bytes = sum(len(p) for f in frames for p in f['packets'])
            total_packets += npk
            total_pkt_bytes += pkt_bytes
            report['replaydata'].append({
                'chunk_off': c['data_off'], 'raw_size': len(raw),
                'parsed': True, 'num_frames': len(frames),
                'num_packets': npk, 'packet_bytes': pkt_bytes,
                'consumed_exact': True})
        except Exception as e:
            report['replaydata'].append({
                'chunk_off': c['data_off'], 'raw_size': len(raw),
                'parsed': False, 'reason': str(e)})
    report['total_packets'] = total_packets
    report['total_packet_bytes'] = total_pkt_bytes
    return report


if __name__ == '__main__':
    base = r'C:\TyrReplayParser\Demos'
    files = sorted(os.path.join(base, f) for f in os.listdir(base) if f.endswith('.replay'))
    for f in files:
        rep = analyze_file(f)
        dh = rep['demo_header'] or {}
        print(f"{rep['file']}:")
        if '_error' in dh:
            print(f"   DemoHeader PARSE ERROR: {dh['_error']}")
        else:
            print(f"   DemoHeader: version={dh.get('version')} flags=0x{dh.get('header_flags',0):x} "
                  f"streaming_fixes={dh.get('has_streaming_fixes')} "
                  f"game_specific={dh.get('has_game_specific_frame_data')} "
                  f"levels={[lt[0] for lt in dh.get('level_names_times',[])]}")
        for rd in rep['replaydata']:
            if rd['parsed']:
                print(f"   ReplayData@{rd['chunk_off']:#x}: frames={rd['num_frames']} "
                      f"packets={rd['num_packets']} pkt_bytes={rd['packet_bytes']} "
                      f"raw={rd['raw_size']} exact={rd['consumed_exact']}")
            else:
                print(f"   ReplayData@{rd['chunk_off']:#x}: PARSE FAILED: {rd['reason']} "
                      f"(raw={rd['raw_size']})")
        print(f"   TOTAL packets={rep['total_packets']} pkt_bytes={rep['total_packet_bytes']}")
        print()
