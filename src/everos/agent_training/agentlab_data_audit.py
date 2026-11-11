"""Audit published ADP AgentLab records without executing actions or training."""
import argparse
import ast
import collections
import hashlib
import json
import re
from pathlib import Path


def load_partitions(path):
    rows = {}
    groups = collections.defaultdict(set)
    with open(path, encoding='utf-8') as stream:
        for line in stream:
            item = json.loads(line)
            if item['dataset'] != 'mind2web':
                continue
            ident, split = item['id'], item['partition']
            if ident in rows and rows[ident]['partition'] != split:
                raise ValueError(f'conflicting partitions for {ident}')
            rows[ident] = item
            if item.get('source_group'):
                groups[item['source_group']].add(split)
    bad = [key for key, values in groups.items() if len(values) > 1]
    if bad:
        raise ValueError(f'conflicting source groups: {bad[:5]}')
    return rows


def classify_record(row, partitions):
    record_id = row.get('id', '')
    trajectory, separator, step = record_id.rpartition('-')
    reasons = []
    info = partitions.get(trajectory) if separator and step.isdigit() else None
    if info is None:
        reasons.append('source_id_unmatched')
    result = {'id': record_id, 'trajectory_id': trajectory if separator else None,
              'partition': info['partition'] if info else None,
              'source_group': info.get('source_group') if info else None,
              'task_sha256': info.get('task_sha256') if info else None,
              'original_partition': info.get('original_partition') if info else None,
              'training_ready': False}
    conversations = row.get('conversations', [])
    try:
        if len(conversations) != 2:
            raise ValueError('expected exactly two conversation messages')
        user, assistant = conversations
        if user.get('role') != 'user' or assistant.get('role') != 'assistant':
            raise ValueError('unexpected roles')
        payload = json.loads(assistant['content'])
        action = payload['action']
        parsed = ast.parse(action, mode='eval').body
        if not isinstance(parsed, ast.Call) or not isinstance(parsed.func, ast.Name):
            raise ValueError('action is not a direct call')
        name = parsed.func.id
        result['action'] = name
        if name not in {'click', 'fill', 'select_option', 'goto', 'send_msg_to_user'}:
            reasons.append('unsupported_action_' + name)
        for kw in parsed.keywords:
            if kw.arg is None:
                raise ValueError('expanded keyword arguments')
        if any(not isinstance(x, ast.Constant) for x in parsed.args) or any(not isinstance(x.value, ast.Constant) for x in parsed.keywords):
            raise ValueError('nonliteral action argument')
        user_text = user['content']
        start = user_text.find('# Current page Accessibility Tree')
        end = user_text.find('# History of past actions', start + 1)
        tree = user_text[start:end if end != -1 else len(user_text)] if start != -1 else ''
        result['has_accessibility_tree'] = bool(tree)
        bid = next((kw.value.value for kw in parsed.keywords if kw.arg == 'bid'), None)
        if bid is None and parsed.args and name in {'click', 'fill', 'select_option'}:
            bid = parsed.args[0].value
        if name in {'click', 'fill', 'select_option'}:
            if not isinstance(bid, str) or not bid:
                reasons.append('missing_bid')
            else:
                count = len(re.findall(r'\[' + re.escape(bid) + r'\]', tree))
                result['observation_bid_occurrences'] = count
                if count != 1:
                    reasons.append('bid_not_unique_in_observation')
                reasons.append('browsergym_live_id_mapping_unverified')
        if not tree and name not in {'goto', 'send_msg_to_user'}:
            reasons.append('missing_accessibility_tree')
    except (KeyError, TypeError, ValueError, SyntaxError, json.JSONDecodeError) as exc:
        result['action'] = None
        reasons.append('invalid_record_' + type(exc).__name__)
    reasons.append('source_revision_and_supervision_unverified')
    result['reasons'] = reasons
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', type=Path, required=True)
    ap.add_argument('--manifest', type=Path, required=True)
    ap.add_argument('--output-dir', type=Path, required=True)
    args = ap.parse_args()
    partitions = load_partitions(args.manifest)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    counts, actions, reasons, seen, sha = (collections.Counter(), collections.Counter(), collections.Counter(), set(), hashlib.sha256())
    with args.input.open('rb') as source, (args.output_dir / 'records.jsonl').open('x', encoding='utf-8') as out:
        for line_number, line in enumerate(source, 1):
            sha.update(line)
            result = classify_record(json.loads(line), partitions)
            if result['id'] in seen:
                raise ValueError(f"duplicate SFT id: {result['id']}")
            seen.add(result['id'])
            result['source_line'] = line_number
            counts[result['partition'] or 'unmatched'] += 1
            actions[result['action'] or 'invalid'] += 1
            reasons.update(result['reasons'])
            out.write(json.dumps(result, ensure_ascii=False) + '\n')
    report = {'source_sha256': sha.hexdigest(), 'records': sum(counts.values()),
              'counts': dict(counts), 'actions': dict(actions), 'reasons': dict(reasons),
              'training_ready': 0, 'matching_scope': 'exact trajectory ID, source revision and semantics unverified'}
    (args.output_dir / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
