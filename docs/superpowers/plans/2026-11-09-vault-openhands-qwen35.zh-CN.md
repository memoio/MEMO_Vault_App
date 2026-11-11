# OpenHands + Qwen3.5-0.8B 最小训练与评测开发计划

> 更新：2026-11-10。官方数据下载、全量候选划分、Qwen3.5 / LLaMA-Factory 不截断适配及数据加载器核验已完成；数据的正式训练审核仍未完成。OpenHands 模型服务、基座基线和 QLoRA 短测 / 扩训脚本已生成，**尚未在有完整模型权重的机器上启动服务或训练**；Adapter 与自主任务对照仍待新服务器执行。

**目标：**使用已转换的 AgentInstruct OS 与 CodeActInstruct OpenHands SFT 数据，完成可追溯的数据选择、LLaMA-Factory QLoRA 微调，以及真实 OpenHands 环境中的基座 / Adapter 对比。

**架构：**优先下载 ADP 发布的 OpenHands SFT 轨迹，直接从这一阶段开始审计；在确认来源和工具协议后，适配 Qwen3.5 模板及 LLaMA-Factory 数据格式，训练独立的 OpenHands Adapter。评测固定 OpenHands 版本、工具、运行环境及任务，训练和评测分别运行。

**技术：**Qwen/Qwen3.5-0.8B，模型 revision `2fc06364715b967f1860aea9cf38778875588b17`；LLaMA-Factory v0.9.5；NF4 QLoRA；OpenHands v0 风格数据与待核验的匹配运行时；Docker 隔离环境。

## 一、调查结果与关键判断

