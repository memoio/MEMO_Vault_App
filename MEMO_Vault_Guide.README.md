# EverOS 项目结构与开发上手指南

> 盘点基准：当前工作区代码，2026-10-28；包含尚未提交的实现。本指南描述工作区现状，不代表这些改动已经合并、推送或发布。

## 1. 项目是什么

EverOS 是本地优先的记忆框架，负责生成、保存和检索记忆。正式记忆以 Markdown 文件为准，SQLite 保存运行状态，LanceDB 保存可重建的搜索索引。

MEMO Vault 是本工作区中的本地记忆管理应用：用户导入不同平台的导出文件，审核候选记忆，管理正式记忆，并通过文件导出或授权外部应用使用选定内容。当前系统由前端、EverOS 核心 API、Vault 本地管理 API 和可选的外部网关组成：

```text
浏览器 / MEMO Vault 前端（5173）
        │ 本地所有者会话
        ▼
Vault 管理 API（8765） ── 原文件、UMIF、候选、授权与审计
        │                        │ 共用 Vault 数据目录
        ▼                        ▼
EverOS 核心 API（8000） ◄── 外部网关（8766，需启用）
        │                        ▲
        ▼                        │ 外部授权凭据
正式记忆 / Knowledge         外部 HTTP 客户端或本地 MCP 适配器
        │
        ▼
Ollama 或其他已配置模型服务
```

管理 API 与外部网关是两个独立服务：前者负责导入、审核和授权管理；后者只开放受授权约束的读取接口。端口、启动和首次连接方式见第 7 节。

## 2. 仓库根目录

| 目录或文件 | 内容与用途 |
|---|---|
| `src/everos/` | Python 后端主程序，见下文的分层说明。 |
| `apps/memo-vault-ui/` | MEMO Vault 的 React、TypeScript、Vite 前端。 |
| `tests/` | 单元、集成和端到端测试，以及测试用的样例数据。 |
| `docs/` | API、架构、配置、存储、运维说明，以及 MEMO Vault 需求和设计记录。 |
| `benchmarks/` | 记忆检索等能力的评测适配器、配置、数据和指标。 |
| `examples/` | EverOS 与 Langfuse、本地 Ollama 等环境的示例。 |
| `use-cases/` | Claude Code 插件、演示项目和外部项目集成样例。 |
| `scripts/` | 开发、数据处理和端到端流程辅助脚本。 |
| `deploy/` | 部署辅助内容；目前包含 Netlify relay。 |
| `data/` | 仓库工作区数据目录，不是 Python 源码入口。 |
| `.github/`、`.claude/`、`.agents/` | CI、协作规范、代码规则和开发流程配置。 |
| `pyproject.toml`、`uv.lock`、`Makefile` | Python 项目信息、锁定依赖和常用开发命令。 |
| `README.md`、`README.zh-CN.md`、`QUICKSTART.md` | 项目简介和使用入门。 |
| `MEMO_VAULT_MVP_DEVELOPMENT_PLAN_*.zh-CN.md` | MEMO Vault 方案与迭代计划；部分内容早于当前代码。 |

`.venv/`、`apps/memo-vault-ui/node_modules/`、`dist/` 和缓存目录属于本地依赖、构建产物或工具缓存，不是主要业务源码。`.everos-local/`、`.everos-ollama/` 和 `.memo-vault/` 是本机配置或运行数据目录，也不是 Python 包的一部分。

## 3. 后端代码怎么分层

主要代码位于 `src/everos/`，按“入口接收请求、业务层组织流程、记忆领域处理规则、基础设施读写数据”的方向划分：

| 目录 | 用通俗的话说 | 常见内容 |
|---|---|---|
| `entrypoints/api/` | EverOS 的 HTTP 接口入口 | FastAPI 应用、健康检查、记忆、知识库和其他 API 路由。 |
| `entrypoints/memo_vault/` | MEMO Vault 管理 API 和外部网关 | 平台解析、UMIF 投影、导入任务、记忆库、导出、所有者会话、授权与网关路由。 |
| `entrypoints/memo_vault_mcp/` | Vault 的 MCP 适配器 | 将被授权的网关操作提供给本地 stdio MCP 客户端。 |
| `umif/` | 各来源共用的中间数据格式 | 记录模型、稳定 ID、包读写和导入前校验；不依赖 DeepSeek 或 Vault 数据库。 |
| `entrypoints/cli/` | 命令行入口 | `everos` 命令及其子命令。 |
| `entrypoints/tui/` | 终端界面入口 | 文本界面和交互演示。 |
| `service/` | 业务流程协调层 | 把入口、记忆规则和存储能力串成“添加、检索、候选发布”等用例。 |
| `memory/` | 记忆业务规则 | 抽取、检索、级联处理、生命周期和提示词相关逻辑。 |
| `infra/` | 数据读写实现 | Markdown、SQLite、LanceDB、Milvus 等存储适配，以及后台任务实现。 |
| `core/` | 多模块共用的运行底座 | 应用生命周期、上下文、中间件、日志、指标、追踪和持久化接口。 |
| `component/` | 可替换的外部能力 | LLM、Embedding、重排、解析、分词、配置和通用工具。 |
| `config/` | 默认配置与提示词配置 | 应用设置和 Prompt Slot 默认内容。 |
| `templates/` | 随程序分发的模板 | 初始化配置等模板文件。 |

