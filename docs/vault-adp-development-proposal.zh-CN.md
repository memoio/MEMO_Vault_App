# Vault：Agent 轨迹转 ADP 与训练数据开发方案

日期：2026-11-04。检查基线：`c2b60487380922156af254122c2f797d0548c000`，本次未修改现有产品代码。关联文档：[训练资源与预实验](vault-adp-training-resource-plan.zh-CN.md)。

## 一、结论和范围

“导入用户数据 → 抽取工具调用 → 转 ADP”是合理的起点，但如果只留下工具结果，它既不能表达完整的决策轨迹，也通常不能直接用于 Agent SFT。训练所需的是用户任务、工具调用前的上下文、助手动作（工具名及参数 / 自然语言）、结果和后续行动。ADP 是中间表示；训练还需要转换成目标模型和 Agent 所使用的工具调用格式、构造标签并评测。

Vault **已有 ADP/ATIF 导出 Beta**，不应从零重做。当前可针对已归档的 DeepSeek Harness v4、Claude 会话构建轨迹，并保留用户文本、助手文本、工具调用和返回结果；现有校验通过并不代表所有语义完整或适合训练。下一步优先基于现有轨迹补齐数据质量和 SFT 适配层。

注意版本：论文中的 ADP `Trajectory` 与 Vault 本地论文版子集不同于当前上游仓库以 ATIF 作为规范中间层的实现；两者都不应被不加区分地称为“当前官方 ADP 格式”。兼容性应固定版本并通过上游验证及样例测试，而非凭名称推断。来源：[论文](https://arxiv.org/pdf/2510.24702)、[上游当前 README](https://github.com/neulab/agent-data-protocol)。

## 二、项目现状与完整性检查

| 内容 | 现有证据 | 当前判断和缺口 |
|---|---|---|
| 导入源 | `src/everos/entrypoints/memo_vault/adp_export.py:list_traces` 仅列出 archived 的 `deepseek_harness` 和 `claude` | 已有两种来源；其他来源包括 ChatGPT 虽有导入代码，不等于支持 ADP 导出，需单独实现和验证。Claude 指项目实现支持的归档消息结构，不代表所有 Claude Code、Claude API 或任意版本导出均支持。 |
| 用户任务 | `adp_converter.py` 中 `user/message` 和 `adp_export.py:_claude_raw_trajectory` 的用户文本映射 `text_observation` | 有原始文本时可保留；缺失上下文、跨会话历史或截断时不能恢复。 |
| 助手自然语言 | 两个转换器将支持的助手文本映射为 `message_action` | 已覆盖部分文本；是否存在最终答复/完整任务边界取决于原始数据，未强制语义完整。 |
| 工具调用与反馈 | `api_action(function, kwargs)` 和环境 `text_observation`；`details.tool_links` 关联调用和结果 | 支持结构化工具名、参数、文本结果、配对核验；错误状态在 `details`，下游生成时须显式保留。DeepSeek 原始事件需内嵌调用与独立事件一致。 |
| 推理与系统上下文 | DeepSeek 的 `details.system_messages` 和 reasoning 及部分 description；Claude 原始内容保存在 `details.source_conversation`，动作 description 为空 | 不保证思考可获取，也不应编造；将可用的系统提示及上下文有选择地映射到 SFT，避免盲目训练或复制隐藏推理。 |
| 工具定义 | 调用记录含工具名和参数；未发现导出器保证保存每次调用对应工具描述、JSON Schema 和版本的机制 | 缺失时难以判断调用是否合理或对新任务泛化；允许用户关联工具目录，保存定义来源、版本及适用时间，缺失则标记 unknown。 |
| 多模态和动作种类 | 本地 `adp_schema.json` 只支持 `text_observation`、`message_action`、`api_action` | 不支持论文结构中的 `code_action`、`web_observation`；Claude 不支持的工具结果类型会拒绝导出。代码作为工具参数记录时不等于支持原生 CodeAction；图像等不得静默丢失。 |
| 轨迹边界 | `adp_converter.py` 明确每个 session 一条轨迹；Beta 文档说明同会话多主题不自动切分 | 长会话可能多任务、多用户轮次；训练时应按决策点生成样本，依据任务边界做受控切分并保留依赖历史，不能简单按长度截断。 |
| 数据治理 | `routes/exports.py`、`adp_export.py` 支持列出、授权、校验、预览、导出；训练授权默认关闭；递归脱敏 | 授权只是标志，不表示训练发生或来源内容均有权使用；脱敏无法保证检出全部敏感信息。训练入口需独立验证授权和可追溯性。 |
| 训练 | 在 memo_vault 导出链路未发现 ADP→SFT、训练作业及评测实现 | 目前是候选轨迹导出，不是已完成训练闭环；本结论针对所检查的代码路径。 |

本机运行 `.venv/bin/python -m pytest tests/unit/test_entrypoints/test_adp_export_beta.py -q`：**9 passed**。测试证明已有测试覆盖的路径通过，不代表所有数据源或训练可用。

### 2.1 一条可训练轨迹的例子

```text
用户：查询北京天气，然后告诉我是否需要带伞。          [用户观测]
助手：调用 weather(city="北京")                       [APIAction]
环境：{"rain_probability": 80}                        [环境观测]
助手：降雨概率较高，建议带伞。                        [MessageAction]
```

以上仅展示语义，并非可直接运行的完整 Schema。只保留 `{"rain_probability": 80}`，模型无法知道为什么调用、如何调用，以及拿到结果后该如何回应。即使轨迹符合本地 Schema，如果任务、调用参数、结果或工具定义缺失，也应按其缺失类型标记，不能自动视为训练就绪。

### 2.2 训练就绪分级（建议）

- **可归档**：能保留原始证据与来源，允许不完整。
- **可转换**：版本识别成功、内容类型受支持、调用和结果可关联，本地 Schema 校验通过。
- **候选 SFT**：有训练授权和可用的用户任务、上下文、助手动作及匹配结果；工具定义可获知或已标记缺失并进入人工审核；不存在不可接受的敏感信息。需保留失败/中断标记，训练目标明确后再选择是否采用。
- **评测通过**：用留出的独立任务与工具执行环境验证，不能仅依据结构检查或训练 loss。

任务成功与否不总能从日志推断；没有标注时应为 unknown。带纠错的失败轨迹可用于训练恢复能力；未经处理的失败动作不应无条件当作正例。

## 三、建议架构与接口

```mermaid
flowchart LR
 A[用户导入归档] --> B[现有 DeepSeek / Claude 解析与溯源]
 B --> C[版本化轨迹: 本地论文版 ADP 或 ATIF]
 C --> D[完整度 / 配对 / 脱敏 / 权限审核]
 D --> E[数据集快照与按会话划分]
 E --> F[Vault 目标工具格式 SFT 转换]
 F --> G[训练服务: 先 LoRA / QLoRA]
 G --> H[离线评测 + 沙箱工具执行]
 H --> I[适配器版本与评测报告]
```

**路径选择**：推荐以现有导出器作为输入层，内部定义版本化轨迹及工具注册信息，再建立 ATIF 与论文版 profile 的明确适配，逐步验证上游兼容。备选是仅扩展论文版子集：实现成本较低，但未来与上游 ATIF 适配仍有迁移工作；直接替换为上游 schema 会改变现有输出和兼容性，应先做迁移评估。推荐路径可逐步接入，不要求立即丢弃现有用户数据。

建议的逻辑接口（设计提案，非现有 API）：

1. `inspect_trajectory(source, version) -> report`：来源版本、字段覆盖、缺失项、调用配对、数据许可/授权、脱敏风险、超长情况；失败原因可定位。
2. `build_dataset(trace_ids, tool_registry_version, policy) -> dataset_manifest`：固定源记录 hash、转换器/schema 版本、目标模板、筛选规则、排除原因、随机种子和 train/eval/test 划分；按会话/任务及近重复分组划分，避免泄漏。
3. `render_sft(trajectory, target_profile) -> examples`：以决策点为单位生成目标模型模板；工具定义和历史作为上下文，助手文本与工具调用为学习目标；用户消息、工具反馈、padding 在 loss 中屏蔽；正确处理一次消息多调用及 call ID 配对，训练与推理模板一致。
4. `submit_training(manifest, config) -> job_id`：服务端验证授权、资源限额、版本和输出位置；训练依赖独立 worker，支持进度、取消、错误、可复现配置和产物清理。
5. `evaluate(adapter, heldout_set, tool_sandbox) -> report`：同任务比较基座与微调模型，输出解析成功率、工具选择准确率、参数有效率、执行/任务成功率、错误恢复、非工具任务回归及评测成本。

实现时保持原始记录和 `details` 不直接全量灌入 prompt：它们可能重复对话、包含敏感信息或来自不可信工具输出。根据目标模板选择可用上下文，同时保留可审计来源。缺少 Schema 时可提供有限场景的已知工具补录流程，但不得推断伪造历史工具定义。训练只接收服务端已授权、审核通过的数据集快照；当前“可导出”和授权标记不自动构成可训练判定。

## 四、开发顺序与验收

| 阶段 | 工作与预计涉及位置 | 验收 |
|---|---|---|
| P0 轨迹审计 | 复用 `adp_converter.py`、`adp_export.py`、`adp_schema.json`；新增独立 readiness 检查与报告 | 覆盖两来源真实匿名样本与现有 fixture；报告缺任务、缺工具定义、错误/中断、缺结果、敏感信息和不支持类型；无静默丢失。 |
| P1 版本适配 | 在 `memo_vault` 旁新增 schema/profile 和上游 ATIF adapter，保留现有下载协议 | 固定上游 commit/schema，双向可表达字段 round-trip，标注不等价/不支持项；现有 beta 测试保持通过。 |
| P2 数据集生成 | 新增数据集 manifest、任务切分、工具注册及 SFT renderer | 合成端到端例子中还原任务、工具定义、助手调用、工具结果及最终答复；上下文和标签 mask 正确；并发调用按 ID 关联；同会话及近重复不跨集合。 |
| P3 小规模训练 | 独立训练 worker，先支持固定 1 个开源基座与 1 个目标工具模板；见资源方案 | 100–500 个已授权样本试跑；可取消、限资源、保存可加载适配器，记录可复现的 token、GPU、时长、显存、成本。样本数只是试验建议，不保证效果。 |
| P4 效果与交付 | 沙箱评测、基座对照、数据集和适配器版本管理 | 固定独立任务集，工具调用与参数正确率、任务成功率、非工具任务回归达到产品团队事先确定的阈值；未达标不发布。 |

优先测试：缺失或重复 call ID、多并发调用和乱序结果；工具报错及中断；原始内容为图片或未知块；对话多任务及超上下文；prompt 注入文本和敏感字段；源文件变更或授权撤销；schema 和目标模板版本变化。不要将训练时重放历史工具产生真实副作用；评测采用受控 mock/沙箱。

## 五、论文数据、Agent 与结果（精简）

论文统一 13 份数据集，总原始规模超过 130 万条：AgentInstruct、Code-Feedback、CodeActInstruct、Go-Browse、Mind2Web、Nebius SWE Trajectories、NNetNav-live、NNetNav-wa、openhands-feedback、Orca AgentInstruct、SWE-Gym、SWE-smith、Synatra。论文用 LLaMA-Factory 做 SFT；OpenHands 和 SWE-Agent 按非网页数据采样约 3 万条，AgentLab 用网页数据约 2 万条，而非每次都训练全部数据。

| Agent 框架与评测 | Qwen2.5 结果 |
|---|---|
| SWE-Agent / SWE-Bench Verified | 7B：0.4% → 20.2%；14B：2.0% → 34.4%；32B：2.2% → 40.3% |
| OpenHands / SWE-Bench Verified | 7B：2.8% → 20.4%；14B：5.8% → 30.6%；32B：10.6% → 36.8% |
| AgentLab / WebArena | 7B：4.5% → 21.0%；14B：5.5% → 22.2%；32B：10.9% → 22.9% |
| OpenHands / AgentBench OS、GAIA | 7B：3.5% → 27.1%；GAIA 7B Instruct：7.3% → 9.1% |

数字是论文特定模型、框架和评测下的成绩；提升是数据组合、SFT、模型和框架共同作用，不能归功于格式本身。论文没有披露足够 GPU 时长、型号和训练参数，不能以论文结果直接计算 Vault 成本。[论文表 1、表 3–5、附录 C](https://arxiv.org/pdf/2510.24702)
