# MEMO Vault 统一导入、内容轨道与 Knowledge 导出设计

> **状态：** 已实现代码与静态构建检查；Claude 四 ZIP 全流程及 Knowledge 实际投递/下载仍需在运行环境中验收。
>
> **日期：** 2026-10-26

## 1. 背景与目标

MEMO Vault 已支持通用文件、DeepSeek Harness 和 Claude 导入。DeepSeek 适配器会将可见聊天文本与工具调用/结果分别标准化；Claude 支持 Manifest 下载和一次上传多个本地 ZIP，并将整份导出转换成一个 UMIF 包。当前前端却把 Claude 的上传、预览、归档及后处理放在独立组件中，同时保留主导入向导步骤条，用户会同时看到两套进度。

导入数据也有不同用途：用户对话可生成记忆候选；Agent 工具轨迹应作为独立轨迹保留；附件和项目文件可在人工确认后进入项目 Knowledge。当前来源 ZIP 和 UMIF 归档能够追溯原始证据，但不同类型数据的审核状态、最终存储轨道和导出入口还没有统一呈现。

本设计将用户确认的方向落为可实施需求：

1. 所有来源共用一套五步向导；Claude 多 ZIP 是“上传与检查”中的来源专用控件。
2. UMIF 记录根据类型投影到用户记忆、Agent 轨迹、Knowledge 文档等轨道；原始证据与来源关联始终保留。
3. 每类内容进入正式记忆或活动 Knowledge 之前都要有明确审核决定；归档原件本身不等同于发布或建立索引。
4. 在导出入口新增 Knowledge 文档导出，按项目和筛选条件选择单篇下载或 ZIP 下载。
5. 保持现有 DeepSeek 工具轨迹的解析、保存、预览、脱敏、关联和记忆抽取隔离行为，不回归或改写其语义。

## 2. 已确认的用户决策

- 采用统一五步导入向导，不为 Claude 保留第二套步骤条。
- Claude 本地导入可一次提交同一份导出的多个 ZIP；Manifest 可选，仅作文件清单核对，不打开下载链接。
- 原始来源文件和 UMIF 包继续保存在 Vault，供审计和重试。
- 用户记忆、Agent 轨迹、Knowledge 文档分别处理；不可把工具事件伪装成用户聊天。
- 用户记忆及导入资料进入活动存储/索引前须展示审核与处理状态。Knowledge 投递要由用户明确批准；仅归档和排除后续处理均不删除原始证据。
- Memory Pack 仍表示已审核的正式用户记忆。Knowledge 文档导出与 Memory Pack 是同一导出入口内的不同功能，不混为同一种数据包。
- Knowledge 单篇下载返回 Knowledge 中保存的原始文件。
- Knowledge ZIP 默认包含所选原始文件和清单；用户可选择同时附带 EverOS 生成的摘要和 Topic Markdown。
- DeepSeek 工具轨迹现有路径不得被本次改动破坏。

## 3. 当前实现与约束

### 3.1 当前 UI 的分叉

`App.tsx` 已定义五步 `选择来源 / 上传与检查 / 解析预览 / 抽取候选 / 完成`。当来源为 Claude 时，主页面在步骤 1 之后渲染完整 `ClaudeImport`，该组件又维护自己的“选择文件 / 解析预览 / 归档后处理”进度和任务状态。组件没有接收主向导当前步骤及阶段回调，因此外层步骤会停留在“上传与检查”，内层却继续推进。

目标是将 Claude 组件改为来源适配面板：上传任务、Manifest/ZIP 校验、重试和历史任务仍使用 Claude 特有能力；步骤状态、预览确认、记忆抽取/审核、完成状态由主导入向导协调。

### 3.2 UMIF 和目标轨道

UMIF 是来源适配器与 EverOS/Vault 之间的版本化交换包，不是最终的产品存储轨道。当前 UMIF 记录包括 Principal、Session、Message、ToolCall、ToolResult、Asset、Document 和 MemoryAssertion 等类型；投影时必须保留 `record_id`、`source`、`scope`、`provenance`、内容哈希及来源包标识。

