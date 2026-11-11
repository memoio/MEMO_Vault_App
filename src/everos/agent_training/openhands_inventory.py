"""Create a non-destructive inventory of the pinned OpenHands SFT sources."""
import argparse
import collections
import hashlib
import json
import re
from pathlib import Path

SOURCES = {
    'agenttuning_os': ('agentinstruct', '7879f2122fd07fab5382b6289beeb25c352f5c35c4b305e359f9718fd1805e61'),
    'codeactinstruct': ('codeactinstruct', 'd01452a99787e722a1e86ff62cf965c668f39ddbf968a0cfcac428cb4b3aab80'),
}
FUNCTION = re.compile(r'<function=([^>]+)>')
SUPPORTED = {'execute_bash', 'execute_ipython_cell'}


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--old-manifest', required=True, type=Path)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()

    for name, (_, expected) in SOURCES.items():
        path = args.data_dir / (name + '.jsonl')
        actual = sha256_file(path)
        if actual != expected:
            raise SystemExit(f'SHA-256 mismatch: {path}: {actual}')
    old = {}
    with args.old_manifest.open(encoding='utf-8') as stream:
        for line in stream:
            item = json.loads(line)
            key = (item['dataset'], str(item['id']))
            if key in old:
                raise ValueError(f'duplicate old manifest ID: {key}')
            old[key] = item

    records, duplicate_index = [], collections.defaultdict(list)
    stats = collections.defaultdict(collections.Counter)
    function_stats = collections.defaultdict(collections.Counter)
    for source, (old_name, source_hash) in SOURCES.items():
        seen_ids = set()
        with (args.data_dir / (source + '.jsonl')).open('rb') as stream:
            for line_number, raw in enumerate(stream, 1):
                item = json.loads(raw)
                ident = str(item['id'])
                if ident in seen_ids:
                    raise ValueError(f'duplicate source ID: {source}/{ident}')
                seen_ids.add(ident)
                messages = item['conversations']
                functions = collections.Counter()
                flags = set()
                malformed = 0
                assistant_messages = 0
                for msg in messages:
                    flags.update(k for k in ('loss', 'trainable', 'mask') if k in msg)
                    if msg.get('from', msg.get('role')) not in ('gpt', 'assistant'):
                        continue
                    assistant_messages += 1
                    text = msg.get('value', msg.get('content', ''))
                    if not isinstance(text, str):
                        malformed += 1
                        continue
                    names = FUNCTION.findall(text)
                    functions.update(names)
                    if text.count('<function=') != len(names) or len(names) != text.count('</function>'):
                        malformed += 1
                canonical = json.dumps({'system': item.get('system'), 'conversations': messages}, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
                content_hash = hashlib.sha256(canonical).hexdigest()
                prior = old.get((old_name, ident))
                reasons = ['original_loss_flags_not_verified', 'task_group_and_leakage_not_verified']
                if not prior:
                    reasons.append('no_old_manifest_match')
                if malformed:
                    reasons.append('possible_malformed_function_markup')
                unknown = sorted(set(functions) - SUPPORTED)
                if unknown:
                    reasons.append('function_not_yet_verified')
                if not functions:
                    reasons.append('no_assistant_function_tag')
                record = {'source': source, 'id': ident, 'source_file': source + '.jsonl',
                          'source_sha256': source_hash, 'source_line': line_number,
                          'raw_line_sha256': hashlib.sha256(raw).hexdigest(),
                          'content_sha256': content_hash,
                          'old_partition_reference': prior.get('partition') if prior else None,
                          'old_original_partition_reference': prior.get('original_partition') if prior else None,
                          'old_source_group_reference': prior.get('source_group') if prior else None,
                          'old_match_evidence': 'dataset_and_id_only' if prior else None,
                          'assigned_partition': 'unassigned', 'training_ready': False,
                          'assistant_messages': assistant_messages, 'function_counts': dict(functions),
                          'unverified_functions': unknown, 'possible_malformed_messages': malformed,
                          'published_message_flag_fields': sorted(flags), 'review_reasons': reasons}
                records.append(record)
                duplicate_index[content_hash].append({'source': source, 'id': ident, 'line': line_number, 'old_partition_reference': record['old_partition_reference']})
                stats[source]['records'] += 1
                stats[source]['old_' + (record['old_partition_reference'] or 'unmatched')] += 1
                stats[source]['possible_malformed_messages'] += malformed
                stats[source]['with_unverified_functions'] += bool(unknown)
                stats[source]['without_function_tag'] += not bool(functions)
                stats[source]['messages_with_flag_field_records'] += bool(flags)
                function_stats[source].update(functions)

    duplicates = [{'content_sha256': key, 'members': members} for key, members in duplicate_index.items() if len(members) > 1]
    report = {'source_revision': '31a76bfb0124d77ae7322eabbb0171bf11ee2c67',
              'old_manifest_sha256': sha256_file(args.old_manifest),
              'scope': 'Exact dataset/ID matching and exact system+conversation duplicates only. Markup checks are heuristic; task grouping, cross-source semantic duplicates and original loss labels are not verified.',
              'records_total': len(records), 'all_records_unassigned_and_preserved': True,
              'sources': {name: {'counts': dict(stats[name]), 'assistant_function_counts': dict(function_stats[name])} for name in SOURCES},
              'exact_duplicate_groups': len(duplicates), 'exact_duplicate_records': sum(len(x['members']) for x in duplicates)}
    args.output_dir.mkdir(parents=True, exist_ok=False)
    with (args.output_dir / 'inventory.jsonl').open('x', encoding='utf-8') as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
    with (args.output_dir / 'exact-duplicates.jsonl').open('x', encoding='utf-8') as stream:
        for item in duplicates:
            stream.write(json.dumps(item, ensure_ascii=False) + '\n')
    with (args.output_dir / 'report.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f'输出目录: {args.output_dir}')


if __name__ == '__main__':
    main()
