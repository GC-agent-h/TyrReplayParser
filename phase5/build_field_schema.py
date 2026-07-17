"""Build field_schema.json from a replay's export groups (phase5).

Step 1 deliverable (replay_parser_next_steps.md Section 1.4), scoped to what the
replay stream actually carries: the bit-0 / first replicated property name per class
plus its changemask size (cm_size). The replay's export section stores exactly ONE
FNetFieldExport name per group; the full bit->name list lives in the checkpoint's
FReplicationStateDescriptor (binary, not recoverable without engine source). So this
schema is the partial-but-real naming layer: bit 0 named for every exported class.

Output (field_schema.json):
    {
      "<class_path>": {
        "cm_size": <int>,
        "bit0_field": "<name or hardcoded#N>",
        "class_short": "<short name>"
      }, ...
    }

Run: python phase5/build_field_schema.py Demos/TyrReplay4.replay
"""
import sys, os, json, argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import object_identity as oi


def build_schema(replay_path):
    groups, err, _ = oi.extract_export_groups(replay_path)
    schema = {}
    for path, nec, fields, pni, we in groups:
        if not we or not path:
            continue
        b0 = fields[0] if fields else None
        if b0:
            b0 = b0.rstrip('\x00')
        schema[path.rstrip('\x00')] = {
            'cm_size': nec,
            'bit0_field': b0,
            'class_short': oi.class_short(path),
        }
    return schema, err


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('replay')
    ap.add_argument('--out', default='out/field_schema.json')
    a = ap.parse_args()
    schema, err = build_schema(a.replay)
    os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
    with open(a.out, 'w') as f:
        json.dump(schema, f, indent=2)
    print('wrote %s (%d classes, err=%s)' % (a.out, len(schema), err))
