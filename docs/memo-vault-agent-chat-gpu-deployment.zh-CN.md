# MEMO Vault 对话 Agent：已有 GPU 服务器升级部署方案

> 面向服务器管理员。本文是原 `docs/memo-vault-gpu-deployment.zh-CN.md` 的增量方案，不是重新部署 EverOS、数据库或远程 MCP 的方案。目标服务器以已有运行环境和管理员核实结果为准；截至编写时未登录目标服务器、未执行这些命令或验证其 GPU / 网络 / 权限。请在维护窗口按顺序操作，任何一步失败先停止并保留日志。

## 1. 目标、版本与范围

现有服务器依据用户描述部署于 commit `c2b60487`（`feat:add remote MCP Server`），该版本早于“对话 Agent”，**不能直接使用本方案提供的 UI/API**。管理员有仓库权限，可按既有流程拉取并合并包含本功能的代码；本文从代码更新完成后的依赖、模型和框架部署开始，不重复说明 Git 操作。

本次交付的当前实现为：Default 的普通文本模型调用；OpenHands、SWE-agent、AgentLab 的文本对话适配；可以独立选择基座与三个 Adapter。三个框架当前**未开放文件、终端和网页工具**，SWE/AgentLab 有文本模式适配动作，不能当作真实代码/浏览器任务能力。当前 Qwen worker 第一次启动时会加载 **全部三个 Adapter**，即使只测试基座也必须都就位。部署成功不能证明微调效果或工具协议兼容。

原文档采用账号和代码目录 `/srv/memo-vault/app`。用户截图能确认服务账户为 `memo-vault`，`HOME=/srv/memo-vault` 及 `MEMO_VAULT_DATA_DIR=/srv/memo-vault/data/vault`，**截图不能证明实际代码目录就是 `/srv/memo-vault/app`**。以下以 `/srv/memo-vault/app` 为工作假设；操作前必须通过 `systemctl cat memo-vault` 和实际 `WorkingDirectory`/`ExecStart` 核对。若真实路径或服务名不同，把下面路径统一替换，尤其是 systemd 中的变量。

## 2. 路径和数据隔离

| 用途 | 已有或推荐服务器路径 | 规则 |
|---|---|---|
| 应用源码、主服务环境 | `/srv/memo-vault/app`、`/srv/memo-vault/app/.venv` | 仅代码与现有依赖；先核对实际服务路径 |
| EverOS 数据 | `/srv/memo-vault/data/everos` | 已有数据；不要重复 init、覆盖配置 |
| Vault 数据与聊天数据库 | `/srv/memo-vault/data/vault/vault.sqlite3` | 保持 `MEMO_VAULT_DATA_DIR=/srv/memo-vault/data/vault`；聊天表随服务启动建在这个数据库中 |
| 对话基座和 Adapter | `/srv/memo-vault/models/agent-chat` | 新建独立模型目录，不放在仓库或 Vault 数据库目录 |
| 对话专用 Python/源码 | `/srv/memo-vault/agent-chat/envs`、`/srv/memo-vault/agent-chat/src` | 独立于主服务 `.venv`；不复制开发机虚拟环境 |
| 浏览器（预下载） | `/srv/memo-vault/agent-chat/playwright-browsers` | 部署时预下载 Chromium；本期不执行网页任务 |

实际代码中，没有把 `/root/EverOS` 硬编码为加载路径；但**默认值**根据源码位置指向项目目录内 `.memo-vault/...`，与目标服务器方案不一致。下面必须显式配置：

```ini
MEMO_VAULT_DATA_DIR=/srv/memo-vault/data/vault
MEMO_AGENT_WEIGHTS=/srv/memo-vault/models/agent-chat
MEMO_AGENT_PYTHON=/srv/memo-vault/agent-chat/envs/qwen35/bin/python
MEMO_OPENHANDS_PYTHON=/srv/memo-vault/agent-chat/envs/openhands/bin/python
MEMO_SWE_PYTHON=/srv/memo-vault/agent-chat/envs/sweagent/bin/python
MEMO_AGENTLAB_PYTHON=/srv/memo-vault/agent-chat/envs/agentlab/bin/python
```

`MEMO_AGENT_WEIGHTS` 是**四个目录的父目录**，不是单个模型目录。`adapter_config.json` 中的 `base_model_name_or_path=/data/openhands/model/qwen35-08b` 是旧训练机器记录的元数据；现有 worker 显式从 `MEMO_AGENT_WEIGHTS/qwen35-08b` 加载基座，再将各 Adapter 加到模型，不需要旧目录。不要手动覆盖该元数据文件。

