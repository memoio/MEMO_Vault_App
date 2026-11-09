"""Audit source-native SWE and Mind2Web tool evidence, without executing tools."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

SWE_REFERENCE = 'https://github.com/SWE-bench/SWE-smith/blob/main/agent/swesmith_infer.yaml'
WEB_REFERENCE = 'https://github.com/OSU-NLP-Group/Mind2Web#data-fields'
EDITOR_COMMANDS = {'view', 'create', 'str_replace', 'insert', 'undo_edit'}
EDITOR_KEYS = {'command', 'path', 'file_text', 'old_str', 'new_str', 'insert_line', 'view_range'}


def inspect_swe_link(content, link):
    issues = []
    idx = link.get('action_index')
    if not isinstance(idx, int) or not 0 <= idx < len(content):
        return {'issues':['invalid_action_index'], 'name':None}
    action = content[idx]
    args = link.get('source_kwargs')
    if not isinstance(args, dict):
        return {'issues':['invalid_source_arguments'], 'name':None}
    if action.get('class_') == 'code_action' and action.get('language') == 'bash':
        name = 'bash'
        if set(args) != {'command'} or not isinstance(args.get('command'), str):
            issues.append('bash_argument_shape')
        if action.get('content') != args.get('command'):
            issues.append('action_arguments_mismatch')
    elif action.get('class_') == 'api_action':
        name = action.get('function')
        if action.get('kwargs') != args:
            issues.append('action_arguments_mismatch')
        if name == 'submit':
            if args:
                issues.append('submit_has_arguments')
        elif name == 'str_replace_editor':
            if set(args) - EDITOR_KEYS or args.get('command') not in EDITOR_COMMANDS or not isinstance(args.get('path'), str):
                issues.append('editor_argument_shape')
        else:
            issues.append('unknown_tool')
    else:
        name = None
        issues.append('unknown_action_type')
    obs = link.get('observation_index')
    terminal = link.get('terminal') is True and name == 'submit' and idx == len(content)-1
    if obs is None and not terminal:
        issues.append('missing_observation')
    elif obs is not None:
        if not isinstance(obs, int) or obs <= idx or obs >= len(content) or content[obs].get('class_') != 'text_observation' or content[obs].get('source') != 'environment':
            issues.append('invalid_observation_link')
    return {'name':name, 'issues':issues, 'terminal_submit':terminal, 'has_observation':obs is not None,
            'source_version_compatibility_verified':False}


def inspect_web_action(content, idx):
    action = content[idx]
    args = action.get('kwargs') or {}
    node = args.get('backend_node_id')
    name = action.get('function')
    issues = []
    if name not in {'click', 'type', 'select'}:
        issues.append('unknown_operation')
    if not isinstance(node, str) or not node:
        issues.append('missing_node_id')
    if idx == 0 or content[idx-1].get('class_') != 'web_observation':
        issues.append('missing_pre_action_snapshot')
        html = ''
    else:
        html = content[idx-1].get('html') or ''
    matches = len(re.findall(r'\bbackend_node_id\s*=\s*["\']' + re.escape(str(node)) + r'["\']', html)) if node else 0
    if matches != 1:
        issues.append('target_not_unique_in_html')
    return {'name':name, 'target_matches':matches, 'issues':issues, 'xpath_mapping_verified':False,
            'next_snapshot_exists':idx+1 < len(content) and content[idx+1].get('class_') == 'web_observation'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir', type=Path, default=Path('.memo-vault/agent-training/datasets/paper_adp_v2'))
    p.add_argument('--output-dir', type=Path, default=Path('.memo-vault/agent-training/audits/tool_interfaces_v1'))
    p.add_argument('--verify-raw-swe', action='store_true')
    p.add_argument('--raw-dir', type=Path, default=Path('.memo-vault/agent-training/datasets/raw'))
    a = p.parse_args()
    if a.output_dir.exists():
        raise FileExistsError('choose a new output directory')
    a.output_dir.mkdir(parents=True)
    summary = {'scope':'train/general only', 'references':{'swe_current_upstream':SWE_REFERENCE, 'mind2web_data_fields':WEB_REFERENCE},
               'limitations':['SWE reference is current main, not proven collection-time version.', 'Matching IDs in HTML does not establish XPath, executable browser mapping, or sufficient context.', 'No tools executed or final training samples produced.'], 'datasets':{}}
    with (a.output_dir / 'actions.jsonl').open('w') as out:
        for dataset in ('swe_smith','mind2web'):
            path = a.data_dir/'train/general'/f'{dataset}.jsonl'
            digest=hashlib.sha256(); counts=Counter(); names=Counter(); issues=Counter()
            raw_cache = {}
            with path.open('rb') as src:
                for line in src:
                    digest.update(line)
                    row=json.loads(line); counts['trajectories']+=1
                    content=row['content']; source=json.loads(row['details']['source_json'])
                    if dataset=='swe_smith' and a.verify_raw_swe:
                        import pyarrow.parquet as pq
                        from everos.agent_training.paper_adp_conversion import convert_swe
                        relative = source['relative_file']
                        if relative not in raw_cache:
                            raw_path = a.raw_dir / relative
                            raw_hash = hashlib.sha256()
                            with raw_path.open('rb') as raw:
                                for block in iter(lambda: raw.read(1024*1024), b''):
                                    raw_hash.update(block)
                            if raw_hash.hexdigest() != row['details']['source_file_sha256']:
                                raise ValueError('raw SWE hash mismatch')
                            raw_cache[relative] = pq.read_table(raw_path, columns=['traj_id','messages','resolved','model'])
                        raw_record = raw_cache[relative].slice(source['row_index'], 1).to_pylist()[0]
                        reconstructed = convert_swe(raw_record)
                        if (reconstructed['id'] != row['id'] or reconstructed['content'] != content or
                            json.loads(reconstructed['details']['tool_links_json']) != json.loads(row['details']['tool_links_json'])):
                            raise ValueError('raw SWE reconstruction mismatch: '+row['id'])
                        counts['raw_reconstructed_match'] += 1
                    if dataset=='swe_smith':
                        actions=[(x['action_index'], inspect_swe_link(content,x)) for x in json.loads(row['details']['tool_links_json'])]
                    else:
                        actions=[(i,inspect_web_action(content,i)) for i,x in enumerate(content) if x.get('class_')=='api_action']
                    for index, result in actions:
                        counts['actions']+=1;names[result.get('name') or 'unknown']+=1
                        if result['issues']:
                            counts['actions_with_issues']+=1;issues.update(result['issues'])
                        else:counts['actions_without_detected_shape_issues']+=1
                        if result.get('terminal_submit'):counts['terminal_submit']+=1
                        if result.get('has_observation'):counts['with_linked_observation']+=1
                        if result.get('target_matches')==1:counts['unique_html_node']+=1
                        if result.get('next_snapshot_exists'):counts['has_following_snapshot']+=1
                        out.write(json.dumps({'dataset':dataset,'trajectory_id':row['id'],'source':source,'content_index':index,**result},ensure_ascii=False)+'\n')
            summary['datasets'][dataset]={'counts':dict(counts),'action_names':dict(names),'issues':dict(issues),'input_sha256':digest.hexdigest()}
    (a.output_dir/'report.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary['datasets'],ensure_ascii=False))


if __name__=='__main__':
    main()
