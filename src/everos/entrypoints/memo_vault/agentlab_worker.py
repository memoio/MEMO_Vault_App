"""AgentLab MostBasicAgent in no-browser text mode, using the local model bridge."""
import json
import sys
from dataclasses import dataclass
from importlib.metadata import version

protocol = sys.stdout
sys.stdout = sys.stderr

from agentlab.agents.most_basic_agent.most_basic_agent import MostBasicAgent
from agentlab.llm.base_api import AbstractChatModel


def emit(value):
    print(json.dumps(value, ensure_ascii=False), file=protocol, flush=True)


class VaultChatModel(AbstractChatModel):
    def __init__(self, prior):
        self.prior = prior
        self.answer = None

    def __call__(self, discussion):
        # MostBasicAgent only provides a web-task prompt which requests a click
        # even on an empty page. Translate that prompt for no-browser chat;
        # AgentLab still owns get_action and its parser, not a browser workflow.
        if len(discussion) < 2:
            raise ValueError('AgentLab 未提供预期的请求')
        prompts = [
            {'role':'system','content':'You are a helpful assistant. Answer the user directly. No external tools or browser are available.'},
            *self.prior,
            {'role':'user','content':request['history'][-1]['content']},
        ]
        emit({'type':'model_request','messages':prompts})
        reply=json.loads(sys.stdin.readline())
        if 'error' in reply:
            raise RuntimeError(reply['error'])
        answer=reply.get('text', '')
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError('模型返回空文本')
        self.answer=answer
        # Text-only bridge adapts the response for AgentLab's code-block parser.
        # This sentinel is NOT a model-generated browser action and is never run.
        return {'role':'assistant','content':'```\nsend_msg_to_user("EVEROS_TEXT_REPLY")\n```'}


@dataclass
class VaultModelArgs:
    model_name: str = 'everos-local'
    def make_model(self):
        return VaultChatModel(request['history'][:-1])


try:
    request=json.loads(sys.stdin.readline())
    history=request['history']
    if not history or history[-1]['role'] != 'user':
        raise ValueError('缺少用户消息')
    agent=MostBasicAgent(temperature=0, use_chain_of_thought=False, chat_model_args=VaultModelArgs())
    # The native agent composes its own prompt; advertise no available tools.
    class NoTools:
        def describe(self, **kwargs):
            return 'No tools or web page are available. Answer the user directly as text.'
    agent.action_set = NoTools()
    emit({'type':'framework_started','version':version('agentlab')})
    action, info=agent.get_action({'goal':'Text-only chat; answer this message directly: ' + history[-1]['content'], 'pruned_html':'No page or browser is available. Reply to the user in text.'})
    if action.strip() != 'send_msg_to_user("EVEROS_TEXT_REPLY")' or agent.chat.answer is None:
        raise RuntimeError('AgentLab 文本适配未正常结束')
    emit({'type':'completed','text':agent.chat.answer})
except Exception as exc:
    emit({'type':'error','error':f'{type(exc).__name__}: {exc}'})