排查一个请求时，可以从 `entrypoints` 找到路由，再沿着调用进入 `service`、`memory`，最后查看 `infra` 中的数据读写实现。MEMO Vault 自己的导入、候选、记忆库和 Memory Pack 逻辑主要在 `entrypoints/memo_vault/`，它通过 `EverOSAdapter` 与核心服务交互。

### MEMO Vault 关键实现入口

| 功能 | 优先阅读的文件（相对于仓库根目录） |
|---|---|
| 导入与 UMIF | `src/everos/entrypoints/memo_vault/imports.py`、`deepseek_harness.py`、`deepseek_web.py`、`chatgpt_export.py`、`claude_export.py`、`claude_manifest.py`、`import_jobs.py`、`umif_projection.py`，以及 `src/everos/umif/`。 |
| 候选抽取 | `src/everos/entrypoints/api/routes/memo_vault_candidates.py`、`src/everos/service/memo_vault_candidates.py` 与 `memo_vault_candidate_prompts.py`。 |
| 人工候选与审核 | `src/everos/entrypoints/memo_vault/routes/candidates.py`、`contracts.py`、`storage.py`。 |
| 来源片段与定位 | `src/everos/entrypoints/memo_vault/evidence.py`；候选来源接口和记忆库共用。 |
| 正式记忆与生命周期 | `src/everos/entrypoints/memo_vault/library.py`、`adapter.py`，以及 `src/everos/service/memo_vault_lifecycle.py`。 |
| Memory Pack 和 Knowledge 导出 | `src/everos/entrypoints/memo_vault/memory_pack.py`、`routes/exports.py`、`routes/knowledge_exports.py`、`src/everos/service/knowledge_export.py`。 |
| 授权与外部网关 | `src/everos/entrypoints/memo_vault/auth.py`、`routes/grants.py`、`external_app.py`、`routes/external_gateway.py`、`src/everos/service/memo_vault_grants.py`、`memo_vault_gateway.py` 和 `src/everos/infra/memo_vault_gateway.py`。 |
| MCP 与启动命令 | `src/everos/entrypoints/memo_vault_mcp/server.py`、`src/everos/entrypoints/cli/commands/vault.py`。 |

上表中未单独写出完整路径的文件名，与同一格前面的文件位于同一目录。

### MEMO Vault 前端

`apps/memo-vault-ui/` 是独立的网页前端：

- `src/App.tsx`：页面导航和导入、候选审核、记忆库、Memory Pack 等页面流程。
- `src/api.ts`、`src/gatewayApi.ts`：管理 API 与网关 API 的调用函数和数据类型。
- `src/ClaudeImport.tsx`、`src/ChatGPTReview.tsx`、`src/TrackInventory.tsx`：平台专属导入、来源内容审核与清单。
- `src/AuthorizationMcpPage.tsx`、`src/OwnerSessionGate.tsx`：授权管理页面和本地所有者连接界面。
- `src/KnowledgeExport.tsx`：Knowledge 原文件和 ZIP 导出。
- `src/styles.css`：页面样式。
- `src/main.tsx`：React 应用启动入口。
- `vite.config.ts`：开发时把 `/api/*` 请求转发给本机 8765 端口的 Vault API。

## 4. 功能现状与使用流程

以下依据当前工作区实现整理，部分文件尚未提交。代码存在表示对应流程已实现，不表示所有导出样本或客户端版本均已完成验收。

| 功能 | 当前能力与边界 |
|---|---|
| 导入与归档 | 前端开放通用文件、DeepSeek Harness、ChatGPT、Claude 四个来源。导入前校验、预览，确认后保留原文件、UMIF 包与投影记录。归档和候选抽取是两个动作。 |
| UMIF | 各来源转换到统一记录格式，保留来源标识、时间、内容和路由信息，并校验引用与文件；它是内部适配与归档格式，不是 LLM，也不是正式记忆。 |
| 候选抽取 | 会话消息按 MemCell 组织后生成 Episode 和可能存在的画像候选；画像以用户消息为依据。候选抽取有中文输出约束，结果仍需核对；个别单元的画像抽取失败可以跳过，不保证每次都有画像。 |
| 候选审核 | 可查看来源、编辑、确认或拒绝候选，也可人工新增情节或画像。人工新增带人工来源标记，不伪造原对话；只有确认后才写入 EverOS。 |
| 来源与证据 | 原始文件保留用于追溯；来源消息、MemCell 输入片段和关键位置帮助核对候选和正式记忆。当前关键位置以词面匹配为主，不能视为对 LLM 总结真实性的证明。 |
| 正式记忆库 | 查看、搜索、修正、撤销或删除正式记忆；关联 Vault 保存的项目范围、来源和生命周期信息。 |
| Memory Pack | 从正式记忆按项目、类型和预算预览并导出 Markdown 或 `vault-memory/1` JSON；`deny` 不外发，`ask` 需要逐条选择并确认。导出前重新检查快照。 |
| Knowledge 与导出中心 | 经用户批准的来源文档可投递到项目 Knowledge；导出中心支持选择已进入 Knowledge 的原文件，单个下载或打包 ZIP，可选择附带摘要及 Topic Markdown。仅归档在 Vault 的文件不会自动列入 Knowledge 导出。 |
| ADP / ATIF 导出 Beta | 从已归档的 DeepSeek Harness 和 Claude 会话选择带有工具调用的完整轨迹，设置训练授权、检查脱敏明细并执行本地 Schema 校验，下载论文版 ADP 或 ATIF-v1.7 候选 JSONL。一个完整会话对应一条轨迹，不自动按主题拆分。 |
| 授权与管理 | 所有者管理外部授权、凭据轮换和撤销，查看审计并暂停或开启外部访问。授权只在指定项目、类型、操作和期限内生效。 |
| 独立网关与 MCP | 本地网关提供受授权限制的 HTTP 读取；可选的 stdio MCP 适配器将允许的工具暴露给 AI 客户端。现有 EverOS 核心 MCP 与此 Vault 授权适配器是两套入口。 |
| 尚未开放的能力 | Cursor / Agent 专用导入和自动抓取在界面不可用。当前也没有远程 HTTP MCP/OAuth、云交付、多账号隔离或加密保险箱。 |

