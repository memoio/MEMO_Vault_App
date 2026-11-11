"""Recover auditable candidate partitions from fixed Mind2Web source evidence."""
import argparse
import collections
import hashlib
import json
import re
import unicodedata
from pathlib import Path

import ijson

SFT_SHA = '9b5fe4336aebb3e7f84143e1705785376b5180b919fea37adb6a976454274120'
LEGACY_SHA = '1c4264295d5d158343301512868befc3cfc4ba24a96625d0b00bb8cce8af9023'
RAW = {'train_0.json': 'c8b622901057bca813a6d171733c41e4fc266c2902a23d63b9094c0add3f8f2c',
       'train_1.json': '41084922c50174ec032185707ae43bb04d3e8d725788b2e95936db629ba59100',
       'train_2.json': '3e8a77b835517a3b88d59d0afb3412448b3bbf7f6791db796459268f73106bdf',
       'train_10.json': '182542d7947b3fa9e90fc57a3d82d4d8f2997ca5a06664217720d7a78a956e33'}


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def task_fingerprint(task):
    value = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", task).casefold()).strip()
    return hashlib.sha256(value.encode()).hexdigest()


def goal_matches(task, content):
    return bool(task and isinstance(content, str) and task in content.split('# Action Space', 1)[0])


def source_loss_status(row):
    # Only actual loss fields count; the published file does not expose original labels otherwise.
    if 'loss' in row or any('loss' in x for x in row.get('conversations', []) if isinstance(x, dict)):
        return 'explicit'
    return 'unknown'


def assign_partition(tid, legacy, site, groups, valid):
    if not valid:
        return None, 'source_goal_mismatch'
    if tid in legacy:
        return legacy[tid], 'exact_legacy_id'
    options = groups.get(site, set())
    if len(options) == 1:
        return next(iter(options)), 'legacy_website_group'
    return None, 'ambiguous_website_group' if options else 'website_group_unmatched'


