import pytest

from everos.entrypoints.memo_vault.agent_chat_service import resolve_model


def test_model_and_framework_are_independent():
    assert resolve_model('qwen-2', 'openhands') == 'swe'
    assert resolve_model('qwen', 'openhands') == 'default'
    with pytest.raises(ValueError, match='未知模型或 Agent'):
        resolve_model('qwen', 'unknown')


@pytest.mark.asyncio
async def test_real_openhands_conversation_uses_selected_model_and_prior_turns():
    from everos.entrypoints.memo_vault.openhands_runtime import OpenHandsRuntime

    class LocalModel:
        def __init__(self):
            self.calls = []
        async def generate(self, model, messages):
            self.calls.append((model, messages))
            return 'SDK replied'

    model = LocalModel()
    runtime = OpenHandsRuntime(model)
    answer = await runtime.generate('swe', [
        {'role': 'user', 'content': 'Remember moon'},
        {'role': 'assistant', 'content': 'I will remember moon'},
        {'role': 'user', 'content': 'What did I say?'},
    ])
    assert answer == 'SDK replied'
    assert model.calls[0][0] == 'swe'
    assert [x['role'] for x in model.calls[0][1]] == ['system', 'user', 'assistant', 'user']
    assert model.calls[0][1][-1]['content'] == 'What did I say?'


def test_framework_events_are_deleted_with_their_session(tmp_path):
    from everos.entrypoints.memo_vault.storage import VaultStore
    from everos.entrypoints.memo_vault.agent_chat_service import AgentChatStore
    vault = VaultStore(tmp_path)
    vault.initialize()
    store = AgentChatStore(vault)
    store.initialize()
    sid = store.create_session('personal')['id']
    aid = store.begin_message(sid, 'hello', 'qwen', 'openhands', 'r')['assistant_id']
    store.add_event(aid, 'model_output', 'hello')
    store.finish_message(aid, 'hello', framework_version='OpenHands SDK 1.62.0')
    assert store.get_session(sid)['messages'][-1]['events'][0]['content'] == 'hello'
    store.delete_session(sid)
    with vault.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM agent_chat_events').fetchone()[0] == 0
