import json

from everos.agent_training.openhands_sources import inspect_sources


def test_inspect_sources_preserves_split_and_reports_unknown_ids(tmp_path):
    source = tmp_path / 'source.jsonl'
    source.write_text('\n'.join(json.dumps(x) for x in [
        {'id': 'a', 'system': 'tools', 'conversations': [{'from': 'gpt', 'value': '<function=execute_bash></function>'}]},
        {'id': 'b', 'system': 'tools', 'conversations': [{'from': 'human', 'value': 'question'}]},
    ]) + '\n')
    manifest = tmp_path / 'manifest.jsonl'
    manifest.write_text(json.dumps({'dataset': 'agentinstruct', 'id': 'a', 'partition': 'test'}) + '\n')
    result = inspect_sources({'agenttuning_os': source}, manifest)
    item = result['sources']['agenttuning_os']
    assert item['rows'] == 2
    assert item['matched_partitions'] == {'test': 1}
    assert item['unmatched_published_ids'] == 1
    assert item['functions'] == {'execute_bash': 1}
    assert item['unmatched_existing_ids'] == 0


def test_conflicting_existing_partitions_are_rejected(tmp_path):
    import pytest
    source = tmp_path / 'source.jsonl'
    source.write_text(json.dumps({'id': 'x', 'conversations': []}) + '\n')
    manifest = tmp_path / 'manifest.jsonl'
    manifest.write_text('\n'.join(json.dumps({'dataset': 'agentinstruct', 'id': 'x', 'partition': p}) for p in ('train', 'test')) + '\n')
    with pytest.raises(ValueError, match='conflicting partitions'):
        inspect_sources({'agenttuning_os': source}, manifest)


def test_duplicate_published_ids_are_rejected(tmp_path):
    import pytest
    source = tmp_path / 'source.jsonl'
    source.write_text((json.dumps({'id': 'x', 'conversations': []}) + '\n') * 2)
    manifest = tmp_path / 'manifest.jsonl'
    manifest.write_text('')
    with pytest.raises(ValueError, match='duplicate source id'):
        inspect_sources({'agenttuning_os': source}, manifest)