独立主机运行账号 `memo-vault` 必须可读取所有模型文件和可执行环境，且可写 Vault 数据目录。ZIP 是开发机 root 创建的，复制时不要沿用开发机文件属主；按服务器服务账户创建并解压。已有数据库和所有者凭据属于原服务器，不从开发机复制。执行前由管理员按既有策略备份数据库、配置与源码，检查当前运行任务；新代码的初始化会为聊天功能建表/补列，回滚二进制不会自动回滚数据库。

## 3. 更新应用依赖并构建前端

管理员按既有仓库权限拉取并合并代码后，在应用目录更新主环境依赖并构建前端。请沿用服务器现有 Python、Node 和发布方式；`uv sync` 会更新主 `.venv`，不应指向下面的对话专用环境。

```bash
sudo -iu memo-vault bash -lc 'cd /srv/memo-vault/app && uv sync --locked --python 3.12 --extra local-rerank --extra mcp'
sudo -iu memo-vault bash -lc 'cd /srv/memo-vault/app/apps/memo-vault-ui && npm ci && npm run build'
```

## 4. 接收 Adapter ZIP

开发机交付文件：`agent-chat-adapters-20261116.zip`，SHA-256：

```text
4eba7891f579b402589e2ae00986e818d0a3ae8fc5995d88adff7502b15481dd
```

ZIP 内只有 `openhands/`、`sweagent/`、`agentlab/` 各自的 `adapter_config.json`、`adapter_model.safetensors`，以及 `SHA256SUMS` 和说明，不含训练日志、tokenizer、数据、凭据或基座。开发机原始目录见第 10 节。三种 Adapter 均独立挂接同一基座；服务器无需下载三份基座。

管理员先确认接收路径并以服务账号创建目标父目录：

```bash
sudo -iu memo-vault bash -lc 'mkdir -p /srv/memo-vault/models/agent-chat /srv/memo-vault/agent-chat/envs /srv/memo-vault/agent-chat/src'
```

项目负责人从开发机向管理员事先指定且有权限的接收目录传输；以下 `<服务器地址>` 必须替换，截图没有提供可用 SSH 主机名或地址：

```bash
scp /root/EverOS/artifacts/agent-chat-adapters-20261116.zip memo-vault@<服务器地址>:/srv/memo-vault/agent-chat/
```

以 `memo-vault` 账号在服务器校验；以下命令要求 `unzip`。先检查三个目标不存在，避免覆盖已有权重。SHA 清单同时校验每个 Adapter；ZIP 目标若已存在应由管理员检查，而不是自动替换：

```bash
sudo -iu memo-vault bash -lc 'cd /srv/memo-vault/agent-chat && echo "4eba7891f579b402589e2ae00986e818d0a3ae8fc5995d88adff7502b15481dd  agent-chat-adapters-20261116.zip" | sha256sum -c && unzip -t agent-chat-adapters-20261116.zip'
sudo -iu memo-vault bash -lc 'cd /srv/memo-vault/models/agent-chat && test ! -e openhands && test ! -e sweagent && test ! -e agentlab && unzip -n /srv/memo-vault/agent-chat/agent-chat-adapters-20261116.zip && sha256sum -c SHA256SUMS'
```

若校验失败、目标已存在或属主不对，停止排查，不要继续启动。解压后目录应为：

```text
/srv/memo-vault/models/agent-chat/
├── qwen35-08b/              # 下一节单独下载
├── openhands/adapter_config.json + adapter_model.safetensors
├── sweagent/adapter_config.json + adapter_model.safetensors
├── agentlab/adapter_config.json + adapter_model.safetensors
├── SHA256SUMS
└── README.zh-CN.txt
```

## 5. 创建 Qwen 推理环境并下载基座

开发机已验证的独立 Qwen 推理环境为 Python 3.12.3、PyTorch 2.6.0 / CUDA 12.6、Transformers 5.20.2、PEFT 0.21.2、bitsandbytes 0.50.2、accelerate 1.4.0、huggingface_hub 2.4.0。它是可参考的本机快照，**不是目标服务器已验证的锁文件**。根据服务器 GPU 与驱动由管理员选择可用的 CUDA wheel；下方 `cu126` 仅在驱动兼容时作为候选。各包可能无法通过所用镜像获取，应保留错误并停下；切换包源不能改变主 `.venv`。

