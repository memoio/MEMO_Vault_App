# Vault Agent 数据准备

该目录目前提供两个离线训练准备脚本：`download_datasets.py` 下载固定版本的公开原始数据，`prepare_datasets.py` 对下载的四个来源执行保守转换、分组、划分和抽样。这里不执行模型训练。

## 运行

从仓库根目录执行，数据准备环境需要 `pyarrow`、`ijson`、`jsonschema`：

```bash
python3 src/everos/agent_training/download_datasets.py
PYTHONPATH=src /root/anaconda3/bin/python -m everos.agent_training.prepare_datasets
```

上面的 Anaconda 路径是当前机器的已验证环境；其他机器请替换为安装所需依赖的 Python。输入默认为 `.memo-vault/agent-training/datasets/raw`，输出为 `.memo-vault/agent-training/datasets/prepared_v1`，两者均在 Git 忽略目录；命令行可以通过 `--raw-dir`、`--output-dir`、`--seed` 等参数指定。重复运行会覆盖相同输出目录中的候选 JSONL 和报告，若要保留历史结果请先指定新的输出目录。

## 输出和边界

- `train/general/<数据集>.jsonl`：通用候选；`train/domain/<数据集>.jsonl`：领域候选。
- `validation/validation/`、`test/test/`：验证与测试候选。报告 `report.json` 包含各来源的排除原因、抽样数量、截断情况与策略。
- 一行是一条 Vault 本地论文版 ADP 子集轨迹，包含 `id`、`content`、`details`；它不是上游完整 ATIF，也不是模型训练格式。每条都标记 `training_ready: false`。
- 仓库数据按仓库、网页数据按网站分组；CodeAct 使用来源任务 ID 前缀，AgentInstruct 使用原 ID。固定种子按组划分，训练集内 general/domain 也按组分配。
- 仅做基于规范化用户内容的**完全一致**去重，不能保证跨数据源的语义近重复或共享示范已被识别。AgentInstruct 当前只识别 OS/bash 子集，且原有说明文字可能未完全保留；其余子集会记入排除统计。
- 页面与长输出可能被截断，截断位置和长度保存在 `details.conversion`；网页中动作后的最终状态可能没有记录。不支持的结果不会静默标记为可训练。来源记录可通过相对文件和行号回查。
- 训练前仍需核查工具定义、权限和许可、截断及动作配对，处理近重复和源数据示范，建立目标模型/工具的训练模板和标签，并通过独立评测。

## 论文时期五类型 ADP：`paper_adp_v2`

论文明确包含 API、代码、消息三类动作，以及文本、网页两类观测。`paper_adp_conversion.py` 使用此前 `prepared_v1` 的来源索引，从原始文件重新读取已选中的轨迹，生成独立的 `paper_adp_v2` 数据。它保留原有训练、验证和测试归属，不覆盖旧结果；本次重转的是已选出的 897 条，而非全部原始数据。

```bash
PYTHONPATH=src /root/anaconda3/bin/python -m everos.agent_training.paper_adp_conversion
```

若默认输出目录已存在，脚本会拒绝覆盖；需要重复实验时使用 `--output-dir` 指向新目录。运行前会比对来源文件哈希；输出包含 `report.json` 和 `failed_records.jsonl`，记录结果及错误。

实现参考论文发表附近的上游提交 [`ab1180306a3d4d27e9195448c90f030607eba981`](https://github.com/neulab/agent-data-protocol/tree/ab1180306a3d4d27e9195448c90f030607eba981)：Python/Bash 使用 `code_action`，原始网页 HTML 使用 `web_observation`，其他工具使用 `api_action`。本次 897 条均通过该提交中的 `schema.trajectory.Trajectory` 独立验证。当前代码本身提供的是针对本次数据构建的五类型本地验证器，不是上游完整 Schema；当前上游主线已经转向 ATIF。

网页 HTML 和文本不按 8,000 字符截断；不知道的 URL、无障碍树、截图和视口设为 `null`。网页动作保留源 `backend_node_id`，尚未转换成上游特定浏览器的 XPath 或 Vault 的工具格式；原始文件仍是其他字段的权威来源。数据仍需质量、授权和近重复审查，且必须适配目标模型和运行时后才能训练。

## 阶段一数据和工具信息审计

`audit_stage1.py` 对 `paper_adp_v2` 四个划分进行只读检查，输出每条记录的来源、动作计数及工具说明线索，以及跨划分分组和任务哈希冲突。可选用本地 Qwen tokenizer 统计 ADP JSON 内容的大致长度；该数字不是最终训练模板 token 数。默认输出 `.memo-vault/agent-training/audits/stage1_v1/`，重跑会覆盖该目录的同名报告。

```bash
LD_LIBRARY_PATH=/root/anaconda3/envs/rag_1/lib PYTHONPATH=src \
  /root/anaconda3/bin/python -m everos.agent_training.audit_stage1 \
  --tokenizer Qwen/Qwen2.5-0.5B-Instruct
```

本机详细过程、统计口径、上游定义链接和下一步见 `docs/vault-stage1-data-audit.zh-CN.md`。脚本不会自动证明工具声明匹配、样本可训练或工具可执行；数据许可、语义近重复、长轨迹切片和样本质量仍需进一步检查。

## Qwen 阶段一训练候选试制

`prepare_qwen_stage1.py` 从已选 ADP 轨迹索引回查并校验固定哈希的源消息，使用 Qwen 本地聊天模板生成按助手决策划分的候选及 loss 掩码。暂缓工具接口未核实的 SWE、网页轨迹，并按决策记录超长或模板边界异常。输出仅供审核，不得直接视为训练就绪。完整规则和本机结果见 `docs/vault-stage1-qwen-candidates.zh-CN.md`。

## Qwen3.5 本机流程短测与可选截断

`prepare_qwen_stage1.py` 的 `--truncate` 显式启用左侧上下文截断，默认关闭；`--max-tokens` 控制上限。启用时保留完整助手目标及至少一个前缀 token，目标过长则记录排除；每条样本记录 `original_tokens` 和 `truncated_tokens`。左截断可能移除任务、工具声明和历史，输出只用于管线试验，不保证可执行或有效训练。迁移服务器时可不传开关以拒绝超长样本，并按照资源调整上限。

```bash
PYTHONPATH=src .memo-vault/agent-training/envs/qwen35-training/bin/python \
  -m everos.agent_training.prepare_qwen_stage1 \
  --tokenizer /root/.cache/huggingface/hub/models--Qwen--Qwen3.5-0.8B/snapshots/2fc06364715b967f1860aea9cf38778875588b17 \
  --revision 2fc06364715b967f1860aea9cf38778875588b17 --max-tokens 512 --truncate \
  --output-dir .memo-vault/agent-training/datasets/qwen35_openhands_source_smoke_512_v2
```

输出目录必须不存在；服务器需改为其本地模型路径。现阶段只处理 `train/general` 的 AgentInstruct 和 CodeAct 原始协议，仍不等于 OpenHands 接口适配；SWE 和网页明确暂缓。2026-11-09 在本机生成 1,003 个候选，927 个截断，3 个目标过长排除。两条实际候选的 5 步 QLoRA 测试成功并保存重载，结果位于 `.memo-vault/agent-training/probes/qwen35_source_candidate_512_20261109/result.json`。这是两个来源的训练器连通性测试，不是三种真实 Harness 的闭环测试或正式实验。
