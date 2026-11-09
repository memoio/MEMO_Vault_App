# MEMO Vault：GPU 服务器首次部署参考

本文供服务器管理员使用当前 GitHub `main` 分支部署独立测试实例。2026-11-02 核对的版本为 `841b11f48e0b73dadc269e28b5bc924e37d7f67f`；实际部署时以克隆得到的版本为准，记录 `git rev-parse HEAD` 便于追踪。本文目前需由项目负责人直接发送给管理员，只有提交并推送后才能从仓库获得。远程 MCP 部分依赖包含 `src/everos/entrypoints/memo_vault_mcp/http_server.py` 的新代码：本地修改目前尚未提交、推送或部署，旧版 `841b11f` 和旧交接包不包含该入口。请先交付并核对包含该文件的实际版本，不能仅按本文配置旧版。以下是单机 Ubuntu/systemd 参考方案；请勿覆盖现有服务或复制旧服务器的虚拟环境。

## 1. 资源和隔离要求

- GPU：现有机器 24 GB 显存；需确认实际型号、驱动、可用显存和 Ollama 支持情况，管理员安排共享资源配额。使用 `nvidia-smi`、`nvidia-smi -L` 检查；Ollama GPU 支持见 <https://docs.ollama.com/gpu>。
- 建议初始分配 4–8 逻辑 CPU、16 GB 内存、30 GB 可用磁盘，属于测试建议而非性能保证；导入文件和日志增加时需扩容。
- Linux、Git、curl、Python 3.12、uv、Node.js 24 和 npm、Ollama、反向代理（如 Nginx）。安装模型和依赖需联网或由管理员预先准备离线包。
- 建议使用独立普通服务用户和持久化目录；管理员也可按现有隔离方案选择账户或容器，不是项目的强制要求。模型和数据需要运行账户可读写。示例采用账户 `memo-vault`、路径 `/srv/memo-vault`；若已有同名账户或路径，管理员须更换名称。
- EverOS、Vault 和 Ollama 均不直接对公网开放；浏览器经受控的 HTTPS 入口访问。所有者认证、访问授权和真实数据交接由管理员与项目负责人另行确定。

## 2. 端口和组件

| 组件 | 本机示例端口 | 说明 |
|---|---:|---|
| Ollama | 11434 | 仅本机访问，复用现有服务前须由管理员确认资源与配置；不要直接重启共享 Ollama |
| EverOS 核心 | 8000 | 仅本机；无内置通用访问认证 |
| Vault 管理 API | 8765 | 仅本机，使用单 worker；不要直接暴露 |
| 外部授权网关 | 8766 | 远程 MCP 必需；仅供本机 MCP 服务调用 |
| 远程 MCP 服务 | 8767 | 仅本机，由 HTTPS 入口将 `/mcp` 转发到此服务 |
| Nginx 入口 | 3000 | 以下示例仅监听本机，由管理员已有 HTTPS 入口转发 |

端口冲突时管理员需同步调整服务、反向代理和客户端配置。Ollama、EverOS、Vault 如果部署在不同网络命名空间，`127.0.0.1` 不再跨组件可达，需重新配置地址和访问控制。

## 2.1 使用已提供的交接包部署

如果收到的是 `MEMO-Vault-handoff.tar`，请使用本节代替第 3 节的 Git 克隆步骤，以及第 4 节中 Ollama 和 reranker 的下载步骤。交接包包含提交 `841b11f` 的源码、原服务器的 `.venv` 和 `node_modules`、两个 Ollama 模型及 reranker 模型；不包含旧 Vault 真实数据、所有者凭据或服务器服务配置。部署文档需单独提供。

以下路径均为示例；管理员先确认文件来源、磁盘空间、现有服务及权限。不要覆盖服务器已有目录、模型或服务。解包前可先检查目录：

```bash
tar -tf /接收目录/MEMO-Vault-handoff.tar | head -n 30
```

管理员创建第 3 节示例账户 `memo-vault` 和 `/srv/memo-vault` 后，以该账户在目录中解包；务必将 `/接收目录/MEMO-Vault-handoff.tar` 改成实际路径，并确保该账户可读取压缩包：

