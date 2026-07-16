#!/usr/bin/env python3
"""
Tyr Replay Parser - Phase 1: Container & Header parsing.
No decompression involved. Reads stock UE5.6 .replay local-file container.

Format reference (engine 5.6.1):
  LocalFileNetworkReplayStreaming.cpp  ReadReplayInfo()
  - uint32 Magic = 0x1CA2E27F
  - uint32 FileVersion (== 7 here -> CustomVersions present)
  - if FileVersion >= 7: FCustomVersionContainer (Optimized):
        int32 Count; for each: FGuid(16) + int32 Version   (no friendly name)
  - int32  LengthInMS
  - uint32 NetworkVersion
  - uint32 Changelist
  - FString FriendlyName  (int32 SaveNum; SaveNum<0 => UTF16, |SaveNum| chars *2 bytes)
  - uint32 IsLive
  - if ver >= 3: int64 Timestamp (FDateTime ticks)
  - if ver >= 2: uint32 Compressed
  - if ver >= 6: uint32 Encrypted; if Encrypted: TArray<uint8> EncryptionKey (int32 len + bytes)
  - then chunks until EOF:
        uint32 ChunkType  (0 Header,1 ReplayData,2 Checkpoint,3 Event,0xFFFFFFFF Unknown)
        int32  SizeInBytes
        ... per-type metadata, then seek(DataOffset + SizeInBytes)
"""
import struct
import sys
import os

CHUNK_TYPES = {0: "Header", 1: "ReplayData", 2: "Checkpoint", 3: "Event", 0xFFFFFFFF: "Unknown"}

class ReplayReader:
    def __init__(self, path):
        self.data = open(path, 'rb').read()
        self.path = path
        self.offset = 0
        self.info = {}

    def u32(self):
        v = struct.unpack_from('<I', self.data, self.offset)[0]; self.offset += 4; return v
    def i32(self):
        v = struct.unpack_from('<i', self.data, self.offset)[0]; self.offset += 4; return v
    def i64(self):
        v = struct.unpack_from('<q', self.data, self.offset)[0]; self.offset += 8; return v
    def fstring(self):
        n = self.i32()
        if n == 0:
            return ""
        if n < 0:
            n = -n
            raw = self.data[self.offset:self.offset + n * 2]; self.offset += n * 2
            return raw.decode('utf-16-le', errors='replace')
        else:
            raw = self.data[self.offset:self.offset + n]; self.offset += n
            return raw.decode('latin-1', errors='replace')

    def parse_header(self):
        d = self.data
        self.offset = 0
        magic = self.u32()
        if magic != 0x1CA2E27F:
            raise ValueError(f"Bad magic 0x{magic:08X}")
        ver = self.u32()
        self.info['FileVersion'] = ver
        if ver >= 7:
            ncv = self.i32()
            cvs = []
            for _ in range(ncv):
                guid = d[self.offset:self.offset+16].hex(); self.offset += 16
                cv = self.i32()
                cvs.append((guid, cv))
            self.info['CustomVersions'] = cvs
        self.info['LengthInMS'] = self.i32()
        self.info['NetworkVersion'] = self.u32()
        self.info['Changelist'] = self.u32()
        # FriendlyName
        fn_off = self.offset
        self.info['FriendlyName'] = self.fstring()
        self.info['FriendlyNameOffset'] = fn_off
        self.info['IsLive'] = self.u32()
        if ver >= 3:
            self.info['Timestamp'] = self.i64()
        if ver >= 2:
            self.info['Compressed'] = self.u32()
        # EncryptionSupport(=6) <= GetLocalFileReplayVersion()(=7) *should* mean an
        # Encrypted uint32 + optional key follows. But the actual Tyr builds omit it
        # (write-time version check differs from source). Detect adaptively: peek the
        # Encrypted field, then verify a full chunk parse sums to EOF; if not, rewind.
        enc = self.u32()
        self.info['Encrypted'] = enc
        if enc:
            klen = self.i32()
            key = d[self.offset:self.offset+klen]; self.offset += klen
            self.info['EncryptionKey'] = key
        # tentative header end
        self.info['HeaderEndOffset'] = self.offset
        if not self._chunks_sum_to_eof():
            # rewind past the Encrypted guess and retry without it
            if 'EncryptionKey' in self.info:
                del self.info['EncryptionKey']
            self.info['Encrypted'] = 0
            # Encrypted uint32 was 4 bytes; back off
            self.offset -= 4
            # also back off key if we consumed one (enc was nonzero)
            # (not expected here)
            self.info['HeaderEndOffset'] = self.offset
        return self.info

    def _chunks_sum_to_eof(self):
        """Cheap validity check: parse chunks from HeaderEndOffset, require all
        chunk types valid, positive sizes, and final offset == EOF."""
        d = self.data
        off = self.info['HeaderEndOffset']
        N = len(d)
        guard = 0
        while off < N:
            if off + 8 > N:
                return False
            ctype = struct.unpack_from('<I', d, off)[0]; off += 4
            if ctype not in (0, 1, 2, 3, 0xFFFFFFFF):
                return False
            csize = struct.unpack_from('<i', d, off)[0]; off += 4
            if csize < 0:
                return False
            end = off + csize
            if end > N or end < off:
                return False
            off = end
            guard += 1
            if guard > 100000:
                return False
        return off == N

    def parse_chunks(self):
        d = self.data
        chunks = []
        self.offset = self.info['HeaderEndOffset']
        # version for chunk sub-metadata
        ver = self.info['FileVersion']
        while self.offset < len(d):
            type_off = self.offset
            ctype = self.u32()
            csize = self.i32()
            data_off = self.offset
            meta = {}
            if ctype == 2:  # Checkpoint
                meta['Id'] = self.fstring()
                meta['Group'] = self.fstring()
                meta['Metadata'] = self.fstring()
                meta['Time1'] = self.u32()
                meta['Time2'] = self.u32()
                meta['EventDataSize'] = self.i32()
                meta['EventDataOffset'] = self.offset
            elif ctype == 1:  # ReplayData
                # version >= 4 (StreamChunkTimes): Time1,Time2,SizeInBytes(int32)
                meta['Time1'] = self.u32()
                meta['Time2'] = self.u32()
                meta['SizeInBytes'] = self.i32()
                meta['MemorySizeInBytes'] = self.i32()
                meta['ReplayDataOffset'] = self.offset
            elif ctype == 3:  # Event
                meta['Id'] = self.fstring()
                meta['Group'] = self.fstring()
                meta['Metadata'] = self.fstring()
                meta['Time1'] = self.u32()
                meta['Time2'] = self.u32()
                meta['EventDataSize'] = self.i32()
                meta['EventDataOffset'] = self.offset
            elif ctype == 0:  # Header
                pass
            # seek to end of chunk
            end = data_off + csize
            if end < 0 or end > len(d):
                # something off; stop to avoid garbage
                chunks.append({'type': ctype, 'type_off': type_off, 'size': csize,
                               'data_off': data_off, 'meta': meta, 'error': 'bad end'})
                break
            self.offset = end
            chunks.append({'type': ctype, 'type_off': type_off, 'size': csize,
                           'data_off': data_off, 'meta': meta})
        self.chunks = chunks
        return chunks


