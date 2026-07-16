import sys, glob, os
sys.path.insert(0, 'phase1'); sys.path.insert(0, 'phase2'); sys.path.insert(0, 'phase3')
sys.path.insert(0, 'phase4')
from replay_reader import ReplayReader
from decompress import extract_payloads
import frame_parser as fp
import bitstream as bs

def analyze(path, max_frames=20, max_pkts_per_frame=200):
    r = ReplayReader(path); r.parse_header(); r.parse_chunks()
    payload = None
    for name, c, raw, d, method in extract_payloads(r):
        if name == 'ReplayData':
            payload = d; break
    frames, ok, params = fp.parse_replaydata(payload)
    total_pkts = 0
    tiled = 0
    failures = []
    nframes = min(max_frames, len(frames))
    for fi, fr in enumerate(frames[:nframes]):
        for pi, pkt in enumerate(fr['packets'][:max_pkts_per_frame]):
            total_pkts += 1
            bunches, pok, pparams = bs.parse_packet_auto(pkt, engine_net=42)
            if pok:
                tiled += 1
            else:
                if len(failures) < 5:
                    failures.append((fi, pi, len(pkt)))
    return os.path.basename(path), nframes, total_pkts, tiled, failures

print("Parsing packets with auto-detect bunch reader (engine_net=42)")
all_ok = True
for f in sorted(glob.glob("Demos/*.replay")):
    name, nframes, total, tiled, fails = analyze(f)
    status = "OK" if (tiled == total and total > 0) else "FAIL"
    if tiled != total:
        all_ok = False
    print("%-18s frames=%d pkts=%d tiled=%d %s%s" % (
        name, nframes, total, tiled, status,
        (" fails=%s" % fails) if fails else ""))
print("\nALL PACKETS TILE:", all_ok)
