import json
import pytest
from everos.agent_training.agentlab_untruncated import adapt_decision, check_metadata, registry_for


def sample():
    return {'id':'task-0', 'system':'system', 'conversations':[{'role':'user','content':'full observation'}, {'role':'assistant','content':'{"thought":"", "action":"click(1)"}'}]}


def test_adaptation_keeps_complete_observation_and_target():
    row=sample()
    prepared, manifest=adapt_decision(row,lambda msgs,sys,target,cutoff: ([1,2,3],[-100,-100,3]))
    assert prepared['system']=='system'
    assert prepared['conversations'][0]['value']=='full observation'
    assert prepared['conversations'][1]['value']==row['conversations'][1]['content']
    assert manifest['truncated'] is False and manifest['original_loss_flag']=='unknown'


def test_partition_and_line_integrity_checked():
    meta={'id':'task-0','partition':'train','source_line_sha256':'abc'}
    check_metadata('task-0','train','abc',meta)
    with pytest.raises(ValueError): check_metadata('task-0','validation','abc',meta)
    with pytest.raises(ValueError): check_metadata('task-0','train','changed',meta)


def test_test_data_never_registered_for_training():
    r=registry_for()
    assert 'agentlab_train' in r and 'agentlab_validation' in r
    assert 'agentlab_test' not in r
