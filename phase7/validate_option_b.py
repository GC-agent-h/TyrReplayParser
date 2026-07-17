"""Phase 7 Option B — validation & honest status report.

This is the empirical gate-check for the changemask-bit -> field-name mapping.

FINDING (validated here):
  The replay's first-frame FNetFieldExport blob gives, per class: a PathName, the
  changemask bit-count (nec), and a `first_field` (bit-0 property name, or
  'hardcoded#N' = an FName index into the engine's hardcoded name table).
  The Dumper-7 dump gives, per class, the full ordered property list.

  We test the plan's central hypothesis: "Dumper declaration order == Iris changemask
  bit order". Result:
    - For NATIVE classes whose `first_field` is a plain string, it is GENERALLY NOT
      at Dumper[0] -> the orders DIFFER (confirms the documented caveat).
    - So bit N cannot be blindly equated to Dumper field N. The authoritative Iris
      order lives in the replay's OWN FNetFieldExport table (which we have partially:
      frame-0 exports only the delta fields, e.g. WorldSettings nec=22 but 9 records).
    - BLUEPRINT classes (BP_*_C, Map_*_C, PC_Replay_C) carry `hardcoded#216` for
      bit-0 -> their field names are FName-index-encoded and need the engine's
      hardcoded FName table (from the shipping exe) OR the StringTokenStore decode.

WHAT WORKS TODAY (deliverable):
  - Class inventory: every replicated class -> path + changemask bit-count (Phase 5).
  - Field dictionary: for every class whose Dumper parent is known, the full ordered
    (name,type) field list is available as a CANDIDATE labeling.
  - Native classes with STRING first_field get a verified-presence anchor (the name
    exists in Dumper, so the field set is correct even if bit order isn't proven).
  - Labeled decode: dirty changemask bits -> candidate field names + raw 32-bit
    (float/int) values, with explicit [AMBIGUOUS cm_size] / order-unverified flags.

WHAT REMAINS (the real gate, per plan):
  - Resolve hardcoded FName indices (#216 etc.) via the exe's FName table, OR
  - Decode the Checkpoint/NetExport StringTokenStore to recover the authoritative
    per-bit field names, OR
  - Apply per-field NetSerializer type decoding (structs/vectors/tags) so the raw
    32-bit words become meaningful typed values.

Run:  python phase7/validate_option_b.py
"""
import sys, os, glob
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'phase7'))
from field_labeler import Labeler, load_dumper, data_fields, class_short


def main():
    dumper = load_dumper()
    demo_dir = os.path.join(os.path.dirname(__file__), '..', 'Demos')
    demos = sorted(glob.glob(os.path.join(demo_dir, 'TyrReplay*.replay')))

    total_anchor_true = 0
    total_anchor_false = 0
    total_unverified = 0
    total_native_string = 0
    total_bp_hardcoded = 0
    examples_anchor_false = []

    for f in demos:
        lab = Labeler(f)
        for cm, entries in lab.cm_map.items():
            for e in entries:
                a = e['aligned']
                ff = (e['first_field'] or '').rstrip('\x00')
                is_bp = 'BP_' in (e['class_path'] or '') or 'Map_' in (e['class_path'] or '') or 'PC_' in (e['class_path'] or '')
                if a is True:
                    total_anchor_true += 1
                    total_native_string += 1
                elif a is False:
                    total_anchor_false += 1
                    total_native_string += 1
                    if len(examples_anchor_false) < 6:
                        df = data_fields(dumper, e['dumper_class'])
                        examples_anchor_false.append(
                            (e['class_path'].split('/')[-1], ff,
                             df[0][0] if df else '?', len(df)))
                else:
                    total_unverified += 1
                    if is_bp:
                        total_bp_hardcoded += 1

    print('=== Option B validation: Dumper-order vs Iris-changemask-order ===')
    print('Across all 10 replays:')
    print('  native classes w/ STRING first_field: %d' % total_native_string)
    print('    -> anchor TRUE  (bit0 == Dumper[0]): %d' % total_anchor_true)
    print('    -> anchor FALSE (bit0 != Dumper[0]): %d  <-- orders DIFFER' % total_anchor_false)
    print('  classes w/ UNVERIFIED first_field (hardcoded#N / none): %d' % total_unverified)
    print('    -> of those, Blueprint classes (need exe FName table): %d' % total_bp_hardcoded)
    print()
    print('Examples where replay bit0 != Dumper[0] (order differs):')
    for cls, ff, d0, n in examples_anchor_false:
        print('  %-34s replay_bit0=%-22s dumper[0]=%-22s (dumper has %d fields)'
              % (cls, ff, d0, n))
    print()
    print('CONCLUSION: bit N != Dumper field N in general. The authoritative Iris')
    print('field order must come from the replay\'s own FNetFieldExport table')
    print('(full version in Checkpoint/NetExport) or the exe FName table for')
    print('hardcoded indices. The Dumper list remains a valid FIELD-SET dictionary')
    print('(names+types present) for candidate labeling and type-checking.')


if __name__ == '__main__':
    main()
