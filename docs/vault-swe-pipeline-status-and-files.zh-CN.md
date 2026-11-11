# SWE-Agent + Qwen3.5：流程、文件与当前进度

更新日期：2026-11-09。本文用于在新服务器上梳理 SWE-smith 数据转换、LLaMA-Factory 微调与 SWE-Agent 评测。目标模型统一为 `Qwen/Qwen3.5-0.8B`，固定 revision `2fc06364715b967f1860aea9cf38778875588b17`。

**当前状态：**已实现 ADP 到 SWE-Agent 对话及 LLaMA-Factory 数据导出代码，SWE 1024 token 训练数据已在 GPU 服务器导出。此前旧 PEFT 实验完成过小样本 QLoRA 训练与 Adapter 重载；新的 LLaMA-Factory 路径尚未完成真实数据训练、Adapter 保存重载和 SWE-Agent 自主评测闭环。

```text
SWE-smith 原始轨迹
    ↓  转换、固定数据划分
ADP 标准轨迹
    ↓  转为 SWE-Agent 对话及 XML 工具动作
SWE-Agent 格式轨迹
    ↓  拆分决策、截断审计、Qwen 模板与标签
LLaMA-Factory SFT 数据
    ↓  QLoRA 训练并保存 Adapter
Qwen3.5 基座 + SWE Adapter
    ↓  相同任务、工具和参数下的基座 / Adapter 对比
SWE-Agent + Docker 真实任务评测
```

状态描述基于截至本文日期已有的实验记录；迁移后应重新检查路径、版本和运行结果。

## 一、文件位置与迁移约定

以下以仓库根目录为 `<项目根目录>`。`src/`、`scripts/`、`tests/` 和 `docs/` 下列出的文件属于项目工作区，**实际是否包含在新服务器的 Git 版本中，以提交和 `git status` 为准**。本文建立时部分文件可能仍未提交；克隆 Git 仓库不代表能拿到未提交文件。

`.memo-vault/` 下的数据、模型、实验脚本、实验日志和 Adapter 通常被 Git 忽略，需要另行复制或重新准备。新服务器上的 `/root/vault-swe-run/` 是此前训练机器的运行目录，不保证另一台机器存在相同路径；下文将其写作 `<运行目录>`。迁移后检查文件哈希、模型 revision、上游代码版本和报告，再修改使用说明中的环境变量。

| 位置 | 内容 | 迁移方式 |
|---|---|---|
| `<项目根目录>/src/everos/agent_training/` | 本项目的数据转换和训练数据导出代码 | 提交到 Git 后拉取，或单独复制并核对版本。 |
| `<项目根目录>/.memo-vault/agent-training/` | 本机数据、上游检出、评测实验和产物 | 不假设由 Git 携带；单独迁移或重新准备。 |
| `<运行目录>/model` | Qwen3.5-0.8B 模型快照 | 单独复制或下载，核验固定 revision。 |
| `<运行目录>/data/paper_adp_v2` | SWE 等 ADP 数据 | 单独复制或重新准备，核验来源与划分。 |
| `<运行目录>/upstream/agent-data-protocol` | ADP 官方转换器检出 | 准备固定提交 `ab1180306a3d4d27e9195448c90f030607eba981`。 |
| `<运行目录>/LlamaFactory` 与独立虚拟环境 | LLaMA-Factory 训练代码及依赖 | 固定 v0.9.5、提交 `7af909522a951e3ad9f022ea6f88b6755257eaa5`，按服务器重建环境。 |

## 二、SWE-smith 原始数据 → ADP

**目的：**将原始轨迹转换为统一 ADP 数据格式，并保持训练、验证和测试划分。

| 文件或目录 | 作用 |
|---|---|
| `src/everos/agent_training/paper_adp_conversion.py` | `convert_swe` 将 SWE 数据转换为 ADP 轨迹。 |
| `src/everos/agent_training/prepare_datasets.py` | 数据准备及划分相关代码。 |
| `.memo-vault/agent-training/datasets/paper_adp_v2/` | 本机现有 ADP 数据，分别保存 train、validation、test。 |
| `.memo-vault/agent-training/sweagent/data_audit_20261109/report.json` | 本机轨迹来源和划分审计结果。 |

**已有结果：**训练 200 条、验证 20 条、测试 20 条轨迹；源 ID、来源行和任务哈希等已做跨划分检查。语义近重复、许可和质量问题仍需按项目计划检查。测试集保留给方案确定后的最终评测。

## 三、ADP → SWE-Agent 对话

**目的：**把 ADP 转换成 SWE-Agent 的系统提示、对话、XML 工具动作和对应观察，例如 bash、文件编辑和 submit。