### 三个专用解析器

| 来源 | 上传什么 | 解析后如何处理 | 主要限制 |
|---|---|---|---|
| DeepSeek | Harness v4 的 `session.v4.jsonl` 或仅含该文件的 ZIP；网页端账户导出 ZIP，包含 `user.json` 和 `conversations.json`。 | 自动区分两种格式并转换为 UMIF。Harness 识别消息和工具轨迹；网页导出按 REQUEST/RESPONSE 还原用户与助手消息，原 ZIP 保留。 | 思考、搜索和文件片段不作为聊天记忆抽取。网页导出未指定当前分支时，分叉后的消息归档但不参与抽取，预览给出提示；系统不猜测活跃分支。其他未知导出格式未承诺兼容。 |
| ChatGPT | 账户导出 ZIP，包含 `conversations.json`。 | 沿 `current_node` 和 `parent` 识别当前分支；符合条件的用户/助手文本可参与抽取。其他可见分支保留但不参与抽取；能明确定位到的附件可留待审核并批准进入 Knowledge。 | `is_do_not_remember` 会阻止会话进入抽取；隐藏思考、未知类型保留在原 ZIP，不作为聊天正文抽取。当前不提供账号登录或在线下载。 |
| Claude | 同一次导出的多个本地 ZIP 一次选齐；也可上传 Manifest，让服务下载仍可访问的链接。 | 多个 ZIP 合并为一个导入任务和 UMIF 包。会话走候选抽取；Claude 已保存的记忆作为平台摘要单独编辑审核；项目文档由用户决定是否批准进入 Knowledge。 | 本地 ZIP 可选 Manifest 核对文件名；不带 Manifest 时需要用户确认选齐。多个来源项目映射到同一目标项目需确认；过期链接可补交原文件名的 ZIP。不是自动按来源创建多个目标项目。 |

通用 Markdown、TXT、JSON 和受限 ZIP 走通用解析路径。没有对话角色结构的普通文本当前按用户消息处理；尚未将所有通用文件自动当作 Knowledge 文档生成 Topic。

### 三个平台测试数据导入

启动 Ollama、EverOS、Vault 后端和前端。管理页面默认无需输入所有者凭据；如已开启认证，则先使用所有者凭据连接。导入不需要外部网关；浏览器文件选择框读取的是浏览器所在电脑的文件。

测试数据位于旧电脑 WSL 的 `/root/EverOS/docs/memo_input/`：

- **DeepSeek**：在导入页面选择 DeepSeek 和目标项目，上传 `deepseek/deepseek_web_smoke_test.zip` 测试网页导出；`deepseek/dsh-session-session-be49397b-fd45-4c34-84d6-6fc724f851b3.zip` 可单独测试 Harness 导入。
- **ChatGPT**：选择 ChatGPT 导出和目标项目，上传 `ChatGPT/c0b40102f78911d98d4d67cdb9e6e6be40a9e4ccb390c178eb0388c971f74fe3-2026-10-26-14-50-23-a6ab51ccb8a14df1920bb186c6f84f45.zip`。
- **Claude**：选择 Claude 导出和目标项目，一次选中 `Claude/` 中同一批次的 `memories-000.zip`、`conversations-000.zip`、`projects-000.zip`、`light_metadata-000.zip`；可选对应 Manifest 核对文件名，不提供 Manifest 时按提示确认选齐。不要混合不同批次。

各平台均按页面检查预览、确认归档、抽取候选，并在“候选审核”中确认后才进入正式记忆。Claude 平台摘要需单独审核；文档或附件需批准并成功投递后才进入 Knowledge。

将三个测试文件夹从**旧电脑 WSL**复制到新服务器，输入新服务器 root 密码：

```bash
ssh -p 15322 root@183.240.197.189 'mkdir -p /root/workspace/MEMO_Vault_App/docs/memo_input'
scp -P 15322 -r /root/EverOS/docs/memo_input/{deepseek,ChatGPT,Claude} \
  root@183.240.197.189:/root/workspace/MEMO_Vault_App/docs/memo_input/
```