def find_collisions(rows, legacy_rows):
    collisions = {}
    for field in ('trajectory_id', 'source_group', 'task_sha256'):
        indexed = collections.defaultdict(set)
        for r in rows:
            if r[field] and r['candidate_partition']:
                indexed[r[field]].add(r['candidate_partition'])
        old_field = 'id' if field == 'trajectory_id' else field
        for r in legacy_rows:
            if r.get(old_field) and r.get('partition'):
                indexed[r[old_field]].add(r['partition'])
        collisions[field] = {key for key, splits in indexed.items() if len(splits) > 1}
    return collisions


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for arg in ('input', 'legacy-manifest', 'raw-dir', 'audit', 'output-dir'):
        ap.add_argument('--' + arg, type=Path, required=True)
    args = ap.parse_args()
    if sha(args.input) != SFT_SHA or sha(args.legacy_manifest) != LEGACY_SHA:
        raise ValueError('SFT or legacy manifest SHA-256 mismatch')
    for name, expected in RAW.items():
        if sha(args.raw_dir / name) != expected:
            raise ValueError('raw file SHA-256 mismatch: ' + name)
    legacy, oldrows, sites = {}, {}, collections.defaultdict(set)
    for line in args.legacy_manifest.open(encoding='utf-8'):
        x = json.loads(line)
        if x['id'] in legacy: raise ValueError('duplicate legacy ID')
        legacy[x['id']] = x['partition']; oldrows[x['id']] = x
        sites[x['source_group']].add(x['partition'])
    raw = {}
    for name, file_sha in RAW.items():
        with (args.raw_dir / name).open('rb') as f:
            for rownum, x in enumerate(ijson.items(f, 'item')):
                tid = x['annotation_id']
                if tid in raw: raise ValueError('duplicate raw task ID')
                task = x['confirmed_task']; site = x['website']
                raw[tid] = {'task': task, 'source_group': site, 'raw_file': name,
                            'raw_file_sha256': file_sha, 'raw_row_zero_based': rownum,
                            'original_partition': 'train',
                            'task_sha256': task_fingerprint(task)}
    audit_report = json.loads((args.audit / 'report.json').read_text())
    if audit_report['source_sha256'] != SFT_SHA: raise ValueError('audit source mismatch')
    audits = [json.loads(x) for x in (args.audit / 'records.jsonl').open(encoding='utf-8')]
    rows, seen = [], set()
    with args.input.open('rb') as stream:
        for line_num, line in enumerate(stream, 1):
            x = json.loads(line); a = audits[line_num-1]
            if a['source_line'] != line_num or a['id'] != x['id'] or x['id'] in seen:
                raise ValueError('audit/source line mismatch or duplicate decision ID')
            seen.add(x['id']); tid = a['trajectory_id']; r = raw.get(tid)
            user = x.get('conversations', [{}])[0].get('content', '')
            goal_ok = bool(r and goal_matches(r['task'], user))
            if r and tid in oldrows:
                old = oldrows[tid]
                goal_ok &= (r['source_group'] == old['source_group'] and r['task_sha256'] == old['task_sha256']
                            and r['raw_file_sha256'] == old['source_file_sha256'] and r['raw_row_zero_based'] == old['source_row'])
            partition, method = assign_partition(tid, legacy, r['source_group'] if r else None, sites, goal_ok) if r else (None, 'raw_task_unmatched')
            rows.append({'id': x['id'], 'trajectory_id': tid, 'source_line': line_num,
                         'source_line_sha256': hashlib.sha256(line).hexdigest(),
                         'source_file_sha256': SFT_SHA, 'source': 'mind2web_agentlab_sft',
                         'candidate_partition': partition, 'mapping_method': method,
                         'source_group': r['source_group'] if r else None,
                         'task_sha256': r['task_sha256'] if r else None,
                         'original_partition': r['original_partition'] if r else None,
                         'raw_file': r['raw_file'] if r else None,
                         'raw_file_sha256': r['raw_file_sha256'] if r else None,
                         'raw_row_zero_based': r['raw_row_zero_based'] if r else None,
                         'action': a.get('action'), 'loss_status': source_loss_status(x),
                         'audit_reasons': a['reasons'], 'training_ready': False})
    if len(rows) != len(audits) or len(rows) != audit_report['records']: raise ValueError('audit/source count mismatch')
    # All decisions of each trajectory must agree before any candidate partition is retained.
    trajectories = collections.defaultdict(list)
    for r in rows: trajectories[r['trajectory_id']].append(r)
    for group in trajectories.values():
        if len({(r['candidate_partition'], r['mapping_method']) for r in group}) != 1:
            for r in group: r['candidate_partition'] = None; r['mapping_method'] = 'trajectory_evidence_conflict'
    # Exclude all implicated trajectories for exact task-text or site collisions across partitions.
    collisions = find_collisions(rows, oldrows.values())
    for r in rows:
        if any(r[field] in values for field, values in collisions.items()):
            r['candidate_partition'] = None; r['mapping_method'] = 'cross_partition_conflict'
    args.output_dir.mkdir(parents=True, exist_ok=False)
    counts = collections.defaultdict(lambda: {'decisions': 0, 'trajectory_ids': set(), 'actions': collections.Counter(), 'reasons': collections.Counter(), 'loss': collections.Counter(), 'mapping': collections.Counter()})
    with (args.output_dir / 'manifest.jsonl').open('x', encoding='utf-8') as mf, (args.output_dir / 'excluded.jsonl').open('x', encoding='utf-8') as ex:
        for r in rows:
            # All records remain excluded from formal training until supervision and action review.
            reasons = list(r['audit_reasons'])
            if r['candidate_partition'] is None: reasons.append(r['mapping_method'])
            if r['loss_status'] == 'unknown': reasons.append('original_loss_unknown')
            r['exclusion_reasons'] = sorted(set(reasons))
            line = json.dumps(r, ensure_ascii=False) + '\n'; mf.write(line); ex.write(line)
            c = counts[r['candidate_partition'] or 'quarantine']; c['decisions'] += 1; c['trajectory_ids'].add(r['trajectory_id']); c['actions'][r['action'] or 'invalid'] += 1; c['reasons'].update(r['exclusion_reasons']); c['loss'][r['loss_status']] += 1; c['mapping'][r['mapping_method']] += 1
    stats = {k: {**v, 'trajectories': len(v['trajectory_ids'])} for k,v in counts.items()}
    for v in stats.values(): del v['trajectory_ids']
    report = {'source_sha256': SFT_SHA, 'legacy_sha256': LEGACY_SHA, 'raw_sha256': RAW,
              'audit_records_sha256': sha(args.audit / 'records.jsonl'), 'source': 'mind2web_agentlab_sft',
              'records': len(rows), 'trajectories': len(trajectories), 'raw_tasks_indexed': len(raw),
              'stats': stats, 'cross_partition_conflicts_detected': {k: len(v) for k,v in collisions.items()},
              'semantic_near_duplicate_review': 'not performed', 'original_loss_recovered': False,
              'formal_experiment_ready': False, 'training_ready_records': 0,
              'manifest_sha256': sha(args.output_dir / 'manifest.jsonl'), 'excluded_sha256': sha(args.output_dir / 'excluded.jsonl')}
    (args.output_dir / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
