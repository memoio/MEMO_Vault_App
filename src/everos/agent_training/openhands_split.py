"""Partition pinned OpenHands SFT trajectories without modifying source files."""
import argparse
import collections
import hashlib
import json
from pathlib import Path

from .openhands_inventory import SOURCES, sha256_file

PARTS = ('train', 'validation', 'test')
SEED = 'vault-openhands-full-v1'
OS_SHARED_FIRST_USER_SHA256 = '7a345dd4af673fb89e3314721b10610c1fddbd0d6eaa38967530ccb209c5589b'


def group_for(source, ident):
    if source == 'agenttuning_os':
        return 'shared_first_user_task_7a345dd4'
    parts = ident.split('/')
    if source != 'codeactinstruct' or len(parts) < 6:
        raise ValueError(f'unrecognized CodeAct ID structure: {ident}')
    return '/'.join(parts[:4])


def hashed_split(source, group):
    value = int.from_bytes(hashlib.sha256(f'{SEED}\0{source}\0{group}'.encode()).digest()[:8], 'big') % 10000
    return 'train' if value < 8000 else 'validation' if value < 9000 else 'test'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--inventory', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise SystemExit(f'output directory already exists: {args.output_dir}')
    for source, (_, expected) in SOURCES.items():
        if sha256_file(args.data_dir / (source + '.jsonl')) != expected:
            raise SystemExit(f'source SHA-256 mismatch: {source}')
    records = []
    with args.inventory.open(encoding='utf-8') as stream:
        for line in stream:
            records.append(json.loads(line))
    lookup, groups = {}, collections.defaultdict(list)
    for row in records:
        source, ident = row['source'], row['id']
        if source not in SOURCES or row['source_sha256'] != SOURCES[source][1]:
            raise ValueError(f'invalid source metadata: {source}/{ident}')
        group = group_for(source, ident)
        old_group = row.get('old_source_group_reference')
        if source == 'codeactinstruct' and old_group and old_group != group:
            raise ValueError(f'old group mismatch: {source}/{ident}')
        key = (source, row['source_line'])
        if key in lookup:
            raise ValueError(f'duplicate inventory line: {key}')
        row['task_group'] = group
        lookup[key] = row
        groups[(source, group)].append(row)
    assignments = {}
    for key, members in groups.items():
        old = {r['old_partition_reference'] for r in members if r.get('old_partition_reference')}
        if old - set(PARTS):
            raise ValueError(f'conflicting old split for {key}: {old}')
        if key[0] == 'agenttuning_os':
            assignments[key] = ('test', 'shared_task_conservatively_reserved_as_test')
        elif len(old) > 1:
            raise ValueError(f'conflicting old split for {key}: {old}')
        else:
            assignments[key] = (next(iter(old)), 'old_id_group_reference') if old else (hashed_split(*key), 'sha256_group_hash')

    # Read and validate all source lines before writing outputs.
    output = {part: [] for part in PARTS}
    manifests = {part: [] for part in PARTS}
    task_hash_parts = collections.defaultdict(set)
    content_hash_parts = collections.defaultdict(set)
    for source, (_, expected) in SOURCES.items():
        with (args.data_dir / (source + '.jsonl')).open('rb') as stream:
            for line_number, raw in enumerate(stream, 1):
                key = (source, line_number)
                row = lookup.pop(key, None)
                if row is None or hashlib.sha256(raw).hexdigest() != row['raw_line_sha256']:
                    raise ValueError(f'inventory/source mismatch: {key}')
                item = json.loads(raw)
                if item['id'] != row['id']:
                    raise ValueError(f'ID mismatch: {key}')
                part, reason = assignments[(source, row['task_group'])]
                row['assigned_partition'] = part
                row['assignment_reason'] = reason
                # First human message is a conservative exact-text leakage signal,
                # not a verified canonical task identity.
                first = next((m.get('value', m.get('content')) for m in item['conversations'] if m.get('from', m.get('role')) in ('human', 'user')), None)
                task_hash = hashlib.sha256(' '.join(first.split()).encode()).hexdigest() if isinstance(first, str) and first.strip() else None
                if source == 'agenttuning_os' and task_hash != OS_SHARED_FIRST_USER_SHA256:
                    raise ValueError(f'AgentInstruct task assumption failed: {row["id"]}')
                row['first_user_text_sha256'] = task_hash
                if task_hash:
                    task_hash_parts[task_hash].add(part)
                content_hash_parts[row['content_sha256']].add(part)
                output[part].append(raw if raw.endswith(b'\n') else raw + b'\n')
                manifests[part].append(row)
    if lookup:
        raise ValueError(f'inventory has source rows not found: {len(lookup)}')
    task_conflicts = {key: sorted(parts) for key, parts in task_hash_parts.items() if len(parts) > 1}
    content_conflicts = {key: sorted(parts) for key, parts in content_hash_parts.items() if len(parts) > 1}
    if content_conflicts:
        raise ValueError(f'exact full-content duplicates cross splits: {len(content_conflicts)}')

    args.output_dir.mkdir(parents=True, exist_ok=False)
    for part in PARTS:
        with (args.output_dir / f'{part}.jsonl').open('xb') as stream:
            stream.writelines(output[part])
        with (args.output_dir / f'{part}.manifest.jsonl').open('x', encoding='utf-8') as stream:
            for row in manifests[part]:
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    with (args.output_dir / 'first-user-cross-split.jsonl').open('x', encoding='utf-8') as stream:
        for key, parts in sorted(task_conflicts.items()):
            members = [{'source': r['source'], 'id': r['id'], 'partition': p, 'task_group': r['task_group']} for p in PARTS for r in manifests[p] if r['first_user_text_sha256'] == key]
            stream.write(json.dumps({'first_user_text_sha256': key, 'partitions': parts, 'members': members}, ensure_ascii=False) + '\n')
    report = {'seed': SEED, 'policy': 'Conservatively reserve all AgentInstruct OS as test due to identical first-user task text; preserve CodeAct old group references and hash remaining groups 80/10/10.',
              'grouping': 'agenttuning_os: shared first-user text; codeactinstruct: first four ID components; both require further source-task verification',
              'inventory_sha256': sha256_file(args.inventory), 'sources_sha256': {k: v[1] for k, v in SOURCES.items()},
              'counts': {p: dict(collections.Counter(r['source'] for r in manifests[p])) | {'total': len(manifests[p])} for p in PARTS},
              'total': sum(len(output[p]) for p in PARTS), 'group_count': len(groups),
              'group_conflicts': 0, 'exact_full_content_cross_split_conflicts': 0,
              'first_user_text_cross_split_conflicts': len(task_conflicts),
              'old_reference_moved': dict(collections.Counter(f'{r["source"]}:{r["old_partition_reference"]}->{p}' for p in PARTS for r in manifests[p] if r.get('old_partition_reference') and r['old_partition_reference'] != p)),
              'output_sha256': {p: sha256_file(args.output_dir / f'{p}.jsonl') for p in PARTS},
              'training_ready': False,
              'limitations': ['Old ID matches do not establish revision equivalence.', 'Exact first-user text is not canonical task identity.', 'Semantic near-duplicates, loss flags, tool compatibility and decision conversion are not fully verified.', 'Outputs are original SFT trajectories, not prepared LLaMA-Factory training data.']}
    with (args.output_dir / 'report.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