def summarize(path):
    r = ReplayReader(path)
    info = r.parse_header()
    chunks = r.parse_chunks()
    total = len(r.data)
    # Real invariant: the chunk loop tiles [HeaderEndOffset, EOF) exactly when valid.
    consumed = r.chunks[-1]['data_off'] + r.chunks[-1]['size'] if r.chunks else 0
    print(f"=== {os.path.basename(path)} (size={total}) ===")
    print(f"  FileVersion={info['FileVersion']} CustomVersions={info.get('CustomVersions')}")
    print(f"  LengthInMS={info['LengthInMS']} NetworkVersion={info['NetworkVersion']} "
          f"Changelist={info['Changelist']}")
    print(f"  FriendlyName={info['FriendlyName']!r} IsLive={info['IsLive']} "
          f"Timestamp(ticks)={info.get('Timestamp')}")
    print(f"  Compressed={info.get('Compressed')} Encrypted={info.get('Encrypted')}")
    print(f"  HeaderEndOffset=0x{info['HeaderEndOffset']:x}  "
          f"chunk-loop end=0x{consumed:x}  eof=0x{total:x}  "
          f"exact_tiling={consumed==total}")
    from collections import Counter
    cnt = Counter(c['type'] for c in chunks)
    print(f"  Chunk counts: " + ", ".join(f"{CHUNK_TYPES.get(t,t)}:{n}" for t, n in sorted(cnt.items())))
    print(f"  consumed(header+sum sizes)={consumed}  file={total}  match={consumed==total}")
    # per-type size table
    bytype = {}
    for c in chunks:
        bytype.setdefault(c['type'], [0, 0])
        bytype[c['type']][0] += 1
        bytype[c['type']][1] += c['size']
    print(f"  Per-type size summary (count, total bytes):")
    for t, (n, s) in sorted(bytype.items()):
        print(f"    {CHUNK_TYPES.get(t,t):10s} count={n:4d}  bytes={s}")
    errs = [c for c in chunks if 'error' in c]
    if errs:
        print(f"  !! {len(errs)} chunk(s) had errors")
    return r


if __name__ == '__main__':
    base = r'C:\TyrReplayParser\Demos'
    files = sorted([os.path.join(base, f) for f in os.listdir(base) if f.endswith('.replay')])
    for f in files:
        try:
            summarize(f)
        except Exception as e:
            print(f"{os.path.basename(f)}: ERROR {e}")
        print()
