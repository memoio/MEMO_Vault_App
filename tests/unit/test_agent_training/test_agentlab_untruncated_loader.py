import pytest
from everos.agent_training.agentlab_untruncated_loader import compare_encoded
from everos.agent_training.swe_llamafactory import token_hash


def test_loader_check_detects_changed_labels():
    actual={'input_ids':[1,2,3], 'labels':[-100,-100,3]}
    expected={'input_ids_sha256':token_hash(actual['input_ids']), 'labels_sha256':token_hash(actual['labels'])}
    compare_encoded(actual,expected,0)
    actual['labels']=[-100,2,3]
    with pytest.raises(ValueError,match='token/label mismatch'):
        compare_encoded(actual,expected,0)