| UMIF 来源记录 | 目标轨道 | 后续用途 | 当前能力边界 |
|---|---|---|---|
| Session、用户/助手 Message | 用户对话 | 候选抽取，再经候选审核发布为 Episode、Fact/Preference、Profile 等正式记忆 | DeepSeek 与 Claude 的可见聊天使用现有 Vault 候选流程 |
| ToolCall、ToolResult 及其来源关联 | Agent 轨迹 | 后续按 Agent 算法形成 Case、Skill | DeepSeek 已独立解析并存入 `normalized_tool_events`；当前不送入用户记忆抽取，也不自动物化为 Case/Skill |
| Asset、Document、项目文件 | Knowledge Wiki | Knowledge Document、Topic，用于项目内检索 | Claude 文档在用户确认后通过 Knowledge 上传接口处理；Knowledge 自身产生摘要、Topic 和搜索索引 |
| MemoryAssertion | 用户记忆审核 | 平台已有记忆摘要转为来源声明/候选，审核后进入正式记忆 | 不能伪装成用户原始发言 |

三条轨道通过来源映射关联：`UMIF package_id + record_id + content_sha256 -> track + target_id + review_status`。映射与审核状态存 Vault 可查询的持久化账本；不以修改 DeepSeek 事件语义的方式实现分流。

### 3.3 DeepSeek 必须保持的行为

现有 `deepseek_harness.py`、UMIF 投影、`imports.py` 和 `normalized_tool_events` 保存流程是本需求的兼容边界：

- Harness v4 `.jsonl` 与既有支持的 ZIP 输入、稳定事件 ID、顺序、时间及来源字段继续有效。
- `tool/call`、`tool/result` 独立存储并保留 call ID、来源事件序号、参数/结果关系和内容哈希。
- 预览继续限制条数，并对参数和结果中的敏感值脱敏、截断。
- 工具事件不会进入用户/助手聊天记忆抽取；抽取消息筛选规则不变。
- 原始来源文件与 UMIF 包继续归档；重复输入、冲突检测、事务行为和重试语义不变。
- 此功能可以把现有工具轨迹在 UI 和轨道清单中标记为 Agent 轨迹，但不得把既有 DeepSeek 记录迁移、重写、删除或重新解释为聊天消息。
- 本期不新增 Agent Case/Skill 自动生成算法；后续若实现，须新增独立投影/审核流程，并用已有 DeepSeek 测试保护本路径。

回归测试应覆盖 `tests/unit/test_entrypoints/test_deepseek_harness.py` 和 `tests/unit/test_entrypoints/test_memo_vault_imports.py` 中现有 Harness 解析、预览、存储、脱敏、去重及“工具轨迹不进入记忆抽取”行为。测试数据和数据库断言应证明轨迹行数、ID、关联序号及记忆抽取输入保持既有语义。

## 4. 统一五步向导

### 第 1 步：选择来源

继续展示 Generic、DeepSeek Harness、Claude 等来源。选择来源后进入同一向导状态机。

### 第 2 步：上传与检查

- 通用/DeepSeek 继续使用当前单文件输入形态和校验规则。
- Claude 显示多 ZIP 上传、可选 Manifest 离线核对、项目选择、下载/解码任务状态、任务历史与恢复入口。
- Claude ZIP 合集必须来自同一份导出；作为一个任务转换为一个 UMIF 包。Manifest 核对只比较 ZIP 文件名，不访问 URL。
- 此步骤只负责获取、校验和解析任务状态，不显示第二套步骤导航。

### 第 3 步：解析预览

共用主步骤标题和进度条，内容由来源适配器提供。显示：

- 会话、消息、ToolCall/ToolResult、平台记忆、Document、Asset 数量；
- 来源文件清单、文件名、类型、大小、哈希和解析状态；
- 警告、缺失/歧义附件、未识别文件及未转换内容；
- 目标 app/project 和来源项目映射；
- 用户可检查的消息、文档摘要/文本及工具轨迹安全预览。

不得因 UI 只显示常用对象而隐藏仍保存在原 ZIP/UMIF 中的其他文件。超大内容按需展开或分页，不在列表中一次传输完整二进制。

### 第 4 步：抽取与审核

建议将全局步骤“抽取候选”改名为“抽取与审核”，以涵盖不同轨道。

