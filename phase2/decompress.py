#!/usr/bin/env python3
"""
Tyr Replay Parser - Phase 2: Chunk payload extraction & decompression.

Empirical finding (verified against all 10 Tyr replays):
  FLocalFileNetworkReplayStreamer::SupportsCompression() returns FALSE
  (LocalFileNetworkReplayStreaming.h:565). Therefore chunk payloads are written
  RAW (CompressBuffer is never called; CompressedData = StreamData). The container
  header also reports Compressed=0. So at the *container* level there is NO
  Oodle/zlib wrapper. (Oodle compression, per PROJECT_INFO, lives *inside* the
  network stream as per-packet compression and is handled in Phase 3/4.)

We still implement a proper dispatcher so the parser is correct on any replay
where SupportsCompression()==true:
  1. zlib  (deflate, magic 0x78)
  2. Oodle (UE FOodleDataCompression; detect by Oodle stream magic)
  3. raw passthrough (the common Tyr case)

Usage:
  python3 decompress.py            # validates all Demos/*.replay
  python3 decompress.py <file>     # detailed per-chunk report for one file
"""
import os
import sys
import struct
import zlib

# Re-use the Phase-1 reader
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase1'))
from replay_reader import ReplayReader, CHUNK_TYPES

OOLE_MAGIC = b'Oodl'   # FOodleDataCompression may prepend an Oodle header


def looks_like_zlib(buf):
    if len(buf) < 2:
        return False
    b0, b1 = buf[0], buf[1]
    # zlib header: 0x78 0x01/0x9c/0xda ; also raw deflate 0x03/0x05
    return (b0 == 0x78 and b1 in (0x01, 0x9c, 0xda)) or (b0 & 0x0f == 0x08)


def looks_like_oodle(buf):
    if len(buf) < 4:
        return False
    # UE's FOodleDataCompression writes a small header. Oodle compressed streams
    # can also be recognized by the Kraken/Selkie/etc. 4-byte chunk magic, but the
    # safest detector is the UE wrapper prefix.
    return buf[:4] == OOLE_MAGIC or buf[:4] in (b'\x8c\x02\x17\xa0', b'\x8c\x02\x17\xa1')


def decompress(buf):
    """Return (data, method) where method in {'zlib','oodle','raw'}."""
    if looks_like_zlib(buf):
        try:
            return zlib.decompress(buf), 'zlib'
        except Exception:
            pass
    if looks_like_oodle(buf):
        # Oodle decompression requires the Oodle SDK; not bundled here.
        # We intentionally return the raw bytes and mark it so the caller knows
        # Oodle is needed. For the Tyr dataset this branch is never taken.
        return buf, 'oodle-needed'
    return buf, 'raw'


def extract_payloads(reader):
    """Yield (chunk_type_name, chunk_dict, raw_payload, decompressed, method)."""
    d = reader.data
    out = []
    for c in reader.chunks:
        if c['type'] == 0xFFFFFFFF:   # Unknown/clearned header chunk - skip
            continue
        raw = d[c['data_off']:c['data_off'] + c['size']]
        data, method = decompress(raw)
        out.append((CHUNK_TYPES.get(c['type'], c['type']), c, raw, data, method))
    return out


def validate_file(path):
    r = ReplayReader(path)
    r.parse_header()
    r.parse_chunks()
    payloads = extract_payloads(r)
    methods = {}
    total_raw = 0
    total_dec = 0
    for name, c, raw, data, method in payloads:
        methods[name] = methods.get(name, {})
        methods[name][method] = methods[name].get(method, 0) + 1
        total_raw += len(raw)
        total_dec += len(data)
    compressed_any = any('zlib' in m or 'oodle-needed' in m for m in methods.values())
    print(f"{os.path.basename(path)}: {len(payloads)} payloads, "
          f"raw={total_raw} dec={total_dec}, compressed={compressed_any}")
    for name, m in sorted(methods.items()):
        print(f"   {name:11s}: {dict(m)}")
    return not compressed_any


def detail_file(path):
    r = ReplayReader(path)
    r.parse_header()
    r.parse_chunks()
    print(f"=== {os.path.basename(path)} ===")
    for name, c, raw, data, method in extract_payloads(r):
        meta = c['meta']
        extra = ""
        if name == 'ReplayData':
            extra = (f" t1={meta.get('Time1')} t2={meta.get('Time2')} "
                     f"size={meta.get('SizeInBytes')} mem={meta.get('MemorySizeInBytes')}")
        elif name in ('Checkpoint', 'Event'):
            extra = (f" id={meta.get('Id')!r} grp={meta.get('Group')!r} "
                     f"md={meta.get('Metadata')!r} t1={meta.get('Time1')} "
                     f"t2={meta.get('Time2')} evSize={meta.get('EventDataSize')}")
        head = raw[:8].hex()
        print(f"  {name:11s} off=0x{c['data_off']:x} size={c['size']:7d} "
              f"-> dec={len(data):7d} [{method}] head={head}{extra}")


if __name__ == '__main__':
    if len(sys.argv) > 1:
        detail_file(sys.argv[1])
    else:
        base = r'C:\TyrReplayParser\Demos'
        files = sorted(os.path.join(base, f) for f in os.listdir(base) if f.endswith('.replay'))
        all_raw = True
        for f in files:
            ok = validate_file(f)
            all_raw = all_raw and ok
        print()
        print("CONTAINER-LEVEL DECOMPRESSION: all files raw (no zlib/oodle wrapper) =", all_raw)
