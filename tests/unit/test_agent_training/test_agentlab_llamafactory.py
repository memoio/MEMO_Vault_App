import json
import pytest
from everos.agent_training.agentlab_llamafactory import select_reviewed_decision


def example():
    row = {'id':'task-1', 'system':'system', 'conversations':[
        {'role':'user','content':'# Current page Accessibility Tree\n[7] button X\n# History of past actions'},
        {'role':'assistant','content':json.dumps({'thought':'','action':'click(bid="7")'})}]}
    source = {'task': {'partition':'train', 'source_group':'site'}}
    return row, source


def test_reviewed_valid_click_preserves_input_and_target():
    row, source = example()
    review = {'id':'task-1', 'approved':True, 'evidence':'local replay: task-1', 'source_sha256':'a'*64}
    out = select_reviewed_decision(row, source, review, 'a'*64)
    assert out['target'] == row['conversations'][1]['content']
    assert out['messages'][1]['content'] == row['conversations'][0]['content']
    assert out['split'] == 'train'


def test_cannot_override_unknown_source_or_missing_bid():
    row, source = example()
    review = {'id':'task-1', 'approved':True, 'evidence':'manual', 'source_sha256':'a'*64}
    with pytest.raises(ValueError, match='source_id_unmatched'):
        select_reviewed_decision(row, {}, review, 'a'*64)
    row['conversations'][0]['content'] = '# Current page Accessibility Tree\n[8] button'
    with pytest.raises(ValueError, match='bid_not_unique'):
        select_reviewed_decision(row, source, review, 'a'*64)


def test_test_set_and_wrong_hash_cannot_enter_training():
    row, source = example()
    review = {'id':'task-1', 'approved':True, 'evidence':'manual', 'source_sha256':'a'*64}
    source['task']['partition'] = 'test'
    with pytest.raises(ValueError, match='test'):
        select_reviewed_decision(row, source, review, 'a'*64)
    source['task']['partition'] = 'train'
    with pytest.raises(ValueError, match='sha256'):
        select_reviewed_decision(row, source, review, 'b'*64)


def test_export_produces_auditable_data_and_never_truncates(tmp_path, monkeypatch):
    from argparse import Namespace
    from everos.agent_training import agentlab_llamafactory as module
    row, _ = example()
    inp = tmp_path/'source.jsonl'; inp.write_text(json.dumps(row)+'\n')
    source_hash = module.sha256(inp)
    manifest = tmp_path/'source_manifest.jsonl'
    manifest.write_text(json.dumps({'id':'task','partition':'train','dataset':'mind2web','source_group':'site'})+'\n')
    review = tmp_path/'review.jsonl'
    review.write_text(json.dumps({'id':'task-1','approved':True,'evidence':'manual review','source_sha256':source_hash})+'\n')
    def fake_encoder(*_args):
        return lambda messages, system, target, cutoff: ([1,2,3],[-100,-100,3])
    monkeypatch.setattr(module, 'factory_encoder', fake_encoder)
    args = Namespace(input=inp, manifest=manifest, review=review, output_dir=tmp_path/'out',
                     train_output_dir=tmp_path/'training', model='model', local_only=True,
                     max_tokens=128, max_steps=10)
    module.export(args)
    record = json.loads((args.output_dir/'train.jsonl').read_text())
    assert record['conversations'][1]['value'] == row['conversations'][1]['content']
    report = json.loads((args.output_dir/'report.json').read_text())
    assert report['counts']['train'] == 1 and report['truncate'] is False
    module.verify(Namespace(prepared_dir=args.output_dir))
    (args.output_dir/'train.jsonl').write_text('tampered')
    with pytest.raises(ValueError, match='prepared file changed'):
        module.verify(Namespace(prepared_dir=args.output_dir))
