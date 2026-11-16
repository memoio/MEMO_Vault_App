"""Run a real OpenHands conversation with a parent-owned local model bridge."""
import asyncio
import contextlib
import json
import sys
import tempfile

# Reserve stdout for the private line protocol; SDK logs go to stderr.
protocol = sys.stdout
sys.stdout = sys.stderr

from litellm.types.utils import Choices, Message as RawMessage, ModelResponse
from openhands.sdk import Agent, Conversation, LLM
from openhands.sdk.llm import Message, TextContent
from openhands.sdk.llm.llm_response import LLMResponse
from openhands.sdk.llm.utils.metrics import MetricsSnapshot, TokenUsage


def emit(data):
    print(json.dumps(data, ensure_ascii=False), file=protocol, flush=True)


class VaultLLM(LLM):
    def uses_responses_api(self):
        return False

    def completion(self, messages, tools=None, **kwargs):
        history = []
        for message in messages:
            if message.role not in ('system', 'user', 'assistant') or message.tool_calls:
                raise ValueError('本地模型桥不支持该 OpenHands 消息类型')
            parts = []
            for part in message.content:
                if not isinstance(part, TextContent):
                    raise ValueError('本地模型桥只支持文本消息')
                parts.append(part.text)
            history.append({'role': message.role, 'content': '\n'.join(parts)})
        # Replay stored turns inside the framework-provided system/user context.
        request_history = request['history'][:-1]
        index = next((i for i, item in enumerate(history) if item['role'] == 'user'), len(history))
        history[index:index] = request_history
        emit({'type': 'model_request', 'messages': history})
        reply = json.loads(sys.stdin.readline())
        if 'error' in reply:
            raise RuntimeError(reply['error'])
        text = reply['text']
        msg = Message(role='assistant', content=[TextContent(text=text)])
        raw = ModelResponse(id='vault-local', choices=[Choices(message=RawMessage(role='assistant', content=text), index=0, finish_reason='stop')], model=self.model, object='chat.completion')
        metrics = MetricsSnapshot(model_name=self.model, accumulated_cost=0.0, max_budget_per_task=None, accumulated_token_usage=TokenUsage(model=self.model, prompt_tokens=0, completion_tokens=0))
        return LLMResponse(message=msg, metrics=metrics, raw_response=raw)

    async def acompletion(self, messages, tools=None, **kwargs):
        return await asyncio.to_thread(self.completion, messages, tools=tools, **kwargs)


try:
    request = json.loads(sys.stdin.readline())
    captured = []
    def on_event(event):
        if type(event).__name__ == 'MessageEvent' and getattr(event, 'source', None) == 'agent':
            captured.append(event)
    with tempfile.TemporaryDirectory(prefix='everos-openhands-') as workspace:
        agent = Agent(llm=VaultLLM(model='vault-local', usage_id='vault-local'), tools=[], system_prompt='You are a helpful assistant. Answer the user directly. No external tools are available.')
        conversation = Conversation(agent=agent, workspace=workspace, callbacks=[on_event], visualizer=None, max_iteration_per_run=2)
        try:
            emit({'type': 'framework_started', 'version': '1.62.0'})
            conversation.send_message(request['history'][-1]['content'])
            conversation.run()
            if not captured:
                raise RuntimeError('OpenHands 未返回助手消息')
            message = captured[-1].llm_message
            text = '\n'.join(part.text for part in message.content if isinstance(part, TextContent))
            if not text.strip():
                raise RuntimeError('OpenHands 返回空消息')
            emit({'type': 'completed', 'text': text})
        finally:
            conversation.close()
except Exception as exc:
    emit({'type': 'error', 'error': str(exc)})
