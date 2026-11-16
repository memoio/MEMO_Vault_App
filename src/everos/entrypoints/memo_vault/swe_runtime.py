"""Supervise a real SWE-agent run using the local model worker."""
import asyncio
import json
import os
from pathlib import Path


class SWERuntime:
    framework_version = "SWE-agent 1.1.0"
    def __init__(self, model_runtime):
        self.model_runtime = model_runtime

    async def generate(self, model, messages, emit=None):
        root = Path(__file__).resolve().parents[4]
        python = os.environ.get('MEMO_SWE_PYTHON', str(root / '.memo-vault/agent-frameworks/envs/sweagent-py312/bin/python'))
        worker = Path(__file__).with_name('swe_worker.py')
        process = await asyncio.create_subprocess_exec(python, '-u', str(worker), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=None)
        async def send(value):
            process.stdin.write((json.dumps(value, ensure_ascii=False) + '\n').encode())
            await process.stdin.drain()
        try:
            await send({'history': messages})
            async with asyncio.timeout(900):
                while line := await process.stdout.readline():
                    event = json.loads(line)
                    if event['type'] == 'framework_started':
                        if emit: emit('framework_started', event['version'])
                    elif event['type'] == 'model_request':
                        if emit: emit('model_request', '本地模型调用')
                        try:
                            output = await self.model_runtime.generate(model, event['messages'])
                            if emit: emit('model_output', output)
                            await send({'text': output})
                        except Exception as exc:
                            await send({'error': str(exc)})
                    elif event['type'] == 'completed':
                        if emit: emit('framework_completed', '文本对话适配 · 工具调用：0 次')
                        return event['text']
                    elif event['type'] == 'error':
                        raise RuntimeError('SWE-agent 运行失败：' + event['error'])
                raise RuntimeError('SWE-agent 进程已退出')
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