如果 WSL 用户无法读取 `/root/EverOS`，对 `scp` 使用 `sudo`。复制到服务器不会自动让本机浏览器的文件选择框看到这些文件；在本机浏览器上传时，仍需从本机选择文件。

### 从导入到使用的共同流程

1. 在“导入”选择来源和目标项目，上传文件；系统检查文件、解析来源、转换 UMIF 并提供预览。来源清单区分对话、Agent 轨迹、Knowledge、平台声明及仅保留的原文件内容。
2. 确认预览后归档。原文件、规范化记录及 UMIF 包保留在 Vault；Claude 任务支持查看进度、重试和恢复。抽取失败不会让已归档原文件消失。
3. 对可参与记忆抽取的消息，Vault 调用 EverOS 候选抽取。消息按约 2,200 tokens、最多 20 条预分块，长消息按约 1,400 tokens 切片，随后检测 MemCell 边界；具体结果受模型和内容影响。工具轨迹不会自动生成 Agent Case 或 Skill。
4. 在“候选审核”核对正文与来源，可修改、拒绝或确认；确认后才进入正式记忆库。Claude 平台摘要仍标记为平台生成摘要，不能当作用户原话。
5. 文档与记忆分别处理：用户批准来源文档后，系统尝试投递 Knowledge；查看记忆、创建 Memory Pack 或在导出中心选择已入库文档是后续动作。
6. 本地复制/下载 Pack，或创建外部授权让客户端通过网关读取。已经交付的内容无法从接收方撤回。

原文件可用、输入片段可还原、关键位置匹配成功是不同状态。解析器升级不会自动改写旧批次或旧候选。

候选抽取 API 当前允许单条消息最多 1,000,000 个字符；超过该限制的消息需要先处理。大批次和长会话会产生大量模型调用，处理时间及成功率受模型、上下文窗口和服务配置影响。

### ADP 导出 Beta 和脱敏规则

ADP 导出使用 Agent 轨道中的真实工具调用及结果，并读取归档来源还原上下文。DeepSeek Harness 从原始 v4 文件转换，Claude 按原始消息块顺序转换；调用 ID、来源文件哈希和调用结果等检查失败时，页面显示原因并阻止该次下载。没有真实工具调用的会话不能选作轨迹。当前不支持按主题拆分会话。

使用步骤：

1. 在“ADP / ATIF 导出 Beta”选择已归档的轨迹；每行代表一个完整会话，可能包含多个主题。
2. 逐条或批量设置训练授权，默认为关闭。论文版 ADP 写入 `details.training_consent`，ATIF 写入 `extra.training_consent`；授权仅代表本人的选择，不代表已经执行训练，也不能代替轨迹中其他人的授权。
3. 点击“预览”查看脱敏明细。明细按消息、工具参数、工具结果和元数据分组，显示 JSON 位置和替换后的上下文；同一信息可能在正文和元数据中分别出现，因此可能重复计数。完整候选 JSON 可展开查看。
4. 选择论文版 ADP 或 ATIF-v1.7 作为预览与校验格式，再点击“Schema 校验”。分别使用本项目维护的对应格式子集 Schema，校验不判断内容是否真实、任务是否成功，也不能证明数据已完全脱敏；这些 Schema 不是官方完整验证器。
5. 校验通过后点击对应的“下载 ADP JSONL”或“下载 ATIF JSONL”，每行是一条完整会话。论文版 ADP 根字段为 `id`、`content`、`details`；ATIF 使用 `schema_version`、`session_id`、`agent`、`steps` 和 `extra` 等字段。切换格式、来源或授权变化后需重新校验。导出只生成候选数据，不执行工具或训练。

当前规则递归检查导出对象的消息、工具参数、工具结果和 `details`，包括可解析为完整 JSON 对象或数组的字符串：

| 检查位置 | 当前匹配规则 | 处理 |
|---|---|---|
| JSON 字段名 | 字段名包含 `secret`、`token`、`password`、`passwd`、`authorization`、`api_key` / `api-key` 或 `private_key` / `private-key`，不区分大小写 | 将整个字段值替换为 `[REDACTED]`。已知模型用量字段（如 `inputTokens`、`outputTokens`）为数字、布尔值或空值时不遮盖。 |
| 普通文本 | 类似 `password=xxx`、`token: xxx`、`api_key=xxx`、`access_token=xxx`、`refresh_token=xxx`、`secret=xxx`、`authorization=xxx` 的形式 | 保留字段名和分隔符，将规则匹配到的值替换为 `[REDACTED]`。 |

“脱敏 0 处”只表示这些规则没有匹配到内容，不代表没有敏感信息。当前没有专门识别姓名、手机号、邮箱、身份证号、地址、客户名称、文件路径、IP 地址，或缺少上述字段名提示的密钥。普通文本规则也不能完整处理所有凭证格式，例如 `authorization: Bearer xxx` 可能只遮盖 `Bearer` 而留下后面的令牌。分享或训练前应人工检查完整内容；不要把 Schema 校验通过或零命中当成隐私安全保证。详细说明见 [ADP 导出 Beta](docs/adp-export-beta.zh-CN.md)。