以下以普通账号执行；与三个框架环境使用同一 Python 3.12 解释器版本，但仍需分别创建虚拟环境，避免依赖版本冲突。已按原服务器部署方案安装 Python 3.12 时无需另下 Python 3.10；各虚拟环境仍会占用各自依赖的磁盘空间，缓存能否复用取决于包管理器、用户、索引及平台。

```bash
sudo -iu memo-vault bash -lc 'test ! -e /srv/memo-vault/agent-chat/envs/qwen35 && python3.12 -m venv /srv/memo-vault/agent-chat/envs/qwen35'
sudo -iu memo-vault bash -lc '/srv/memo-vault/agent-chat/envs/qwen35/bin/python -m pip --version || /srv/memo-vault/agent-chat/envs/qwen35/bin/python -m ensurepip --upgrade'
sudo -iu memo-vault bash -lc '/srv/memo-vault/agent-chat/envs/qwen35/bin/python -m pip install "torch==2.6.0" --index-url https://download.pytorch.org/whl/cu126'
sudo -iu memo-vault bash -lc '/srv/memo-vault/agent-chat/envs/qwen35/bin/python -m pip install "transformers==5.20.2" "peft==0.21.2" "bitsandbytes==0.50.2" "accelerate==1.4.0" "huggingface_hub==2.4.0" safetensors'
sudo -iu memo-vault bash -lc '/srv/memo-vault/agent-chat/envs/qwen35/bin/python -m pip check'
sudo -iu memo-vault bash -lc '/srv/memo-vault/agent-chat/envs/qwen35/bin/python -c "import torch,transformers,peft,bitsandbytes; print(torch.__version__,torch.version.cuda,transformers.__version__,peft.__version__,bitsandbytes.__version__); print(\"CUDA\",torch.cuda.is_available()); assert torch.cuda.is_available()"'
```

