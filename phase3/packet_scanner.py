"""Robust packet-stream scanner for Tyr ReplayData payloads.

The net-field-export section of an InternalAck replay connection is written
through UE's bit-level net serialization, which is not simple byte framing and
requires the full Iris bitstream reader (Phase 4) to parse exactly.  However,
the per-frame PACKET stream is byte-aligned: each saved packet is

    int32 Count ; Count bytes ; (repeated) ; int32 0  (= EndCount)

so we can locate the real packet stream by finding the longest chain of
plausible [int32 Count][Count bytes] sequences that terminates on `int32 0`
and where the bytes after the terminator look like a new frame header
(int32 LevelIndex in a small range, then an f32 TimeSeconds).

This gives us the saved packet buffers (Phase 3 deliverable) without having to
perfectly decode the bit-packed export section.
"""
import struct


def plausible_count(c):
    # Replay packets are typically 8..1<<16 bytes; allow some headroom.
    return 8 <= c <= (1 << 18)


def frame_header_looks_ok(data, off):
    if off + 8 > len(data):
        return False
    li = struct.unpack_from('<i', data, off)[0]
    ts = struct.unpack_from('<f', data, off + 4)[0]
    return (-16 <= li <= 256) and (0.0 <= ts < 1e7) and (ts == ts)  # ts not NaN


def scan_packet_chain(data, start):
    """From `start`, chain [int32 Count][Count bytes] until Count==0 or invalid.
    Returns (packets, end_offset)."""
    off = start
    n = len(data)
    packets = []
    while off + 4 <= n:
        count = struct.unpack_from('<i', data, off)[0]
        if count == 0:
            return packets, off + 4
        if not plausible_count(count):
            return None, off
        if off + 4 + count > n:
            return None, off
        packets.append(data[off + 4:off + 4 + count])
        off += 4 + count
    return None, off


def find_packet_stream(data, max_start=4096):
    """Try candidate starts and return the longest valid packet chain + start."""
    best = None
    for start in range(0, min(max_start, len(data) - 8), 4):
        pkts, end = scan_packet_chain(data, start)
        if pkts is None:
            continue
        ok = (end >= len(data) - 4) or frame_header_looks_ok(data, end)
        if ok and (best is None or len(pkts) > len(best[1])):
            best = (start, pkts, end)
    return best


def extract_packets(data):
    """Extract all saved packets from a ReplayData payload by chaining frames.
    Returns list of (frame_start, packets)."""
    frames = []
    off = 0
    n = len(data)
    guard = 0
    while off + 4 <= n:
        guard += 1
        if guard > 100000:
            break
        count = struct.unpack_from('<i', data, off)[0]
        if count == 0:
            off += 4
            continue
        if not plausible_count(count):
            off += 1
            continue
        if off + 4 + count > n:
            off += 1
            continue
        pkts, end = scan_packet_chain(data, off)
        if pkts is None:
            off += 1
            continue
        if end < n - 4 and not frame_header_looks_ok(data, end):
            off += 1
            continue
        frames.append((off, pkts))
        off = end
        if off >= n:
            break
    return frames


if __name__ == '__main__':
    import sys
    sys.path.insert(0, 'phase1')
    sys.path.insert(0, 'phase2')
    sys.path.insert(0, 'phase3')
    from replay_reader import ReplayReader
    from decompress import extract_payloads
    f = "Demos/TyrReplay1.replay"
    r = ReplayReader(f); r.parse_header(); r.parse_chunks()
    pl = extract_payloads(r)
    for name, c, raw, data, method in pl:
        if name == 'ReplayData':
            best = find_packet_stream(data)
            if best:
                start, pkts, end = best
                print("payload len", len(data), "; best chain start=0x%x" % start,
                      "packets=%d end=0x%x" % (len(pkts), end))
                sizes = [len(p) for p in pkts]
                print("  packet sizes: min=%d max=%d total=%d avg=%d" % (
                    min(sizes), max(sizes), sum(sizes), sum(sizes) // len(sizes)))
                print("  first 4 packet heads:", [p[:8].hex() for p in pkts[:4]])
            else:
                print("payload len", len(data), "no packet chain found")
            break