### 授权和外部使用

1. 启动 Vault 管理 API、EverOS 和独立网关；如已开启管理页面认证，先用所有者凭据连接前端。在“授权与管理”中开启外部访问。新 Vault 的网关初始为暂停；暂停状态会保留。
2. 创建授权，指定主体名称、明确的项目、操作、记忆类型、用途和有效期。项目按 ID 精确匹配；默认 24 小时，可选 1–720 小时。外部凭据只显示一次，后续只能轮换或重新创建。
3. **动态范围**允许在有效期内读取符合范围、已确认、有效且 `allow` 的当前记忆；日后新增的符合条件记忆也可进入该范围。`ask` 和 `deny` 不包含在动态授权内。
4. **固定 Pack**先在 Memory Pack 中选择并确认内容，创建短期审批草稿，再完成授权。授权允许一次内容交付；所选记忆或策略变化时，旧授权包会失效，需要重新审核。
5. 外部客户端通过 Bearer 凭据调用 8766 HTTP 接口，或由本地 MCP 适配器转发。可按权限搜索、获取记忆、构建上下文，或读取获批的固定 Pack。证据默认不开放，摘要读取需单独授权；外部证据接口不会返回完整原对话。
6. 可以撤销单条授权、轮换凭据、查看审计，或暂停全部外部访问。审计记录操作和结果等元数据，不保存记忆正文或凭据。一次性交付在服务端受理后可能已消耗，即使客户端随后断线，也不要自动重试。

外部搜索当前使用关键词子串匹配，不是语义搜索。前端的连接测试针对 HTTP 网关；还需要在具体 AI 客户端中检查 MCP 进程启动、工具发现和实际调用。详细配置及限制见 [授权、API 与 MCP 说明](docs/memo-vault-authorization.md)。

## 5. API 数量与入口

按当前源码中显式声明的 HTTP 方法和路由统计：同一方法与路径算一个操作，不计 FastAPI 自动生成的文档端点；核心 API 的 `/api/v1` 与 `/api/v2` 是同一业务路由的两个挂载，不重复计数。数量表示声明，不等于每个操作都已完成或通过测试。

| 服务 | 不同操作 | 说明 |
|---|---:|---|
| EverOS 核心 API（8000） | 22 | 20 个业务操作同时挂在 v1 和 v2；另有 `/health`、`/metrics`，实际为 42 个方法与路径组合。 |
| Vault 管理 API（8765） | 61 | 含 2 个所有者会话操作及 4 个 ADP 操作。 |
| 外部网关（8766） | 8 | 7 个授权业务操作和 1 个 `/health`，独立于管理 API。 |
| 合计 | 91 | 三个服务的声明数合计；不把核心 v1/v2 视为两套功能。 |

核心 API：记忆 4、Vault 候选 3、Knowledge 11、OME 1、级联控制 1、健康和指标 2。Knowledge 的新增操作包含原文件和 ZIP 导出。

| Vault 管理 API 范围 | 数量 | 用途 |
|---|---:|---|
| 所有者会话 `/auth/session` | 2 | 查询会话、使用本地凭据连接。 |
| `/system/health`、`/projects` | 3 | 健康状态及项目空间。 |
| `/imports/*` | 6 | 预览、归档、抽取、状态和内容清单。 |
| `/import-jobs/*` | 9 | Claude 获取任务、恢复、补交和预览。 |
| 导入内容审核 | 9 | 查看和审核平台摘要、文档及附件，投递或核对 Knowledge。 |
| `/candidates/*`、`/memories/*` | 12 | 候选新增、审核与来源；正式记忆查看、修正和生命周期管理。 |
| `/packs/*`、`/adp/*` | 6 | Memory Pack 预览/导出；ADP 轨迹列表、训练授权、校验与下载。 |
| `/knowledge-export/*` | 4 | Knowledge 文档列表、详情、原件及 ZIP。 |
| `/grants/*`、`/gateway/*` | 10 | 授权、固定 Pack 草稿、轮换、撤销、审计和网关状态/开关。 |

外部网关业务接口是 `/api/v1/external/capabilities`、`memories/search`、`memories/get`、`evidence/get`、`context/build`、`packs/{pack_id}` 和 `packs/{pack_id}/deliver`。实际请求格式见 [授权说明](docs/memo-vault-authorization.md) 或已启动网关的 `/openapi.json`；管理接口不能通过外部网关调用。

## 6. 第一次阅读建议

1. 从 [中文项目介绍](README.zh-CN.md)、[Quickstart](QUICKSTART.md) 和 [架构说明](docs/architecture.md) 了解 EverOS。
2. 查平台导入流程和边界：[ChatGPT 导入](docs/chatgpt-import.md)、[Claude 导入](docs/claude-import.md)、[UMIF 规范](docs/superpowers/specs/UMIF统一中间数据格式与EverOS接入规范.md)。DeepSeek 从 `deepseek_harness.py`、`deepseek_web.py` 和第 4 节入手。
3. 查外部调用和本地安全边界：[授权、API 与 MCP](docs/memo-vault-authorization.md)；它包含请求示例、MCP 配置、限制和撤销行为。
4. 查 Knowledge 配置：[Knowledge 文档](docs/knowledge.md)、[Ollama Knowledge 抽取](docs/ollama-knowledge.md)、[本地重排模型](docs/local-rerank.md)。
5. 追代码时，从第 3 节中的前端组件或路由开始，再跟到服务、适配器和存储。`tests/` 有相关测试，但测试文件存在不代表本次文档更新已运行它们。

