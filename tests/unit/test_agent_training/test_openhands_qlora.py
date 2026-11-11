import pytest

from everos.agent_training.openhands_qlora import assess_data_readiness, assess_training_state


def test_smoke_requires_expected_steps_and_finite_recorded_loss():
    assert assess_training_state({'global_step': 10, 'log_history': [{'loss': 1.2}]}, 'smoke', 10) == (10, True, True)
    assert assess_training_state({'global_step': 9, 'log_history': [{'loss': 1.2}]}, 'smoke', 10) == (9, True, False)
    assert assess_training_state({'global_step': 10, 'log_history': [{'loss': float('nan')}]}, 'smoke', 10) == (10, False, True)
    assert assess_training_state({'global_step': 10, 'log_history': []}, 'smoke', 10) == (10, False, True)


def test_unreviewed_data_is_allowed_only_for_an_explicit_pipeline_smoke():
    report = {'training_ready': False}

    with pytest.raises(ValueError, match='probe-unverified-data'):
        assess_data_readiness(report, 'smoke')

    assert assess_data_readiness(report, 'smoke', probe_unverified_data=True) == {
        'training_ready': False,
        'probe_only': True,
    }

    with pytest.raises(ValueError, match='full training requires training_ready=true'):
        assess_data_readiness(report, 'full', probe_unverified_data=True)


def test_full_training_requires_a_true_readiness_attestation():
    assert assess_data_readiness({'training_ready': True}, 'full') == {
        'training_ready': True,
        'probe_only': False,
    }
    with pytest.raises(ValueError, match='training_ready=true'):
        assess_data_readiness({}, 'full')


def test_scale_mode_requires_the_requested_pilot_steps():
    assert assess_training_state({'global_step': 50, 'log_history': [{'loss': 1.0}]}, 'scale', 50) == (50, True, True)
    assert assess_training_state({'global_step': 49, 'log_history': [{'loss': 1.0}]}, 'scale', 50) == (49, True, False)
