import json
import pytest
from everos.agent_training.swe_lora_pipeline import build_decisions, encode_decision, read_converted

class Tokenizer:
    def apply_chat_template(self, messages, tokenize, add_generation_prompt, **kwargs):
        return {'input_ids': [1, 2, 3, 4, 5] if add_generation_prompt else [1, 2, 3, 4, 5, 6, 7]}


def test_decisions_preserve_history_and_ignore_non_action_messages():
    row = {'id': 'one', 'system': 'rules', 'conversations': [{'from':'human','value':'task'}, {'from':'gpt','value':'<function=bash>\n<parameter=command>pwd</parameter></function>'}, {'from':'human','value':'OBSERVATION: /tmp'}, {'from':'gpt','value':'answer'}]}
    decisions = list(build_decisions(row, 'train'))
    assert len(decisions) == 1
    assert decisions[0]['trajectory_id'] == 'one'
    assert decisions[0]['messages'] == [{'role':'system','content':'rules'}, {'role':'user','content':'task'}]
    assert decisions[0]['event_index'] == 1


def test_opt_in_truncation_keeps_target_and_records_loss():
    d = {'messages':[{'role':'user','content':'task'}], 'target':'answer'}
    assert encode_decision(Tokenizer(), d, 5, False) is None
    x = encode_decision(Tokenizer(), d, 5, True)
    assert x['input_ids'] == [3,4,5,6,7]
    assert x['labels'] == [-100,-100,-100,6,7]
    assert x['truncated_tokens'] == 2
    assert encode_decision(Tokenizer(), d, 2, True) is None


def test_converted_input_rejects_duplicate_trajectory_ids(tmp_path):
    p=tmp_path/'x.jsonl'; p.write_text('\n'.join(json.dumps({'id':'same'}) for _ in range(2))+'\n')
    with pytest.raises(ValueError, match='duplicate'):
        list(read_converted([p]))


def test_conversion_rejects_source_provided_api_declarations():
    from everos.agent_training.swe_lora_pipeline import convert_record
    row = {'id': 'one', 'details': {'available_apis': 'untrusted'}}
    with pytest.raises(ValueError, match='available_apis'):
        convert_record(row, lambda *_: pytest.fail('must not execute converter'))


def test_conversion_uses_empty_api_signatures():
    from everos.agent_training.swe_lora_pipeline import convert_record
    def converter(line, description, signatures):
        assert json.loads(line)['id'] == 'one'
        assert description == '' and signatures == {}
        return {'id': 'one'}
    assert convert_record({'id':'one', 'details':{}}, converter) == {'id':'one'}