- **用户记忆：** 对话按现有路径抽取候选；Claude `MemoryAssertion` 进入来源明确的候选审核。候选仍须人工审核才能发布正式记忆。
- **Agent 轨迹：** 展示独立工具轨迹及关联状态。DeepSeek 本期只沿用既有存储/预览流程；不自动进入用户记忆抽取，不在本期生成 Case/Skill。
- **Knowledge 文件：** 显示所有被识别的文件/文档、来源、预览/解析能力和审核状态。审核决定至少包含：
  - `批准并进入 Knowledge`：将该原始文件投递到选定项目的 Knowledge；
  - `仅保留在 Vault`：保留来源文件，不建立 Knowledge 文档/索引；
  - `排除后续处理`：不再投递或抽取，但原始 ZIP/UMIF 证据仍保留。
- 未支持或不能安全预览的文件应显式标记能力边界；不能静默投递 Knowledge。允许批量选择后执行明确的同一审核决定，同时保留逐项检查和审核记录。
- 保存审核人/本地 Owner、时间、决定、目标 app/project、源 record ID 和后续 Knowledge doc ID。重试不重复创建文档；结果未知时先核对 Knowledge 端状态。

“完成”表示本次导入任务和处理选择已结算，不表示所有资料必须发送 Knowledge。UI 应明确显示仍待审、仅归档、失败或尚未抽取的项目，并提供返回处理入口；未审核内容不得被当作已批准内容导出或投递。

### 第 5 步：完成

展示归档批次、UMIF 校验结果、各轨道记录数量、审核/投递结果、候选审核入口和失败重试入口。用户关闭页面后，任务及审核状态仍可从历史任务恢复。

## 5. Knowledge 投递与项目导出

### 5.1 Knowledge 接入

EverOS Knowledge 当前按 `app_id/project_id` 隔离；一个上传文件形成一个 Document，随后抽取摘要和 Topic，并更新检索索引。原始文件保存在 Knowledge Document 的 `_original/` 目录，Markdown 保存 Document/Topic 结构，向量/BM25 等索引是可重建派生数据。

Vault 对已经明确审核通过的文件继续发送原始字节和 MIME 类型到 Knowledge 上传接口。Knowledge 导入与用户记忆抽取保持分离；Knowledge 生成的 Topic 不自动成为用户记忆候选。

### 5.2 导出中心 UI

将现有 Memory Pack 页面扩展为“导出中心”，提供相互独立的导出面板/标签：

1. **Memory Pack**：现有已确认正式记忆选择、预览、隐私/对外使用确认和导出行为保持不变。
2. **Knowledge 文档**：
   - 必须选择项目空间（`app_id` + `project_id`）；
   - 支持按分类、标题关键字、创建时间、来源/文件类型筛选，并分页显示；
   - 列展示标题、原文件名、分类、Topic 数、来源、导入/审核状态和文件可用状态；
   - 支持选择单篇原件下载，或选择多篇生成 ZIP；
   - ZIP 默认包括所选原始文件和 `manifest.json`；用户可勾选附加 EverOS 生成的摘要及 Topic Markdown；
   - 预览导出文档数、总大小、缺失原件数和最终内容后再下载。

Knowledge 导出仅包含所选项目 Knowledge 中的文档。仅保存在 Vault 归档、未进入 Knowledge 的源文件不会悄悄混入该 ZIP；将来若需要下载这类文件，应提供明确的 Vault 原始归档导出，而不是冒充 Knowledge 导出。

### 5.3 Knowledge API 与下载安全

现有 `GET /api/v2/knowledge/documents` 支持 `app_id`、`project_id`、分类和分页；文档详情可列出 Topic 与 `original_file_path`。后端目前没有提供安全的原件下载流接口。浏览器不得直接访问服务端绝对路径。

实现需补充项目范围校验的后端下载能力：

