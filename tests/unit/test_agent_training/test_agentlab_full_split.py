from everos.agent_training.agentlab_full_split import assign_sites, normalize_task


def test_reserved_sites_and_same_task_cannot_cross_partitions():
    tasks = [{'website': 'one', 'task': ' Ｆind  item '}, {'website': 'two', 'task': 'find item'}, {'website': 'three', 'task': 'new task'}]
    sites = assign_sites(tasks, {'one': 'test'}, 'seed')
    assert sites['one'] == sites['two'] == 'test'
    assert sites['three'] in {'train', 'validation', 'test'}
    assert normalize_task(' Ｆind  item ') == 'find item'


def test_conflicting_reserved_duplicate_task_fails_closed():
    tasks = [{'website': 'one', 'task': 'same'}, {'website': 'two', 'task': 'same'}]
    import pytest
    with pytest.raises(ValueError, match='conflict'):
        assign_sites(tasks, {'one': 'train', 'two': 'test'}, 'seed')
