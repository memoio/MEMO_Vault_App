import pytest
from everos.entrypoints.memo_vault.agent_chat_service import resolve_model


def test_agentlab_uses_selected_model_independently():
    assert resolve_model('qwen', 'agentlab') == 'default'
    assert resolve_model('qwen-3', 'agentlab') == 'agentlab'
    assert resolve_model('qwen-2', 'agentlab') == 'swe'


@pytest.mark.asyncio
async def test_agentlab_native_agent_uses_bridge_and_history():
    from everos.entrypoints.memo_vault.agentlab_runtime import AgentLabRuntime
    class Model:
        def __init__(self): self.calls=[]
        async def generate(self, model, messages):
            self.calls.append((model, messages))
            return 'answer from model'
    model=Model()
    events=[]
    result=await AgentLabRuntime(model).generate('agentlab', [
        {'role':'user','content':'remember blue'},
        {'role':'assistant','content':'noted'},
        {'role':'user','content':'what was it?'},
    ], emit=lambda kind, content: events.append((kind,content)))
    assert result == 'answer from model'
    assert model.calls[0][0] == 'agentlab'
    assert [m['role'] for m in model.calls[0][1]] == ['system','user','assistant','user']
    assert 'what was it?' in model.calls[0][1][-1]['content']
    assert [kind for kind,_ in events] == ['framework_started','model_request','model_output','framework_completed']


@pytest.mark.asyncio
async def test_agentlab_model_failure_is_reported():
    from everos.entrypoints.memo_vault.agentlab_runtime import AgentLabRuntime
    class Broken:
        async def generate(self, model, messages):
            raise RuntimeError('model offline')
    with pytest.raises(RuntimeError, match='model offline'):
        await AgentLabRuntime(Broken()).generate('agentlab', [{'role':'user','content':'hello'}])


@pytest.mark.asyncio
async def test_background_route_persists_agentlab_events_and_response(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from everos.entrypoints.memo_vault.storage import VaultStore
    from everos.entrypoints.memo_vault.agent_chat_service import AgentChatStore
    from everos.entrypoints.memo_vault.agentlab_runtime import AgentLabRuntime
    from everos.entrypoints.memo_vault.routes.agent_chat import NewMessage, send
    class Model:
        async def generate(self, model, messages):
            assert model == 'agentlab'
            return '框架已回答'
    vault=VaultStore(tmp_path); vault.initialize()
    store=AgentChatStore(vault); store.initialize()
    sid=store.create_session('personal')['id']
    state=SimpleNamespace(agent_chat_store=store,agentlab_runtime=AgentLabRuntime(Model()),agent_chat_tasks=set())
    request=SimpleNamespace(app=SimpleNamespace(state=state))
    response=await send(sid, NewMessage(content='你好',model_id='qwen-3',agent_id='agentlab',request_id='test'),request)
    assert response['data']['messages'][-1]['status']=='pending'
    await asyncio.gather(*list(state.agent_chat_tasks))
    message=store.get_session(sid)['messages'][-1]
    assert message['content']=='框架已回答'
    assert message['framework_version'].startswith('AgentLab')
    assert [event['type'] for event in message['events']]==['framework_started','model_request','model_output','framework_completed']


@pytest.mark.asyncio
async def test_agentlab_does_not_execute_or_claim_model_browser_action():
    from everos.entrypoints.memo_vault.agentlab_runtime import AgentLabRuntime
    class Model:
        async def generate(self, model, messages):
            return "click('a314')"
    events=[]
    result=await AgentLabRuntime(Model()).generate('agentlab', [{'role':'user','content':'hello'}], emit=lambda kind, content:events.append((kind,content)))
    assert result == "click('a314')"
    assert events[-1][0] == 'framework_completed'
    assert '工具调用：0 次' in events[-1][1]
    assert all(kind not in ('tool_call','tool_result') for kind,_ in events)
