"""Run explicitly curated tasks with the real OpenHands v0.30 controller."""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid

PINNED_COMMIT = '3720e1f75b35cca65ef02888d4c7314c9fe50c86'
IMAGE_ID = 'sha256:c3bc648710afc8734d447df8a83912f41d36b0c37346433b3ed1f77ba9b9ce13'
MODEL_REV = '2fc06364715b967f1860aea9cf38778875588b17'


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1048576), b''):
            h.update(b)
    return h.hexdigest()


def load_tasks(path):
    tasks = [json.loads(x) for x in path.read_text(encoding='utf-8').splitlines() if x.strip()]
    ids = set()
    for t in tasks:
        if (not isinstance(t.get('id'), str) or not t['id'] or t['id'] in ids or
                not isinstance(t.get('prompt'), str) or not t['prompt'] or
                not isinstance(t.get('expected'), str) or not t['expected'] or
                t.get('checker') != 'solution_exact'):
            raise ValueError('task requires unique id, prompt, expected string and checker=solution_exact')
        ids.add(t['id'])
    if not tasks:
        raise ValueError('empty task set')
    return tasks


def assess(events, expected):
    """A tool cycle requires an agent action, its observation, and a later agent action."""
    calls = {e.get('id'): (i, e) for i, e in enumerate(events)
             if e.get('source') == 'agent' and e.get('action') in ('run', 'run_ipython') and e.get('id') is not None}
    cycle = False
    for i, e in enumerate(events):
        if e.get('observation') not in ('run', 'run_ipython') or e.get('cause') not in calls:
            continue
        _, action = calls[e['cause']]
        if action['action'] != e['observation']:
            continue
        content = str(e.get('content', ''))
        metadata = e.get('extras', {}).get('metadata', {}) or {}
        if 'Traceback (most recent call last)' in content or metadata.get('exit_code', 0) not in (0, None):
            continue
        if any(later.get('source') == 'agent' and later.get('action') for later in events[i + 1:]):
            cycle = True
    answer = ''
    for e in events:
        if e.get('source') != 'agent':
            continue
        if e.get('action') == 'finish':
            a = e.get('args', {})
            answer = str(a.get('final_thought') or a.get('outputs', {}).get('content') or a.get('thought') or '')
        elif e.get('action') == 'message':
            answer = str(e.get('args', {}).get('content', ''))
    matches = re.findall(r'<solution>\s*(.*?)\s*</solution>', answer, flags=re.DOTALL | re.IGNORECASE)
    correct = bool(matches and matches[-1].strip() == expected.strip())
    return {'tool_actions': len(calls), 'tool_cycle': cycle, 'answer_correct': correct,
            'success': bool(correct and cycle), 'answer': answer}


def inventory(args):
    rows = [json.loads(x) for x in args.manifest.read_text(encoding='utf-8').splitlines() if x]
    rows = [x for x in rows if x.get('source') == 'codeactinstruct' and x.get('assigned_partition') == 'test']
    categories = Counter('/'.join(x['id'].split('/')[1:3]) for x in rows)
    report = {'manifest_sha256': sha(args.manifest), 'trajectories': len(rows), 'categories': dict(categories),
              'interaction_ready': 0, 'reason': 'environment and independent answer checker not verified; category is derived from source ID'}
    args.output.mkdir(parents=True, exist_ok=False)
    with (args.output / 'records.jsonl').open('x', encoding='utf-8') as f:
        for x in rows:
            f.write(json.dumps({'source': x['source'], 'id': x['id'], 'source_line': x['source_line'],
                'category': '/'.join(x['id'].split('/')[1:3]), 'interaction_ready': False,
                'reason': 'environment_and_checker_not_verified'}, ensure_ascii=False) + '\n')
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