- 按 `doc_id + app_id + project_id` 定位 Document；
- 对原件路径执行 Knowledge 根目录包含校验、符号链接/路径穿越防护和常规授权检查；
- 单篇下载以安全文件名和正确 MIME 类型流式返回原始字节；原件缺失时返回清晰状态，不泄露服务器路径；
- ZIP 在服务端生成或受控流式组装，包含规范化文件名和清单，防止同名覆盖、路径穿越、资源耗尽及跨项目混入；
- 可选的摘要/Topic 导出只包含所选文档的 Knowledge Markdown/结构化内容，不包含数据库、向量索引、密钥或其他项目文件；
- 设置文件数量/总字节数上限，失败时清理临时产物；导出内容和日志中不得记录个人原文或认证信息。

建议增加按项目列出导出元数据的前端契约，以及后端单篇原件下载和所选文档 ZIP 导出操作。若 Core API 直接提供下载接口，应返回字节流而非本地路径；Vault 可根据 Owner 会话代理并记录审计信息。

## 6. 数据保留、审核与导出边界

| 内容 | 默认保存位置 | 进入活动路径的条件 | 本期导出位置 |
|---|---|---|---|
| 原始 Claude/DeepSeek 导出及 UMIF | Vault 证据批次 | 归档确认；归档本身不等于发布 | 不进入 Memory Pack 或 Knowledge ZIP |
| 用户聊天与候选 | Vault 会话/候选审核 | 候选人工确认后写正式记忆 | Memory Pack |
| Claude 平台已保存记忆 | UMIF assertion + Vault 候选 | 作为来源声明人工审核 | 确认后可进入 Memory Pack |
| Agent ToolCall/ToolResult | UMIF + Vault `normalized_tool_events`（DeepSeek 现有路径） | 本期保持当前独立归档；Case/Skill 生成另行设计 | 本期无 Agent Pack 导出 |
| 经审核 Knowledge 文档原件 | EverOS 项目 Knowledge `_original/`，Vault 保留导入证据 | 文件通过审核并成功 Knowledge 接入 | Knowledge 单篇/ZIP 导出 |
| Knowledge 摘要和 Topics | EverOS 项目 Knowledge Markdown 与派生索引 | Knowledge 文档处理成功；受所选导出范围控制 | 可选随 Knowledge ZIP 导出 Markdown |
| Vault-only/排除处理文件 | Vault 原始证据及 UMIF blob | 明确选择仅归档或排除；不投递 Knowledge | 本期不包含在 Knowledge 导出 |

Memory Pack 永远不包含原始聊天全文、Agent 工具参数/结果、未审核候选或 Knowledge 文件。Knowledge ZIP 不包含用户记忆或 Agent 轨迹。

## 7. 方案比较

| 方案 | 说明 | 取舍 |
|---|---|---|
| **A. 统一向导 + 来源适配器（选定）** | 一个五步外壳；来源专用上传控件可不同；轨道审核与导出分别接入 | 用户只学一套流程；需要把 Claude 独立组件拆成受主向导状态协调的面板 |
| B. 每个来源独立完整向导 | Claude、DeepSeek、Generic 各自拥有步骤和状态 | 初期局部耦合较少，但再次产生重复导航、重复审核与来源间行为差异 |
| C. 只删除 Claude 内部步骤条 | 保留整个独立 Claude 流程但隐藏其中一个视觉元素 | 不能解决主步骤停滞、候选/文件审核分裂及统一完成状态问题 |

采用方案 A。Knowledge 导出在导出中心作为独立面板，不把文档字节与 Memory Pack 文本格式混合。

## 8. 分阶段实现建议

### 阶段 A：统一导入状态机与向导

- 主向导协调来源状态和五步跳转；移除 Claude 内部步骤条。
- 将 Claude 多 ZIP/Manifest、任务历史、恢复和异步状态作为第 2 步适配面板。
- 统一解析预览契约，显示 Claude、DeepSeek 与 Generic 的类型统计和警告。
- 将第 4 步命名为“抽取与审核”，集成现有聊天候选路径，不改变记忆发布规则。

### 阶段 B：轨道清单和审核账本

- 展示可识别文件、UMIF records、ToolCall/ToolResult 与对应来源；未知/未映射项目清楚标记。
- 为 Knowledge 文档增加持久化审核决定和幂等投递映射。
- 归档、仅归档、排除和 Knowledge 投递的状态可在重启后恢复。
- 保持 DeepSeek 的 `normalized_tool_events` 与候选抽取隔离不变。

### 阶段 C：Knowledge 导出

