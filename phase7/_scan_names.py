import re, glob, struct, os

def get_names(data):
    out = []
    for m in re.finditer(rb'TyrTestPlayerStateSubsystem_(\d+)', data):
        base = m.end()
        seg = data[base: base + 60]
        i = seg.find(b'\x00\x00')          # end of path fstring (2 nulls)
        if i < 0:
            continue
        j = i + 2
        rest = seg[j:]
        # layout: 3 bytes, then \x00\x00, then int32 name-len, then name
        k = rest.find(b'\x00\x00')
        if k < 0 or k + 6 >= len(rest):
            continue
        ln = struct.unpack_from('<i', rest, k + 2)[0]
        if 0 < ln < 60:
            nm = rest[k + 6: k + 6 + ln]
            try:
                nm = nm.decode('ascii')
            except Exception:
                nm = repr(nm)
            out.append((m.group(1).decode(), nm))
    return out

for f in sorted(glob.glob('Demos/*.replay')):
    data = open(f, 'rb').read()
    names = get_names(data)
    print(os.path.basename(f), '->', names[:8], '| total', len(names))