```bash
cd /srv/memo-vault
tar -xf /接收目录/MEMO-Vault-handoff.tar
test ! -e app && mv MEMO-Vault-handoff/project app
mkdir -p /srv/memo-vault/models
test ! -e /srv/memo-vault/models/bge-reranker-base && \
  mv MEMO-Vault-handoff/models/bge-reranker-base /srv/memo-vault/models/
```

如 `app` 或目标模型目录已存在，先停止并由管理员处理，不要覆盖。交接包中的 `.venv` 和 `node_modules` 来自另一台机器，可能包含旧路径或 CPU 版 PyTorch；为保证新路径和 GPU 兼容，先保留旧目录，再在新机器重新安装并验证：

```bash
cd /srv/memo-vault/app
mv .venv .venv.from-old-server
mv apps/memo-vault-ui/node_modules apps/memo-vault-ui/node_modules.from-old-server
uv python install 3.12
uv sync --locked --python 3.12 --extra local-rerank --extra mcp
cd apps/memo-vault-ui
npm ci
npm run build
cd /srv/memo-vault/app
```

按照第 4 节的 CUDA 检查确认 EverOS 运行账户可以使用 GPU；如 PyTorch 构建与新机器不兼容，由管理员安装兼容版本并重新检查。交接包是 Git 导出，不含 `.git`，无需执行 `git rev-parse`；后续若需要 Git 更新，管理员需单独建立仓库工作副本，不能直接 `git pull` 此目录。

### 导入交接包中的 Ollama 模型

交接包的 `/srv/memo-vault/MEMO-Vault-handoff/models/ollama/` 中包含两个模型的 `manifests/` 和 `blobs/`。**先由管理员确认新服务器 Ollama 的实际模型目录、运行用户和共享服务策略**；目标目录不一定与旧服务器相同。不要替换整个现有模型库，也不要未经确认重启共享 Ollama。若管理员更愿意，直接通过 `ollama pull` 获取相同模型也是可选方案。

确定目标模型目录后，以有适当目录权限的管理员身份执行以下脚本，将 `/实际/Ollama模型目录` 替换为实际目录。脚本先核对交接包内 blob 的 SHA-256；对已经存在的同名文件检查内容，发现冲突则停止，不覆盖已有文件。复制后目标文件由执行用户创建，管理员须确保 Ollama 服务账户可读取目录和文件。

```bash
python3 - /srv/memo-vault/MEMO-Vault-handoff/models/ollama /实际/Ollama模型目录 <<'PY'
import hashlib
import shutil
import sys
from pathlib import Path

source, target = (Path(arg) for arg in sys.argv[1:])
if not (source / 'blobs').is_dir() or not (source / 'manifests').is_dir():
    raise SystemExit('交接包缺少 blobs 或 manifests，请检查解包路径。')
files = sorted(p for p in source.rglob('*') if p.is_file())
if not files:
    raise SystemExit('没有找到模型文件。')

def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()

for src in files:
    rel = src.relative_to(source)
    digest = sha256(src)
    if rel.parts[0] == 'blobs' and src.name != 'sha256-' + digest:
        raise SystemExit(f'交接包中的 blob 校验失败：{rel}')
    dst = target / rel
    if dst.exists() and (not dst.is_file() or sha256(dst) != digest):
        raise SystemExit(f'目标存在不同内容的文件，未覆盖：{dst}')

for src in files:
    dst = target / src.relative_to(source)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        shutil.copy2(src, dst)
print('模型文件已导入；请确认 Ollama 服务用户有读取权限。')
PY
```

然后运行 `ollama list` 核对 `qwen2.5:3b` 和 `qwen3-embedding:0.6b`；在实际模型调用时以 `ollama ps`、`nvidia-smi` 验证 GPU。若未显示模型，先检查 Ollama 实际模型目录、服务用户权限和服务日志，不要盲目修改共享服务。