- 在导出中心添加 Knowledge 查询和项目筛选 UI。
- 增加安全单篇原件下载接口。
- 增加所选文档 ZIP 导出、清单、可选摘要/Topic Markdown、大小预览及限制。
- 不改动 Memory Pack 序列化和隐私确认策略。

### 阶段 D：回归与端到端验证

- DeepSeek 原有测试必须通过；额外新增 UI/API 回归证明统一向导不改变其导入与抽取行为。
- 使用 Claude 四 ZIP 流程验证上传、UMIF、轨道预览、人工审核和 Knowledge 投递/归档。
- 验证 Knowledge 单篇下载字节与 `_original` 哈希一致；ZIP 清单、筛选、可选 Topic Markdown 与项目隔离正确。
- 验证服务重启后任务/审核状态可恢复，重复请求不会重复创建 Knowledge 文档。

## 9. 验收标准

### 向导和内容审核

- 页面任何时刻只显示一条五步进度条；Claude 在第 2 步上传，在第 3 步预览，在第 4 步抽取/审核。
- Claude 一次提交的多 ZIP 仍是一项 job、一个 UMIF 包、一个 Vault 批次。
- 文件/UMIF 记录不会因没有 Knowledge 关联而从预览和状态清单中消失。
- 每项经审核内容都有可恢复决定及状态；未批准 Knowledge 的文件不会被投递。
- 对话候选和平台摘要均不绕过既有人工审核。

### DeepSeek 无回归

- 既有 DeepSeek Harness parser/conversion/import tests 通过。
- `normalized_tool_events` 的记录 ID、事件类型、call ID、source event sequence、哈希、脱敏预览和重复/冲突行为不变。
- 工具调用/结果不进入用户会话记忆抽取，用户可见文本仍按既有规则抽取。
- 原始 DeepSeek 文件和 UMIF 证据仍完整可追溯。

### Knowledge 导出

- 选定不同 `app_id/project_id` 后仅能看到该项目文档；跨项目读取/下载/打包被拒绝。
- 单篇下载字节与 Knowledge 保留原件一致，下载文件名不含路径。
- ZIP 只含筛选和选中的文档、`manifest.json`，可选 Markdown 内容只来自这些文档。
- 同名文件不会覆盖；原件缺失、文件超限和打包失败均有可理解错误且不泄露绝对路径。
- Memory Pack 输出及其候选/正式状态筛选完全保持现有语义。

## 10. 不在本期范围

- 自动从 DeepSeek 工具轨迹或其他 Agent traces 抽取 Case/Skill；本期只保留并呈现既有独立轨迹。
- 改写 DeepSeek Harness 格式、parser、标准化表、敏感字段处理或聊天抽取规则。
- 把 Knowledge Topic 自动转成正式用户记忆。
- 将原始对话、Agent 工具事件、Vault-only 原件混入 Memory Pack/Knowledge ZIP。
- 从 Vault 原始 ZIP 自动推断所有未知文件语义。
- 将 Memory Pack 改成混合记忆、文件、Agent traces 的单一包格式。

## 11. 相关现有实现

- 主导入向导与 Memory Pack UI：`apps/memo-vault-ui/src/App.tsx`
- Claude 来源面板：`apps/memo-vault-ui/src/ClaudeImport.tsx`
- DeepSeek Harness 解析与 UMIF 转换：`src/everos/entrypoints/memo_vault/deepseek_harness.py`
- Vault 导入投影、归档和工具轨迹持久化：`src/everos/entrypoints/memo_vault/imports.py`
- DeepSeek 行为回归：`tests/unit/test_entrypoints/test_deepseek_harness.py`、`tests/unit/test_entrypoints/test_memo_vault_imports.py`
- UMIF record model：`src/everos/umif/models.py`
- Knowledge API 与项目范围：`src/everos/entrypoints/api/routes/knowledge.py`
- Knowledge 原件和 Markdown 存储模型：`docs/knowledge.md`
- Claude 导入和投递：`src/everos/entrypoints/memo_vault/claude_export.py`、`src/everos/entrypoints/memo_vault/routes/claude_content.py`
- Memory Pack 导出策略：`src/everos/entrypoints/memo_vault/memory_pack.py`