## 7. 本机启动 MEMO Vault（Ollama，五终端与可选网关）

下面以 **Ubuntu / WSL2 + Bash** 为例，仓库路径为 `~/EverOS`。首次使用先完成环境准备，之后日常使用直接按后面的五终端流程启动。服务地址分别是 Ollama `11434`、EverOS `8000`、Vault 管理 API `8765`、前端 `5173`；需要外部授权访问时再启动网关 `8766`。

### 首次使用：安装工具和依赖

`source .venv/bin/activate` **只激活已有虚拟环境，不会创建环境或下载依赖**。新用户需要先安装工具，再安装项目依赖。

| 工具/环境 | 要求与用途 |
|---|---|
| Git、curl | 获取代码、下载安装工具和检查 HTTP 接口。 |
| Python | 项目要求 **3.12 及以上**；下面固定用 3.12，由 uv 安装。 |
| uv | 根据 `pyproject.toml` 和 `uv.lock` 创建 `.venv` 并安装后端依赖。 |
| Node.js、npm | 使用 Node.js **22.12+ 的 22 系列或 24 LTS**；当前前端依赖的引擎范围为 `^20.19.0 || >=22.12.0`，新环境可选 24 LTS。 |
| Ollama | 运行聊天模型与 embedding 模型；模型需要另外下载，见终端 2。 |

**1. 安装基础工具和 uv。** 已安装的工具可以跳过。Ubuntu / WSL2 中执行：

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
uv --version
```

uv 安装方式见 [官方安装文档](https://docs.astral.sh/uv/getting-started/installation/)。Node.js 请按 [官方下载页](https://nodejs.org/en/download) 选择 Linux 对应的安装方式，安装后在当前终端检查：

```bash
node --version
npm --version
```

Node.js 版本门槛也可查 [Vite 官方说明](https://vite.dev/guide/)。在 WSL2 中应使用 Linux 侧的 Node.js/npm，并在 WSL2 终端执行本文命令。

**2. 安装 Ollama。** Linux / WSL2 安装命令来自 [Ollama 官方下载页](https://ollama.com/download/linux)：

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama --version
```

安装后是否已自动启动服务，以后面的终端 1 检查为准。本指南按 Ollama 与后端运行在同一 Linux / WSL2 环境、通过 `127.0.0.1:11434` 连接配置。

**3. 准备本项目源码。** 使用包含本文所述 MEMO Vault 实现的代码副本，并放到 `~/EverOS`；当前工作区仍含未提交实现，直接克隆上游仓库不保证包含这些功能。拿到项目后先确认关键文件存在：

```bash
cd ~/EverOS
ls pyproject.toml uv.lock
ls src/everos/entrypoints/memo_vault/app.py
ls apps/memo-vault-ui/package.json apps/memo-vault-ui/package-lock.json
```

**4. 创建 Python 虚拟环境并安装后端依赖。**

```bash
cd ~/EverOS
uv python install 3.12
uv sync --locked --python 3.12
source .venv/bin/activate
python --version
python -c "import everos, fastapi, uvicorn, multipart; print('Backend dependencies OK')"
.venv/bin/everos --help
```

`uv sync` 会创建仓库下的 `.venv/`，安装本地 EverOS 包、FastAPI、Uvicorn、上传解析、存储、模型客户端和 EverAlgo 等声明的依赖；无需逐个手动 `pip install`。`--locked` 使用仓库锁文件并检查它与依赖声明是否一致，失败时先确认拿到的 `pyproject.toml` 与 `uv.lock` 属于同一版本。

**5. 安装前端依赖。**

```bash
cd ~/EverOS/apps/memo-vault-ui
npm ci
cd ~/EverOS
```

`npm ci` 根据 `package-lock.json` 安装 React、TypeScript、Vite 等依赖到 `node_modules/`。Python 的 `.venv` 不包含这些前端依赖，也不包含 Ollama 程序或模型。首次准备需要联网下载这些工具、依赖和模型。

### 首次使用：准备配置文件

项目已提供依赖清单和 Ollama 配置样例，无需从另一台机器复制 `.venv/` 或另写一份 `requirements.txt`。各类文件的用途如下：

| 文件 | 用途 |
|---|---|
| `pyproject.toml`、`uv.lock` | 后端依赖声明及锁定版本，交给 `uv sync` 安装。 |
| `apps/memo-vault-ui/package.json`、`package-lock.json` | 前端依赖声明及锁定版本，交给 `npm ci` 安装。 |
| `examples/local-ollama/everos.toml` | 可复用的 Ollama 模型配置样例，首次复制到独立 memory root。 |
| `.everos-ollama/everos.toml`、`ome.toml` | EverOS 的本机运行配置，由初始化命令生成；前者再换成本地 Ollama 样例。 |
| `examples/local-ollama/memo-vault.env.example` | Vault 环境变量样例，可复制并在启动 Vault 的终端显式加载。 |