1. [ADP 数据仓库说明](https://huggingface.co/datasets/neulab/agent-data-collection/blob/main/README.md)列出了 `agenttuning_os` 与 `codeactinstruct` 两个配置的 `sft_openhands` 文件，理论上可以跳过原始数据到 ADP、ADP 到 OpenHands 的重新转换；实际下载前还要确认文件可访问、版本和内容。
2. 用户原先选用的 AgentInstruct 是 `zai-org/AgentInstruct`。本机 ADP 的 `datasets/agenttuning_os/README.md` 将其映射到 `agenttuning_os` 的 OS 子集。**不能把微软的 `orca_agentinstruct` 当成相同数据集。**
3. CodeAct 选用 `codeactinstruct`。本机上游 `datasets/codeactinstruct/README.md` 指向 `xingyaoww/code-act`；公开仓库的 [CodeAct SFT 文件目录](https://huggingface.co/datasets/neulab/agent-data-collection/tree/main/codeactinstruct/full_sft)列出 OpenHands 格式文件。
4. `sft_openhands` 是**格式 / 处理阶段名，不是 train / validation / test 的原始数据划分**。下载完整文件不等于自动获得独立训练、验证和测试集，也不能把文件全量作为训练而忽略旧测试任务。先尝试和既有候选清单按来源 ID、源任务和分组映射；做不到可靠映射时隔离未匹配数据，不得假定行号一一对应。
5. 上游示例 `codeactinstruct/sample_sft/openhands_v0.json` 包含 `execute_ipython_cell` XML 动作；`agenttuning_os` 的上游元信息包含 bash。上述只是当前本机检出和样例证据，不证明公开大文件每条记录或目标运行时接口完全一致。原生 OpenHands 工具调用、XML 解析和运行时动作接口需要实测核对。
6. `/root/agent-data-protocol` 当前检出 `040a279b46b2388ae42b43449f8645b9781c7bf7`；此前 SWE 转换固定的是 `ab1180306a3d4d27e9195448c90f030607eba981`。**本机当前代码、此前转换代码与 Hugging Face 数据集提交未必一致**，不能据当前代码推断已发布文件一定由它生成。执行时分别记录固定版本，若发布生成 provenance 未公开则如实记录未知。
7. 已存在的 `qwen35_openhands_source_smoke_512_v2` 只是旧来源协议的短测数据，不是已验证的 OpenHands 框架数据。本计划不将其直接当作最终 OpenHands 训练集。

## 二、方案选型

- **推荐：官方已转换 SFT 文件 → 来源 / 协议审计 → LLaMA-Factory 适配。** 避免重复从原始数据转换；仍需建立独立划分、训练标签、目标模板和运行时工具映射。
- 备选：已发布 SFT 文件无法可靠恢复来源、划分或动作语义时，仅对无法处理的部分使用固定上游转换器从已有 ADP 重建并逐条比对；不得悄悄替代来源。
- 不采用：直接将两个公开文件混合训练、不做划分及工具协议检查。这样无法验证测试隔离和框架兼容性。

## 三、拟新增与复用文件

| 路径 | 职责 |
|---|---|
| `src/everos/agent_training/openhands_sft_pipeline.py`（拟新增） | 读取和审计已发布 SFT、映射来源及划分、生成决策样本和 LLaMA-Factory 输入、输出报告。 |
| `tests/unit/test_agent_training/test_openhands_sft_pipeline.py`（拟新增） | 覆盖解析、来源匹配、划分隔离、损失标签、截断、异常排除和固定样本测试。 |
| `src/everos/agent_training/swe_llamafactory.py`（复用其经过验证的设计） | 参考其 token 测量、可选截断、审计及哈希思路；只有在接口适合且验证通过时才复用代码，避免将 SWE 的 system 或动作假定为 OpenHands 协议。 |
| `docs/vault-openhands-llamafactory.zh-CN.md`（拟新增） | 记录来源、操作命令、训练参数、版本、迁移和评测步骤。 |
| `docs/vault-openhands-progress.zh-CN.md`（拟新增） | 记录每个关口的证据、结果与尚未完成事项。 |
| `.memo-vault/agent-training/openhands/`（运行产物，不进入 Git） | 保存下载数据、校验和、清单、报告、容器 / 评测日志及 Adapter；不同版本使用独立目录。 |

具体函数接口和运行命令以第一阶段拿到真实 schema 后确定；不得提前假定两份文件完全同构。不要覆盖 SWE 工作文件或既有数据。

## 四、执行步骤与验收

### 1. 固定数据与下载来源

- [x] 查询并记录 Hugging Face 数据仓库不可变 revision，核对两个 SFT 文件大小、可访问性、许可证、源文件及下载哈希；只下载这两个文件和必要的元数据，不下载整套大集合。IPv4 受限时可在本机下载后复制到新服务器。
- [x] 文件路径分别为 `agenttuning_os/full_sft/full_sft_openhands.jsonl` 和 `codeactinstruct/full_sft/full_sft_openhands.jsonl`；同时记录下载 URL、时间、revision、SHA-256 和源数据描述。读取少量样本，确认行格式、字段、ID、角色、动作及观察结构。
- [x] 核对来源是否能映射到现有 `harness_audit_20261109/openhands.jsonl` 中的候选；不得假定公开转换数据与本地选取的源 revision、条目或数量相同。

**验收：**两份文件及元数据可复核，来源与许可明确；不可恢复的划分或 provenance 记录为未知。下载失败只报告实际障碍，不擅自替换数据集。

### 2. 固定 OpenHands 版本与工具

- [ ] 核对 ADP 数据格式对应的 OpenHands v0 协议，选择可复现的 OpenHands 版本、commit、工具定义和运行镜像；优先核验上游 README 所称 v0.30.0，但在动作解析和工具执行验证前不将其视为已兼容。
- [ ] 分别检查 bash 与 `execute_ipython_cell` 的函数名、参数、返回观察、工作目录以及 Python 多轮状态；明确是否支持实际示例中的工具。缺少安全、等价的执行方式时记录排除或阻塞，不能把有状态 IPython 静默改成无状态 Python 调用。
- [ ] 使用预设动作在隔离环境验证“解析 → 执行 → 观察返回”；固定 Docker 镜像及资源限制。数据中的命令不在宿主机直接执行。

**验收：**代表性 bash、Python 动作能解析并在受控环境执行；未支持的动作有明确报告。预设动作通过不能视为模型自主任务通过。

### 3. 恢复划分并筛选数据

- [ ] 优先使用可证实的源任务 ID / 分组把已发布轨迹映射到现有 train / validation / test 清单，并保留源 ID、公开文件行号、原始划分、分组和哈希。对无法证明映射的样本，先隔离、统计，不能默认作为训练。
- [ ] 若需要重新划分，先设计确定性的任务级分组方案并检查与既有保留测试任务及跨来源重复；训练 / 验证 / 测试按任务而非决策行分组，测试只用于方案确定后的最终评估。无法证明隔离时不得进入正式实验。
- [ ] 统计每个来源、划分的轨迹数、决策数、动作类型、无动作 / 不支持 / 格式异常等排除原因；检查原始监督标记是否可恢复。若发布 SFT 丢失原始 loss 标记，就标为未知并隔离需要该标记的样本，不假定所有 assistant 内容可训练。

**验收：**产出可追溯 manifest、排除清单及汇总；跨划分任务和已知重复冲突为零；声明语义近重复审查的实际覆盖范围。

### 4. 适配 Qwen3.5 和 LLaMA-Factory

- [ ] 固定 Qwen3.5 模型 revision 与真实 LLaMA-Factory 模板；将轨迹按决策拆分，保持 system、任务、必要历史、工具反馈、当前目标；只对经验证应监督的当前输出计算 loss。
- [ ] 统计完整输入 + 目标长度分布，输出每个预算下的保留、截断、排除数量。截断默认关闭，提供显式开关；开启后记录每条修改，完整保护工具定义和目标。先审计样本可理解性，不以文本长度达标代替语义可用。
- [ ] 使用实际 LLaMA-Factory 数据加载器检查样本 token、标签和角色，核对训练与推理动作前缀；训练、验证分别生成，测试数据不得写入训练文件。
- [ ] 测试覆盖重复 ID、未知动作、缺少目标 / 观察、原 loss 不明、过长目标、截断关闭 / 开启、跨组冲突及两来源样本。生成数据文件、配置、哈希、manifest 和报告。

**验收：**真实样本能被 LLaMA-Factory 重读，样本和标签与报告一致；抽查通过，排除与截断均可追溯。

### 5. 模型服务与基座基线

- [ ] 建立或复用与 OpenHands 接口匹配的 Qwen3.5 服务，固定 thinking、聊天模板、system、工具定义、上下文和生成参数；确认运行时的工具调用协议和训练数据一致。（已生成启动脚本；服务尚未在有完整权重的训练服务器启动。）
- [ ] 构建少量可复现的隔离 bash / Python 任务，记录任务版本、期望结果及独立性；先运行未微调基座，记录生成、解析、执行反馈和最终结果。（任务集与评测入口已存在；尚未完成基座实跑。）

**验收：**至少跑通模型请求、响应及真实工具环境的检查；若模型无法给出可解析动作，如实记录失败，不称自主闭环通过。

### 6. QLoRA 短测与扩大训练

- [ ] 在满足实际显存的机器上，以真实样本先测 10 个优化器步骤，检查有限 loss、参数更新、显存、保存与重载；不得将现有 8 GB 合成 768 token 成功当成真实训练成功。（训练入口已生成；本机缺模型权重，未启动训练。）
- [ ] 同配置继续以 50～100 步测量真实训练速度，按有效样本数估算时长，再决定训练步数；保持独立验证集并保存配置、版本、数据哈希及日志。（新增 50～100 步 pilot 入口；当前数据 `training_ready=false`，完整扩大训练会被脚本拒绝。）
- [ ] 将 Adapter 与同一基座重载，检查有限输出和 Adapter 启用 / 禁用的差异。（重载检查代码已存在，尚无本项目训练生成的 Adapter。）

**验收：**产出可重载的 OpenHands Adapter 和可复核的训练报告；任一步 OOM 或数据不足时记录失败，不把探针视为完成。

### 7. 框架闭环及对照

- [ ] 以与基座完全相同的任务、仓库 / 工作目录快照、工具、提示、运行时和生成参数测试 Adapter，记录模型调用、XML / 工具调用解析、执行反馈、最终结果和失败类别。
- [ ] 在独立验证任务上分析模型是否完成至少一个有效的“模型动作 → 工具执行 → 观察返回 → 后续决策”循环，并比较基座与 Adapter；方案固定后才评测保留测试任务。
- [ ] 汇总数据来源、排除、运行环境、训练记录和评测结果，更新 OpenHands 进度文档。

**最终验收：**真实数据训练、Adapter 重载及至少一个自主工具闭环均有日志；基座和 Adapter 对照可复现。任务未完成应如实报告，不以 loss、预设动作或解析器单测替代成功率。

## 五、待审核的决定

1. 采用 **ADP 官方已转换 SFT 优先**，并将 AgentInstruct 限定为与项目现有选择一致的 `agenttuning_os`；有证据无法匹配时才考虑局部回退。
2. 工具范围先覆盖命令执行和有状态 Python；版本选型以实际数据与 OpenHands 可验证兼容为准，不预先承诺最新版兼容。
3. 截断默认关闭，可显式开启并保留逐条报告；先做小规模训练和独立任务，不追求论文规模及指标。
4. 用户已批准第 1、2 项；2026-11-10 又明确要求执行第 5、6 节并生成训练脚本。本轮完成脚本和复刻命令，不代表已运行基座服务或 GPU 训练。


## 六、第 1、2 项执行记录及新服务器复刻命令（2026-11-09）

### 6.1 固定数据来源和下载

官方仓库 `neulab/agent-data-collection` 在查询时的 revision 固定为 `31a76bfb0124d77ae7322eabbb0171bf11ee2c67`；下载仅两个 OpenHands SFT 文件以及各自说明和许可证，不代表这些文件是训练 / 验证 / 测试划分。源码中发布文件的生成提交无法从本次检查中确定，标记为未知。数据按下面的 SHA-256 检查，不改写现有文件。

| 来源 | 轨迹数 | 文件大小 | SHA-256 |
|---|---:|---:|---|
| `agenttuning_os` | 195 | 4,332,625 字节 | `7879f2122fd07fab5382b6289beeb25c352f5c35c4b305e359f9718fd1805e61` |
| `codeactinstruct` | 7,139 | 185,109,724 字节 | `d01452a99787e722a1e86ff62cf965c668f39ddbf968a0cfcac428cb4b3aab80` |

两个已下载的上游许可证均为 Apache 2.0；衍生使用仍需结合实际来源、声明和项目用途审查。CodeAct 的 SHA-256 与官方 LFS 对象匹配，AgentInstruct OS 的 Git blob ID `a8b1c19f6b08231f83d683aca2c451368d8417ff` 与官方文件元数据匹配。实际数据在本机 `.memo-vault/agent-training/openhands/official-sft-31a76bfb/`，其 `source-inspection.json`、`*-file-metadata.json`、README 和 LICENSE 保存了本次结果；目录默认不随 Git 提交。

**数据下载脚本源码**（仓库中亦保存为 `scripts/download_openhands_adp_sft.sh`）：

```bash
#!/usr/bin/env bash
set -euo pipefail
# Explicit destination; existing files are never overwritten.
output_dir=${1:?usage: download_openhands_adp_sft.sh OUTPUT_DIR}
revision=31a76bfb0124d77ae7322eabbb0171bf11ee2c67
base=https://huggingface.co/datasets/neulab/agent-data-collection/resolve/$revision
mkdir -p "$output_dir"
download() {
  local name=$1 remote=$2 expected=$3 temp
  if [[ -f "$output_dir/$name" ]]; then
    printf '%s  %s\n' "$expected" "$output_dir/$name" | sha256sum -c -
    return
  fi
  temp=$(mktemp "$output_dir/.download.XXXXXXXX")
  if ! curl -4 -L --fail --retry 3 --connect-timeout 20 -o "$temp" "$base/$remote"; then
    rm -f "$temp"
    return 1
  fi
  if ! printf '%s  %s\n' "$expected" "$temp" | sha256sum -c -; then
    rm -f "$temp"
    return 1
  fi
  if ! ln "$temp" "$output_dir/$name"; then
    rm -f "$temp"
    return 1
  fi
  rm -f "$temp"
}
download agenttuning_os.jsonl agenttuning_os/full_sft/full_sft_openhands.jsonl 7879f2122fd07fab5382b6289beeb25c352f5c35c4b305e359f9718fd1805e61
download codeactinstruct.jsonl codeactinstruct/full_sft/full_sft_openhands.jsonl d01452a99787e722a1e86ff62cf965c668f39ddbf968a0cfcac428cb4b3aab80
```

**新服务器复刻数据下载和检查：**在包含本次改动的项目根目录运行；需要 `curl`、`sha256sum` 和 IPv4 出口。以下复用实际可审阅的脚本 `scripts/download_openhands_adp_sft.sh`，它固定 revision 与哈希，若目标已存在则校验、不覆盖。

```bash
cd /path/to/EverOS
export OH_DATA_DIR="$PWD/.memo-vault/agent-training/openhands/official-sft-31a76bfb"
bash scripts/download_openhands_adp_sft.sh "$OH_DATA_DIR"

# 下载相同 revision 的数据集说明和许可证。
export OH_BASE=https://huggingface.co/datasets/neulab/agent-data-collection/resolve/31a76bfb0124d77ae7322eabbb0171bf11ee2c67
for name in agenttuning_os codeactinstruct; do
  curl -4 -L --fail --retry 3 "$OH_BASE/$name/README.md" -o "$OH_DATA_DIR/$name.README.md"
  curl -4 -L --fail --retry 3 "$OH_BASE/$name/LICENSE" -o "$OH_DATA_DIR/$name.LICENSE"
done

# 使用已迁移的原有候选清单；输出必须不存在。
PYTHONPATH=src python -m everos.agent_training.openhands_sources \
  --data-dir "$OH_DATA_DIR" \
  --manifest .memo-vault/agent-training/manifests/harness_audit_20261109/openhands.jsonl \
  --output "$OH_DATA_DIR/source-inspection.json"
```

审计代码为 `src/everos/agent_training/openhands_sources.py`。它只按数据集及 ID 做精确对应：AgentInstruct OS 的 195 条对应既有 train / validation / test 分别 166 / 16 / 13；CodeAct 的 7,139 条中 240 条对应既有 200 / 20 / 20，另有 6,899 条不在旧清单中。既有两个来源的候选 ID 均能找到，但 ID 相同**不证明内容、源 revision、语义或防泄漏已核验**，也没有产生新的训练划分。检查到两个公开文件的消息使用 `from` / `value` 字段，本次扫描没有发现 `loss`、`trainable` 或 `mask` 消息字段；不能据此补造原始监督标记。

### 6.2 固定 OpenHands 与工具协议

本机已检出 OpenHands tag `0.30.0`，实际提交 `3720e1f75b35cca65ef02888d4c7314c9fe50c86`；这只是选定的候选运行版本，不意味着和公开数据集所有动作已完全验证。已检查真实源码：

- `openhands/llm/fn_call_converter.py`：非原生 function-calling 模式的 XML 函数解析。
- `openhands/agenthub/codeact_agent/tools/bash.py`：`execute_bash`，必需参数 `command`；原工具说明为持久 shell。
- `openhands/agenthub/codeact_agent/tools/ipython.py`：`execute_ipython_cell`，必需参数 `code`。
- `openhands/agenthub/codeact_agent/function_calling.py`：分别映射为 `CmdRunAction` 与 `IPythonRunCellAction`。
- `openhands/runtime/plugins/jupyter/__init__.py`：IPython 使用 Jupyter kernel；持久变量、工作目录与观察在真实环境中仍须验证。

复刻固定源码并检验提交：

```bash
export OH_REPO="$PWD/.memo-vault/agent-training/openhands/upstream/OpenHands"
mkdir -p "$(dirname "$OH_REPO")"
git clone --depth 1 --branch 0.30.0 https://github.com/OpenHands/OpenHands.git "$OH_REPO"
test "$(git -C "$OH_REPO" rev-parse HEAD)" = 3720e1f75b35cca65ef02888d4c7314c9fe50c86
```

**真实 XML 解析器检查：**下方只加载原版解析器及原版 bash / IPython 工具声明，不执行命令。需要另行安装与该源码兼容的 `litellm`；本机运行使用已有隔离的 SWE-Agent Python 环境，未修改其依赖。源码使用按文件直接加载方式，避免导入 OpenHands 整个应用；这不是完整框架启动测试。

```bash
export OH_PYTHON=/path/to/python-with-litellm
PYTHONPATH="$OH_REPO" "$OH_PYTHON" - <<'PY'
import importlib.util, json, os
from pathlib import Path
root = Path(os.environ['OH_REPO']) / 'openhands'
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
parser = load('oh_parser', root / 'llm/fn_call_converter.py')
bash = load('oh_bash', root / 'agenthub/codeact_agent/tools/bash.py')
ipython = load('oh_ipython', root / 'agenthub/codeact_agent/tools/ipython.py')
tools = [bash.create_cmd_run_tool(), ipython.IPythonTool]
for fn, param, value in [('execute_bash', 'command', 'pwd'),
                         ('execute_ipython_cell', 'code', 'x = 41')]:
    text = f'<function={fn}>\n<parameter={param}>{value}</parameter>\n</function>'
    result = parser.convert_non_fncall_messages_to_fncall_messages(
        [{'role': 'assistant', 'content': text}], tools)
    call = result[0]['tool_calls'][0]['function']
    assert call['name'] == fn and json.loads(call['arguments'])[param] == value
    print(fn, call['arguments'])
PY
```

本机以上两种预设动作的解析和参数均通过；**尚不能由解析通过推断工具已执行，更不能推断模型自主闭环通过。**

### 6.3 通过南京大学镜像下载并标记 OpenHands 运行时

使用南京大学 GHCR 镜像 `ghcr.nju.edu.cn` 下载官方 `all-hands-ai/runtime` 的固定 amd64 manifest。原始官方镜像摘要为 `sha256:6caf256b8a904b54f810a2969bc59ca705a304b897edfc20e443a65bbf851451`；通过摘要拉取时，Docker 会核对镜像内容是否与请求的摘要相符。以下适用于 Linux / WSL bash：

```bash
# 1. 通过南京大学镜像按固定摘要下载。
export OH_RUNTIME_IMAGE='ghcr.nju.edu.cn/all-hands-ai/runtime@sha256:6caf256b8a904b54f810a2969bc59ca705a304b897edfc20e443a65bbf851451'
mkdir -p /tmp/vault-openhands-docker-config
DOCKER_CONFIG=/tmp/vault-openhands-docker-config docker pull "$OH_RUNTIME_IMAGE"

# 2. 查看镜像 ID 和摘要。
docker image inspect "$OH_RUNTIME_IMAGE" --format '{{.Id}} {{json .RepoDigests}}'

# 3. 添加官方仓库名称的本地标签，不重新下载。
docker tag "$OH_RUNTIME_IMAGE" ghcr.io/all-hands-ai/runtime:0.30-nikolaik

docker image inspect ghcr.io/all-hands-ai/runtime:0.30-nikolaik \
  --format '{{.Id}} {{json .RepoTags}}'
```

注意：`docker tag` 的**目标**必须是 `仓库名:标签`，不能是 `仓库名@sha256:摘要`。本机 Docker 对将 `ghcr.io/all-hands-ai/runtime@sha256:...` 作为目标的命令返回 `refusing to create a tag with a digest reference`。因此这里使用 `ghcr.io/all-hands-ai/runtime:0.30-nikolaik` 作为本地标签。它与镜像源引用应指向相同的镜像 ID，但添加标签不保证 Docker 同时创建官方仓库的 `RepoDigest`；要按固定摘要复验，继续使用南京大学镜像的摘要引用。

**启动基础检查容器：**

```bash
# 不挂载宿主目录、不开放端口；名字已被占用时更换名字。
docker run -d --name vault-openhands-runtime-check-030 \
  --network none --memory 2g --cpus 2 \
  --entrypoint /bin/bash ghcr.io/all-hands-ai/runtime:0.30-nikolaik \
  -c 'while :; do sleep 3600; done'

docker ps --filter name=^/vault-openhands-runtime-check-030$
docker exec vault-openhands-runtime-check-030 /bin/bash -lc \
  'pwd; command -v bash; command -v python || command -v python3'

# 检查后可停止；之后需要时可以再次启动已有容器。
docker stop vault-openhands-runtime-check-030
# docker start vault-openhands-runtime-check-030
```

容器启动成功只证明镜像可以运行基础 shell；它不会自动启动 OpenHands 控制端或完成工具反馈测试。真实评测仍需在版本匹配的 OpenHands 控制端配置 `SANDBOX_RUNTIME_CONTAINER_IMAGE`，完成 bash、两次有状态 IPython 调用和观察返回。以上下载方式按用户提供的实际镜像源记录；容器启动及完整工具链需以执行结果为准。

### 6.4 本次验证与剩余阻塞

- 数据下载脚本的两个 SHA-256 校验通过；对已存在错误文件的检查会失败并拒绝覆盖。来源审计的 3 个针对性测试通过：`PYTHONPATH=src python -m pytest -c /dev/null --noconftest tests/unit/test_agent_training/test_openhands_sources.py -q`。
- 当前全项目测试环境未就绪：`PYTHONPATH=src python -m pytest -q` 在收集阶段因缺少 `asyncio_mode` 对应 pytest 插件失败；常规 conftest 还缺少 `pydantic_settings`。针对性测试使用 `--noconftest`，不能宣称全项目测试通过。
- 原版解析器对两个预设动作的解析已单独验证；用户随后完成了 6.5 的运行时工具执行、工作目录及 IPython 状态检查。解析器与运行时的连接仍待检查，不能将第 2 项整体验收标记为完成。
- 固定 digest 镜像已下载并启动检查容器；下一步按 6.6 验证解析结果到工具观察的连接，并统计数据中未支持的动作。此前使用的 SWE-Agent 镜像未包含 IPython，不足以替代官方运行时测试。

### 6.5 在隔离容器中验证真实 OpenHands 执行服务

2026-11-10 用户提供的结果：南京大学镜像和 `ghcr.io/all-hands-ai/runtime:0.30-nikolaik` 的镜像 ID 均为 `sha256:c3bc648710afc8734d447df8a83912f41d36b0c37346433b3ed1f77ba9b9ce13`；`vault-openhands-runtime-check-030` 容器已启动，工作目录为 `/openhands/code`，能够找到 bash 和 Python。**这只验证基础容器，不表示 OpenHands 服务及工具通过。**

下面是按已下载的 OpenHands 0.30.0 源码 `get_action_execution_server_startup_command` 与 `/execute_action` 接口准备的手动测试命令。2026-11-10 用户报告已完成：bash 在两次调用之间保持 `/tmp` 工作目录，IPython 在两次调用之间保持变量并输出 `42`，四次动作均返回 `success: true`。服务、会话数据和日志均位于前面创建的无网络、无宿主目录挂载的检查容器中。此测试只发送我们明确写出的预设动作，不运行数据集里的命令。

服务端口使用容器内的 `18000`（OpenHands 命令的位置参数，可按需更改）。容器以 `--network none` 启动且没有 `-p` 映射，因此该端口只在容器内部使用，不会占用宿主机上的 Ollama 或其他服务端口。

**第 1 步：在已有容器里启动 OpenHands 官方动作执行服务和 Jupyter 插件。** 只启动一次，不要重复执行；如需重新测试，应先检查现有服务或重建一个新名字的容器。

```bash
docker exec -d vault-openhands-runtime-check-030 /bin/bash -lc \
  'cd /openhands/code && exec /openhands/micromamba/bin/micromamba run -n openhands poetry run python -u -m openhands.runtime.action_execution_server 18000 --working-dir /workspace --plugins jupyter --username root --user-id 0 > /tmp/vault-openhands-runtime.log 2>&1'
```

**第 2 步：等待初始化并读取健康状态。** 首次启动 Jupyter 可能需要一些时间；使用容器内部 loopback，不映射宿主端口。

```bash
docker exec -i vault-openhands-runtime-check-030 python - <<'PY'
import json, time, urllib.request
url = 'http://127.0.0.1:18000/alive'
for attempt in range(60):
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            result = json.load(response)
        if result.get('status') == 'ok':
            print('OpenHands 服务状态:', result)
            break
    except Exception:
        pass
    time.sleep(2)
else:
    raise SystemExit('服务未就绪；请查看 /tmp/vault-openhands-runtime.log')
PY
```

健康检查若失败，查看日志再排查；不要把启动命令的退出状态当作服务就绪：

```bash
docker exec vault-openhands-runtime-check-030 \
  /bin/bash -lc 'tail -n 100 /tmp/vault-openhands-runtime.log'
```

**第 3 步：通过 OpenHands API 测试真实 bash 和有状态 IPython。** 此命令从容器内部依次调用 `/execute_action`，通过 OpenHands 的事件协议测试，不使用主机 Python 执行预设命令。它检查 bash 切换工作目录、IPython 两次调用共享变量、结果作为观察返回。

```bash
docker exec -i vault-openhands-runtime-check-030 python - <<'PY'
import json, urllib.request

url = 'http://127.0.0.1:18000/execute_action'

def run(action_type, args):
    body = json.dumps({'action': {'action': action_type, 'args': args, 'timeout': 60}}).encode()
    request = urllib.request.Request(url, data=body,
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=75) as response:
        result = json.load(response)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get('observation') != action_type:
        raise RuntimeError('工具未返回预期观察类型，请检查上方响应和服务日志')
    return result

print('1. bash 切换工作目录')
run('run', {'command': 'cd /tmp', 'blocking': True})
print('2. bash 检查工作目录保持')
bash_result = run('run', {'command': 'pwd', 'blocking': True})
if '/tmp' not in bash_result.get('content', ''):
    raise RuntimeError('bash 工作目录没有按预期保持')
print('3. IPython 定义变量')
run('run_ipython', {'code': 'vault_probe_value = 41', 'include_extra': False})
print('4. IPython 验证变量在第二次调用中保留')
python_result = run('run_ipython', {'code': 'print(vault_probe_value + 1)', 'include_extra': False})
if '42' not in python_result.get('content', ''):
    raise RuntimeError('未观察到预期结果 42')
print('预设工具执行和观察检查通过')
PY
```

**判定：**第 3 步只有在每次都返回预期观察、工作目录检查通过且第二次 IPython 输出 `42` 时才算通过；HTTP 报错、`error` 观察或进程崩溃均不算通过。该验证使用预设结构化 API 动作，尚未测试 XML 解析器与服务连接、模型自主生成动作、训练或任务完成。

如需结束此次检查，可运行 `docker stop vault-openhands-runtime-check-030`；这是停止容器，而不是删除容器或镜像。稍后 `docker start vault-openhands-runtime-check-030` 只会重启容器原有的保持运行命令，OpenHands 动作执行服务需要重新启动。

### 6.6 将 XML 解析结果连接到执行服务

2026-11-10 用户手动执行通过：bash 返回 `VAULT_XML_BASH_OK`，IPython 第二次调用输出 `74`，三个动作均返回成功观察。此结果证明测试桥接链路可用；数据中其他动作类型仍需盘点。

此检查接续 6.5：从本机固定 commit 的 OpenHands 源码复制 XML 解析器和两个工具定义到检查容器的 `/tmp`，在容器中加载它们。测试脚本将解析出的函数名和参数显式映射为运行时 API 动作，发送到容器内部 `127.0.0.1:18000`，再检查观察结果。**此映射是本检查的测试桥接，不代表已经验证 OpenHands 原生控制器或模型自主闭环。**只执行下面写明的预设动作，不执行数据集内容。

先确认 6.5 的容器与服务仍在运行，并核对固定源码版本：

```bash
cd /root/EverOS
export OH_REPO="$PWD/.memo-vault/agent-training/openhands/upstream/OpenHands"
test "$(git -C "$OH_REPO" rev-parse HEAD)" = 3720e1f75b35cca65ef02888d4c7314c9fe50c86 && echo '源码版本正确' || echo '源码版本不匹配，请停止操作'
docker ps --filter name=^/vault-openhands-runtime-check-030$
docker exec vault-openhands-runtime-check-030 python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:18000/alive', timeout=5).read().decode())"
```

如果健康检查失败，先按 6.5 查看日志；容器重启后需要重新执行 6.5 的服务启动命令，不要重复启动正在运行的服务。

复制固定源码中的三个文件到容器临时目录：

```bash
docker exec vault-openhands-runtime-check-030 mkdir -p /tmp/vault-oh-parser-check
docker cp "$OH_REPO/openhands/llm/fn_call_converter.py" vault-openhands-runtime-check-030:/tmp/vault-oh-parser-check/fn_call_converter.py
docker cp "$OH_REPO/openhands/agenthub/codeact_agent/tools/bash.py" vault-openhands-runtime-check-030:/tmp/vault-oh-parser-check/bash_tool.py
docker cp "$OH_REPO/openhands/agenthub/codeact_agent/tools/ipython.py" vault-openhands-runtime-check-030:/tmp/vault-oh-parser-check/ipython_tool.py
```

执行解析、桥接和观察检查：

```bash
docker exec -i -w /openhands/code vault-openhands-runtime-check-030 \
  /openhands/micromamba/bin/micromamba run -n openhands poetry run python - <<'PY'
import importlib.util
import json
from pathlib import Path
from urllib.request import Request, urlopen

root = Path('/tmp/vault-oh-parser-check')

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, root / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

parser = load('vault_oh_parser', 'fn_call_converter.py')
bash = load('vault_oh_bash', 'bash_tool.py')
ipython = load('vault_oh_ipython', 'ipython_tool.py')
tools = [bash.create_cmd_run_tool(), ipython.IPythonTool]

def check(function, parameter, value, expected_action, expected_content):
    xml = f'<function={function}>\n<parameter={parameter}>{value}</parameter>\n</function>'
    messages = parser.convert_non_fncall_messages_to_fncall_messages(
        [{'role': 'assistant', 'content': xml}], tools)
    calls = messages[0].get('tool_calls', [])
    if len(calls) != 1:
        raise RuntimeError(f'预期一个工具调用，实际得到: {calls!r}')
    call = calls[0]['function']
    arguments = json.loads(call['arguments'])
    if call['name'] != function or arguments != {parameter: value}:
        raise RuntimeError(f'解析结果不符合预期: {call!r}')
    print('解析结果:', call, flush=True)

    # 测试桥接：按固定的两种函数名构造运行时动作。
    if function == 'execute_bash':
        action = {'action': 'run', 'args': {'command': arguments['command'], 'blocking': True}, 'timeout': 60}
    elif function == 'execute_ipython_cell':
        action = {'action': 'run_ipython', 'args': {'code': arguments['code'], 'include_extra': False}, 'timeout': 60}
    else:
        raise RuntimeError(f'未支持的函数: {function}')

    request = Request('http://127.0.0.1:18000/execute_action',
        data=json.dumps({'action': action}).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urlopen(request, timeout=75) as response:
        observation = json.load(response)
    print('执行观察:', json.dumps(observation, ensure_ascii=False), flush=True)
    if observation.get('observation') != expected_action or observation.get('success') is not True:
        raise RuntimeError('执行没有返回预期的成功观察')
    if expected_content not in observation.get('content', ''):
        raise RuntimeError(f'观察中缺少预期内容: {expected_content!r}')
    if expected_action == 'run' and observation.get('extras', {}).get('metadata', {}).get('exit_code') != 0:
        raise RuntimeError('bash 命令退出码不为 0')

check('execute_bash', 'command', 'printf VAULT_XML_BASH_OK', 'run', 'VAULT_XML_BASH_OK')
check('execute_ipython_cell', 'code', 'vault_xml_value = 73', 'run_ipython', '')
check('execute_ipython_cell', 'code', 'print(vault_xml_value + 1)', 'run_ipython', '74')
print('XML 解析、测试桥接、工具执行及观察检查通过')
PY
```

成功时应看到 bash 输出 `VAULT_XML_BASH_OK`、第二次 IPython 输出 `74` 和最后的通过信息。若失败，请保留完整报错及 6.5 的服务日志。通过后仍需统计官方数据中实际动作和格式异常、记录未支持的动作，才能对第 2 步整体验收；第 3 步的数据划分与筛选也尚未完成。

### 6.7 第 3 步第一阶段：完整盘点，保留全部轨迹（待手动执行）

为两份官方 SFT 的每一行生成可追溯索引，统计旧清单对应情况、助手消息中的动作、可疑格式和精确重复。**旧清单是过去的小规模候选集，不是数据全集。**未匹配的 6,899 条 CodeAct 轨迹继续保留在官方原文件中，并逐条进入新清单；标记待复核不等于删除或永久排除。

本阶段仅把旧划分作为 ID 对应的参考，所有轨迹的 `assigned_partition` 暂为 `unassigned`，不会输出可训练的 train / validation / test 数据。相同 ID 尚不证明来源版本和任务完全一致；未匹配轨迹的任务分组、跨划分泄漏及原始监督标记尚待核查。后续需确定任务级分组和划分，并审查标记项。`possible_malformed_messages` 是简单标签数量检查，不是完整 XML 校验；`exact-duplicates.jsonl` 只覆盖 system 和 conversations 完全相同的记录，不能排除语义近重复。

执行以下命令，仅需 Python 3 标准库：

```bash
cd /root/EverOS
export OH_DATA_DIR="$PWD/.memo-vault/agent-training/openhands/official-sft-31a76bfb"
export OH_OLD_MANIFEST="$PWD/.memo-vault/agent-training/manifests/harness_audit_20261109/openhands.jsonl"
export OH_INVENTORY_DIR="$PWD/.memo-vault/agent-training/openhands/inventory-20261110-v1"

ls -lh "$OH_DATA_DIR/agenttuning_os.jsonl" "$OH_DATA_DIR/codeactinstruct.jsonl" "$OH_OLD_MANIFEST"
test -f src/everos/agent_training/openhands_inventory.py && echo '盘点脚本存在'

PYTHONPATH=src python -m everos.agent_training.openhands_inventory \
  --data-dir "$OH_DATA_DIR" \
  --old-manifest "$OH_OLD_MANIFEST" \
  --output-dir "$OH_INVENTORY_DIR"
```

脚本先核对两份源文件的固定 SHA-256，拒绝重复 ID，并拒绝覆盖已有输出目录。需要重新运行时指定新的 `OH_INVENTORY_DIR`。查看输出：

```bash
ls -lh "$OH_INVENTORY_DIR"
cat "$OH_INVENTORY_DIR/report.json"
head -n 2 "$OH_INVENTORY_DIR/inventory.jsonl"
wc -l "$OH_INVENTORY_DIR/inventory.jsonl" "$OH_INVENTORY_DIR/exact-duplicates.jsonl"
```

`inventory.jsonl` 每个源轨迹一行，包含来源、源文件和行号、哈希、旧划分参考、动作计数与待复核原因；`report.json` 是汇总；`exact-duplicates.jsonl` 记录完全重复组。预期清单为 **7,334 行**，即 195 条 AgentInstruct OS 加 7,139 条 CodeAct；实际以脚本输出为准。源文件、旧清单均为只读输入，不会删除、截断或改写。目录属于 `.memo-vault`，迁移服务器时须另行复制或重新生成；脚本和本文档应随仓库一起迁移。

本命令尚未执行；盘点结果不能直接用于正式训练。下一阶段根据报告检查支持范围、来源分组和监督信息，设计可复核的全量任务级划分，并检查测试集与训练集隔离。

### 6.8 全量轨迹的候选划分与复刻（2026-11-10）

**后续核查修正：下述 v2 暂不能作为最终划分策略。** 本地上游 `datasets/agenttuning_os/sample_raw.json` 的 `os_0` 显示，重复的 `/etc` 首条任务是前置示范，其三条助手消息标有 `loss: false`；后面出现新问题，后续助手消息标有 `loss: true`。因此，首条用户文本相同并不足以认定 195 条属于同一个当前任务，把它们全部移入 test 的依据不足；也不能把共用示范文本直接当作训练/测试目标泄漏。需要基于真实当前任务、原始标记及轨迹来源重新审计分组。v1、v2 和短测集均保留作候选和审计，不应视为最终评测划分。

用户执行 6.7 得到 7,334 条，均已索引；系统消息加完整对话的精确重复组为 0。`finish` 被盘点脚本标为未验证函数是因为它不在脚本只包含 bash / IPython 的白名单中，不能据此判断数据错误或 `finish` 不受 OpenHands 支持。

首次划分检查发现 195 条 AgentInstruct OS 的首条用户文本都相同，而旧清单把其中 166 / 16 / 13 条放在 train / validation / test。相同任务文本不证明运行环境完全一致，但将它们随机散入三份集合会带来明显的任务泄漏风险。因此 v2 保守地把 195 条全部放入 test，保留旧参考，并在报告中记录旧 train 的 166 条和旧 validation 的 16 条发生移动。**这意味着 v2 的 train 和 validation 暂无 AgentInstruct OS。**如训练必须同时包含该来源，须先确认各轨迹对应任务和环境是否可区分、并制定独立评测；也可另设明确标注仅供流程验证的训练方案。旧样本可能已经用于先前实验，现有 test 也不能直接宣称历史上从未接触过。

CodeAct 沿用项目原先 ID 前四段的任务组规则：含旧清单 ID 的组保留其参考划分，其余组用固定种子 `vault-openhands-full-v1` 的 SHA-256 和 80/10/10 阈值划分。组规则是基于 ID 格式的假设，旧匹配只证明数据集和 ID 对应。全量结果（轨迹数，不是决策样本数）：

| 集合 | AgentInstruct OS | CodeAct | 总数 |
|---|---:|---:|---:|
| train | 0 | 5,734 | 5,734 |
| validation | 0 | 688 | 688 |
| test | 195 | 717 | 912 |
| 合计 | 195 | 7,139 | 7,334 |

生成目录 `.memo-vault/agent-training/openhands/full-split-20261110-v2/` 含 `train.jsonl`、`validation.jsonl`、`test.jsonl` 三份官方 SFT 原始轨迹，以及同名 `*.manifest.jsonl`、`report.json` 和 `first-user-cross-split.jsonl`。候选 v1 存在跨划分重复任务文本，仅保留用于审计，不作为训练或评估输入。v2 的任务组冲突、完整对话精确重复跨划分、首条用户文本精确重复跨划分统计均为 0；语义近重复、源任务定义和原始 loss 标记仍未验证，文件尚不是 LLaMA-Factory 训练输入。

第一次执行 v2 的命令如下；为避免覆盖，复刻时改用一个尚不存在的目录：

```bash
cd /root/EverOS
export OH_DATA_DIR="$PWD/.memo-vault/agent-training/openhands/official-sft-31a76bfb"
export OH_INVENTORY_DIR="$PWD/.memo-vault/agent-training/openhands/inventory-20261110-v1"
export OH_SPLIT_DIR="$PWD/.memo-vault/agent-training/openhands/full-split-20261110-reproduce-v2"

PYTHONPATH=src python -m everos.agent_training.openhands_split \
  --data-dir "$OH_DATA_DIR" \
  --inventory "$OH_INVENTORY_DIR/inventory.jsonl" \
  --output-dir "$OH_SPLIT_DIR"

cat "$OH_SPLIT_DIR/report.json"
wc -l "$OH_SPLIT_DIR"/{train,validation,test}.jsonl
sha256sum "$OH_SPLIT_DIR"/{train,validation,test}.jsonl
```

旧的小规模数据在 `.memo-vault/agent-training/datasets/qwen35_openhands_source_smoke_512_v2/`：`samples.jsonl` 为 1,003 条训练候选决策样本，`decisions.jsonl` 为对应清单，`report.json` 记录来源与截断；它基于旧来源协议及 train/general，未验证为本方案的 OpenHands 框架训练数据，不是官方 SFT 全量划分，也没有该目录下的 validation / test 文件。旧轨迹候选及旧划分参考在 `.memo-vault/agent-training/manifests/harness_audit_20261109/openhands.jsonl`，这是审计清单而非训练数据。

正式训练前还需核实损失标签、动作和来源，转换成 Qwen3.5 / LLaMA-Factory 的训练决策样本，并单独生成适配后的数据文件及配置。不得把 test 输入训练。上述 `.memo-vault` 产物默认不随 Git 迁移，应复制或在新机器上重建。

### 6.9 新服务器复刻：前置文件、全量划分和短测抽样

本机的 `full-split-20261110-v2` 由 `src/everos/agent_training/openhands_split.py` 生成。复刻需要仓库中的 `openhands_inventory.py`、`openhands_split.py`、`openhands_smoke_split.py` 以及本计划文档。**这些文件目前为未跟踪状态，需先纳入 Git 提交才能通过克隆获得。**`.memo-vault` 不随 Git 迁移；需按 6.1 下载固定 revision 的两份官方 SFT，或者从本机复制。旧清单 `.memo-vault/agent-training/manifests/harness_audit_20261109/openhands.jsonl` 也需复制；它用于复刻旧组划分参考，其 SHA-256 应为 `4e7136cbb4209886d016b856dfe00939fc0f668fa2084bf247d47b265242d416`。若复制旧清单或官方文件到其他位置，可调整以下变量。

依次核对前置文件，创建全量盘点和 v2 划分（目录须不存在；Python 只需标准库）：

```bash
cd /root/EverOS
export OH_DATA_DIR="$PWD/.memo-vault/agent-training/openhands/official-sft-31a76bfb"
export OH_OLD_MANIFEST="$PWD/.memo-vault/agent-training/manifests/harness_audit_20261109/openhands.jsonl"
export OH_INVENTORY_DIR="$PWD/.memo-vault/agent-training/openhands/inventory-reproduce-v1"
export OH_SPLIT_DIR="$PWD/.memo-vault/agent-training/openhands/full-split-reproduce-v2"

sha256sum "$OH_DATA_DIR/agenttuning_os.jsonl" "$OH_DATA_DIR/codeactinstruct.jsonl" "$OH_OLD_MANIFEST"
test "$(sha256sum "$OH_OLD_MANIFEST" | cut -d ' ' -f 1)" = 4e7136cbb4209886d016b856dfe00939fc0f668fa2084bf247d47b265242d416 && echo '旧清单哈希一致' || echo '旧清单不一致，请停止操作'
PYTHONPATH=src python -m everos.agent_training.openhands_inventory \
  --data-dir "$OH_DATA_DIR" --old-manifest "$OH_OLD_MANIFEST" --output-dir "$OH_INVENTORY_DIR"

PYTHONPATH=src python -m everos.agent_training.openhands_split \
  --data-dir "$OH_DATA_DIR" --inventory "$OH_INVENTORY_DIR/inventory.jsonl" --output-dir "$OH_SPLIT_DIR"

cat "$OH_SPLIT_DIR/report.json"
wc -l "$OH_SPLIT_DIR"/{train,validation,test}.jsonl
sha256sum "$OH_SPLIT_DIR"/{train,validation,test}.jsonl
```

v2 的预期 SHA-256：train `ba926d0171da22e93238ce819bbaf76379842585d66d94dee5cca25067215b56`，validation `b48647975ef7bfe17bb2fe5326c88ae2deb97c28db25b0a5113813118a216590`，test `37a7d1edecd948a3ecfd7bffea27ef87f453cca9cb8e358f536c93c3ab145524`。输出为原始 SFT 轨迹，后续仍须适配训练格式。

本机另从 v2 生成 `.memo-vault/agent-training/openhands/smoke-split-200-20-20-v1/`：train 200、validation 20、test 20 条，每个集合有同名轨迹和 manifest 文件，并有 `report.json`。抽取规则是各原集合中单轨迹任务组按固定种子 SHA-256 排序，不跨集合移动或拆分任务组；因此短测三份文件均为 CodeAct，195 条 AgentInstruct OS 保留在全量 v2 的 test，不出现在这 20 条 test 短测样本中。要复刻短测（目录须不存在）：

```bash
export OH_SMOKE_DIR="$PWD/.memo-vault/agent-training/openhands/smoke-split-reproduce-200-20-20-v1"
PYTHONPATH=src python -m everos.agent_training.openhands_smoke_split \
  --full-split-dir "$OH_SPLIT_DIR" --output-dir "$OH_SMOKE_DIR"

cat "$OH_SMOKE_DIR/report.json"
wc -l "$OH_SMOKE_DIR"/{train,validation,test}.jsonl
sha256sum "$OH_SMOKE_DIR"/{train,validation,test}.jsonl
```

短测文件本身仍是轨迹候选，未完成 LLaMA-Factory 决策转换和 loss 审核；20 条 test 留作评测，不注入训练。v1 全量目录的 train / validation / test 是 5,900 / 704 / 730 条，并非 200 条短测集；该版本存在 AgentInstruct 相同首条任务文本跨三个划分的问题，本机只保留作审计，不用于当前训练。

### 6.10 Qwen3.5 与 LLaMA-Factory 数据适配

**用户决定：**本阶段仅使用 CodeAct 数据进行训练和评测，默认监督所有非空助手轮次。该策略是本项目明确选用的监督规则，不表示原始 loss 标记已恢复或与论文训练配置完全一致。AgentInstruct 的原始样例含 `loss: false` 的前置示范；其轨迹不参与本次适配。

第 4 步把官方 OpenHands 轨迹拆成逐次助手决策：每个候选保留原 system、当前目标之前的完整对话，以及一个当前助手输出。使用固定 Qwen3.5-0.8B revision 和 LLaMA-Factory 0.9.5 `qwen3_5_nothink` 实际编码器统计 token，使用与 `mask_history: true`、`train_on_prompt: false` 对应的标签构造：上下文不参与 loss，当前目标参与 loss。每个决策保留源轨迹、消息序号、token 和标签哈希。

官方 SFT 未提供可恢复的原始 loss 标记；按用户选择，每条非空助手输出作为当前决策的目标，先前助手轮次仅作为上下文。空助手消息保留在历史上下文但不作为空目标，记录在 `excluded.jsonl`；格式或编码异常及非 CodeAct 轨迹亦记录。`training_ready` 保持 false，直到完成数据加载及训练前必要审核。测试轨迹生成独立候选文件供后续评测，但不登记到训练 `dataset_info.json`。

本草案**不截断、不因 token 超预算而删除样本**：分别统计超过 1024、2048、4096、8192、16384、32768 的数量及最大长度。真正训练时 LLaMA-Factory 仍有 `cutoff_len`；必须先根据报告选能覆盖所用样本的预算，或单独按预算筛出完整样本并记录排除，核对实际数据加载器输出与标签，防止训练器静默截断。本草案目前仅实现关闭截断；若未来需要开启截断，须另行实现显式开关并验证和记录每条改动。不要把本草案输出直接当作已验收训练集。

脚本为 `src/everos/agent_training/openhands_llamafactory.py`，复用 `swe_llamafactory.py` 的真实编码器。Python 应为安装了固定 LLaMA-Factory 0.9.5 及其兼容依赖的独立环境，环境准备见 `docs/vault-swe-llamafactory.zh-CN.md`；Qwen 模型目录需包含 tokenizer / processor 文件。迁移前确保这些脚本随 Git 提交，数据、模型和虚拟环境需另行准备。以下命令只准备数据，不启动训练。

设置路径（按实际服务器修改前两项；输出目录必须不存在）：

```bash
cd /root/EverOS
export OH_LF_PYTHON=/path/to/llamafactory-venv/bin/python
export OH_MODEL=/path/to/Qwen3.5-0.8B
export OH_FULL_INPUT="$PWD/.memo-vault/agent-training/openhands/full-split-20261110-v2"
export OH_SMOKE_INPUT="$PWD/.memo-vault/agent-training/openhands/smoke-split-200-20-20-v1"
export OH_FULL_OUTPUT="$PWD/.memo-vault/agent-training/openhands/lf-full-untruncated-v1"
export OH_SMOKE_OUTPUT="$PWD/.memo-vault/agent-training/openhands/lf-smoke-untruncated-v2"

"$OH_LF_PYTHON" -c 'from importlib.metadata import version; print("LLaMA-Factory:", version("llamafactory"))'
ls "$OH_MODEL" "$OH_FULL_INPUT" "$OH_SMOKE_INPUT"
```

先生成短测候选，再单独生成全量候选：

```bash
PYTHONPATH=src "$OH_LF_PYTHON" -m everos.agent_training.openhands_llamafactory \
  --input-dir "$OH_SMOKE_INPUT" --output-dir "$OH_SMOKE_OUTPUT" \
  --model "$OH_MODEL" --local-only

PYTHONPATH=src "$OH_LF_PYTHON" -m everos.agent_training.openhands_llamafactory \
  --input-dir "$OH_FULL_INPUT" --output-dir "$OH_FULL_OUTPUT" \
  --model "$OH_MODEL" --local-only
```

查看两个报告和文件（无需启动 GPU 训练）：

```bash
cat "$OH_SMOKE_OUTPUT/report.json"
cat "$OH_FULL_OUTPUT/report.json"
wc -l "$OH_SMOKE_OUTPUT"/{train,validation,test,manifest,excluded}.jsonl
wc -l "$OH_FULL_OUTPUT"/{train,validation,test,manifest,excluded}.jsonl
cat "$OH_SMOKE_OUTPUT/dataset_info.json"
cat "$OH_FULL_OUTPUT/dataset_info.json"
```

每个输出目录包含 `train.jsonl`、`validation.jsonl`、单独保留的 `test.jsonl`、`dataset_info.json`、`manifest.jsonl`、`excluded.jsonl` 和 `report.json`；该脚本不生成训练 YAML，不会启动训练。实际样本数及可用率必须在执行后根据报告确认；真实 LLaMA-Factory 重读核对 token/标签、训练/推理模板对齐和隔离检查仍需完成。上述所有助手输出监督策略不能等同于原始监督标记。

### 6.11 CodeAct 不截断适配结果及验证（2026-11-10）

按用户决定，仅选 `codeactinstruct`，每条非空助手消息分别作为当前决策目标，原 loss 标记不恢复；此前历史仅作上下文。空助手消息仍保留在后续样本的上下文中，但自身不是有效训练目标，记入 `excluded.jsonl`。本次没有训练或启动 GPU 训练任务。

在服务器 `/root/vault-swe-run/openhands-adapt-20261110/` 使用 `/root/vault-swe-run/llamafactory-venv/bin/python`、LLaMA-Factory 0.9.5 和 `/root/vault-swe-run/model` 运行。服务器 `tokenizer.json` 的 SHA-256 为 `5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42`，`tokenizer_config.json` 为 `49e2b6e395f959f077f1e992b338919c0d4a9732fc6e613995e06557f843500c`，均与本机 Qwen3.5 固定 revision 快照一致。两套结果已复制回本机并逐文件比对报告中的 SHA-256：

| 数据 | 本机目录 | train / validation / test 决策样本 |
|---|---|---|
| 短测 | `.memo-vault/agent-training/openhands/llamafactory-output-20261110/smoke-untruncated-v2/` | 763 / 74 / 76 |
| 全量 | `.memo-vault/agent-training/openhands/llamafactory-output-20261110/full-codeact-untruncated-v1/` | 22,496 / 2,748 / 2,776 |

短测排除 15 个空助手目标；全量排除 280 个空助手目标，并按来源选择排除 test 中 195 条 AgentInstruct 轨迹。没有因此删除原轨迹；详见各目录的 `excluded.jsonl`。全量 22,496 个训练样本均超过 4,096 token，38 个超过 8,192，3 个超过 16,384，最长 19,378 token。短测 763 个训练样本也均超过 4,096 token，最长 19,378。生成结果无截断，但训练时若 cutoff 低于这些长度仍可能被框架截断；当前 8 GB 显存不能据此假定可训练。转换报告中的 `training_ready: false` 是尚待完成训练前审核的状态。

真实 LLaMA-Factory 加载器检查使用 `openhands_llamafactory_audit.py`：短测 train 763 和 validation 74 全部通过 token/标签哈希核对；全量 train、validation 各抽查前 32 条通过，**全量不是逐条核对**。test 未登记为训练数据，未用该加载器参与训练检查。训练/推理提示模板对齐、任务语义近重复审查和适合硬件的不截断训练选择仍未完成。不要直接启动训练。

在已准备好该服务器目录后，复刻命令如下；输出目录须不存在：

```bash
cd /root/vault-swe-run/openhands-adapt-20261110
export OH_PY=/root/vault-swe-run/llamafactory-venv/bin/python
export OH_MODEL=/root/vault-swe-run/model

PYTHONPATH=src "$OH_PY" -m everos.agent_training.openhands_llamafactory \
  --input-dir .memo-vault/agent-training/openhands/smoke-split-200-20-20-v1 \
  --output-dir results/smoke-untruncated-reproduce-v2 \
  --model "$OH_MODEL" --local-only --source codeactinstruct

PYTHONPATH=src "$OH_PY" -m everos.agent_training.openhands_llamafactory \
  --input-dir .memo-vault/agent-training/openhands/full-split-20261110-v2 \
  --output-dir results/full-codeact-untruncated-reproduce-v1 \
  --model "$OH_MODEL" --local-only --source codeactinstruct

PYTHONPATH=src "$OH_PY" -m everos.agent_training.openhands_llamafactory_audit \
  --prepared-dir results/smoke-untruncated-reproduce-v2 --model "$OH_MODEL" \
  --max-samples 1000 --cutoff-len 32768 > results/smoke-loader-audit-reproduce.log 2>&1
tail -n 1 results/smoke-loader-audit-reproduce.log

PYTHONPATH=src "$OH_PY" -m everos.agent_training.openhands_llamafactory_audit \
  --prepared-dir results/full-codeact-untruncated-reproduce-v1 --model "$OH_MODEL" \
  --max-samples 32 --cutoff-len 131072 > results/full-loader-audit-reproduce.log 2>&1
tail -n 1 results/full-loader-audit-reproduce.log
```

审计用 cutoff 仅用于 CPU 数据加载与对比，不表示可在 GPU 上训练该长度。复刻时应先按 6.9 准备来源数据和清单，再设置当前机器的仓库、模型和环境路径。适配与审计脚本均须随 Git 提交；生成数据默认不随 Git 提交。

### 6.12 模型请求、QLoRA 短测与全量训练命令（训练尚未执行）

**先准备 LLaMA-Factory。** 固定 v0.9.5，commit `7af909522a951e3ad9f022ea6f88b6755257eaa5`。此前本机源码曾位于 `.memo-vault/agent-training/sweagent/upstream/LLaMA-Factory`，2026-11-10 本次交接检查时该目录已不存在；现有 Qwen3.5 训练 Python 环境也没有安装该包。按下面命令可在需要的机器重新下载。本步骤在新目录创建独立 Python 3.11/3.12 环境，不修改已有环境；需要网络和足够空间下载 PyTorch/CUDA 及其他依赖。先设置路径，`OH_BOOTSTRAP_PYTHON` 应改成当前机器真实存在的 Python 3.11 或 3.12：

```bash
cd /root/EverOS
export OH_PROJECT="$PWD"
export OH_LF_SRC="$PWD/.memo-vault/agent-training/sweagent/upstream/LLaMA-Factory"
export OH_LF_ENV="$PWD/.memo-vault/agent-training/envs/openhands-llamafactory-py312"
export OH_BOOTSTRAP_PYTHON=/root/anaconda3/bin/python

"$OH_BOOTSTRAP_PYTHON" --version
```

检查输出确为 Python 3.11 或 3.12，再下载源码；如果 `OH_LF_SRC` 已存在则复用并核对，不会覆盖：

```bash
mkdir -p "$(dirname "$OH_LF_SRC")"
if [ ! -d "$OH_LF_SRC/.git" ]; then
  git clone --branch v0.9.5 https://github.com/hiyouga/LLaMA-Factory.git "$OH_LF_SRC"
fi
git -C "$OH_LF_SRC" rev-parse HEAD
test "$(git -C "$OH_LF_SRC" rev-parse HEAD)" = 7af909522a951e3ad9f022ea6f88b6755257eaa5 \
  && echo 'LLaMA-Factory commit 正确' \
  || echo 'commit 不匹配，请停止，不要继续安装'
```

确认 commit 正确后安装（新服务器若已有此固定环境，可跳过安装并直接核验；输出目录存在时先检查，避免覆盖）：

```bash
if [ -e "$OH_LF_ENV" ]; then
  echo '目标环境已存在，请先核查；不会自动修改该环境'
else
  "$OH_BOOTSTRAP_PYTHON" -m venv "$OH_LF_ENV"
  "$OH_LF_ENV/bin/python" -m pip install --prefer-binary \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    -c "$OH_PROJECT/scripts/requirements-swe-llamafactory-constraints.txt" \
    -e "$OH_LF_SRC" bitsandbytes
fi

"$OH_LF_ENV/bin/python" -m pip check
"$OH_LF_ENV/bin/python" -c 'import torch, transformers, peft; from importlib.metadata import version; print("torch:", torch.__version__, "CUDA可用:", torch.cuda.is_available(), "transformers:", transformers.__version__, "peft:", peft.__version__, "LLaMA-Factory:", version("llamafactory"))'
```

确认 `pip check`、版本和 `CUDA可用` 的实际输出，不能仅凭安装成功认定 GPU 训练可运行。约束文件含 `av==14.2.0`，用于避免先前服务器尝试编译 PyAV 时缺少 FFmpeg 开发库；新系统兼容性仍以实际安装结果为准。本机 GPU 是 RTX 3050 Laptop 4,096 MiB（2026-11-10 检查空闲约 3,084 MiB，运行时会变化）；未来短测要查看空闲显存、日志和 OOM，并不能预先保证成功。依赖安装及本机 4 GB 训练短测本轮均未执行。

已适配数据可以整个目录复制到新服务器，保留 `train.jsonl`、`validation.jsonl`、`test.jsonl`、`manifest.jsonl`、`dataset_info.json`、`excluded.jsonl` 和 `report.json`。先用 `sha256sum` 与报告核对；不必重新转换。报告内的旧模型路径只是来源记录，训练脚本根据新机器传入的模型路径生成配置；脚本还校验数据哈希和固定 Qwen3.5 tokenizer 的两个文件哈希。LLaMA-Factory 0.9.5 在目标机器用于加载、训练和可选服务，不包含在数据文件内。

示例变量按实际机器调整：

```bash
cd /root/EverOS
export OH_PROJECT="$PWD"
export OH_PYTHON=/path/to/llamafactory-venv/bin/python
export OH_MODEL=/path/to/Qwen3.5-0.8B
export OH_DATA_ROOT="$PWD/.memo-vault/agent-training/openhands/llamafactory-output-20261110"
export OH_SMOKE_DATA="$OH_DATA_ROOT/smoke-untruncated-v2"
export OH_FULL_DATA="$OH_DATA_ROOT/full-codeact-untruncated-v1"
export OH_RUN_ROOT=/path/to/openhands-runs
```

**准备完整模型并启动本机 API。** 当前本机有 tokenizer / processor 配置，没有 Qwen3.5 权重；模型文件不在 Git 和 `.memo-vault` 的 AgentLab tokenizer 目录中。新服务器先下载固定 revision，目标目录必须不存在：

```bash
test ! -e "$OH_MODEL" || { echo "模型目录已存在，先核验后换路径：$OH_MODEL"; exit 1; }
"$OH_PYTHON" - <<'PY'
import os
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='Qwen/Qwen3.5-0.8B',
    revision='2fc06364715b967f1860aea9cf38778875588b17',
    local_dir=os.environ['OH_MODEL'],
    allow_patterns=['*.json', '*.jinja', '*.safetensors', '*.model', '*.txt'],
)
PY
```

模型文件名和哈希由 `openhands_qlora.py` 固定；脚本逐项核验 tokenizer、config、chat template 和模型权重。服务脚本使用本机回环地址、固定 `qwen3_5_nothink` 模板和 NF4 4-bit 推理，默认 API key 为本地占位值 `local`。在终端 A 检查端口并启动基座：

```bash
ss -ltn '( sport = :18081 )'
OH_PYTHON="$OH_PYTHON" bash "$OH_PROJECT/scripts/serve_openhands_qwen35.sh" \
  "$OH_MODEL" base 18081
```

**接口参数差异：**OpenHands v0.30 `LLM` 在同步调用中发送 `max_completion_tokens`，LLaMA-Factory 0.9.5 的 `/v1/chat/completions` schema 读取 `max_tokens`。因此在终端 B 启动 loopback 兼容代理；它只转发 `/v1/models` 与非流式 `/v1/chat/completions`，把长度字段改名后转给终端 A 的 18081 服务，不改 system、messages、工具或其他采样字段：

```bash
OH_CONTROL_PYTHON=/path/to/openhands-control-venv/bin/python \
  bash "$OH_PROJECT/scripts/serve_openhands_lf_api_proxy.sh" \
  --upstream http://127.0.0.1:18081 --listen-port 18082
```

在终端 C 请求代理（显式使用 OpenHands 发出的字段）：

```bash
curl --fail --silent --show-error http://127.0.0.1:18082/v1/models \
  -H 'Authorization: Bearer local'
curl --fail --silent --show-error http://127.0.0.1:18082/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer local' \
  -d '{"model":"qwen35","messages":[{"role":"user","content":"Reply with OK only."}],"max_completion_tokens":16,"temperature":0}'
```

单次 curl 仅验证 API 请求 / 响应，不验证 OpenHands 控制器或容器连通。`scripts/serve_openhands_qwen35.sh` 在启动前检查 LLaMA-Factory 0.9.5 和五个固定文件哈希。`openhands_api_compat.py` 代理默认只监听 `127.0.0.1` 且拒绝流式或其他路径；OpenHands v0.30 评测应指向 `http://127.0.0.1:18082/v1`。本机已用模拟上游做过 loopback GET / POST 转发检查，确认长度字段转换；Qwen API 本身尚未启动。终端 A、B 分别按 Ctrl-C 结束；训练前停止二者释放显存。

训练启动后，终端会实时显示 LLaMA-Factory 的进度条、已完成 / 总优化器步数和预计剩余时间，同时将完整输出写入运行目录的 `training.log`。预计时间根据当前速度变化，数据加载、验证和保存也会影响总耗时；数据准备阶段尚未开始训练时不会显示训练 ETA。远程运行建议使用 `tmux` 保持终端；也可用 `tail -f <运行目录>/training.log` 查看日志。

**训练脚本。** `scripts/train_openhands_qlora_smoke.sh` 运行 10 步；新增 `scripts/train_openhands_qlora_scale.sh` 运行 50～100 步测速；`scripts/train_openhands_qlora_full.sh` 按 epoch 扩大训练。共用 `openhands_qlora.py`，使用 NF4 QLoRA、LoRA rank 8、batch 1、梯度检查点、独立 validation、日志及数据哈希，不读取 test。训练命令显式要求 cutoff；超预算的 train / validation 样本默认使任务失败。只有显式传 `--allow-truncation` 才允许 LLaMA-Factory 后续裁剪，该选项会将超预算数写入报告，裁剪可能损失目标或上下文。

适配报告将短测和全量数据均标为 `training_ready:false`：原始 loss 标记未知，工具数据审核和任务语义复核未完成。现在只有用于检验软件 / 显存链路的 10 步 smoke 可通过显式 `--probe-unverified-data` 运行，报告会标记 `probe_only:true`；50～100 步扩大训练和 full 训练会拒绝该候选，直到审核流程把数据报告置为 `training_ready:true`。程序检查报告里的该布尔值，但它不是签名；应完成审核并重导数据报告，不能直接手动把它改成 true。

全量 train 最大长度 19,378 token；32768 cutoff 覆盖现有 train / validation，但不意味着显卡能运行该上下文。短测 train 最大长度同为 19,378，validation 最大 7,766。先在新目录准备 10 步配置；该命令不训练，但要求完整固定模型已下载：

```bash
export OH_CUTOFF=32768
bash "$OH_PROJECT/scripts/train_openhands_qlora_smoke.sh" \
  "$OH_SMOKE_DATA" "$OH_MODEL" "$OH_RUN_ROOT/smoke-preflight" "$OH_CUTOFF" \
  --probe-unverified-data --max-steps 10 --eval-steps 10 --prepare-only
cat "$OH_RUN_ROOT/smoke-preflight/run-report.json"
```

只有具备足够 GPU 显存时才执行技术短测：

```bash
bash "$OH_PROJECT/scripts/train_openhands_qlora_smoke.sh" \
  "$OH_SMOKE_DATA" "$OH_MODEL" "$OH_RUN_ROOT/smoke-10steps" "$OH_CUTOFF" \
  --probe-unverified-data --max-steps 10 --eval-steps 10
cat "$OH_RUN_ROOT/smoke-10steps/run-report.json"
tail -n 100 "$OH_RUN_ROOT/smoke-10steps/training.log"
```

`training.log` 保存训练和 validation 日志。脚本确认 `global_step`、有限 loss 和 Adapter 权重，并写入数据 / 配置哈希；Adapter 重载检查比较启用 / 禁用时的 logits，结果目录须不存在。短测后运行：

```bash
PYTHONPATH="$OH_PROJECT/src" "$OH_PYTHON" -m everos.agent_training.openhands_adapter_check \
  --model "$OH_MODEL" --adapter "$OH_RUN_ROOT/smoke-10steps/adapter" \
  --report "$OH_RUN_ROOT/smoke-10steps/adapter-reload.json"
```

数据审核通过后，先用全量 train / validation 做 50 步 pilot，命令会拒绝当前 `training_ready:false` 数据：

```bash
bash "$OH_PROJECT/scripts/train_openhands_qlora_scale.sh" \
  "$OH_FULL_DATA" "$OH_MODEL" "$OH_RUN_ROOT/scale-50steps" "$OH_CUTOFF" \
  --max-steps 50 --eval-steps 50
```

根据真实步速决定扩大步数。审核通过、pilot 及验证结果符合预期后，才启动 1 epoch 示例；新目录保存单独 Adapter：

```bash
bash "$OH_PROJECT/scripts/train_openhands_qlora_full.sh" \
  "$OH_FULL_DATA" "$OH_MODEL" "$OH_RUN_ROOT/full-1epoch" "$OH_CUTOFF" \
  --epochs 1 --eval-steps 500

PYTHONPATH="$OH_PROJECT/src" "$OH_PYTHON" -m everos.agent_training.openhands_adapter_check \
  --model "$OH_MODEL" --adapter "$OH_RUN_ROOT/full-1epoch/adapter" \
  --report "$OH_RUN_ROOT/full-1epoch/adapter-reload.json"
```

本轮完成训练入口与 shell 语法核对；由于本机没有模型权重，且本机 GPU 只有 4 GiB，未启动 Qwen API、10 步训练、50～100 步 pilot 或 full 训练。Adapter 保存和重载也尚无本项目产物可检查。需先将新增源文件随 Git 提交或复制到服务器，再使用全模型哈希核验。

### 6.13 第 7 步：OpenHands 框架闭环与基座 / Adapter 对照设计（待实现）

**设计时状态及后续更新。** 用户决定暂不在本机安装 LLaMA-Factory，训练短测移至资源足够的新服务器。现有 6.5/6.6 已验证运行时、预设 bash / 有状态 IPython，以及通过测试桥接的 XML 解析和执行；尚未验证 OpenHands 控制器驱动 Qwen 自主调用工具。随后已新增评测脚本和三个自建开发任务，见 6.14；尚无本项目训练出的 OpenHands Adapter 和已核验的独立 CodeAct 交互任务，不能提前标记第 7 步完成。Adapter 对照必须等第 6 步训练、保存并通过重载后进行。

**已有文件及职责：**

| 文件 | 当前用途与边界 |
|---|---|
| `.memo-vault/agent-training/openhands/upstream/OpenHands/openhands/core/main.py` | 固定 OpenHands v0.30.0 控制器入口，后续核实并接入真实框架控制循环。 |
| `.memo-vault/agent-training/openhands/upstream/OpenHands/evaluation/README.md` | 上游评测总体说明；各基准使用自己的 `run_infer.py` 和配置。 |
| `.memo-vault/agent-training/openhands/upstream/OpenHands/evaluation/benchmarks/mint/scripts/run_infer.sh` | 上游 MINT 基准入口，适合后续研究多轮代码任务；需要单独准备其数据及运行环境，不是当前自建任务的现成脚本。 |
| `src/everos/agent_training/openhands_adapter_check.py` | Adapter 加载及有限 logits 检查；不是任务成功率评测。 |
| `scripts/serve_openhands_qwen35.sh` | 固定版本及模型文件哈希后，以本机地址启动 base 或 Adapter 的 LLaMA-Factory API。 |
| `src/everos/agent_training/openhands_api_compat.py`、`scripts/serve_openhands_lf_api_proxy.sh` | loopback 代理把 OpenHands 的 `max_completion_tokens` 转成 LLaMA-Factory 0.9.5 的 `max_tokens`，防止请求长度参数被忽略。 |
| `scripts/run_openhands_base_baseline.sh` | 经兼容代理检查本机模型 API 后，运行固定 OpenHands 控制器上的开发任务基线。 |
| `scripts/train_openhands_qlora_smoke.sh`、`scripts/train_openhands_qlora_scale.sh`、`scripts/train_openhands_qlora_full.sh` | 分别运行 10 步技术短测、50～100 步测速和 1 epoch 示例；共同调用训练模块并保存 Adapter。 |

以下为原设计中的拟新增文件；实际实现和可用命令见 6.14，不要直接根据下表假定文件已存在：

| 拟新增文件 | 职责 |
|---|---|
| `src/everos/agent_training/openhands_eval.py` | 调用固定版本的真实 OpenHands 控制器和运行时，逐任务运行、限制步数并保存事件，不使用手写循环冒充框架。 |
| `src/everos/agent_training/openhands_eval_tasks.py` | 定义本地任务、工作目录初始状态、版本、检查器及判定条件。 |
| `src/everos/agent_training/openhands_eval_report.py` | 校验两组实验可比性并汇总逐任务结果和失败原因。 |
| `scripts/run_openhands_eval.sh` | 统一传入模型、Adapter、任务集、运行时、配置和输出目录；基座与 Adapter 分别启动。 |
| `tests/unit/test_agent_training/test_openhands_eval.py` | 验证任务隔离、判分、事件统计、参数不一致和失败记录；仅用假数据测试评测逻辑，不冒充真实 Agent 成功。 |

**数据与任务边界。** 本轮训练及评测关注 CodeAct，不以 AgentInstruct 作为本轮结果。之前的 `full-split-20261110-v2/test.jsonl` 和 `smoke-split-200-20-20-v1/test.jsonl` 是来源数据中保留的轨迹；它们可用于离线动作格式 / 标签分析，但不能自动作为交互式任务，因为原数据的执行环境、外部资源与状态不一定能重建。不能把这些 test 轨迹用于训练或根据 test 结果反复调参。先构建自有**开发任务集**进行流程验证和调参，再固定配置与判分规则后使用独立**保留任务集**做最终对照；对任务与训练数据开展 ID、文本精确重复及可执行的语义近重复审查，记录审查范围，无法证明零泄漏时如实说明。

**步骤 1：固定软硬件与任务快照。** 在新服务器记录 Qwen3.5-0.8B revision `2fc06364715b967f1860aea9cf38778875588b17`、tokenizer / 模板哈希、LLaMA-Factory 版本、OpenHands v0.30.0 commit `3720e1f75b35cca65ef02888d4c7314c9fe50c86`、镜像摘要 `sha256:6caf256b8a904b54f810a2969bc59ca705a304b897edfc20e443a65bbf851451`、Python 依赖、GPU 和驱动。记录训练 Adapter 的权重哈希、训练数据清单哈希及训练配置。基座和 Adapter 评测使用相同代码、任务集、运行时、工作目录初始快照、system / 工具列表、模板、上下文上限、输出 token 上限、temperature、top-p、最大交互步数与超时。若限于显存无法相同，停止并标记不可比。生成参数和步数先在开发任务上确认，再固定用于保留任务。

**步骤 2：准备模型和网络。** 先在新服务器执行 6.12 中的本地基座 API 请求，确认 Qwen 响应；再接入真实 OpenHands 控制器。对当前 XML 风格训练数据，优先检查固定版本 `config.template.toml` 中 `native_tool_calling`、`openhands/llm/fn_call_converter.py` 与工具声明的实际行为，统一训练 / 推理模板与动作前缀；不能只看 API 响应正常就认定 XML 接入正确。OpenHands v0.30 同步调用发送 `max_completion_tokens`，因此控制器应访问 6.12 的 `127.0.0.1:18082` loopback 兼容代理，代理再转发到 LLaMA-Factory `127.0.0.1:18081`。Adapter 服务应加载同一基座和第 6 步产物，先按 `openhands_adapter_check.py` 重载核验。已有 6.5 容器通过 `--network none` 隔离且 18000 是容器内动作服务，这不自动建立 OpenHands 控制器到容器的生产通信。实现时需核验 v0.30.0 官方 Docker runtime 的连接方式，在可控网络配置下分别确认控制器→模型 API 和控制器→运行时的连通；若无法同时隔离和连通，则记录阻塞，不关闭隔离后直接声称通过。不要让执行容器访问用户的真实项目或密钥。

**步骤 3：构造最小可复现 CodeAct 任务。** 初始建议在开发集准备至少 3 个有确定判分的任务，每任务使用独立容器会话和工作目录：① IPython 计算给定列表之和，检查返回的数值；② IPython 多步处理提供的本地小型 JSON/CSV，检查生成的结果文件内容；③ 两次 IPython 调用共享变量并依据观察给出最终结果，检查真实事件序列和结果。可另设 1 个 bash 文件查看任务，区分 bash 与 Python 工具，但不要将其当作 CodeAct 全部能力。示例仅用于连通性而非基准结论；每项记录任务 ID、提示词、输入文件哈希、可用工具、最大步数、评判脚本及期望结果。检查器和正确答案不随提示暴露给模型。以新的输入/任务构造独立保留集，并防止同一任务模板或来源与训练集重叠；精确重复检查通过也不证明语义完全独立。

**步骤 4：先验证真实控制器链路。** 用明确的小任务启动固定 OpenHands `CodeActAgent`，记录服务请求、模型响应、框架实际解析出的 action、Docker 运行返回的 observation，以及模型对观察的后续响应和最终完成状态。至少一次成功链路必须包含“模型自行生成有效动作 → 真实工具执行 → observation 回到模型 → 模型后续决策”，并由事件和任务检查器共同证明。模型输出不符合工具协议、只说要执行、API 返回成功但未执行、预设 API 动作成功或超时均不能算自主闭环成功；保留失败日志，不改变分母。

**步骤 5：基座基线。** 在每个开发任务上从相同初始化快照启动未微调 Qwen；任务之间重置容器、文件与 IPython 内核，避免状态泄漏。每条任务独立运行并记录异常：模型启动失败、请求超时、解析失败、无动作、执行错误、环境异常、步数用尽、检查失败等。若环境异常，修复后应在两组均按同一版本重新运行受影响任务，不能只重跑成功的一方。基座连通失败可以如实作为结果，但只有成功经历完整链路才能称该基座跑通自主闭环。

**步骤 6：Adapter 对照。** 第 6 步训练和重载通过后，停止基座服务并以相同基座 + Adapter 启动，使用与基座完全一致的环境和任务快照；先在开发任务上比较，固定方案后分别重新初始化并运行保留任务。记录 Adapter 哈希和服务参数；若模型、模板、量化、上下文、工具、随机采样或任务环境不同，报告中标记不可比。随机采样如启用，固定相同 seed 并记录，但不能承诺所有硬件运行完全确定；小样本任务可以重复多次并报告每次结果，不以单次差异认定模型改进。

**步骤 7：独立判分与报告。** 每次输出至少包括 `config.json`（模型、Adapter、软件、图像、生成参数和哈希）、`tasks.jsonl`（任务版本及输入快照引用）、`events.jsonl`（模型响应、动作、观察及状态）、`results.jsonl`（每任务成功/失败和原因）、`report.json`（汇总指标和限制）。所有输出写入新建的 `.memo-vault/agent-training/openhands/evals/<run-id>/`，不得覆盖历史；两组数据均保存。核心指标：任务成功数/总任务数、出现可解析动作的任务数/总任务数、完整工具闭环数/总任务数、失败原因、模型与工具调用次数、超时数和耗时；格式成功率不能替代任务成功率。由不依赖模型自述的检查器验证结果文件 / 数值；`finish` 或“我做完了”本身不算成功。若框架或环境失败，记录不可判分情况并分别展示，不把它当成模型能力提升或静默移出统计。

**步骤 8：验收与扩展。** 最小验收为：同一固定任务、同一运行时上分别产生基座和 Adapter 的可复核事件、检查结果和汇总报告；至少有一次真实自主工具闭环；两组配置一致且无已知任务组交叉。若基座或 Adapter 都无法闭环、Adapter 训练失败、显存不足或服务不能连接，应报告实际失败和阻塞，不宣称通过。之后再研究复用上游 MINT 或其他适合代码工具的基准；需核验其数据版本、许可证、运行环境及与训练数据的污染风险，独立记录，不能把本地玩具任务成功率称为论文基准成绩。

目前能在本机只读确认的命令（不会启动评测）：

```bash
cd /root/EverOS
export OH_REPO="$PWD/.memo-vault/agent-training/openhands/upstream/OpenHands"
git -C "$OH_REPO" rev-parse HEAD
ls "$OH_REPO/evaluation/README.md" "$OH_REPO/evaluation/benchmarks/mint/scripts/run_infer.sh"
ls src/everos/agent_training/openhands_adapter_check.py scripts/serve_openhands_qwen35.sh scripts/run_openhands_base_baseline.sh scripts/train_openhands_qlora_*.sh
```

基座模型请求可参考 6.12；完整控制器接入和 Adapter 评测尚未执行。实际实现和命令见 6.14。

### 6.14 项目评测脚本和复刻命令（已实现，端到端待新服务器验证）

保留集经清单核对为 **717 条 CodeAct 测试轨迹**；根据 ID 粗分类，HotpotQA 190、ALFWorld 183、WikiTableQuestions 109、APPS 75，其余为数学等任务。分类仅是 ID 线索；搜索、游戏、数据库等环境及答案检查器尚未恢复，不能宣称 717 条均可直接交互评测。盘点清单在 `.memo-vault/agent-training/openhands/eval-inventory-20261110-v1/`，其中 717 条均标记 `interaction_ready=false` 是“尚未核验”，不是永久排除。未来需逐条构造任务输入、初始环境、独立检查器并核实与训练集的隔离，再计入可交互任务数。本次未修改任何 train/validation/test 数据。

已新增：

| 文件 | 实际功能 |
|---|---|
| `src/everos/agent_training/openhands_eval.py` | `inventory` 盘点；`run` 调用固定 OpenHands v0.30 `run_controller` 和 Docker runtime；`compare` 校验配置后比较两组结果。 |
| `configs/openhands_eval/development.jsonl` | 三个自建的本地 IPython 连通性任务，仅用于开发，不来自 717 条保留轨迹。 |
| `src/everos/agent_training/openhands_eval_offline.py` | 用真实 LLaMA-Factory 编码器和 GPU，对保留 test 决策计算监督 token 的 NLL；超预算样本跳过并记账，不截断。 |
| `src/everos/agent_training/openhands_eval_offline_compare.py` | 核验同一数据、模型 revision、长度预算、样本和状态后比较 base/adapter NLL。 |
| `tests/unit/test_agent_training/test_openhands_eval.py` | 检查闭环判分、失败工具调用、无工具回答和任务 ID 等；5 条测试在本机通过。 |

**当前验证边界：**本机判分单测与命令行解析通过；本机 OpenHands 控制端导入缺少 `browsergym`，且没有可用的 OpenHands Adapter / 新服务器模型服务，因此真实控制器、Docker 内部网络、API、GPU 离线 NLL 和 Adapter 对照均**尚未端到端运行**。需在新服务器安装固定 OpenHands 源码及依赖、LLaMA-Factory 0.9.5、模型和运行镜像后逐步短测；不能把单元测试视为 Agent 闭环。

**1. 盘点保留轨迹。** 在包含 v2 清单的项目根目录执行（输出目录须不存在）：

```bash
cd /root/EverOS
export OH_PROJECT="$PWD"
PYTHONPATH="$OH_PROJECT/src" python -m everos.agent_training.openhands_eval inventory \
  --manifest "$OH_PROJECT/.memo-vault/agent-training/openhands/full-split-20261110-v2/test.manifest.jsonl" \
  --output "$OH_PROJECT/.memo-vault/agent-training/openhands/eval-inventory-reproduce-v1"
```

**2. 准备交互开发评测。** 固定版本 OpenHands 和对应 Python 环境需在新服务器另行准备；Docker 镜像参见 6.3，模型 API 参见 6.12。下面的 `OH_CONTROL_PYTHON` 是能导入 OpenHands 全部控制器依赖的 Python，不一定等于 LLaMA-Factory 环境。API 服务需由用户另外启动，使用基座及固定 Qwen3.5 模板；运行前确认服务使用的模型、量化和 generation 设置。以下变量需按服务器修改：

```bash
cd /root/EverOS
export OH_PROJECT="$PWD"
export OH_REPO="$PWD/.memo-vault/agent-training/openhands/upstream/OpenHands"
export OH_CONTROL_PYTHON=/path/to/openhands-control-venv/bin/python
export OH_EVAL_ROOT="$PWD/.memo-vault/agent-training/openhands/evals"
export OH_DEV_TASKS="$PWD/configs/openhands_eval/development.jsonl"
export OH_NETWORK=vault-openhands-eval-internal

git -C "$OH_REPO" rev-parse HEAD
docker image inspect ghcr.io/all-hands-ai/runtime:0.30-nikolaik --format '{{.Id}}'
PYTHONPATH="$OH_REPO:$OH_PROJECT/src" "$OH_CONTROL_PYTHON" -c 'from openhands.core.main import run_controller; print("OpenHands controller import OK")'
curl --fail --silent --show-error http://127.0.0.1:18082/v1/models
```

版本应为 `3720e1f75b35cca65ef02888d4c7314c9fe50c86`，镜像 ID 应为 `sha256:c3bc648710afc8734d447df8a83912f41d36b0c37346433b3ed1f77ba9b9ce13`。准备一个只给评测用的 Docker internal 网络；该配置与 6.5 的 `--network none` 容器不同，运行器需要宿主机与动作服务连通，internal 网络 / 端口映射是否正常须实测：

```bash
docker network inspect "$OH_NETWORK" >/dev/null 2>&1 || docker network create --internal "$OH_NETWORK"
docker network inspect "$OH_NETWORK" --format '{{.Internal}}'
mkdir -p "$OH_EVAL_ROOT"
```

输出必须是 `true` 才继续。运行器每任务启动独立容器，不挂载宿主工作区，限制 2 GiB RAM / 2 CPU，控制器端口绑定本机；不自动使用之前 6.5 的检查容器。Docker internal 网络与端口转发的实际隔离和连通性依赖宿主配置，首次运行需核查。任务内只按 `<solution>答案</solution>` 做精确比对；此外必须观察到关联的工具调用、成功观察和后续 agent 动作才判成功，模型自述不能单独算成功。

**3. 基座与 Adapter 同任务对照。** 先在 6.12 服务中加载基座，执行自建开发任务（输出目录须不存在）：

```bash
PYTHONPATH="$OH_REPO:$OH_PROJECT/src" "$OH_CONTROL_PYTHON" \
  -m everos.agent_training.openhands_eval run \
  --tasks "$OH_DEV_TASKS" --output "$OH_EVAL_ROOT/dev-base-v1" \
  --openhands-repo "$OH_REPO" --mode base --network "$OH_NETWORK" \
  --model-name openai/qwen35 --base-url http://127.0.0.1:18082/v1 \
  --max-iterations 10 --max-output-tokens 512 --timeout 300
```

第 6 步产出 Adapter 后，停止基座服务，用相同模型服务配置和额外的 `--adapter_name_or_path /path/to/adapter` 启动 Adapter 服务。将下方 `OH_ADAPTER` 改为实际目录（若权重是 `.bin`，调整文件名）：

```bash
export OH_ADAPTER=/path/to/adapter
PYTHONPATH="$OH_REPO:$OH_PROJECT/src" "$OH_CONTROL_PYTHON" \
  -m everos.agent_training.openhands_eval run \
  --tasks "$OH_DEV_TASKS" --output "$OH_EVAL_ROOT/dev-adapter-v1" \
  --openhands-repo "$OH_REPO" --mode adapter \
  --adapter-weights "$OH_ADAPTER/adapter_model.safetensors" --network "$OH_NETWORK" \
  --model-name openai/qwen35 --base-url http://127.0.0.1:18082/v1 \
  --max-iterations 10 --max-output-tokens 512 --timeout 300

PYTHONPATH="$OH_PROJECT/src" python -m everos.agent_training.openhands_eval compare \
  --base "$OH_EVAL_ROOT/dev-base-v1" --adapter "$OH_EVAL_ROOT/dev-adapter-v1" \
  --output "$OH_EVAL_ROOT/dev-comparison-v1.json"
```

比较器核对任务文件哈希、运行时、网络和生成参数，统计任务总数、成功数及错误数，不会自动证明服务真的加载了指定 Adapter；`config.json` 中 `server_model_identity_verified=false` 必须靠服务启动日志、权重哈希和人工核对。输出 `events.jsonl` 是框架事件，未保证含模型完整原始响应；排错时还需保存模型服务日志。每任务超时是尽力限制，运行时启动失败后应检查是否遗留容器。以上开发任务仅验证闭环，不可作为 717 条保留数据的效果结论。

要评测已重建的 CodeAct 任务，需另建 JSONL：每行包含唯一 `id`、真实 `prompt`、答案字符串 `expected`、`checker` 固定 `solution_exact`；答案仅供宿主检查器使用，不传给模型。本实现仅支持精确文本答案，无文件、数据库或 ALFWorld 专用检查器；这些复杂任务需在补齐真实环境和专用检查器后扩展，不能把未实现的任务算通过。先在开发任务固定策略，再使用独立的已核验保留任务文件分别运行 base / Adapter；结果不能与离线轨迹数量混同。

**4. 可选离线 NLL。** 使用第 6 步模型环境和已生成的 `full-codeact-untruncated-v1` 决策文件；测试数据仅在方案固定后使用。超出 `--cutoff-len` 的样本会被跳过并记录，不发生截断；GPU 显存不足仍会 OOM。`--max-samples 10` 是演示；设为 `0` 才扫描全部 test 决策。base 和 Adapter 要用相同预算及样本数，且模型服务应先停止以释放显存。

```bash
export OH_PYTHON=/path/to/llamafactory-venv/bin/python
export OH_MODEL=/path/to/Qwen3.5-0.8B
export OH_PREPARED="$OH_PROJECT/.memo-vault/agent-training/openhands/llamafactory-output-20261110/full-codeact-untruncated-v1"
export OH_CUTOFF=8192

PYTHONPATH="$OH_PROJECT/src" "$OH_PYTHON" -m everos.agent_training.openhands_eval_offline \
  --prepared-dir "$OH_PREPARED" --model "$OH_MODEL" --mode base \
  --output "$OH_EVAL_ROOT/offline-base-v1" --cutoff-len "$OH_CUTOFF" --max-samples 10

PYTHONPATH="$OH_PROJECT/src" "$OH_PYTHON" -m everos.agent_training.openhands_eval_offline \
  --prepared-dir "$OH_PREPARED" --model "$OH_MODEL" --mode adapter --adapter "$OH_ADAPTER" \
  --output "$OH_EVAL_ROOT/offline-adapter-v1" --cutoff-len "$OH_CUTOFF" --max-samples 10

PYTHONPATH="$OH_PROJECT/src" python -m everos.agent_training.openhands_eval_offline_compare \
  --base "$OH_EVAL_ROOT/offline-base-v1" --adapter "$OH_EVAL_ROOT/offline-adapter-v1" \
  --output "$OH_EVAL_ROOT/offline-comparison-v1.json"
```

离线 NLL 是对保留助手输出的预测损失，越低通常代表拟合这些目标更好，不等于交互任务成功。所有新增文件尚需纳入 Git；产物在 `.memo-vault`，需另行复制或重建。

### 6.15 新 24 GB 服务器：准备环境与数据

下面按实际执行顺序交接。项目中的新增训练 / 评测脚本须先提交并推送，或另行复制到新服务器；`.memo-vault` 不随 Git 克隆，因此数据需要单独传输。以下路径、服务器地址和端口按实际情况修改。

**1. 在本机复制已适配数据。** 两个目录分别是短测和全量 CodeAct 的 LLaMA-Factory 数据，包含 train、validation、test、登记文件及报告，不需要重新转换。在**本机终端**执行：

```bash
export OH_REMOTE='root@服务器IPv4地址'
export OH_REMOTE_PORT=22
export OH_REMOTE_PROJECT=/root/EverOS
export OH_LOCAL_DATA=/root/EverOS/.memo-vault/agent-training/openhands/llamafactory-output-20261110
export OH_REMOTE_DATA="$OH_REMOTE_PROJECT/.memo-vault/agent-training/openhands/llamafactory-output-20261110"

if ssh -4 -p "$OH_REMOTE_PORT" "$OH_REMOTE" \
  "test ! -e '$OH_REMOTE_DATA/smoke-untruncated-v2' && test ! -e '$OH_REMOTE_DATA/full-codeact-untruncated-v1' && mkdir -p '$OH_REMOTE_DATA'"; then
  scp -4 -P "$OH_REMOTE_PORT" -r \
    "$OH_LOCAL_DATA/smoke-untruncated-v2" "$OH_LOCAL_DATA/full-codeact-untruncated-v1" \
    "$OH_REMOTE:$OH_REMOTE_DATA/"
else
  echo '服务器连接失败或目标数据已存在；请检查后重试'
fi
```

**2. 在新服务器安装环境、下载模型和 OpenHands。**

- 查看 `nvidia-smi`，确认 GPU 型号、可用显存和驱动；准备包含最新项目脚本的 `/root/EverOS`。
- 按 **6.12** 安装固定版本 LLaMA-Factory 及依赖；同节有 Qwen3.5-0.8B 固定 revision 的完整下载命令。模型下载到服务器，训练脚本会核对模型文件哈希。
- 按 **6.2** 下载固定版本 OpenHands 源码，并准备可导入 OpenHands 控制器依赖的 Python 环境；按 **6.3** 下载并标记 Docker runtime 镜像。
- 按 **6.5** 检查容器内 bash、状态保持的 IPython；按 **6.6** 检查 XML 解析、测试桥接和工具观察。这些只是运行时连接检查，真实控制器闭环还需第 6.14 节的评测。训练本身不依赖启动 OpenHands 容器。

后续命令在**新服务器终端**执行，先设置路径：

```bash
cd /root/EverOS
export OH_PROJECT="$PWD"
export OH_PYTHON=/path/to/llamafactory-venv/bin/python
export OH_MODEL=/root/models/qwen35-08b-revision
export OH_DATA_ROOT="$OH_PROJECT/.memo-vault/agent-training/openhands/llamafactory-output-20261110"
export OH_SMOKE_DATA="$OH_DATA_ROOT/smoke-untruncated-v2"
export OH_FULL_DATA="$OH_DATA_ROOT/full-codeact-untruncated-v1"
export OH_RUN_ROOT=/root/openhands-runs

nvidia-smi
"$OH_PYTHON" -m pip check
"$OH_PYTHON" -c 'import torch; from importlib.metadata import version; print("LLaMA-Factory:", version("llamafactory"), "CUDA:", torch.cuda.is_available())'
mkdir -p "$OH_RUN_ROOT"
```

**3. 核验传输后的数据。** 任一文件哈希不一致就先排查，勿开始训练：

```bash
"$OH_PYTHON" - <<'PYDATA'
import hashlib, json, os
from pathlib import Path
for variable in ('OH_SMOKE_DATA', 'OH_FULL_DATA'):
    directory = Path(os.environ[variable])
    report = json.loads((directory / 'report.json').read_text(encoding='utf-8'))
    for name, expected in report['output_sha256'].items():
        with (directory / name).open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != expected:
            raise SystemExit(f'哈希不匹配：{directory / name}')
    print(f'{variable}: 报告所列文件哈希一致')
PYDATA
```

### 6.16 新 24 GB 服务器：短测、训练与评测

**1. 预检和 10 步训练。** 两份数据目前均为 `training_ready:false`，仅可用 `--probe-unverified-data` 执行技术短测；这是检查软件、显存和 loss，并非正式训练验收。输出目录需不存在。以下 32768 token 预算覆盖目前统计的训练与验证样本长度，但 **24 GB 能否运行仍需实测**；若显存不足，降低预算时须显式启用 `--allow-truncation`，并使用 `everos.agent_training.openhands_truncation_audit` 审计和记录被截断样本及监督目标的影响；该工具只检查指定数量的前序样本，也不能证明上下文语义完整。默认不截断。

```bash
export OH_CUTOFF=32768
bash "$OH_PROJECT/scripts/train_openhands_qlora_smoke.sh" \
  "$OH_SMOKE_DATA" "$OH_MODEL" "$OH_RUN_ROOT/smoke-preflight" "$OH_CUTOFF" \
  --probe-unverified-data --max-steps 10 --eval-steps 10 --prepare-only
cat "$OH_RUN_ROOT/smoke-preflight/run-report.json"

bash "$OH_PROJECT/scripts/train_openhands_qlora_smoke.sh" \
  "$OH_SMOKE_DATA" "$OH_MODEL" "$OH_RUN_ROOT/smoke-10steps" "$OH_CUTOFF" \
  --probe-unverified-data --max-steps 10 --eval-steps 10
cat "$OH_RUN_ROOT/smoke-10steps/run-report.json"
tail -n 100 "$OH_RUN_ROOT/smoke-10steps/training.log"

PYTHONPATH="$OH_PROJECT/src" "$OH_PYTHON" -m everos.agent_training.openhands_adapter_check \
  --model "$OH_MODEL" --adapter "$OH_RUN_ROOT/smoke-10steps/adapter" \
  --report "$OH_RUN_ROOT/smoke-10steps/adapter-reload.json"
```

运行时另开终端用 `nvidia-smi -l 1` 观察显存；检查训练与验证 loss、步数和 Adapter 保存 / 重载。失败时保留日志，调整配置后使用新的输出目录。模型服务若已启动，训练前先停止以释放显存。

**2. 扩大及完整训练。** 当前全量候选报告仍为 `training_ready:false`，下面两条命令**现在会被脚本拒绝**。必须先完成工具数据和任务语义等审核并重新生成可追溯的 `training_ready:true` 报告，不能直接手改该标记。审核和短测通过后，先跑 50 步，再按速度、显存和验证结果决定是否进行完整训练：

```bash
bash "$OH_PROJECT/scripts/train_openhands_qlora_scale.sh" \
  "$OH_FULL_DATA" "$OH_MODEL" "$OH_RUN_ROOT/scale-50steps" "$OH_CUTOFF" \
  --max-steps 50 --eval-steps 50

bash "$OH_PROJECT/scripts/train_openhands_qlora_full.sh" \
  "$OH_FULL_DATA" "$OH_MODEL" "$OH_RUN_ROOT/full-1epoch" "$OH_CUTOFF" \
  --epochs 1 --eval-steps 500

PYTHONPATH="$OH_PROJECT/src" "$OH_PYTHON" -m everos.agent_training.openhands_adapter_check \
  --model "$OH_MODEL" --adapter "$OH_RUN_ROOT/full-1epoch/adapter" \
  --report "$OH_RUN_ROOT/full-1epoch/adapter-reload.json"
```

微调产物保存在对应运行目录的 `adapter/`，保留该目录、日志和报告；完整训练不读取 test。

**3. OpenHands 评测。** 按 **6.12** 启动基座模型服务与接口兼容代理并测试请求，再按 **6.14 第 2～3 项**准备 OpenHands 控制环境、Docker 内部网络和开发任务，分别运行基座与 Adapter 并比较。两组须使用相同基座、任务、工具配置和生成参数；手动核对服务日志确认加载了目标 Adapter。可按 **6.14 第 4 项**另做保留 test 的离线 NLL 对照。

当前提供的三个开发任务只用于验证闭环；保留的 717 条 CodeAct 轨迹尚未完成环境及检查器重建，不能直接声称可全部交互评测。以上真实 GPU 训练、控制器闭环与基座 / Adapter 对照尚待新服务器验证。
