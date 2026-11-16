import pytest
from everos.entrypoints.memo_vault.agent_chat_service import resolve_model


def test_swe_uses_selected_model_independently():
    assert resolve_model('qwen', 'swe') == 'default'
    assert resolve_model('qwen-2', 'swe') == 'swe'
    assert resolve_model('qwen-1', 'swe') == 'openhands'


@pytest.mark.asyncio
async def test_swe_native_agent_uses_bridge_and_history():
    from everos.entrypoints.memo_vault.swe_runtime import SWERuntime
    class Model:
        def __init__(self): self.calls=[]
        async def generate(self, model, messages):
            self.calls.append((model,messages))
            return 'answer from model'
    model=Model()
    events=[]
    result=await SWERuntime(model).generate('swe', [
        {'role':'user','content':'remember blue'},
        {'role':'assistant','content':'noted'},
        {'role':'user','content':'what was it?'},
    ], emit=lambda kind, content: events.append((kind,content)))
    assert result == 'answer from model'
    assert model.calls[0][0] == 'swe'
    assert [m['role'] for m in model.calls[0][1]] == ['system','user','assistant','user']
    assert model.calls[0][1][-1]['content'] == 'what was it?'
    assert [k for k,_ in events] == ['framework_started','model_request','model_output','framework_completed']


@pytest.mark.asyncio
async def test_model_error_is_reported_without_running_a_command():
    from everos.entrypoints.memo_vault.swe_runtime import SWERuntime
    class BrokenModel:
        async def generate(self, model, messages):
            raise RuntimeError('model offline')
    with pytest.raises(RuntimeError, match='model offline'):
        await SWERuntime(BrokenModel()).generate('swe', [{'role':'user','content':'hello'}])