`python3.12 -m venv` 需要系统提供 venv/ensurepip；上面的 `ensurepip` 仅处理新建的对话专用环境。如果依赖下载失败或 pip 解析冲突，由管理员提供兼容离线包；不要复制 WSL `.venv`。安装加速算子不是本期前提，本机缺少可用加速实现时有 PyTorch 参考实现降速提示；不能仅凭包安装判断 GPU 内核生效。驱动和 PyTorch 的官方选择见 [PyTorch 安装文档](https://pytorch.org/get-started/previous-versions/)。

使用 [Qwen 官方模型](https://huggingface.co/Qwen/Qwen3.5-0.8B) 下载**训练使用的固定 revision**，不是 Ollama 模型，也不是仅下载 tokenizer。需要服务账号网络与 Hugging Face 下载权限；如果目录已存在先停止核查，不覆盖。下载过程中断可检查目录后决定续传；不要把中断目录当作完整模型：

```bash
sudo -iu memo-vault bash -lc 'test ! -e /srv/memo-vault/models/agent-chat/qwen35-08b && /srv/memo-vault/agent-chat/envs/qwen35/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id=\"Qwen/Qwen3.5-0.8B\", revision=\"2fc06364715b967f1860aea9cf38778875588b17\", local_dir=\"/srv/memo-vault/models/agent-chat/qwen35-08b\")"'
```

实际运行时 worker 强制离线读取。下载后检查文件（模型分片形式以实际 revision 为准），并以模型 worker 真实加载作为最终完整性核验：

```bash
sudo -iu memo-vault bash -lc 'ls -lah /srv/memo-vault/models/agent-chat/qwen35-08b && test -f /srv/memo-vault/models/agent-chat/qwen35-08b/config.json && test -f /srv/memo-vault/models/agent-chat/qwen35-08b/tokenizer.json'
```

## 6. 创建框架独立环境

以下命令为**候选可复现安装**，以当前开发机可运行版本为参考；服务器依赖、平台和网络未经测试。请以 `memo-vault` 账号运行；每个 `test ! -e` 失败表示目录已存在，应检查内容并按需复用，不能清理已有目录或覆盖。三个框架的 Python 是 3.12，独立于 Qwen 推理环境。

### 6.1 OpenHands SDK

```bash
sudo -iu memo-vault bash -lc 'test ! -e /srv/memo-vault/agent-chat/envs/openhands && python3.12 -m venv /srv/memo-vault/agent-chat/envs/openhands'
sudo -iu memo-vault bash -lc '/srv/memo-vault/agent-chat/envs/openhands/bin/python -m pip install --upgrade pip'
sudo -iu memo-vault bash -lc '/srv/memo-vault/agent-chat/envs/openhands/bin/python -m pip install "openhands-sdk==1.62.0" "openhands-tools==1.62.0"'
sudo -iu memo-vault bash -lc '/srv/memo-vault/agent-chat/envs/openhands/bin/python -m pip check && /srv/memo-vault/agent-chat/envs/openhands/bin/python -c "from openhands.sdk import LLM,Agent,Conversation; from openhands.tools.terminal import TerminalTool; print(\"OpenHands OK\")"'
```

SDK 与 tools 需同版本，见 [OpenHands 官方安装说明](https://github.com/OpenHands/docs/blob/main/sdk/getting-started.mdx)。该安装不开放终端工具。

### 6.2 SWE-agent

固定当前本机验证过的源码 commit，采用 editable 安装，源码目录必须保留：

```bash
sudo -iu memo-vault bash -lc 'test ! -e /srv/memo-vault/agent-chat/src/SWE-agent && git clone https://github.com/SWE-agent/SWE-agent.git /srv/memo-vault/agent-chat/src/SWE-agent && git -C /srv/memo-vault/agent-chat/src/SWE-agent checkout --detach 3ea751c087f32b16e039a2233dd6eefecef325d5'
sudo -iu memo-vault bash -lc 'git -C /srv/memo-vault/agent-chat/src/SWE-agent rev-parse HEAD && git -C /srv/memo-vault/agent-chat/src/SWE-agent status --short'
sudo -iu memo-vault bash -lc 'test ! -e /srv/memo-vault/agent-chat/envs/sweagent && python3.12 -m venv /srv/memo-vault/agent-chat/envs/sweagent'
sudo -iu memo-vault bash -lc '/srv/memo-vault/agent-chat/envs/sweagent/bin/python -m pip install --upgrade pip'
sudo -iu memo-vault bash -lc '/srv/memo-vault/agent-chat/envs/sweagent/bin/python -m pip install -e /srv/memo-vault/agent-chat/src/SWE-agent "swe-rex==1.4.0"'
sudo -iu memo-vault bash -lc '/srv/memo-vault/agent-chat/envs/sweagent/bin/python -m pip check && /srv/memo-vault/agent-chat/envs/sweagent/bin/sweagent --help'
```

本期只运行无 shell 的文本适配，不需要拉 SWE Docker 镜像；未来真实代码执行须另外设计隔离环境。官方步骤见 [SWE-agent 安装说明](https://github.com/SWE-agent/SWE-agent/blob/main/docs/installation/source.md)。

### 6.3 AgentLab

固定本机使用的 AgentLab commit 和部分已核查关键包。依赖较大，PyPI 的传递依赖未全部固定，故仍需 `pip check` 和真实测试；不得称为与开发机逐包完全一致。

```bash
sudo -iu memo-vault bash -lc 'test ! -e /srv/memo-vault/agent-chat/src/AgentLab && git clone https://github.com/ServiceNow/AgentLab.git /srv/memo-vault/agent-chat/src/AgentLab && git -C /srv/memo-vault/agent-chat/src/AgentLab checkout --detach 8488c564eea0525aaff5dea189bb03a9b529546e'
sudo -iu memo-vault bash -lc 'git -C /srv/memo-vault/agent-chat/src/AgentLab rev-parse HEAD && git -C /srv/memo-vault/agent-chat/src/AgentLab status --short'
sudo -iu memo-vault bash -lc 'test ! -e /srv/memo-vault/agent-chat/envs/agentlab && python3.12 -m venv /srv/memo-vault/agent-chat/envs/agentlab'
sudo -iu memo-vault bash -lc '/srv/memo-vault/agent-chat/envs/agentlab/bin/python -m pip install --upgrade pip'
sudo -iu memo-vault bash -lc '/srv/memo-vault/agent-chat/envs/agentlab/bin/python -m pip install --prefer-binary -e /srv/memo-vault/agent-chat/src/AgentLab "browsergym==0.14.2" "playwright==1.44.0" "anthropic==0.62.0" "litellm==1.75.3" "transformers==4.55.0" "gradio==5.41.1" "mcp==1.10.1" "pydantic==2.11.7" "browsergym-workarena==0.4.3" "weblinx-browsergym==0.0.3"'
sudo -iu memo-vault bash -lc '/srv/memo-vault/agent-chat/envs/agentlab/bin/python -m pip check && /srv/memo-vault/agent-chat/envs/agentlab/bin/python -c "import agentlab,bgym; from agentlab.agents.most_basic_agent.most_basic_agent import MostBasicAgent; print(\"AgentLab OK\")"'
```

当前文本模式不打开网页，但按部署准备需求，先把 Chromium 下载到 AgentLab 专用目录，并保存本次环境快照，便于后续启用浏览器任务时复用。未来服务实际启动浏览器时也要设置同一个 `PLAYWRIGHT_BROWSERS_PATH`；下载浏览器本身不代表本期已接通网页功能，运行时如缺系统库还需按服务器发行版补齐。

```bash
sudo -iu memo-vault bash -lc 'export PLAYWRIGHT_BROWSERS_PATH=/srv/memo-vault/agent-chat/playwright-browsers; /srv/memo-vault/agent-chat/envs/agentlab/bin/python -m playwright install chromium'
sudo -iu memo-vault bash -lc '/srv/memo-vault/agent-chat/envs/agentlab/bin/python -m pip freeze > /srv/memo-vault/agent-chat/agentlab-candidate-freeze.txt'
```

参考 [AgentLab 官方文档](https://github.com/ServiceNow/AgentLab/blob/main/README.md)。

记录安装快照，不包含凭据：

```bash
sudo -iu memo-vault bash -lc 'for name in qwen35 openhands sweagent agentlab; do /srv/memo-vault/agent-chat/envs/$name/bin/python -m pip freeze > /srv/memo-vault/agent-chat/$name-freeze.txt; done'
```

## 7. 重启前模型与权限探针

以服务账号、相同环境变量直接运行源码中的模型 worker，打印四个响应；该命令实际加载模型和三个 Adapter、占用 GPU，提前由管理员安排资源，不代表框架已接通。保留 stderr 日志检查 GPU / CUDA 错误：

```bash
sudo -iu memo-vault bash -lc 'cd /srv/memo-vault/app && printf "%s\n" "{\"agent\":\"default\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}" "{\"agent\":\"openhands\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}" "{\"agent\":\"swe\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}" "{\"agent\":\"agentlab\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}]}" | MEMO_AGENT_WEIGHTS=/srv/memo-vault/models/agent-chat MEMO_AGENT_MAX_NEW_TOKENS=8 /srv/memo-vault/agent-chat/envs/qwen35/bin/python -u src/everos/entrypoints/memo_vault/agent_inference_worker.py'
```

四行 JSON 均应包含对应 `agent`、非空 `text` 和 `device`，没有 `error`；`device=cuda` 才说明该次走 GPU。环境可能因 OOM/依赖缺失回退 CPU 或失败；结合 `nvidia-smi` 和日志排查，不要仅依赖 `torch.cuda.is_available()`。本机已验证 ZIP 解包的 6 个 Adapter 文件加独立基座可在 CUDA 分别加载三种 Adapter；不能据此保证目标服务器通过。

## 8. 设置已部署服务环境并验收

管理员先查看当前服务和环境文件，**仅向实际 Vault systemd 服务添加变量**；可以编辑管理员原有 `/etc/memo-vault/runtime.env` 或使用 `systemctl edit memo-vault` 建 drop-in。下面是 drop-in 内容示例，复制前确认实际工作目录、服务用户和路径；不要覆盖 `MEMO_VAULT_EVEROS_URL`、`MEMO_VAULT_UI_ORIGINS`、MCP、原有 token 等已有设置：

```ini
[Service]
Environment="MEMO_VAULT_DATA_DIR=/srv/memo-vault/data/vault"
Environment="MEMO_AGENT_WEIGHTS=/srv/memo-vault/models/agent-chat"
Environment="MEMO_AGENT_PYTHON=/srv/memo-vault/agent-chat/envs/qwen35/bin/python"
Environment="MEMO_OPENHANDS_PYTHON=/srv/memo-vault/agent-chat/envs/openhands/bin/python"
Environment="MEMO_SWE_PYTHON=/srv/memo-vault/agent-chat/envs/sweagent/bin/python"
Environment="MEMO_AGENTLAB_PYTHON=/srv/memo-vault/agent-chat/envs/agentlab/bin/python"
```

更改前核对原数据路径，确认正在运行的 Vault 是单 worker。重启会中断 Vault 的后台任务，必须由管理员选定维护窗口。完成代码、前端、环境、模型和备份检查后再执行：

```bash
sudo systemctl daemon-reload
sudo systemctl restart memo-vault
systemctl is-active memo-vault
sudo journalctl -u memo-vault -n 100 --no-pager
```

检查 `systemctl cat memo-vault`、服务实际环境与路径（不要输出或分享整个进程环境，其中可能有凭据），确认 `MEMO_VAULT_DATA_DIR` 未改变；通过浏览器 owner 认证进入“对话 Agent”。不要为此重启 EverOS、MCP、Ollama 或 Nginx，除非管理员确认其确实需要；前端按服务器既有静态资源发布方式发布本次 `dist`。原数据库和项目会话仍应存在。

逐项验收并记录实际模型、框架、设备、时间和结果：

1. Default + 基座发送简短消息；确认会话出现在原 `/srv/memo-vault/data/vault` 数据库。
2. OpenHands + `Qwen3.5-0.8B-o`；SWE + `-s`；AgentLab + `-a` 分别发送消息并展开运行详情，检查真实框架启动、模型请求、最终回答和零工具调用。三个框架也各自用基座验证一次。
3. 连续两轮，例如先发送 `Remember the word moon.`，再问 `What word did I ask you to remember?`。记录实际答案，框架运行成功与回答正确分开判断。
4. 创建两个会话并切换；检查后台运行期间页面仍可操作；可在测试会话确认删除及其数据库消息清理。模型/Agent 的会话选择保存在**浏览器本地**，不随数据库跨浏览器同步。
5. 检查 `nvidia-smi` 和服务日志中的设备与报错；现有 GPU 也可能由 Ollama/reranker 使用，观察真实峰值而非只看理论显存。无凭据请求仍保持原有权限限制，远程 MCP 原有健康和认证验证按原文档执行。

## 9. 失败处理与回滚

- `No module named ...`：核对 `MEMO_*_PYTHON`、实际服务用户和独立环境，先执行对应解释器 `pip check`，不要向主 `.venv` 盲目安装。
- 找不到 base / Adapter：核对 `MEMO_AGENT_WEIGHTS` 是四个子目录的父目录、文件名大小写、服务账户目录遍历权限和 `SHA256SUMS`。
- `CUDA` 失败、显存不足或 CPU 回退：查实际驱动、共享负载、PyTorch CUDA、bitsandbytes 和 stderr；不能只看 `nvidia-smi`。本机见到过缺少加速算子导致参考实现变慢，实际兼容性以服务器为准。
- 框架导入失败：检查源码固定 commit、对应环境 Python、`pip check`、完整 stderr；可编辑安装需要源码存在。
- AgentLab/SWE/OpenHands 出现命令或 `click(...)` 文本：本期不执行它们；确认“运行详情”是否有工具调用，不要把文本当执行结果。AgentLab 的网页训练数据格式与当前文本适配尚未验证兼容。
- 重启后数据库看似为空：**先检查实际 `MEMO_VAULT_DATA_DIR` 和用户权限**，不要重复 init、覆盖或拷贝数据库。
- 如需回滚，管理员在维护窗口按既有 Git/发布流程恢复上一个已记录版本和相应前端构建、恢复原环境配置，并验证原 EverOS/Vault/MCP；数据库迁移不会自动撤销。模型、独立环境和数据库先保留，不运行清库或删除命令。

## 10. 开发机交接清单

- 原 Adapter 目录（包含训练产物，不应整目录上 Git）：`/root/EverOS/.memo-vault/model-weights-20261113/openhands`、`/root/EverOS/.memo-vault/model-weights-20261113/sweagent`、`/root/EverOS/.memo-vault/model-weights-20261113/agentlab`。
- 交接 ZIP：`/root/EverOS/artifacts/agent-chat-adapters-20261116.zip`；SHA-256 见第 4 节。
- 基座源 `Qwen/Qwen3.5-0.8B`，固定 revision `2fc06364715b967f1860aea9cf38778875588b17`；服务器按第 5 节单独下载。
- ZIP 不包含代码、环境或数据库；请项目负责人把**所有**相关未跟踪代码纳入提交后提供完整 commit SHA 和本文，服务器再按其既有流程合并。