| 文件 | 作用 |
|---|---|
| `src/everos/agent_training/swe_lora_pipeline.py` | `convert` 子命令读取 ADP，调用固定版本的上游转换器并生成输出及报告。 |
| `.memo-vault/agent-training/sweagent/upstream/agent-data-protocol/agents/sweagent/std_to_sft.py` | 本机检出的 ADP 上游 SWE-Agent 转换器。 |
| `.memo-vault/agent-training/sweagent/upstream/agent-data-protocol/agents/sweagent/system_message.py` | 上游 SWE-Agent 系统提示与工具说明。 |
| `.memo-vault/agent-training/sweagent/upstream_conversion_20261109/report.json` | 本机转换报告。 |
| `.memo-vault/agent-training/sweagent/parser_audit_20261109/report.json` | XML 解析与参数离线对照结果。 |

**已有结果：**240 条轨迹转换完成，共识别 5,811 个工具决策；使用真实 SWE-Agent XML 解析器进行了离线检查。该检查没有逐条执行轨迹里的工具命令，不能证明所有动作的真实运行行为完全等价。

新服务器操作命令见 [LLaMA-Factory 使用说明](vault-swe-llamafactory.zh-CN.md) 的“转换数据”。此前 GPU 服务器已生成 `<运行目录>/converted-v1/`；复用前应检查报告和来源哈希。转换输出目录必须是新的不存在目录，避免覆盖。

## 四、生成和审计 LLaMA-Factory 数据

**目的：**把每个助手动作整理为独立决策样本，使用 Qwen3.5 模板处理输入和标签。当前动作作为训练目标；system、用户消息及历史观察提供上下文。

| 文件或产物 | 作用 |
|---|---|
| `src/everos/agent_training/swe_llamafactory.py` 的 `export` | 输出 ShareGPT 数据、`dataset_info.json`、训练 YAML 和审计报告。 |
| 同文件 `fit_decision` | 控制长度、可选截断或排除超长样本。 |
| 同文件 `verify` | 根据哈希校验导出产物和配置。 |
| 同文件 `audit` | 通过 LLaMA-Factory 数据加载器重新核对训练、验证数据的 token 和标签。 |
| `tests/unit/test_agent_training/test_swe_llamafactory.py` | 部分数据处理和校验行为的单元测试。 |
| [LLaMA-Factory 使用说明](vault-swe-llamafactory.zh-CN.md) | `export`、`verify`、`audit` 的命令及参数。 |

截断默认关闭。启用后先移除最早的完整用户 / 助手历史轮次，仍超长时从最后一条用户消息左侧裁剪；保留完整 system 和目标动作，仍不满足预算则排除。截断可能删除任务描述或重要反馈，必须检查逐条报告并人工抽查。

**已有结果：**此前 GPU 服务器的 `<运行目录>/lf-prepared-1024-v1/` 已完成导出：

| 划分 | 决策数 | 保留 | 排除 |
|---|---:|---:|---:|
| 训练 | 4,923 | 2,026 | 2,897 |
| 验证 | 442 | 190 | 252 |

保留样本全部发生截断。`report.json` 是总体报告，`manifest.jsonl` 和 `excluded.jsonl` 是逐条记录，`train.jsonl`、`validation.jsonl` 是训练和验证数据，`smoke.yaml` 是短测配置。测试集未用于本次训练导出。当前 1024 数据适不适合后续训练，仍需结合截断内容、排除原因和质量审计判断。

## 五、LLaMA-Factory QLoRA 微调

**目的：**在训练服务器加载 Qwen3.5-0.8B 和训练数据，训练少量 LoRA 参数。离线 SFT 使用 LLaMA-Factory，不需要启动 SWE-Agent 或 Docker；它们用于后续真实任务交互。

| 文件 | 作用 |
|---|---|
| `src/everos/agent_training/swe_llamafactory.py` | 导出训练样本时生成 LLaMA-Factory 的 `smoke.yaml`。 |
| `scripts/requirements-swe-llamafactory-constraints.txt` | 对独立训练环境部分依赖进行约束。 |
| [LLaMA-Factory 使用说明](vault-swe-llamafactory.zh-CN.md) | 环境准备和 `llamafactory-cli train` 命令。 |
| `src/everos/agent_training/swe_lora_pipeline.py` 的 `train` | 保留的自写训练对照路径；新实验优先使用 LLaMA-Factory。 |
| `tests/unit/test_agent_training/test_swe_lora_pipeline.py` | 旧脚本相关单元测试。 |

**已有结果与边界：**

- 旧 PEFT 实验曾用 9 个严重截断的决策完成 9 步训练，保存并重载 Adapter；记录在 `.memo-vault/agent-training/sweagent/qwen35_swe_qlora_256_9step_20261109/result.json`。这不等于新的 LLaMA-Factory 流程已完成。
- 新 GPU 服务器曾用完整模型做合成单步显存探针：768 token 完成参数更新，896 和 1024 token 前向显存不足。它不是实际数据集上的多步训练。
- **尚未完成新路径的真实数据 10 步训练、正式训练及保存重载。** 当前 8 GB GPU 在现有配置下不能直接运行 1024 token 短测；需要调整实现或更大显存，并重新验证。

