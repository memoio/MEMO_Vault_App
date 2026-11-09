# Vault 阶段一：数据与工具定义审计

本次审计基于本机 `paper_adp_v2` 的 897 条已选轨迹，其中仅 `train/general` 的 466 条属于阶段一通用训练候选；其余 283 条领域训练候选、76 条验证和 72 条测试用于检查划分，不加入阶段一训练。当前数据全部仍标记 `training_ready=false`。本次没有训练模型、生成训练标签、执行工具或对数据进行授权审查。

## 复现方法

从仓库根目录执行本次实际完成的结构审计：

```bash
PYTHONPATH=src /root/anaconda3/bin/python -m everos.agent_training.audit_stage1
./.venv/bin/python -m pytest -q tests/unit/test_agent_training
```

本次也尝试使用本地 Qwen tokenizer 扫描全部轨迹，但原始网页体积较大，运行数分钟后主动中止，故本次报告没有 token 统计；后续确定切片方法后需要重新测量。可选参数 `--tokenizer Qwen/Qwen2.5-0.5B-Instruct` 启用完整统计，本机可能需要 `LD_LIBRARY_PATH=/root/anaconda3/envs/rag_1/lib`。`--data-dir` 与 `--output-dir` 可自定义。输出目录会覆盖同名报告，请为每次运行指定不同目录。`report.json` 保存分来源汇总和输入文件 SHA-256；`records.jsonl` 保存每条轨迹的 ID、来源文件、行号、分组、动作类型和审计指标，不存原文。输入数据不修改。结果位于 Git 忽略的 `.memo-vault/agent-training/audits/stage1_v1/`，代码和本报告可随项目保存。

## 过程与统计口径

1. 逐行读取四类划分的四个来源文件，统计轨迹、动作类型、是否存在非空用户观测；按各来源内部的 group 和 task_sha256 检查跨划分重叠，并记录每个输入文件的 SHA-256。
2. 根据来源字段确认工具说明保留位置，以名称和声明来源形成可复核的证据；这只是来源级线索，不代表逐条验证了每次调用与工具声明兼容，更不代表工具可运行。
3. 按动作后紧邻项是否为观测计数。这是结构提示，不是错误判定：终止提交可能本来不需要结果，网页最后一步可能没有下一快照，也可能有中间消息或丢失关联。需要结合 `tool_links_json`、来源行回查。
4. 可选 token 统计采用 Qwen tokenizer 对“保留的系统文本 + 每个 ADP 内容对象的 JSON 序列化文本”计数；不是聊天模板下的实际训练长度，不能直接当作实际显存或模型输入长度。
5. 分组与哈希检查不发现共享 few-shot 示例、跨来源同题和语义近重复；没有人工标注正确性、隐私、许可及实时工具可用性。

| 来源 | 通用训练候选 | 领域候选 | 验证 | 测试 |
|---|---:|---:|---:|---:|
| AgentInstruct | 124 | 42 | 16 | 13 |
| CodeActInstruct | 150 | 50 | 20 | 20 |
| SWE-smith | 100 | 100 | 20 | 20 |
| Mind2Web | 92 | 91 | 20 | 19 |
| 合计 | 466 | 283 | 76 | 72 |

本次同一数据集内部的现有 group、task_sha256 均无跨划分冲突；这不证明不存在其他泄漏。466 条都包含非空 user 观测，只代表该字段存在，不证明任务信息足够。

## 工具信息与来源

| 来源 | 训练候选中的实际动作 | 工具说明来源与局限 |
|---|---|---|
| AgentInstruct | 124 条、608 个 bash 代码动作 | 操作规则位于第一条用户文本，含 `bash`、`finish`、`answer`，不是独立 system 字段；`source_loss_flags_json` 有源示例标记，应回查原始消息，避免将示例当目标。来源的 OS 环境仍需另建。 |
| CodeActInstruct | 150 条、418 个 Python 动作 | 150 条都保留系统消息，定义 `<execute>` 交互规则；其中 119 条包含字面 `Tool function available` 标记，73 条提及 `wikipedia_search`，46 条匹配家庭环境工具标记；其余 31 条没有该标记不等于缺失全部上下文或一定无需其他依赖。需结合原文逐条核实。 |
| SWE-smith | 100 条、938 个 bash、1035 个 `str_replace_editor`、191 个 `submit` | 保存的系统消息是通用助手提示。上游有 bash、editor 和 submit 的声明，可用于寻找证据；必须核对原数据所用版本、参数和每条调用，不能仅按名字假定兼容。真实运行还需要对应仓库状态。 |
| Mind2Web | 92 条、520 个 `click`、60 个 `type`、30 个 `select` | 轨迹使用 `backend_node_id`。论文时期上游 API 使用 XPath，不能直接换字段；网页执行还需要交互环境。本地保留网页快照，并非完整可交互网站。 |

源码线索：[AgentInstruct OS 转换器](https://github.com/neulab/agent-data-protocol/blob/ab1180306a3d4d27e9195448c90f030607eba981/datasets/agenttuning_os/raw_to_standardized.py)、[CodeAct API 声明](https://github.com/neulab/agent-data-protocol/blob/ab1180306a3d4d27e9195448c90f030607eba981/datasets/codeactinstruct/api.py)、[SWE-smith 配置](https://github.com/SWE-bench/SWE-smith/blob/main/agent/swesmith_infer.yaml)、[ADP SWE-smith 声明](https://github.com/neulab/agent-data-protocol/blob/ab1180306a3d4d27e9195448c90f030607eba981/datasets/swe-smith/api.py)、[Mind2Web API 声明](https://github.com/neulab/agent-data-protocol/blob/ab1180306a3d4d27e9195448c90f030607eba981/datasets/mind2web/api.py)。前述 `main` 链接未固定版本；API 声明中的空函数体也不是可运行工具。

紧邻观测检查：AgentInstruct 0/608、CodeAct 61/418、SWE-smith 99/2164、Mind2Web 92/610 个动作后没有紧邻观测。SWE 的终止动作和 Mind2Web 的最后动作不应简单视为错误；CodeAct 的 61 个中，46 个位于轨迹末尾，15 个后面紧接消息动作；是否存在源侧缺失或转换关联问题仍需逐条回查。每条的明细在 records.jsonl。

## 后续进入训练前

- 固定统一的运行接口和 Qwen 聊天模板；从保留的来源文本/声明恢复或映射工具信息，并记录声明的版本与适用范围；无法确定者标记为待核实，不编造定义。
- 逐条核实源 `loss` 标志、动作结果配对、示例污染及任务条件；对跨来源近重复进一步排查。公开数据许可和适用范围也需要确认。
- 对长轨迹按决策点构建训练序列，保留任务、必要的工具说明和上下文；信息不足的片段排除。之后才能统计实际模板 token 长度和得到最终可训练条数。
- 离线 LoRA 使用记录中的工具结果作为上下文即可，不要求当场执行工具；训练后想测真实闭环才需要可执行工具与匹配环境。建立验证集指标并保留最终测试集。
