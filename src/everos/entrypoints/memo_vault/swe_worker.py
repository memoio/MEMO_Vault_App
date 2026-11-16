"""SWE-agent native run loop for text-only chat, with no shell or tools."""
import json
import sys
import tempfile
from pathlib import Path

protocol = sys.stdout
sys.stdout = sys.stderr

from sweagent import __version__
from sweagent.agent.agents import DefaultAgent, TemplateConfig
from sweagent.agent.models import AbstractModel, InstanceStats
from sweagent.agent.problem_statement import TextProblemStatement
from sweagent.tools.tools import ToolConfig, ToolHandler


def emit(event):
    print(json.dumps(event, ensure_ascii=False), file=protocol, flush=True)


class TextOnlyEnvironment:
    name = 'everos-text-only'
    repo = None

    def set_env_variables(self, variables):
        pass

    def communicate(self, *args, **kwargs):
        raise RuntimeError('文本对话未开放命令执行')


class TextOnlyTools(ToolHandler):
    def __init__(self):
        super().__init__(ToolConfig(enable_bash_tool=False))
        self.mock_state = {}

    def install(self, env):
        pass

    def parse_actions(self, output):
        # Chat adaptation: return the unmodified answer and request the native
        # agent's exit action; this is not a model-generated tool call.
        return output['message'], 'exit'


class TextOnlyAgent(DefaultAgent):
    def attempt_autosubmission_after_error(self, step):
        # No repository or submission exists in chat mode; never invoke shell.
        raise RuntimeError(step.output or 'SWE-agent 运行失败')


class VaultModel(AbstractModel):
    def __init__(self, prior):
        self.stats = InstanceStats()
        self.prior = prior

    def query(self, history, action_prompt='> '):
        system = [item for item in history if item['role'] == 'system']
        current = [item for item in history if item['role'] == 'user']
        if len(system) != 1 or not current:
            raise ValueError('SWE-agent 对话历史格式不受支持')
        messages = [{'role':'system','content':system[0]['content']}, *self.prior, {'role':'user','content':current[-1]['content']}]
        emit({'type':'model_request','messages':messages})
        reply = json.loads(sys.stdin.readline())
        if 'error' in reply:
            raise RuntimeError(reply['error'])
        if not reply.get('text', '').strip():
            raise ValueError('模型返回空文本')
        self.stats.api_calls += 1
        return {'message':reply['text']}


try:
    request=json.loads(sys.stdin.readline())
    history=request['history']
    if not history or history[-1]['role'] != 'user':
        raise ValueError('缺少用户消息')
    model=VaultModel(history[:-1])
    agent=TextOnlyAgent(templates=TemplateConfig(
        system_template='You are a helpful assistant. Answer the user directly. No external tools are available.',
        instance_template='{{ problem_statement }}',
    ), tools=TextOnlyTools(), history_processors=[], model=model, max_requeries=1)
    emit({'type':'framework_started','version':__version__})
    with tempfile.TemporaryDirectory(prefix='everos-swe-') as directory:
        result=agent.run(TextOnlyEnvironment(), TextProblemStatement(text=history[-1]['content']), output_dir=Path(directory))
        if result.info.get('exit_status') != 'exit_command' or not result.trajectory:
            raise RuntimeError('SWE-agent 未正常结束文本对话')
        emit({'type':'completed','text':result.trajectory[-1]['response']})
except Exception as exc:
    emit({'type':'error','error':f'{type(exc).__name__}: {exc}'})