随后回到第 4 节执行**初始化 EverOS 数据目录**及“修改 EverOS 配置”，但跳过 Ollama 和 reranker 下载命令；继续按第 5–7 节配置服务、前端并验收。新环境应使用新建数据目录和新生成的所有者凭据。

## 3. 获取代码和依赖

下面是独立账户部署的示例。旧服务器使用 root 也可运行；共享物理机是否创建隔离账户由管理员决定。操作前请检查用户、目录及端口是否已存在；如采用其他用户，后续所有路径、文件权限和服务文件中的 `User`、`Group` 必须一并调整：

```bash
sudo useradd --system --create-home --shell /bin/bash memo-vault
sudo install -d -o memo-vault -g memo-vault /srv/memo-vault
sudo -iu memo-vault
cd /srv/memo-vault
git clone https://github.com/memoio/MEMO_Vault_App.git app
cd app
git rev-parse HEAD
git log -1 --oneline
```

确保 git、uv、Node 和 npm 对服务账户可用。安装依赖（将 `uv` 换成管理员实际安装路径）：

```bash
cd /srv/memo-vault/app
uv python install 3.12
uv sync --locked --python 3.12 --extra local-rerank --extra mcp
.venv/bin/python -c "import everos, fastapi, uvicorn, torch, sentence_transformers; print('backend dependencies OK')"
cd apps/memo-vault-ui
npm ci
npm run build
```

`local-rerank` 包含 PyTorch；在共享 GPU 机器上请选择与显卡、驱动和管理员环境兼容的 CUDA 版 PyTorch。下面的配置让 reranker 和 Ollama 使用 GPU；它们是不同进程，必须分别验证 GPU 可用性，并由管理员确认可使用的显存和算力。同步依赖可能替换已有包，使用本项目独立 `.venv`，不要在共享 Python 环境执行。

## 4. 模型和配置

管理员安排 Ollama 服务及显卡分配后，下载（或从可信离线来源导入）模型：

```bash
ollama pull qwen2.5:3b
ollama pull qwen3-embedding:0.6b
ollama list
```

创建独立数据目录并初始化 EverOS（仅首次执行）：

```bash
cd /srv/memo-vault/app
mkdir -p /srv/memo-vault/data
.venv/bin/everos init --root /srv/memo-vault/data/everos
cp examples/local-ollama/everos.toml /srv/memo-vault/data/everos/everos.toml
mkdir -p /srv/memo-vault/data/vault
```

如果数据目录已存在，先备份并检查，不要重复初始化或覆盖配置。

### 本地 reranker：安装和下载

上一步 `uv sync --locked --python 3.12 --extra local-rerank --extra mcp` 已安装项目声明的 PyTorch 和 `sentence-transformers`。先确认依赖和 Hugging Face 下载工具可用：

```bash
cd /srv/memo-vault/app
.venv/bin/python -c "import torch, sentence_transformers; print('torch:', torch.__version__); print('reranker dependencies OK')"
.venv/bin/hf --help
```

确认 **EverOS 运行账户和实际运行环境**中的 PyTorch 能访问目标 GPU：

```bash
.venv/bin/python -c "import torch; print('PyTorch:', torch.__version__, 'CUDA:', torch.version.cuda); print('CUDA 可用:', torch.cuda.is_available()); assert torch.cuda.is_available(), 'EverOS 运行环境无法使用 CUDA'"
```

