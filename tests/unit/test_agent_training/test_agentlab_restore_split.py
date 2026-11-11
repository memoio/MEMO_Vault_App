import json
from everos.agent_training.agentlab_restore_split import assign_partition, goal_matches, source_loss_status


def test_group_assignment_refuses_conflicts_and_missing_evidence():
    groups = {'site': {'train'}, 'conflict': {'train', 'test'}}
    assert assign_partition('old', {'old': 'test'}, 'site', groups, True) == ('test', 'exact_legacy_id')
    assert assign_partition('new', {}, 'site', groups, True) == ('train', 'legacy_website_group')
    assert assign_partition('new', {}, 'conflict', groups, True) == (None, 'ambiguous_website_group')
    assert assign_partition('new', {}, 'missing', groups, True) == (None, 'website_group_unmatched')
    assert assign_partition('new', {}, 'site', groups, False) == (None, 'source_goal_mismatch')


def test_goal_and_supervision_are_not_inferred_from_missing_fields():
    assert goal_matches('Find an item', '# Goal\nGo to the website https://example.com and Find an item\n\n# Action Space\n')
    assert not goal_matches('Find an item', '# Goal\nDifferent task\n\n# Action Space\nFind an item')
    assert source_loss_status({'conversations': [{'role': 'assistant', 'content': '{}'}]}) == 'unknown'
    assert source_loss_status({'conversations': [{'role': 'assistant', 'content': '{}', 'loss': False}]}) == 'explicit'


def test_task_fingerprint_matches_legacy_normalization():
    from everos.agent_training.agentlab_restore_split import task_fingerprint
    assert task_fingerprint('  Ｈello\nWORLD ') == task_fingerprint('hello world')


def test_conflicts_include_reserved_legacy_tasks_absent_from_sft():
    from everos.agent_training.agentlab_restore_split import find_collisions
    candidates = [{'trajectory_id': 'new', 'source_group': 'one', 'task_sha256': 'same', 'candidate_partition': 'train'}]
    reserved = [{'id': 'held', 'source_group': 'two', 'task_sha256': 'same', 'partition': 'test'}]
    assert find_collisions(candidates, reserved)['task_sha256'] == {'same'}