迁移后先检查 `verify`、`audit`，再用真实数据做少量训练步骤，记录 loss、参数是否更新、显存峰值、运行时长和训练产物。长度、截断策略或训练配置变化后，应使用新的数据和输出目录并重新审计。

## 六、保存与重载 Adapter

**目的：**将训练出的 Adapter 加载回同一基座模型，检查产物能够实际使用。

| 文件 | 作用 |
|---|---|
| `src/everos/agent_training/swe_llamafactory.py` 的 `check-adapter` | 加载基座和 Adapter，检查有限 logits 及启用 / 禁用 Adapter 的输出差异。 |
| [LLaMA-Factory 使用说明](vault-swe-llamafactory.zh-CN.md) | 重载检查命令及输入路径。 |

**当前状态：**检查代码已实现，旧实验 Adapter 曾保存并重载；新 LLaMA-Factory Adapter 尚未产生，故该路径的重载检查尚未运行。重载成功不等于任务评测成功。

## 七、SWE-Agent 与 Docker 评测

**目的：**在同一个仓库任务、提示模板、工具配置、仓库快照和生成参数下，分别运行基座和基座加 Adapter；让 SWE-Agent 解析模型动作、在 Docker 环境执行工具并返回反馈，记录任务结果。

| 文件或目录 | 作用 |
|---|---|
| [SWE-Agent 实施进度](vault-sweagent-progress.zh-CN.md) | 本机框架、工具、训练与玩具评测实验记录；其中部分状态早于后来数据导出。 |
| `.memo-vault/agent-training/sweagent/framework_tool_smoke_v3.py` | 基于真实工具处理器和容器的预设动作测试。 |
| `.memo-vault/agent-training/sweagent/framework_tool_smoke_20261109_v3.json` | 预设读取、编辑、提交动作的结果。 |
| `.memo-vault/agent-training/sweagent/qwen35_model_server.py` | 本机旧实验使用的模型服务。 |
| `.memo-vault/agent-training/sweagent/baseline_toy_add_v1/` | 基座玩具任务配置和轨迹。 |
| `.memo-vault/agent-training/sweagent/adapter_toy_add_v1/` | 旧实验 Adapter 玩具任务配置和轨迹。 |
| `.memo-vault/agent-training/sweagent/runtime-bundles/` | 本机实验用工具配置和脚本。 |

**已有结果：**真实 SWE-Agent 工具处理器、SWE-ReX 和 Docker 容器曾通过预设动作检查，能够在临时仓库执行读取、编辑和提交。基座与旧 Adapter 均在玩具任务中输出了不可用的 XML 动作，因此没有完成任务。

**尚未完成：**模型自主生成有效动作、工具执行、观察返回并继续决策的闭环；新的 LLaMA-Factory Adapter 评测；多个独立验证任务和最终测试。训练使用的 LLaMA-Factory `qwen3_5_nothink` 模板与旧实验服务的生成提示存在差异，评测前须逐 token 核对动作前缀，并在相同设置下重新建立基座基线。

Docker 在这里提供运行仓库和工具的环境；SWE-Agent 负责组织模型调用、解析动作、调用工具和回传观察。模型微调和框架运行是不同阶段。

## 八、新服务器执行顺序和验收

1. 获取包含本方案代码的项目版本，单独迁移或准备模型、ADP 数据及固定提交的上游转换器；检查数据划分、哈希和磁盘空间。
2. 创建独立 LLaMA-Factory 环境并检查 CUDA、依赖版本；若复用旧转换结果，检查转换报告和输入哈希。
3. 按可用显存确定初始 token 预算，用新目录导出数据；检查有效数、排除原因、截断比例，并人工核验样本。运行 `verify` 和 `audit`。
4. 用真实数据运行约 10 个优化器步骤，检查有限 loss、参数更新、显存峰值；保存并运行 `check-adapter`。通过后再用 50～100 步测量速度，并决定是否扩大训练。
5. 准备固定版本的 SWE-Agent、工具配置、Docker 镜像及独立任务；先跑基座，再用同样条件跑新 Adapter。记录解析、工具执行、轨迹和任务结果。
6. 根据验证任务检查问题、调整训练和推理模板；方案确定后再使用保留测试任务进行最终评估。

数据、显存及资源评估见 [三条 Harness 训练资源评估](vault-harness-training-resource-assessment.zh-CN.md)，资源申请配置和时间估算见 [资源与训练时长方案](vault-harness-training-resource-proposal.zh-CN.md)。
