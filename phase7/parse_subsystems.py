"""Decode TyrTestPlayerStateSubsystem_<id> blocks from a replay's ReplayData stream.

Empirical layout (verified on all 10 replays, TyrReplay4 shown):
    'TyrTestPlayerStateSubsystem_<id>'  FString path (null-terminated ascii)
    <u32 = 0x18 (24)>                  count? prefix length?
    <prefix bytes: 0x28 0x03 0x24 0x02>
    then a REPEATING list of components:
        <u32 length L>  (FString byte length, e.g. 0x10=16 "HealthComponent")
        <L bytes ascii component name>  (NOT null-terminated by the length; a 0x00 follows)
        <0x00>
        <u32 = handle/size (0x30=48, 0x2f=47, ...)>
        <prefix bytes: 0xNN 0x02 0x03 0x24 0x02>   (NN varies ~2b/2f/37/3b/43/47/4b/...)
    repeat until the next FString length no longer points at a printable component name.

The component names are real UE classes: HealthComponent,
TyrPlayerComponentSubsystem_<id>, TyrPlayerTechTreeSubsystem_<id>, etc.
Each references other subsystem ids -> join graph between players.

This is the cheapest high-value win from replay_parser_next_steps.md Step 2.5/3:
fully decode each block (not just the username), building the player->component
roster that later anchors per-player stat joins.
"""
import sys, os, re, json, argparse
sys.path.insert(0, 'phase1'); sys.path.insert(0, 'phase2'); sys.path.insert(0, 'phase3')
sys.path.insert(0, 'phase5')
import replay_reader as rr
import decompress as dc


def _read_u32_le(b, o):
    return int.from_bytes(b[o:o+4], 'little')


def parse_subsystem_block(rd, off):
    """Parse one TyrTestPlayerStateSubsystem block starting at `off` (the 'T' of the path).
    Returns (subsystem_id, [component_name, ...]) or None if unparseable."""
    end_id = rd.find(b'\x00', off)
    if end_id < 0:
        return None
    sid = rd[off+len(b'TyrTestPlayerStateSubsystem_'):end_id].decode('ascii', 'replace')
    p = end_id + 1  # first byte after the path null
    # structure after the path: u32(0x18) + 4-byte prefix (0x28 0x03 0x24 0x02)
    # then a repeating list of components: u32 length L, L ascii bytes, 0x00,
    # u32 handle/size, 4-byte prefix (0xNN 0x02 0x03 0x24 0x02).
    comps = []
    i = p + 4 + 4  # skip the leading u32 + prefix to reach the first component length
    n = len(rd)
    while i + 4 <= n:
        L = _read_u32_le(rd, i)
        # plausible FString byte length for a component name: 4..200
        if not (4 <= L <= 200):
            break
        j = i + 4
        if j + L > n:
            break
        name = rd[j:j+L-1]  # UE FString length L includes the null terminator
        if not name.replace(b'_', b'').isalnum():
            break
        nm = name.decode('ascii', 'replace')
        comps.append(nm)
        k = j + L  # full FString (including its embedded null)
        if k >= n:
            break
        # after the FString: u32 handle/size (4), then a variable-length prefix
        # whose constant tail is b'\x02\x03\x24\x02'. Skip to the byte AFTER that
        # signature to land on the next component's u32 length.
        k += 4
        sig = rd.find(b'\x02\x03\x24\x02', k)
        if sig < 0 or sig + 4 + 4 > n:
            break
        i = sig + 4  # position of the next component's length u32
    return sid, comps


def extract_subsystems(replay_path):
    r = rr.ReplayReader(replay_path); r.parse_header(); r.parse_chunks()
    payloads = dc.extract_payloads(r)
    rd = None
    for name, c, raw, d, method in payloads:
        if name == 'ReplayData':
            rd = d; break
    if rd is None:
        return []
    needle = b'TyrTestPlayerStateSubsystem_'
    out = []
    for m in re.finditer(re.escape(needle), rd):
        res = parse_subsystem_block(rd, m.start())
        if res:
            out.append(res)
    return out


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('replay', nargs='?', default='Demos/TyrReplay4.replay')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    blocks = extract_subsystems(a.replay)
    if a.json:
        print(json.dumps([{'subsystem_id': s, 'components': c} for s, c in blocks], indent=2))
    else:
        for sid, comps in blocks:
            print('subsystem %s -> %d components' % (sid, len(comps)))
            for c in comps:
                print('    ', c)
