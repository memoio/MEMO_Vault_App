import json
import pytest
from everos.agent_training.agentlab_data_audit import classify_record, load_partitions


def test_records_require_real_tree_and_known_click_id():
    row = {'id': 'task-1', 'system': 'instructions', 'conversations': [
        {'role': 'user', 'content': '# Goal\nFind it\n# Current page Accessibility Tree\n[7] button X\n# History of past actions\n'},
        {'role': 'assistant', 'content': json.dumps({'thought':'', 'action':'click(bid="7")'})}]}
    result = classify_record(row, {'task': {'partition': 'train', 'source_group': 'site'}})
    assert result['partition'] == 'train'
    assert result['action'] == 'click'
    assert result['observation_bid_occurrences'] == 1
    assert not result['training_ready']
    assert 'browsergym_live_id_mapping_unverified' in result['reasons']


def test_unsupported_action_and_unknown_source_are_excluded():
    row = {'id': 'unknown-0', 'system': 's', 'conversations': [
        {'role': 'user', 'content': 'text'},
        {'role': 'assistant', 'content': json.dumps({'thought':'', 'action':'type(bid="1", value="a")'})}]}
    result = classify_record(row, {})
    assert 'source_id_unmatched' in result['reasons']
    assert 'unsupported_action_type' in result['reasons']
    assert not result['training_ready']


def test_conflicting_partitions_fail_closed(tmp_path):
    import json
    p = tmp_path / 'manifest.jsonl'
    p.write_text('\n'.join(json.dumps({'id':'task', 'partition':s, 'dataset':'mind2web'}) for s in ['train','test'])+'\n')
    with pytest.raises(ValueError, match='conflicting'):
        load_partitions(p)


def test_bid_in_history_does_not_count_as_current_page():
    row = {'id': 'task-2', 'system': 's', 'conversations': [
        {'role': 'user', 'content': '# Current page Accessibility Tree\n[1] button\n# History of past actions\n[7]'},
        {'role': 'assistant', 'content': json.dumps({'thought': '', 'action': "click(bid='7')"})}]}
    result = classify_record(row, {'task': {'partition': 'train'}})
    assert result['observation_bid_occurrences'] == 0
    assert 'bid_not_unique_in_observation' in result['reasons']
