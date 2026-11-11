"""Read-only inspection of pinned, already-converted ADP OpenHands files."""
import argparse
import collections
import hashlib
import json
import re
from pathlib import Path

SOURCE_NAMES = {'agenttuning_os': 'agentinstruct', 'codeactinstruct': 'codeactinstruct'}
FUNCTION = re.compile(r'<function=([^>]+)>')


def inspect_sources(paths, manifest):
    existing = collections.defaultdict(dict)
    with open(manifest, encoding='utf-8') as f:
        for line in f:
            item = json.loads(line)
            dataset, ident, split = item['dataset'], item['id'], item['partition']
            if ident in existing[dataset] and existing[dataset][ident] != split:
                raise ValueError(f'conflicting partitions: {dataset}/{ident}')
            existing[dataset][ident] = split
    report = {'matching_scope': 'exact ID within dataset only; not proof of source revision or task identity', 'sources': {}}
    for name, path in paths.items():
        matches, roles, functions, flags = (collections.Counter() for _ in range(4))
        identities = set()
        digest = hashlib.sha256()
        rows = 0
        with open(path, 'rb') as stream:
            for line in stream:
                digest.update(line)
                item = json.loads(line)
                ident = item['id']
                if ident in identities:
                    raise ValueError(f'duplicate source id: {name}/{ident}')
                identities.add(ident)
                rows += 1
                for message in item['conversations']:
                    roles[str(message.get('from', message.get('role')))] += 1
                    text = message.get('value', message.get('content', ''))
                    if isinstance(text, str):
                        functions.update(FUNCTION.findall(text))
                    flags.update(k for k in ('loss', 'trainable', 'mask') if k in message)
                if ident in existing[SOURCE_NAMES[name]]:
                    matches[existing[SOURCE_NAMES[name]][ident]] += 1
        old_ids = existing[SOURCE_NAMES[name]].keys()
        report['sources'][name] = {'file': str(path), 'sha256': digest.hexdigest(),
            'rows': rows, 'roles': dict(roles), 'functions': dict(functions),
            'message_flag_fields': dict(flags), 'matched_partitions': dict(matches),
            'unmatched_published_ids': len(identities - old_ids),
            'unmatched_existing_ids': len(old_ids - identities)}
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = inspect_sources({name: args.data_dir / (name + '.jsonl') for name in SOURCE_NAMES}, args.manifest)
    with args.output.open('x', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(args.output)


if __name__ == '__main__':
    main()
