"""Compare two offline CodeAct loss reports on exactly the same decisions."""
import argparse
import json
from pathlib import Path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base', type=Path, required=True)
    p.add_argument('--adapter', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    a = json.loads((args.base/'report.json').read_text())
    b = json.loads((args.adapter/'report.json').read_text())
    if a['mode'] != 'base' or b['mode'] != 'adapter':
        raise ValueError('expected base and adapter reports')
    fields = ('model_revision', 'base_model_weight_sha256', 'test_sha256', 'manifest_sha256', 'cutoff_len', 'max_samples', 'truncation')
    mismatches = [key for key in fields if a[key] != b[key]]
    if mismatches:
        raise ValueError('incomparable reports: ' + ', '.join(mismatches))
    ra = [json.loads(x) for x in (args.base/'decisions.jsonl').read_text().splitlines()]
    rb = [json.loads(x) for x in (args.adapter/'decisions.jsonl').read_text().splitlines()]
    key = lambda r: (r['sample_index'], r['trajectory_id'], r['status'])
    if [key(r) for r in ra] != [key(r) for r in rb] or a['evaluated'] == 0:
        raise ValueError('evaluated decisions differ or there are no evaluated decisions')
    result = {'decisions': a['evaluated'], 'excluded_over_cutoff': a['over_cutoff_excluded'],
              'base_token_weighted_nll': a['token_weighted_nll'],
              'adapter_token_weighted_nll': b['token_weighted_nll'],
              'adapter_minus_base_nll': b['token_weighted_nll'] - a['token_weighted_nll'],
              'note': 'Lower NLL is better for these held-out outputs; this is not interactive task success.'}
    with args.output.open('x', encoding='utf-8') as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
        out.write('\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
