"""Inventory and split the pinned SWE-smith snapshot without executing its content."""
import argparse
import collections
import hashlib
import json
import pathlib
import re

import pyarrow as pa
import pyarrow.parquet as pq

SEED = '20261110'
REVISION = '08e109b4a59eaeebf80e4675cd125d42e7ac99a4'


def repo_group(instance):
    if not isinstance(instance, str):
        return None
    found = list(re.finditer(r'\.([0-9a-f]{8,40})\.', instance))
    if len(found) != 1:
        return None
    group = instance[:found[0].start()]
    return group if re.fullmatch(r'[^/\s]+__[^/\s]+', group) else None


def rank(value):
    return hashlib.sha256((SEED + '\0' + value).encode()).hexdigest()


def split_for(group):
    n = int(rank(group), 16) % 10000
    return 'train' if n < 9000 else 'validation' if n < 9500 else 'test'


def choose_smoke(records, limit):
    groups = collections.defaultdict(list)
    for item in records:
        groups[item['group']].append(item)
    for group in groups:
        groups[group].sort(key=lambda x: rank(x['traj_id']))
    chosen = []
    while groups and len(chosen) < limit:
        for group in sorted(groups, key=rank):
            if len(chosen) == limit:
                break
            chosen.append(groups[group].pop(0))
        groups = {g: items for g, items in groups.items() if items}
    return chosen


def digest(path, git_blob=False):
    size = path.stat().st_size
    h = hashlib.sha1(('blob '+str(size)+'\0').encode()) if git_blob else hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)+'\n')


def run(base, out):
    info = json.loads((base/'metadata/source.json').read_text())
    if info['revision'] != REVISION:
        raise ValueError('unexpected revision')
    if out.exists():
        raise FileExistsError(out)
    root = base/'raw'
    source_files = {x['path'] for x in info['files']}
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and '.cache' not in p.parts}
    if actual != source_files:
        raise ValueError(f'file list mismatch missing={source_files-actual} extra={actual-source_files}')
    hashes = []
    for f in info['files']:
        p = root/f['path']
        if p.stat().st_size != f['size']:
            raise ValueError(f'size mismatch {p}')
        sha = digest(p)
        if f['lfs_sha256'] and sha != f['lfs_sha256']:
            raise ValueError(f'SHA-256 mismatch {p}')
        if not f['lfs_sha256'] and digest(p, True) != f['git_blob']:
            raise ValueError(f'git blob mismatch {p}')
        hashes.append(f'{sha}  {f["path"]}\n')
    out.mkdir(parents=True)
    (out/'raw.sha256').write_text(''.join(hashes))
    records=[]
    identities=collections.defaultdict(set)
    by_format=collections.Counter()
    for p in sorted((root/'data').glob('*.parquet')):
        fmt=p.name.split('-')[0]; pf=pq.ParquetFile(p); pos=0
        for batch in pf.iter_batches(batch_size=512, columns=['instance_id','traj_id','resolved','model']):
            for row in batch.to_pylist():
                row.update(format=fmt, path=p.relative_to(root).as_posix(), row=pos)
                pos+=1; row['group']=repo_group(row['instance_id'])
                row['reason']=None
                if not row['group'] or not row['traj_id'] or not row['traj_id'].startswith(row['instance_id']+'.'):
                    row['reason']='invalid identity or group'
                else:
                    identities[row['traj_id']].add((row['instance_id'],row['group'],row['model'],row['resolved']))
                records.append(row); by_format[fmt]+=1
        if pos != pf.metadata.num_rows: raise ValueError('row count mismatch')
    for row in records:
        if not row['reason'] and len(identities[row['traj_id']]) != 1:
            row['reason']='conflicting trajectory metadata'
        row['split']='quarantine' if row['reason'] else split_for(row['group'])
    unique={}
    for row in records:
        if not row['reason']:
            unique.setdefault(row['traj_id'],row)
    smoke={}
    for split, limit in [('train',200),('validation',20),('test',20)]:
        selected=choose_smoke([r for r in unique.values() if r['split']==split],limit)
        if len(selected)!=limit: raise ValueError('not enough trajectories for '+split)
        smoke.update((x['traj_id'],split) for x in selected)
    with (out/'full_manifest.jsonl').open('w') as f, (out/'smoke_manifest.jsonl').open('w') as s, (out/'excluded.jsonl').open('w') as e:
        for row in records:
            row['smoke'] = row['traj_id'] in smoke and row['split'] == smoke[row['traj_id']]
            line=json.dumps(row,ensure_ascii=False,sort_keys=True)+'\n'; f.write(line)
            if row['smoke']: s.write(line)
            if row['reason']: e.write(line)
    groups={r['group']:r['split'] for r in records if not r['reason']}
    write_json(out/'group_assignments.json',dict(sorted(groups.items())))
    report={'revision':REVISION,'seed':SEED,'split_algorithm':'sha256(seed + NUL + repo) mod 10000; 0-8999 train, 9000-9499 validation, 9500-9999 test', 'rows':len(records),'rows_by_format':dict(by_format),'rows_by_split':dict(collections.Counter(r['split'] for r in records)), 'unique_trajectories_by_split':dict(collections.Counter(r['split'] for r in unique.values())), 'groups_by_split':dict(collections.Counter(groups.values())), 'smoke_unique_by_split':dict(collections.Counter(smoke.values())), 'quarantine_reasons':dict(collections.Counter(r['reason'] for r in records if r['reason'])), 'formats_per_trajectory':dict(collections.Counter(str(len({r['format'] for r in records if r['traj_id']==t})) for t in [])), 'limitations':['repo group inferred from instance_id, not independently checked against source task metadata','same trajectory ID and metadata are association evidence, not semantic equivalence','near duplicates and cross-source leakage not checked']}
    formats=collections.defaultdict(set)
    for r in records: formats[r['traj_id']].add(r['format'])
    report['formats_per_trajectory']=dict(collections.Counter(str(len(v)) for v in formats.values()))
    write_json(out/'report.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--base',type=pathlib.Path,required=True); p.add_argument('--output',type=pathlib.Path,required=True)
    a=p.parse_args(); run(a.base,a.output)