**1. 初始化 EverOS 的独立运行目录。以下仅在首次创建 `.everos-ollama` 时执行：**

```bash
cd ~/EverOS
.venv/bin/everos init --root .everos-ollama
cp examples/local-ollama/everos.toml .everos-ollama/everos.toml
```

已有配置的用户直接检查文件，保留自己的配置，不要重复执行上面的复制命令。样例配置的关键值为：

| 配置项 | 本地值 |
|---|---|
| `llm.model` | `qwen2.5:3b` |
| `embedding.model` | `qwen3-embedding:0.6b` |
| 两者的 `base_url` | `http://127.0.0.1:11434/v1` |
| 两者的 `api_key` | `ollama`，仅为非空占位值，无需购买或填写真实 Key。 |
| `memorize.mode` | `chat` |

完整参数见 [Ollama 配置样例](examples/local-ollama/everos.toml)。模型名称必须与终端 2 下载的名称一致。

**2. 可选：把 Vault 环境变量保存为本机配置文件。** 首次创建：

```bash
cd ~/EverOS
mkdir -p .memo-vault
cp examples/local-ollama/memo-vault.env.example .memo-vault/runtime.env
```

之后可编辑 `.memo-vault/runtime.env`。文件内容如下，其中 `$PWD` 在加载时展开，因此要先进入仓库根目录：

```bash
export MEMO_VAULT_DATA_DIR="$PWD/.memo-vault"
export MEMO_VAULT_EVEROS_URL="http://127.0.0.1:8000"
export MEMO_VAULT_EVEROS_TIMEOUT_SECONDS=600
```

Vault 当前直接读取进程环境变量，**不会自动加载这个文件**。终端 4 可以保留后面的 `export` 方式，也可以替换为：

```bash
cd ~/EverOS
source .venv/bin/activate
source .memo-vault/runtime.env
uvicorn everos.entrypoints.memo_vault.app:app --host 127.0.0.1 --port 8765
```

两种方式选择一种即可。`.memo-vault` 的 SQLite 数据库、证据目录和本地身份由 Vault 首次启动自动初始化；不需要额外安装数据库服务器。以后在新终端仍需激活环境并设置或加载变量，但不需要每次重装依赖。切换数据目录会切换所使用的 Vault 数据和身份，应固定使用同一目录。

完成以上准备后，按下面五个终端启动；Ollama 的进程参数仍在终端 1 设置，模型仍在终端 2 下载。

### 终端 1：启动 Ollama

若 Ollama 已由系统服务启动，先检查：

```bash
systemctl status ollama
```

如果服务正在运行，不要再运行第二个 `ollama serve`。如果 WSL 没有 systemd 服务，就在此终端启动：

```bash
export OLLAMA_MAX_LOADED_MODELS=1
export OLLAMA_NUM_PARALLEL=1
export OLLAMA_CONTEXT_LENGTH=8192
ollama serve
```

这些环境变量作用于此终端启动的 Ollama 进程；不会修改已经运行的系统服务配置。

### 终端 2：下载并检查模型

```bash
cd ~/EverOS
ollama pull qwen2.5:3b
ollama pull qwen3-embedding:0.6b
ollama list
curl http://127.0.0.1:11434/api/tags
```

### 终端 3：启动 EverOS API

```bash
cd ~/EverOS
.venv/bin/everos server start --root .everos-ollama
```

另开终端检查：

```bash
curl http://127.0.0.1:8000/health
```

### 终端 4：启动 MEMO Vault 后端

```bash
cd ~/EverOS
source .venv/bin/activate
export MEMO_VAULT_DATA_DIR="$PWD/.memo-vault"
export MEMO_VAULT_EVEROS_URL="http://127.0.0.1:8000"
uvicorn everos.entrypoints.memo_vault.app:app --host 127.0.0.1 --port 8765
```

Vault 首次启动会在数据目录生成仅供本机所有者使用的 `owner-bootstrap.token`。默认免 token 时，健康接口可直接访问；开启认证后，可在另一个本地终端使用凭据检查：

```bash
cd ~/EverOS
read -r VAULT_OWNER_TOKEN < .memo-vault/owner-bootstrap.token
curl -H "Authorization: Bearer $VAULT_OWNER_TOKEN" http://127.0.0.1:8765/api/v1/system/health
unset VAULT_OWNER_TOKEN
```

响应中的 `data.everos_base_url` 应为 `http://127.0.0.1:8000`，`data.everos_status` 应为 `online`，`data.data_dir` 应指向本仓库的 `.memo-vault`。管理页面默认免 token；只有显式开启认证后，未认证请求返回 401 才是预期行为。不要公开所有者凭据。

### 终端 5：启动前端

```bash
cd ~/EverOS/apps/memo-vault-ui
npm run dev
```

打开 `http://127.0.0.1:5173`，默认无需输入 token。要启用所有者 token 登录，在启动 Vault 管理 API 的环境中设置 `MEMO_VAULT_PUBLIC_DEMO=0` 并重启；恢复免 token 可移除该变量或设为 `1` 后重启。部署网址还需配置在 `MEMO_VAULT_UI_ORIGINS` 中。免 token 时任何能访问管理网址的人都可操作 Vault，应通过可信内网、VPN 或反向代理限制访问。

