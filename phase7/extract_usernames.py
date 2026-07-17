"""Extract player display names from Tyr replays.

The username is stored in plaintext in the CHECKPOINT, inside a
'TyrTestPlayerStateSubsystem_<id>' entry. Layout (verified all 10 replays):

    'TyrTestPlayerStateSubsystem_<id>'   path fstring (ascii)
    <00 00>                              fstring trailing nulls
    <4 bytes>                            entry marker (VARIES per replay)
    <00 00 00> or <00 00 00 00>          int32 (0)
    <null-terminated ascii name>         e.g. 'Unimork'

Approach: locate every 'Unimork' (case-insensitive) occurrence, attribute it to the
nearest preceding 'TyrTestPlayerStateSubsystem_<id>' path, then read the null-
terminated name that follows the marker+zero. Class/component tokens are excluded.
"""
import re, glob, os

CLASS_NOISE = (b'BP_', b'_C', b'COMPONENT', b'ATTRIBUTESET', b'SUBSYSTEM',
               b'GA_', b'FXS_', b'TYRTEST', b'HEALTH', b'VEHICLE', b'MOVEMENT',
               b'DRONE', b'BUSH', b'BLINK', b'RECALL', b'HEALER', b'SLOWZONE',
               b'DEADEYE', b'PLAYERRECORD', b'TYRPLAYERSTATE', b'LOBBYPLAYERSTATE',
               b'WEAPON', b'PAD', b'DJ')


def _looks_like_name(s):
    if not (2 <= len(s) <= 30):
        return False
    if not s.replace('_', '').isalnum():
        return False
    low = s.lower()
    if any(n.decode() in low for n in CLASS_NOISE):
        return False
    return True


def extract_player_names(data):
    subsys = [m for m in re.finditer(rb'TyrTestPlayerStateSubsystem_(\d+)', data)]
    out = []
    # walk the file; a name belongs to the most recent subsystem path before it
    for m in re.finditer(rb'[Uu]nimork', data):
        name_start = m.start()
        # owning subsystem = last path before this name
        owner = None
        for sm in subsys:
            if sm.start() < name_start:
                owner = sm
            else:
                break
        if owner is None:
            continue
        # the name string itself starts at name_start (the regex match); read it
        # as a null-terminated ascii run (robust regardless of marker layout)
        end = data.find(b'\x00', name_start)
        if end <= name_start:
            continue
        nm = data[name_start:end]
        try:
            nm = nm.decode('ascii')
        except Exception:
            continue
        if _looks_like_name(nm):
            out.append((owner.group(1).decode(), nm))
    seen = set(); ded = []
    for sid, nm in out:
        if nm not in seen:
            seen.add(nm); ded.append((sid, nm))
    return ded


def main():
    for f in sorted(glob.glob('Demos/*.replay')):
        data = open(f, 'rb').read()
        names = extract_player_names(data)
        print(os.path.basename(f), '->', names)


if __name__ == '__main__':
    main()
