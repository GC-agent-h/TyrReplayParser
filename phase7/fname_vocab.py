"""Extract the Tyr game's hardcoded FName vocabulary from the shipping exe.

The replay's export groups reference properties by FName index (e.g. `hardcoded#216`)
for Blueprint classes. The authoritative name strings live in TyrClient-Win64-Shipping.exe
as plaintext string literals (used in FName(TEXT("...")) code). This module scans the exe
for UE-ish identifier strings (PascalCase, contains BP_/Tyr/Component/etc.) in both ANSI and
UTF-16LE, producing the complete name vocabulary the game uses. This anchors bit->name
resolution: once the FName pool's index->name ordering is recovered (needs engine
FNamePool source, absent), every `hardcoded#N` resolves to a string in this set.

Run: python phase7/fname_vocab.py
"""
import sys, os, re, json, argparse

EXE = r'C:/Program Files (x86)/Steam/steamapps/common/Tyr Playtest/Tyr/Binaries/Win64/TyrClient-Win64-Shipping.exe'

# UE-ish identifier: starts with letter/underscore, alnum/underscore, >=4 chars,
# and contains a token suggesting it's a UE type/property/field name.
_UE_HINT = re.compile(r'(BP_|Tyr|Component|Subsystem|PlayerState|GameState|Ability|Ammo|Health|Score|Team|Kill|Death|_C$|Replicated|Property|Actor|Widget|Anim|Mesh|Material|State|Record|Controller|Pawn|Weapon|Damage|Buff|Tag|Attr|Stat|Resource|Loadout|Match|Round|Lobby|Session|Inventory|Item|Skill|Class)')
_ANSI = re.compile(rb'[A-Za-z_][A-Za-z0-9_]{3,}')


def extract_vocab(exe_path=EXE):
    data = open(exe_path, 'rb').read()
    ansi = set()
    for m in _ANSI.finditer(data):
        s = m.group()
        if _UE_HINT.search(s.decode('latin1', 'replace')):
            ansi.add(s.decode('latin1'))
    # UTF-16LE wide identifiers
    wide = set()
    for m in re.finditer(rb'(?:[A-Za-z_][\x00][A-Za-z0-9_\x00]{3,})\x00', data):
        seg = m.group()
        try:
            s = seg.decode('utf-16-le')
        except Exception:
            continue
        if _UE_HINT.search(s):
            wide.add(s)
    vocab = sorted(ansi | wide)
    return vocab


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--exe', default=EXE)
    ap.add_argument('--out', default='out/fname_vocab.json')
    a = ap.parse_args()
    vocab = extract_vocab(a.exe)
    os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
    with open(a.out, 'w') as f:
        json.dump({'exe': a.exe, 'count': len(vocab), 'names': vocab}, f, indent=2)
    print('wrote %s (%d names)' % (a.out, len(vocab)))
    for want in ('PlayerName', 'Kills', 'Score', 'TeamID', 'BP_TyrPlayerState_C', 'ATyrPlayerStateBase'):
        print('  %-24s %s' % (want, want in vocab))
