import json

import pytest

from everos.agent_training.openhands_eval import assess, load_tasks


def test_score_requires_linked_tool_observation_and_later_decision():
    events = [
        {'id': 1, 'source': 'agent', 'action': 'run_ipython', 'args': {'code': 'print(2)'}},
        {'id': 2, 'source': 'environment', 'observation': 'run_ipython', 'cause': 1, 'content': '2'},
        {'id': 3, 'source': 'agent', 'action': 'finish', 'args': {'final_thought': '<solution>2</solution>'}},
    ]
    assert assess(events, '2')['success'] is True
    assert assess(events, '3')['success'] is False
    events[1]['cause'] = 99
    assert assess(events, '2')['success'] is False


def test_tool_error_does_not_count_as_success():
    events = [
        {'id': 1, 'source': 'agent', 'action': 'run', 'args': {'command': 'false'}},
        {'id': 2, 'source': 'environment', 'observation': 'run', 'cause': 1,
         'content': '', 'extras': {'metadata': {'exit_code': 1}}},
        {'id': 3, 'source': 'agent', 'action': 'finish', 'args': {'final_thought': '<solution>ok</solution>'}},
    ]
    assert assess(events, 'ok')['success'] is False


def test_task_manifest_rejects_duplicate_ids(tmp_path):
    path = tmp_path/'tasks.jsonl'
    row = {'id': 'same', 'prompt': 'task', 'expected': 'answer', 'checker': 'solution_exact'}
    path.write_text((json.dumps(row)+'\n')*2)
    with pytest.raises(ValueError, match='unique id'):
        load_tasks(path)


def test_agent_answer_without_tool_is_not_success():
    events = [{'id': 1, 'source': 'agent', 'action': 'finish',
               'args': {'final_thought': '<solution>42</solution>'}}]
    result = assess(events, '42')
    assert result['answer_correct'] is True
    assert result['tool_cycle'] is False
    assert result['success'] is False


def test_tool_observation_without_followup_is_not_a_cycle():
    events = [{'id': 1, 'source': 'agent', 'action': 'run_ipython'},
              {'id': 2, 'source': 'environment', 'observation': 'run_ipython', 'cause': 1, 'content': '42'}]
    assert assess(events, '42')['tool_cycle'] is False