def run(args):
    tasks = load_tasks(args.tasks)
    if args.max_iterations < 1 or args.max_output_tokens < 1 or args.timeout < 1:
        raise ValueError('max iterations, output tokens and timeout must be positive')
    if args.output.exists():
        raise ValueError('output directory already exists')
    commit = subprocess.check_output(['git', '-C', str(args.openhands_repo), 'rev-parse', 'HEAD'], text=True).strip()
    if commit != PINNED_COMMIT:
        raise ValueError(f'OpenHands commit mismatch: {commit}')
    image_id = subprocess.check_output(['docker', 'image', 'inspect', args.image, '--format', '{{.Id}}'], text=True).strip()
    if image_id != IMAGE_ID:
        raise ValueError(f'runtime image ID mismatch: {image_id}')
    internal = subprocess.check_output(['docker', 'network', 'inspect', args.network, '--format', '{{.Internal}}'], text=True).strip()
    if internal.lower() != 'true':
        raise ValueError('Docker network must be an existing --internal network')
    if (args.mode == 'adapter') != bool(args.adapter_weights):
        raise ValueError('adapter mode requires --adapter-weights; base mode must not provide it')
    adapter_hash = sha(args.adapter_weights) if args.adapter_weights else None
    import openhands
    if not Path(openhands.__file__).resolve().is_relative_to(args.openhands_repo.resolve()):
        raise ValueError('imported OpenHands is not the verified repository')
    from openhands.core.config import AppConfig, LLMConfig, SandboxConfig
    from openhands.core.main import create_runtime, run_controller
    from openhands.events.action import MessageAction
    from openhands.events.serialization import event_to_dict
    from openhands.utils.async_utils import call_async_from_sync

    args.output.mkdir(parents=True, exist_ok=False)
    config_record = {'mode': args.mode, 'tasks_sha256': sha(args.tasks), 'openhands_commit': commit,
        'runtime_image_id': image_id, 'network': args.network, 'model_revision_claim': MODEL_REV,
        'model_name': args.model_name, 'base_url': args.base_url, 'adapter_weights_sha256': adapter_hash,
        'server_model_identity_verified': False, 'max_iterations': args.max_iterations,
        'max_output_tokens': args.max_output_tokens, 'temperature': 0.0, 'top_p': 1.0,
        'timeout_seconds': args.timeout, 'native_tool_calling': False,
        'limitations': ['The caller must verify which base and adapter the model server actually loaded.',
                        'Curated tasks and checkers must be independently verified and checked for training overlap.']}
    (args.output/'config.json').write_text(json.dumps(config_record, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    with (args.output/'events.jsonl').open('x', encoding='utf-8') as event_out, (args.output/'results.jsonl').open('x', encoding='utf-8') as result_out:
        for task in tasks:
            started = time.monotonic()
            result = {'id': task['id'], 'status': 'environment_or_controller_error', 'success': False}
            runtime = None
            try:
                sandbox = SandboxConfig(runtime_container_image=args.image, runtime_binding_address='127.0.0.1',
                    use_host_network=False, rm_all_containers=False, user_id=0,
                    docker_runtime_kwargs={'network': args.network, 'mem_limit': '2g', 'nano_cpus': 2000000000})
                config = AppConfig(default_agent='CodeActAgent', run_as_openhands=False, runtime='docker',
                    max_iterations=args.max_iterations, sandbox=sandbox, workspace_base=None,
                    workspace_mount_path=None, file_store='local', file_store_path=str(args.output.resolve()/'store'))
                config.set_llm_config(LLMConfig(model=args.model_name, base_url=args.base_url,
                    api_key=os.environ.get('OH_EVAL_API_KEY', 'local'), native_tool_calling=False,
                    temperature=0.0, top_p=1.0, max_output_tokens=args.max_output_tokens, timeout=args.timeout))
                sid = uuid.uuid4().hex
                runtime = create_runtime(config, sid=sid, headless_mode=True)
                def timed_out(_signum, _frame):
                    raise TimeoutError('task wall-clock timeout')
                previous = signal.signal(signal.SIGALRM, timed_out)
                signal.alarm(args.timeout)
                try:
                    call_async_from_sync(runtime.connect)
                    state = asyncio.run(run_controller(config=config, initial_user_action=MessageAction(content=task['prompt']),
                                                       sid=sid, runtime=runtime, exit_on_message=True))
                finally:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, previous)
                if state is None:
                    raise RuntimeError('controller returned no state')
                events = [event_to_dict(e) for e in state.history]
                for event in events:
                    event_out.write(json.dumps({'task_id': task['id'], 'event': event}, ensure_ascii=False, default=str)+'\n')
                result.update(assess(events, task['expected']))
                result['status'] = 'completed' if not state.last_error else 'controller_error'
                result['controller_state'] = str(state.agent_state)
                if state.last_error:
                    result['error'] = str(state.last_error)
                    result['success'] = False
            except Exception as exc:
                result['error'] = f'{type(exc).__name__}: {exc}'
            finally:
                if runtime is not None:
                    try:
                        runtime.close(rm_all_containers=False)
                    except Exception as exc:
                        result['cleanup_error'] = f'{type(exc).__name__}: {exc}'
                result['elapsed_seconds'] = round(time.monotonic() - started, 3)
                result_out.write(json.dumps(result, ensure_ascii=False)+'\n')
                result_out.flush()
                event_out.flush()
                print(json.dumps(result, ensure_ascii=False), flush=True)
    results = [json.loads(x) for x in (args.output/'results.jsonl').read_text(encoding='utf-8').splitlines()]
    summary = {'total': len(results), 'successful': sum(x['success'] for x in results),
               'tool_cycles': sum(x.get('tool_cycle', False) for x in results),
               'statuses': dict(Counter(x['status'] for x in results)), 'config': config_record}
    (args.output/'report.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def compare(args):
    a = json.loads((args.base/'config.json').read_text())
    b = json.loads((args.adapter/'config.json').read_text())
    if a['mode'] != 'base' or b['mode'] != 'adapter':
        raise ValueError('expected base run and adapter run')
    fields = ('tasks_sha256', 'openhands_commit', 'runtime_image_id', 'network', 'model_revision_claim',
              'model_name', 'base_url', 'max_iterations', 'max_output_tokens', 'temperature', 'top_p',
              'timeout_seconds', 'native_tool_calling')
    mismatch = [k for k in fields if a[k] != b[k]]
    if mismatch:
        raise ValueError('configuration mismatch: ' + ', '.join(mismatch))
    ra = {x['id']: x for x in map(json.loads, (args.base/'results.jsonl').read_text().splitlines())}
    rb = {x['id']: x for x in map(json.loads, (args.adapter/'results.jsonl').read_text().splitlines())}
    if set(ra) != set(rb):
        raise ValueError('task IDs mismatch')
    report = {'tasks': len(ra), 'base_successes': sum(x['success'] for x in ra.values()),
              'adapter_successes': sum(x['success'] for x in rb.values()),
              'base_errors': sum(x['status'] != 'completed' for x in ra.values()),
              'adapter_errors': sum(x['status'] != 'completed' for x in rb.values()),
              'server_identity_verified': False,
              'note': 'Server model/adapter identity, task independence and semantic comparability require separate verification.'}
    with args.output.open('x', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    i = sub.add_parser('inventory')
    i.add_argument('--manifest', type=Path, required=True); i.add_argument('--output', type=Path, required=True); i.set_defaults(func=inventory)
    r = sub.add_parser('run')
    r.add_argument('--tasks', type=Path, required=True); r.add_argument('--output', type=Path, required=True)
    r.add_argument('--openhands-repo', type=Path, required=True); r.add_argument('--mode', choices=('base','adapter'), required=True)
    r.add_argument('--adapter-weights', type=Path); r.add_argument('--image', default='ghcr.io/all-hands-ai/runtime:0.30-nikolaik')
    r.add_argument('--network', required=True); r.add_argument('--model-name', default='openai/qwen35')
    r.add_argument('--base-url', default='http://127.0.0.1:18081/v1'); r.add_argument('--max-iterations', type=int, default=10)
    r.add_argument('--max-output-tokens', type=int, default=512); r.add_argument('--timeout', type=int, default=300); r.set_defaults(func=run)
    c = sub.add_parser('compare'); c.add_argument('--base', type=Path, required=True); c.add_argument('--adapter', type=Path, required=True); c.add_argument('--output', type=Path, required=True); c.set_defaults(func=compare)
    args = p.parse_args(); args.func(args)


if __name__ == '__main__':
    main()
