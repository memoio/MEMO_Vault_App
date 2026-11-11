import argparse
import json
import pytest

from everos.agent_training.swe_llamafactory import fit_decision, check_splits


def encoder(messages, system, target, cutoff):
    # Models the relevant contract of LLaMA-Factory: truncation changes tokens.
    text = system + ''.join(m['content'] for m in messages)
    ids = list(range(len(text))) + [900, 901]
    labels = [-100] * len(text) + [900, 901]
    return ids[:cutoff], labels[:cutoff]


def decision():
    return {'messages': [{'role': 'system', 'content': 'rules'},
                         {'role': 'user', 'content': 'old'},
                         {'role': 'assistant', 'content': 'action'},
                         {'role': 'user', 'content': 'new'}], 'target': 'target'}


def test_truncation_is_explicit_and_preserves_target():
    assert fit_decision(decision(), encoder, 12, False) is None
    result = fit_decision(decision(), encoder, 12, True)
    assert result['messages'] == [{'role': 'user', 'content': 'new'}]
    assert result['removed_turns'] == 1
    assert result['labels'][-2:] == [900, 901]
    assert result['system'] == 'rules'


def test_does_not_silently_truncate_target():
    assert fit_decision(decision(), encoder, 4, True) is None


def test_truncation_can_shorten_last_user_without_changing_system_or_target():
    d = decision()
    d['messages'][-1]['content'] = '123456789'
    result = fit_decision(d, encoder, 10, True)
    assert result['system'] == 'rules'
    assert result['messages'][-1]['content'] == '789'
    assert result['removed_user_chars'] == 6
    assert result['labels'][-2:] == [900, 901]


def test_rejects_non_alternating_context():
    d = decision()
    d['messages'][2]['role'] = 'user'
    with pytest.raises(ValueError, match='alternat'):
        fit_decision(d, encoder, 100, False)


def test_rejects_cross_split_source_group():
    with pytest.raises(ValueError, match='group'):
        check_splits([{'id': 'a', 'source': {'group': 'x'}}],
                     [{'id': 'b', 'source': {'group': 'x'}}])


def test_export_registers_data_and_detects_later_changes(tmp_path, monkeypatch):
    from everos.agent_training import swe_llamafactory as module
    monkeypatch.setattr(module, 'factory_encoder', lambda *_: encoder)
    source = tmp_path / 'source.jsonl'
    source.write_text(json.dumps({'id': 'one', 'system': 'rules', 'conversations': [
        {'from': 'human', 'value': 'task'},
        {'from': 'gpt', 'value': '<function=bash>\n<parameter=command>pwd</parameter></function>'}]}) + '\n')
    prepared = tmp_path / 'prepared'
    args = argparse.Namespace(train_input=[source], validation_input=[], model='model', revision='rev',
                              local_only=True, max_tokens=100, truncate=False, max_steps=10,
                              output_dir=prepared, train_output_dir=tmp_path / 'model-output')
    module.export(args)
    module.verify(argparse.Namespace(prepared_dir=prepared))
    assert json.loads((prepared / 'dataset_info.json').read_text())['swe_train']['formatting'] == 'sharegpt'
    (prepared / 'train.jsonl').write_text('{}\n')
    with pytest.raises(ValueError, match='changed'):
        module.verify(argparse.Namespace(prepared_dir=prepared))
