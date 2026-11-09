from everos.agent_training.prepare_qwen_stage1 import build_candidates, labels_for_decision


def test_source_example_is_context_not_target():
    rows = [{'from': 'human', 'value': 'task'}, {'from': 'gpt', 'value': 'example', 'loss': False}, {'from': 'human', 'value': 'result'}, {'from': 'gpt', 'value': 'answer', 'loss': True}]
    examples, reasons = build_candidates('agentinstruct', rows)
    assert len(examples) == 1
    assert examples[0]['target'] == 'answer'
    assert examples[0]['messages'][1]['content'] == 'example'
    assert not reasons


def test_missing_loss_is_not_assumed_trainable():
    examples, reasons = build_candidates('agentinstruct', [{'from':'human','value':'task'}, {'from':'gpt','value':'answer'}])
    assert not examples
    assert reasons['unknown_source_loss'] == 1


def test_label_mask_and_no_silent_cut():
    class Tokenizer:
        def apply_chat_template(self, messages, tokenize, add_generation_prompt):
            if add_generation_prompt:
                return [1, 2]
            return [1, 2, 3, 4]
    result = labels_for_decision(Tokenizer(), [{'role':'user','content':'hi'}], 'answer', 4)
    assert result['labels'] == [-100, -100, 3, 4]
    assert labels_for_decision(Tokenizer(), [{'role':'user','content':'hi'}], 'answer', 3) is None


def test_template_boundary_mismatch_does_not_create_wrong_mask():
    class Tokenizer:
        def apply_chat_template(self, messages, tokenize, add_generation_prompt):
            return [1, 2] if add_generation_prompt else [1, 3, 4]
    import pytest
    with pytest.raises(ValueError, match='qwen_chat_template_prefix_mismatch'):
        labels_for_decision(Tokenizer(), [{'role':'user','content':'hi'}], 'answer', 10)


def test_opt_in_truncation_preserves_all_target_labels_and_records_loss():
    class Tokenizer:
        def apply_chat_template(self, messages, tokenize, add_generation_prompt, **kwargs):
            return {'input_ids': [1, 2, 3, 4, 5] if add_generation_prompt else [1, 2, 3, 4, 5, 6, 7]}
    result = labels_for_decision(Tokenizer(), [{'role':'user','content':'hi'}], 'answer', 5, truncate=True)
    assert result['input_ids'] == [3, 4, 5, 6, 7]
    assert result['labels'] == [-100, -100, -100, 6, 7]
    assert result['truncated_tokens'] == 2
    assert result['original_tokens'] == 7


def test_truncation_rejects_target_that_leaves_no_context():
    class Tokenizer:
        def apply_chat_template(self, messages, tokenize, add_generation_prompt, **kwargs):
            return [1, 2] if add_generation_prompt else [1, 2, 3, 4, 5]
    assert labels_for_decision(Tokenizer(), [{'role':'user','content':'hi'}], 'answer', 3, truncate=True) is None
