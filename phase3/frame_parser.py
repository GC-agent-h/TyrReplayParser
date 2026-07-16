"""Full ReplayData frame parser (byte-aligned, bit0 varint per FArchive::SerializeIntPacked).

Frame format traced from ReplayHelper.cpp / PackageMapClient.cpp (UE5.6):
  [i32 LevelIndex][f32 TimeSeconds]
  ReceiveExportData:
     ReceiveNetFieldExports: SerializeIntPacked(NumNetExports);
        per export: SerializeIntPacked(PathNameIndex), SerializeIntPacked(WasExported);
           if WasExported: FString PathName; SerializeIntPacked(NumExportsInGroup)
           ALWAYS Ar << FNetFieldExport
     ReceiveNetExportGUIDs: SerializeIntPacked(NumGUIDs); per: TArray<uint8> GUIDData (int32 len + bytes)
  streaming levels (always; format depends on HasStreamingFixes)
  external data / game-specific / packets / EndCount  (per ReadDemoFrame)
"""
import struct

class NetReader:
    def __init__(self, data):
        self.d = data; self.o = 0; self.n = len(data); self.err = False
    def tell(self): return self.o
    def seek(self, o): self.o = max(0, min(o, self.n))
    def u8(self):
        if self.o+1 > self.n: self.err=True; return 0
        v = self.d[self.o]; self.o+=1; return v
    def i32(self):
        if self.o+4 > self.n: self.err=True; return 0
        v = struct.unpack_from('<i', self.d, self.o)[0]; self.o+=4; return v
    def f32(self):
        if self.o+4 > self.n: self.err=True; return 0.0
        v = struct.unpack_from('<f', self.d, self.o)[0]; self.o+=4; return v
    def sip(self):  # FArchive::SerializeIntPacked (uint32): bit0=more, payload=byte>>1
        val=0; cnt=0
        while True:
            if self.o >= self.n: self.err=True; return 0
            b = self.d[self.o]; self.o+=1
            val += (b>>1) << (7*cnt); cnt+=1
            if not (b & 1): break
        return val
    def sip64(self):  # FArchive::SerializeIntPacked64: bit0=more, 7-bit payload, up to 9 bytes
        val=0; cnt=0
        while True:
            if self.o >= self.n: self.err=True; return 0
            b = self.d[self.o]; self.o+=1
            val += (b>>1) << (7*cnt); cnt+=1
            if not (b & 1): break
            if cnt >= 9: break
        return val
    def fstring(self):
        ln = self.i32()
        if self.err or ln==0: return ""
        if ln<0:
            nchars = -ln
            if self.o + nchars*2 > self.n: self.err=True; return ""
            raw = self.d[self.o:self.o+nchars*2]; self.o += nchars*2
            try: return raw.decode('utf-16-le')
            except: self.err=True; return ""
        if ln>0:
            if self.o + ln > self.n: self.err=True; return ""
            raw = self.d[self.o:self.o+ln]; self.o += ln
            try: return raw.decode('ascii')
            except: self.err=True; return ""
        return ""
    def net_field_export(self):
        # FNetFieldExport::operator<< : uint8 Flags (bExported=bit0, bExportBlob=bit1)
        # if bExported:
        #    SerializeIntPacked(Handle); u32 CompatibleChecksum;
        #    StaticSerializeName: SerializeBits(bHardcoded,1)=1 BYTE;
        #       if bHardcoded: SerializeIntPacked(NameIndex)
        #       else: FString ExportName << int32 Number
        # if bExportBlob: SafeNetSerializeTArray<4096> = SerializeIntPacked(len)+len bytes
        flags = self.u8()
        if self.err: return
        if flags & 1:  # bExported
            self.sip()        # Handle
            self.o += 4       # CompatibleChecksum (u32)
            bhard = self.u8()  # SerializeBits(&bHardcoded,1) -> 1 byte
            if bhard:
                self.sip()    # NameIndex (varint)
            else:
                self.fstring()  # ExportName
                self.o += 4     # InNumber (int32)
        if flags & 2:  # bExportBlob
            ln = self.sip()
            if self.err or ln > 1<<20 or self.o + ln > self.n: self.err=True; return
            self.o += ln
    def net_field_exports(self):
        num = self.sip()
        if self.err or num > 500000: self.err=True; return
        for _ in range(num):
            pi = self.sip()
            ne = self.sip()
            if self.err or pi<1: self.err=True; return
            if ne:
                self.fstring()  # PathName
                self.sip()      # NumExportsInGroup
            if self.err: return
            self.net_field_export()
            if self.err: return
    def net_export_guids(self):
        num = self.sip()
        if self.err or num > 500000: self.err=True; return
        for _ in range(num):
            # TArray<uint8> GUIDData: `Ar << GUIDData` writes a raw int32 count + bytes
            ln = self.i32()
            if self.err or ln < 0 or ln > 1<<20 or self.o + ln > self.n: self.err=True; return
            self.o += ln
    def export_data(self, order='EG'):
        if order == 'EG':
            self.net_field_exports(); self.net_export_guids()
        else:
            self.net_export_guids(); self.net_field_exports()
    def streaming_levels(self, sf):
        # ReadDemoFrame (lines 1885-1946):
        #  if HasStreamingFixes (sf): SerializeIntPacked(NumStreamingLevels); per: FString NameTemp
        #  else:                       SerializeIntPacked(NumStreamingLevels); per: FString PackageName; FString PackageNameToLoad; FTransform(64B)
        num = self.sip()
        if self.err or num > 100000: self.err=True; return
        for _ in range(num):
            self.fstring()
            if not sf:
                self.fstring()
                self.o += 64  # FTransform LWC: dvec3(24)+FQuat(16)+dvec3(24)
    def external_data(self, sf):
        # ReadDemoFrame (1969-1983): if HasStreamingFixes, read int64 SkipExternalOffset (discarded in normal read), then LoadExternalData
        if sf:
            if self.o+8 > self.n: self.err=True; return
            self.o += 8  # SkipExternalOffset (int64) - consumed but not used for normal read
        # LoadExternalData (1590-1623): SerializeIntPacked(NumBits); if 0 return; FNetworkGUID(sip64); bytes
        while True:
            if self.o >= self.n: break
            nb = self.sip()
            if self.err or nb == 0: break
            self.sip64()  # FNetworkGUID (SerializeIntPacked64)
            nbytes = (nb + 7) >> 3
            if self.err or self.o + nbytes > self.n: self.err=True; return
            self.o += nbytes
    def packet_stream(self, sf):
        # ReadDemoFrame packet loop (2016-2055) + ReadPacket (2063+):
        #   if HasStreamingFixes: per packet SerializeIntPacked(SeenLevelIndex) precedes ReadPacket
        #   ReadPacket: Archive << BufferSize (int32); if 0 -> End
        pkts=[]
        while True:
            if self.o + 4 > self.n: self.err=True; return pkts
            if sf:
                self.sip()  # SeenLevelIndex
                if self.err: return pkts
            cnt = self.i32()
            if self.err: return pkts
            if cnt == 0:
                return pkts  # EndCount
            if cnt < 0 or cnt > (1<<24) or self.o + cnt > self.n:
                self.err=True; return pkts
            pkts.append(self.d[self.o:self.o+cnt]); self.o += cnt
        return pkts

