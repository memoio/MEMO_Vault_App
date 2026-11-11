"""Select reproducible whole singleton task groups from the v2 full split."""
import argparse
import collections
import hashlib
import json
from pathlib import Path

from .openhands_inventory import sha256_file

SIZES = {'train': 200, 'validation': 20, 'test': 20}
EXPECTED = {
    'train': 'ba926d0171da22e93238ce819bbaf76379842585d66d94dee5cca25067215b56',
    'validation': 'b48647975ef7bfe17bb2fe5326c88ae2deb97c28db25b0a5113813118a216590',
    'test': '37a7d1edecd948a3ecfd7bffea27ef87f453cca9cb8e358f536c93c3ab145524',
}
SEED = 'vault-openhands-smoke-v1'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full-split-dir', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise SystemExit(f'output directory already exists: {args.output_dir}')
    selected = {}
    for partition, target in SIZES.items():
        data_path = args.full_split_dir / f'{partition}.jsonl'
        manifest_path = args.full_split_dir / f'{partition}.manifest.jsonl'
        if sha256_file(data_path) != EXPECTED[partition]:
            raise ValueError(f'full split hash mismatch: {data_path}')
        with data_path.open('rb') as data, manifest_path.open(encoding='utf-8') as manifests:
            data_lines, metadata = list(data), [json.loads(line) for line in manifests]
        if len(data_lines) != len(metadata):
            raise ValueError(f'data and manifest counts differ: {partition}')
        groups = collections.Counter((r['source'], r['task_group']) for r in metadata)
        candidates = []
        for raw, row in zip(data_lines, metadata):
            if (row['assigned_partition'] != partition or json.loads(raw)['id'] != row['id']
                    or hashlib.sha256(raw).hexdigest() != row['raw_line_sha256']):
                raise ValueError(f'data/manifest mismatch: {partition}/{row["id"]}')
            # Singleton selection avoids partially sampling a larger task group.
            if groups[(row['source'], row['task_group'])] == 1:
                key = hashlib.sha256(f'{SEED}\0{partition}\0{row["source"]}\0{row["task_group"]}'.encode()).hexdigest()
                candidates.append((key, raw, row))
        candidates.sort(key=lambda item: item[0])
        if len(candidates) < target:
            raise ValueError(f'insufficient singleton groups: {partition}')
        selected[partition] = candidates[:target]
    args.output_dir.mkdir(parents=True, exist_ok=False)
    for partition, rows in selected.items():
        with (args.output_dir / f'{partition}.jsonl').open('xb') as data, (args.output_dir / f'{partition}.manifest.jsonl').open('x', encoding='utf-8') as manifest:
            for _, raw, row in rows:
                data.write(raw)
                manifest.write(json.dumps(row, ensure_ascii=False) + '\n')
    report = {'seed': SEED, 'selection': 'SHA-256 ranking of singleton task groups within each existing v2 partition; no cross-partition movement',
              'source_split_sha256': EXPECTED, 'counts': {p: len(v) for p, v in selected.items()},
              'sources': {p: dict(collections.Counter(row['source'] for _, _, row in rows)) for p, rows in selected.items()},
              'output_sha256': {p: sha256_file(args.output_dir / f'{p}.jsonl') for p in SIZES},
              'training_ready': False,
              'limitations': ['The 195 AgentInstruct OS trajectories form one task group and are not included in this 20-trajectory test sample.', 'Candidate trajectories are not LLaMA-Factory input; supervision and tool compatibility require verification.', 'Group identity and semantic near-duplicates remain subject to review.']}
    with (args.output_dir / 'report.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