如果显示 `CUDA 可用: False` 或断言失败，请管理员依据 [PyTorch 官方安装说明](https://pytorch.org/get-started/locally/)选择与服务器驱动和项目依赖兼容的 CUDA 构建，并检查服务账户的 GPU 访问权限。`nvidia-smi` 正常或 Ollama 能使用 GPU，不能单独证明 EverOS 的 PyTorch 也能使用 GPU。重新同步依赖后应再次检查。

下载 `BAAI/bge-reranker-base` 到独立模型目录。下面每个 `--include` 对应一个文件模式；如果服务器无法访问 Hugging Face，可由管理员在其他机器下载相同文件后安全复制到该目录：

```bash
mkdir -p /srv/memo-vault/models/bge-reranker-base
.venv/bin/hf download BAAI/bge-reranker-base \
  --local-dir /srv/memo-vault/models/bge-reranker-base \
  --include '*.json' \
  --include '*.safetensors' \
  --include '*.model' \
  --include 'vocab.txt'
```

确认目录含 `config.json`、模型 `.safetensors` 权重及 tokenizer 配置、词表，并确保 EverOS 运行用户能够读取。模型运行时从本地读取，不会自动下载缺失文件。该重排模型在 EverOS 进程内运行，不需要独立端口或服务；此处配置为 GPU，与 Ollama 共享显卡资源。

### 修改 EverOS 配置

编辑 `/srv/memo-vault/data/everos/everos.toml`，示例如下：

```toml
# 修改已有的 [llm] 段（模型名称与 Ollama 下载的名称一致）：
[llm]
model = "qwen2.5:3b"
api_key = "ollama"
base_url = "http://127.0.0.1:11434/v1"
timeout_seconds = 600.0

# 替换样例中已有的 [rerank] 段，避免出现重复段：
[rerank]
provider = "local"
model = "/srv/memo-vault/models/bge-reranker-base"
device = "cuda"
max_length = 512
batch_size = 4
max_concurrent = 1
timeout_seconds = 120

# 若已存在此段，只修改字段，不重复添加：
[knowledge.extraction]
total_timeout_seconds = 7200.0
```

`[llm].timeout_seconds` 是单次请求超时，也影响其他 LLM 功能；示例显式设置 7200 秒为原生 Ollama Knowledge 单份文档抽取的总时限。GitHub 当前版本的默认总时限为 600 秒，因此必须在实际配置中写入此项才能生效。大文件仍可能受到模型输出、单次请求、资源及其他限制。Ollama 和 Vault 在同一宿主机时样例中的 `127.0.0.1` 地址可用。

如管理员暂时无法给 EverOS 分配 GPU，或共享显存不足，可将 `[rerank]` 的 `device` 改回 `"cpu"` 并重启 EverOS。不要假设会自动回退到 CPU；应按下方验收步骤进行真实检索，确认模型成功加载与运行。

本机尚未提交的 8K 候选分块实验不属于当前 GitHub 版本，新环境按仓库当前实现部署即可。克隆代码不会复制开发机的本地模型、缓存或忽略的运行配置。

## 5. 以 systemd 启动（管理员示例）

以下服务名称和路径须按实际修改。管理员先创建 `/etc/memo-vault/runtime.env`，设置文件权限只允许相关账户读取；`MEMO_VAULT_UI_ORIGINS` 换成实际浏览器 HTTPS 地址：

```ini
MEMO_VAULT_DATA_DIR=/srv/memo-vault/data/vault
MEMO_VAULT_EVEROS_URL=http://127.0.0.1:8000
MEMO_VAULT_EVEROS_TIMEOUT_SECONDS=600
MEMO_VAULT_EXTRACTION_TIMEOUT_SECONDS=7200
MEMO_VAULT_KNOWLEDGE_TIMEOUT_SECONDS=7800
MEMO_VAULT_PUBLIC_DEMO=0
MEMO_VAULT_UI_ORIGINS=https://<实际访问域名>
MEMO_VAULT_MCP_GATEWAY_URL=http://127.0.0.1:8766
MEMO_VAULT_MCP_ALLOWED_HOSTS=<实际访问域名>,127.0.0.1:8767,localhost:8767
```

`PUBLIC_DEMO=0` 启用所有者认证；初次启动生成的 `owner-bootstrap.token` 必须通过安全方式交给授权用户，不写进仓库、聊天记录或日志。后台候选任务在单个 Vault 进程内排队；采用单 worker，避免跨 worker 状态和任务协调问题。

`/etc/systemd/system/memo-everos.service`：

```ini
[Unit]
Description=MEMO Vault EverOS core
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=memo-vault
Group=memo-vault
WorkingDirectory=/srv/memo-vault/app
ExecStart=/srv/memo-vault/app/.venv/bin/everos server start --root /srv/memo-vault/data/everos --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/memo-vault.service`：

```ini
[Unit]
Description=MEMO Vault management API
After=network-online.target memo-everos.service
Wants=network-online.target

[Service]
Type=simple
User=memo-vault
Group=memo-vault
WorkingDirectory=/srv/memo-vault/app
EnvironmentFile=/etc/memo-vault/runtime.env
ExecStart=/srv/memo-vault/app/.venv/bin/uvicorn everos.entrypoints.memo_vault.app:app --host 127.0.0.1 --port 8765 --workers 1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

外部客户端确有需求时，另建 `/etc/systemd/system/memo-gateway.service`；网关使用与 Vault 相同的数据目录和 EverOS 地址：

```ini
[Unit]
Description=MEMO Vault external gateway
After=network-online.target memo-everos.service memo-vault.service
Wants=network-online.target

[Service]
Type=simple
User=memo-vault
Group=memo-vault
WorkingDirectory=/srv/memo-vault/app
EnvironmentFile=/etc/memo-vault/runtime.env
ExecStart=/srv/memo-vault/app/.venv/bin/everos vault-gateway --host 127.0.0.1 --port 8766
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

远程 MCP 场景还需创建 `/etc/systemd/system/memo-mcp.service`。该服务必须与上述网关同时运行；示例只监听本机，不直接公开端口。

```ini
[Unit]
Description=MEMO Vault remote MCP
After=network-online.target memo-everos.service memo-vault.service memo-gateway.service
Wants=network-online.target

[Service]
Type=simple
User=memo-vault
Group=memo-vault
WorkingDirectory=/srv/memo-vault/app
EnvironmentFile=/etc/memo-vault/runtime.env
ExecStart=/srv/memo-vault/app/.venv/bin/uvicorn everos.entrypoints.memo_vault_mcp.http_server:app --host 127.0.0.1 --port 8767 --workers 1 --no-access-log
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

MCP 服务使用服务端配置的网关地址，不能放用户 Key；每个客户端请求携带独立的外部授权 Key。携带 `Origin` 的客户端还需将该来源加入 `MEMO_VAULT_MCP_ALLOWED_ORIGINS`（逗号分隔）；但当前 MCP 入口没有为浏览器直连实现 CORS 预检，配置来源白名单本身不能保证浏览器网页直接连接。优先使用服务端发起连接、支持自定义请求头的客户端。`MEMO_VAULT_MCP_ALLOWED_HOSTS` 必须包含反向代理最终传给 8767 的 Host。

首次创建服务文件后，按需启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now memo-everos
curl --retry 10 --retry-connrefused --retry-delay 2 --max-time 5 --fail http://127.0.0.1:8000/health
sudo systemctl enable --now memo-vault
sudo systemctl enable --now memo-gateway  # 仅在创建网关服务时执行
sudo systemctl enable --now memo-mcp  # 仅在创建 MCP 服务时执行，需先启动网关
```

`After` 并不保证依赖已就绪；健康检查失败时查看日志并停止后续部署。网关健康只表示进程可达，仍需要在页面中启用外部访问并创建授权。

## 6. 前端及反向代理

管理员通过其既有部署方式发布 `apps/memo-vault-ui/dist/`；确保 Nginx 工作用户可读取静态文件。示例虚拟主机仅监听本机，公网 HTTPS 由管理员现有入口负责：

```nginx
server {
    listen 127.0.0.1:3000;
    server_name _;
    root /srv/memo-vault/app/apps/memo-vault-ui/dist;
    index index.html;
    client_max_body_size 110m;

    location /api/ {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 660s;
        proxy_send_timeout 660s;
    }
    # 只转发 MCP，禁止将 8766/8767 直接发布到公网。
    location = /mcp {
        proxy_pass http://127.0.0.1:8767;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
        access_log off;
    }
    location / { try_files $uri $uri/ /index.html; }
}
```

若前面还有 HTTPS 代理，必须由管理员将 `/mcp` 原路径和 `Authorization`、`Host`、`Accept`、`Content-Type` 及 MCP 协议相关请求头和响应头透传至此 Nginx；该示例不负责 TLS 终止。上游访问日志同样不能记录 Key。仅代理前端首页或 `/api/` 并不会使 MCP 可访问。上线前确认外部网关已在管理页面恢复访问，并为每个客户端创建独立、范围受限的外部授权。

上传大小还受 Vault 和 EverOS 的应用配置限制；代理的 110 MB 示例不代表应用可以接受任意大小的文件。后台任务通过状态轮询，浏览器不应保持整个抽取期间的单个请求。管理员按现有架构设置 TLS、域名、访问策略和必要的代理头，检查 `nginx -t` 后再重载，避免影响其他站点。

## 7. 验收与运维

```bash
nvidia-smi
ollama list
curl --fail --max-time 10 http://127.0.0.1:11434/api/tags
curl --fail --max-time 10 http://127.0.0.1:8000/health
curl --fail --max-time 10 http://127.0.0.1:8765/api/v1/auth/session
curl --fail --max-time 10 http://127.0.0.1:8766/health  # 如果启用了网关
systemctl is-active memo-everos memo-vault nginx
systemctl is-active memo-gateway memo-mcp  # 如已启用远程 MCP
```

### 远程 MCP 验收

创建客户端授权前，先在本机验证 MCP 服务对无凭据请求返回 401（该结果表示鉴权生效，不表示业务调用已成功）：

```bash
curl --noproxy '*' -sS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8767/mcp
```

从服务器外验证 `https://<实际访问域名>/mcp` 的无凭据请求返回 401，不能返回前端 HTML；再在支持 **Streamable HTTP + 自定义 Bearer 请求头**的 MCP 客户端填写 URL 和外部授权 Key，验证连接、工具发现和允许范围内的一次读取。撤销测试授权后，再验证连接或调用被拒绝；使用两个权限不同的 Key 验证不能交叉访问。固定 Pack 的 `read_memory_pack` 成功后会消耗一次授权，验收时先使用不消耗授权的 `get_pack_info`。不要在日志、截图、命令行参数或聊天中暴露真实 Key。使用受控客户端进行真实连接验证；仅用 `/health`、页面展示地址或 Inspector 的本机测试不能证明公网链路已通。

该静态 Key 方案不提供 OAuth；只支持 OAuth 或禁止自定义请求头的客户端未必能连接。MCP Inspector 是可选测试客户端，不是服务器运行依赖。网络入口必须支持上述代理路径及所需方法和头部；如公网受管理网关、VPN 或防火墙保护，应按实际访问策略进行外网测试。

在模型请求运行期间检查 `ollama ps` 的 PROCESSOR 列和 `nvidia-smi`，确认 Ollama 实际使用目标 GPU；在真实检索触发 reranker 后检查 `nvidia-smi` 中 EverOS 进程的 GPU 使用情况。空闲时模型可能尚未加载或已经卸载。`/health` 返回正常不等于 LLM 和 reranker 已完成真实推理。24 GB 为显卡总显存，实际可分配容量取决于其他工作负载；分别记录测试前后显存、推理耗时和错误日志。

使用经允许的测试数据分别完成一次小文件候选抽取、一次 Knowledge 投递、候选确认和检索，并记录后台任务状态、耗时、GPU/内存使用及错误日志。之后再以代表性大文件验证完整性和性能；不得仅根据文件大小或 HTTP 200 断言结果完整。

```bash
journalctl -u memo-everos -n 100 --no-pager
journalctl -u memo-vault -n 100 --no-pager
journalctl -u memo-gateway -n 100 --no-pager  # 如已启用
journalctl -u memo-mcp -n 100 --no-pager  # 如已启用
```

部署升级前确认版本并备份 EverOS 与 Vault 数据。重启 EverOS、Vault 或 Ollama 可能中断任务，应先安排维护窗口；不要为本项目修改或重启共享服务而未经管理员确认。数据、访问凭证、模型目录和日志可能包含敏感信息，按组织策略控制权限与备份。