def parse_frame(r, sf, gsf, order='EG', export_start=None):
    """Parse one frame starting at r.o. Returns (frame_dict, ok).
    export_start: if not None, seek to this offset before reading export data (prefix skip, frame 0 only)."""
    start = r.tell()
    li = r.i32()
    ts = r.f32()
    if r.err: return None, False
    if export_start is not None:
        r.seek(export_start)
    r.export_data(order=order)
    if r.err: return None, False
    r.streaming_levels(sf)
    if r.err: return None, False
    r.external_data(sf)
    if r.err: return None, False
    if gsf:
        # HasGameSpecificFrameData: read int64 SkipGameSpecificOffset; if >0, data follows (len=skip)
        if r.o+8 > r.n: r.err=True; return None, False
        skip = struct.unpack_from('<q', r.d, r.o)[0]; r.o+=8
        if skip > 0:
            if r.o + skip > r.n: r.err=True; return None, False
            r.o += skip  # skip FDemoFrameDataMap (length == skip)
    pkts = r.packet_stream(sf)
    if r.err: return None, False
    return {'level':li,'time':ts,'packets':len(pkts),'start':start}, True

def parse_replaydata(payload, sf=None, gsf=None, order=None, frame0_export_starts=(0x08,0x10,0x18)):
    """Parse a full ReplayData chunk payload into a list of frames.
    If sf/gsf/order are None, auto-detect by trying all combinations and picking the one
    that tiles the entire payload to EOF.
    Returns (frames, ok, params). Each frame: {level, time, packets:[bytes], start, end}."""
    n = len(payload)
    if sf is None or gsf is None or order is None:
        for od in ('EG','GE'):
            for s in (False, True):
                for g in (False, True):
                    for es in frame0_export_starts:
                        rr = NetReader(payload); frames = []; first = True; ok = True
                        while rr.tell() < n:
                            if rr.n - rr.tell() < 8: break
                            use_es = es if (first and rr.tell() == 0) else None
                            frm, fok = parse_frame(rr, s, g, order=od, export_start=use_es)
                            if not fok: ok = False; break
                            frm['end'] = rr.tell(); frames.append(frm); first = False
                        if ok and rr.tell() == n:
                            return frames, True, {'order':od,'sf':s,'gsf':g,'export_start':es}
        # none tiled perfectly; return best-effort with EG/sf=False/gsf=False
        sf = sf if sf is not None else False
        gsf = gsf if gsf is not None else False
        order = order if order is not None else 'EG'
    rr = NetReader(payload); frames = []; first = True
    while rr.tell() < n:
        if rr.n - rr.tell() < 8: break
        use_es = frame0_export_starts[0] if (first and rr.tell() == 0) else None
        frm, fok = parse_frame(rr, sf, gsf, order=order, export_start=use_es)
        if not fok: break
        frm['end'] = rr.tell(); frames.append(frm); first = False
    return frames, (rr.tell() == n), {'order':order,'sf':sf,'gsf':gsf,'export_start':frame0_export_starts[0]}


