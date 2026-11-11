"""Copy indexed source rows to separate Parquet datasets without modifying input."""
import argparse
import collections
import json
import pathlib
import pyarrow as pa
import pyarrow.parquet as pq


def run(base, index, output):
    if output.exists(): raise FileExistsError(output)
    rows=collections.defaultdict(list)
    with (index/'full_manifest.jsonl').open() as f:
        for line in f:
            row=json.loads(line); rows[row['path']].append(row)
    output.mkdir(parents=True)
    counts=collections.Counter()
    for name, entries in sorted(rows.items()):
        pf=pq.ParquetFile(base/'raw'/name)
        if len(entries)!=pf.metadata.num_rows or any(r['row']!=i for i,r in enumerate(entries)):
            raise ValueError('invalid row index '+name)
        writers={}; offset=0
        try:
            for batch in pf.iter_batches(batch_size=8):
                current=entries[offset:offset+len(batch)]; offset+=len(batch)
                selections=collections.defaultdict(list)
                for i,row in enumerate(current):
                    selections[('full',row['split'])].append(i)
                    if row['smoke']: selections[('smoke',row['split'])].append(i)
                for key, positions in selections.items():
                    target=output/key[0]/key[1]/name
                    if key not in writers:
                        target.parent.mkdir(parents=True,exist_ok=True)
                        writers[key]=pq.ParquetWriter(target,pf.schema_arrow,compression='zstd')
                    writers[key].write_table(pa.Table.from_batches([batch]).take(pa.array(positions,type=pa.int32())))
                    counts['/'.join(key)]+=len(positions)
        finally:
            for writer in writers.values(): writer.close()
        print(name,offset,flush=True)
    (output/'counts.json').write_text(json.dumps(dict(counts),indent=2,sort_keys=True)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--base',type=pathlib.Path,required=True); p.add_argument('--index',type=pathlib.Path,required=True); p.add_argument('--output',type=pathlib.Path,required=True); a=p.parse_args(); run(a.base,a.index,a.output)