开启认证后，本机 Vite 开发模式会尝试读取 Vault 数据目录的 `owner-bootstrap.token` 并自动填入，点击连接即可；如果自动填充失败，可手动读取并粘贴。若 Vault 使用其他数据目录，启动前端时应设置相同的 `MEMO_VAULT_DATA_DIR`。生产构建不提供自动填充。

这是所有者凭据，与后续签发给外部应用的凭据不同。开发模式下能访问本机开发服务器的客户端可能取得该凭据，应仅供可信的本机测试使用。成功连接后浏览器使用约 8 小时的会话，管理 API 重启后需要重新连接。凭据不要提交到仓库或发给其他人。

可在只有自己能看到的终端读取凭据：

```bash
cd ~/EverOS
cat .memo-vault/owner-bootstrap.token
```

Vite 将 `/api/*` 转发给 `http://127.0.0.1:8765`；抽取请求由 Vault 再调用 EverOS，浏览器不直接调用 Ollama。改变前端端口时，检查 `MEMO_VAULT_UI_ORIGINS` 是否包含实际浏览器源。

### 可选：终端 6，启动外部网关

只有需要授权外部 HTTP 或 MCP 客户端时才需要运行。网关与 Vault 管理 API 必须使用同一个 `MEMO_VAULT_DATA_DIR`，并指向相同的 EverOS 服务：

```bash
cd ~/EverOS
source .venv/bin/activate
export MEMO_VAULT_DATA_DIR="$PWD/.memo-vault"
export MEMO_VAULT_EVEROS_URL="http://127.0.0.1:8000"
everos vault-gateway
```

网关默认监听 `127.0.0.1:8766`，新 Vault 默认暂停。先在前端“授权与管理”启用外部访问并创建授权，再用对应的外部凭据访问。`curl http://127.0.0.1:8766/health` 可检查网关进程；它只说明网关 HTTP 进程可达，不代表外部授权已启用或 EverOS 正常。不要将本地管理端口暴露到公网。

如需 stdio MCP，在运行适配器的同一 Python 环境安装可选依赖：

```bash
cd ~/EverOS
source .venv/bin/activate
pip install -e '.[mcp]'
```

然后在“授权与管理”复制 MCP 配置模板，按 [授权说明](docs/memo-vault-authorization.md) 配置具体客户端；网关需要保持运行。该适配器使用外部授权凭据，不使用所有者凭据。

### 抽取失败时按链路检查

- Ollama：`ollama list` 和 `/api/tags` 是否包含上述两个模型；EverOS 的 `.everos-ollama/everos.toml` 是否指向同一 Ollama 地址。
- EverOS：`/health` 是否可达，终端 3 是否有模型连接、超时或结构化输出错误。
- Vault：使用上面的所有者认证检查 `/api/v1/system/health`；确认 EverOS 地址和 `online`，再查看终端 4 中对应批次的错误。
- 前端：确认 Vault 可连接；若已启用认证，检查所有者凭据。确认导入是否已归档。对失败批次使用“重试抽取”；抽取失败不会删除已归档原文件。
- Claude：下载链接不可访问时可按原文件名补交 ZIP 并重试；若投递 Knowledge 报重排未配置，检查 EverOS 的 embedding 与 rerank 配置，参考 [本地重排模型](docs/local-rerank.md)。投递结果不确定时先核对，不要盲目重复提交。
- 外部访问：检查 8766 进程、网关是否已启用、授权是否过期/撤销，以及项目和操作是否在授权范围内。MCP 还要检查客户端实际启动的 Python 环境和可选依赖。

## 8. 常用词语

- **候选记忆**：系统从导入对话中提取的草稿，需要用户审核；审核前不等于正式记忆。
- **MemCell**：从参与抽取的消息中划出的记忆单元；来源片段可记录实际输入的消息顺序和字符范围。
- **UMIF**：各来源转换后的统一中间数据格式，用于校验、归档和向 Vault 投影；不是正式记忆本身。
- **画像与偏好标识**：当前 UI 将 `kind=preference` 显示为“画像”，新数据写入 EverOS 的类型为 `profile`；阅读旧设计时要核对这一差异。
- **正式记忆**：已确认并写入 EverOS 的记忆内容，当前以 Markdown 记录作为权威来源。
- **证据**：指向原始导入文件或对话消息的信息，用于回看记忆从何而来。
- **项目空间**：MEMO Vault 中的范围边界；查询和导出时先选定项目，避免把不同项目的记忆混在一起。
- **Memory Pack**：把用户选定且符合外发策略的正式记忆整理成可复制、下载或授权交付的内容包。
- **Knowledge**：EverOS 保存和检索项目文档的知识库；来源文件需经批准并成功投递，才会出现在 Knowledge 导出中心。
- **所有者凭据**：本地管理界面的连接凭据，保存在 Vault 数据目录，不是给外部应用使用的令牌。
- **外部授权与网关**：所有者为客户端设定的访问范围、期限与操作，以及负责逐次检查权限的独立服务。

