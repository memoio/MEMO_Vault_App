"""Serial model worker supervisor, isolated from the Vault's Python environment."""

import asyncio
import json
import os
from pathlib import Path


class AgentRuntime:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.process = None

    async def generate(self, agent, messages):
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                root = Path(__file__).resolve().parents[4]
                python = os.environ.get(
                    "MEMO_AGENT_PYTHON",
                    str(
                        root
                        / ".memo-vault/agent-training/envs/qwen35-inference-py312/bin/python"
                    ),
                )
                worker = Path(__file__).with_name("agent_inference_worker.py")
                self.process = await asyncio.create_subprocess_exec(
                    python,
                    "-u",
                    str(worker),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=None,
                )
            try:
                self.process.stdin.write(
                    (
                        json.dumps(
                            {"agent": agent, "messages": messages}, ensure_ascii=False
                        )
                        + "\n"
                    ).encode()
                )
                await self.process.stdin.drain()
                response = await asyncio.wait_for(
                    self.process.stdout.readline(), timeout=900
                )
                if not response:
                    raise RuntimeError("本地模型进程已退出，请检查模型环境")
                result = json.loads(response)
                if "error" in result:
                    raise RuntimeError(result["error"])
                if result.get("agent") != agent:
                    raise RuntimeError("模型路由不一致")
                return result["text"]
            except (TimeoutError, asyncio.CancelledError):
                await self.close()
                raise

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None
