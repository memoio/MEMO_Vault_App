# SWE-smith 全量轨迹 → SWE-Agent + Qwen3.5 开发与新服务器复刻计划

> 状态：阶段 1–3 的实际执行记录见文末第十三节；阶段 4 按用户要求缩减为第十四节的新服务器安装与容器最小测试，尚未在新服务器执行。阶段 5 已完成候选转换和短测加载器审计，全量编码尚未完成，详见第十六节；训练与评测尚未执行。前文是原计划，实际结果及路径以文末为准。

**目标：**保留 SWE-smith 官方发布的完整轨迹，建立可复刻的全量清点、去重分组、划分、转换和审计流程；从各划分抽取约 train 200 / validation 20 / test 20 条轨迹短测，在约 24 GB 单 GPU 上训练 Qwen3.5-0.8B Adapter，并用固定 SWE-Agent 和独立仓库任务对照基座。

**架构：**官方轨迹的不可变快照 → 原样存档与清单 → 审计三种表达及源工具 → 任务/仓库级划分 → ADP/目标 SWE-Agent 格式 → LLaMA-Factory 决策数据 → QLoRA → 真实容器中基座/Adapter 对照。完整原始数据及所有排除记录均保留；短测从全量划分派生。优先在本机完成数据处理，带校验和传到新服务器；同样保留从源重建的代码和命令。

**技术：**Python 3.11/3.12、Hugging Face Hub/datasets、PyArrow、ADP、SWE-Agent/SWE-ReX、Docker、LLaMA-Factory v0.9.5、Qwen/Qwen3.5-0.8B、NF4 QLoRA。GPU 型号、驱动、显存和训练吞吐以服务器实测为准。

## 一、范围、现状与依据

1. 唯一全量源的基准是 [SWE-bench/SWE-smith-trajectories](https://huggingface.co/datasets/SWE-bench/SWE-smith-trajectories)，不是旧的本地 240 条，也不是 [SWE-smith 任务实例](https://huggingface.co/datasets/SWE-bench/SWE-smith)。上游卡片列出 `tool`、`xml`、`ticks` 三个数据划分；当前所见分别为 24,100、26,076、25,826 行。这些是格式分区而非 train/test/validation，不可简单相加视为互不重复轨迹。卡片的 5,017 条训练说明与分区行数口径不同，需实测并记明，不能推测其覆盖关系。锁定版本后以实际清单和计数为准。
2. 上游 [SWE-smith 项目](https://github.com/SWE-bench/SWE-smith)区分任务实例、轨迹和仓库环境。全量指该官方轨迹仓库固定 revision 下的全部文件和记录，并不声称覆盖每个任务实例或其他机构生成的所有轨迹。ADP [公开集合](https://huggingface.co/datasets/neulab/agent-data-collection)与固定版本转换器作为对照，只有对齐来源后才能判断覆盖，不能将其 `full_sft` 当作官方轨迹全量。
3. 已有 [SWE 流程与状态](../../vault-swe-pipeline-status-and-files.zh-CN.md)记录：旧候选 200/20/20，240 条转为 SWE-Agent 对话、5,811 个决策；预设工具动作通过但未证明所有源动作等价；旧基座和 Adapter 未完成玩具任务。新 LLaMA-Factory 路径未完成真实训练。旧 1024 数据所有保留样本均截断，不直接作为新实验的质量保证。
4. 固定基座 revision `2fc06364715b967f1860aea9cf38778875588b17`；候选 LLaMA-Factory v0.9.5 commit `7af909522a951e3ad9f022ea6f88b6755257eaa5`、旧 ADP commit `ab1180306a3d4d27e9195448c90f030607eba981`。旧 SWE-Agent 版本及镜像须从实际记录核实，不猜测。训练与运行使用相同工具定义和动作模板。
5. 不覆盖源文件、旧实验或已有输出。每个阶段写独立新目录；记录执行日期、代码 commit/未提交差异、参数、输入输出哈希、版本、日志、失败和验收。容器内执行的轨迹动作不能在宿主机直接运行。

## 二、选择与交付物

推荐**保存全部官方格式，按轨迹/任务关联，再选经验证的单一表达进行训练**。优点是保留完整资料并避免将同一轨迹的多种格式重复训练；代价是需要逐格式解析与对齐。备选是从原始表达逐条转换全部版本，但必须先证明语义等价；只下载 ADP 已转换文件不能满足完整上游保留。无法匹配或语义不明的记录全部留存、隔离并报告，不静默丢弃或强行变换。

| 位置 | 职责 / 状态 |
|---|---|
| `src/everos/agent_training/swe_full_dataset.py` | **拟新增**：流式清点、身份映射、分组划分、短测抽样、manifest 和报告。 |
| `src/everos/agent_training/swe_full_convert.py` | **拟新增或按实情扩展现有转换器**：三种源表达解析、事件转换与拒绝原因；不执行数据内代码。 |
| `tests/unit/test_agent_training/test_swe_full_dataset.py`、`test_swe_full_convert.py` | **拟新增**：确定性、泄漏、异常、映射和工具语义测试。 |
| `swe_lora_pipeline.py`、`swe_llamafactory.py` | **已有**：先检查输入 schema、可扩展性和现有约束；通过测试后复用或小幅扩展，不能假定支持全量。 |
| `.memo-vault/agent-training/sweagent/full-<revision>/` | 运行产物：`raw/`、`metadata/`、`inventory/`、`splits/`、`converted/`、`lf/`、`reports/`；不进入 Git。 |
| `docs/vault-swe-pipeline-status-and-files.zh-CN.md` | 后续实施后补充实际版本、文件、命令和结果。 |

## 三、阶段 0：环境与版本清点（待执行）

- [ ] 确认本机及新服务器操作系统、Python、磁盘空间、GPU/驱动/CUDA、Docker 和网络；同时记录时间与硬件。全量数据、解压/中间文件、模型、缓存、检查点均需要空间；先根据 Hub 文件大小和实际展开估算，留足余量，空间不足停止，不删除旧数据。
- [ ] 记录项目 `git status`、HEAD、相关未提交文件和依赖；审阅 `paper_adp_conversion.py`、`prepare_datasets.py`、`swe_lora_pipeline.py`、`swe_llamafactory.py` 及旧工具记录，确定已实现与需要新增的接口。迁移未提交代码时单独打包，不能假定 clone 包含它们。
- [ ] 用 Hub API 查询 `SWE-bench/SWE-smith-trajectories` 的完整 commit SHA、文件清单、大小、数据卡及许可证，写入 `metadata/source.json`；下载时必须使用这一 SHA。查官方任务实例版本、SWE-Agent 工具来源和数据生成版本，未知即标记未知，不编造版本。

只读检查示例：

```bash
cd /root/EverOS
git status --short
git rev-parse HEAD
python3 --version
df -h /root
nvidia-smi
docker --version
```

后两项若命令不存在则记录，不表示步骤通过。实际运行目录采用新目录，例如：

```bash
export SWE_PROJECT=/root/EverOS
export SWE_RUN=/root/EverOS/.memo-vault/agent-training/sweagent/full-<实际revision短码>
```

**验收：**来源、不可变 revision、机器资源、源码状态与许可有记录；输入产物没有被覆盖。上述尖括号为执行时替换项，不直接复制运行。

## 四、阶段 1：下载和原样保存完整轨迹（后续任务）

- [ ] 在独立 Python 环境安装经确认兼容的 `huggingface_hub`、`datasets`、`pyarrow` 并冻结依赖；以下为固定 SHA 下载的操作模板，首次执行前读取 `snapshot_download` 当前接口并检查目标目录不存在。不要将源仓库的远程代码作为数据加载脚本执行。
- [ ] 保存所有 `tool/xml/ticks` 数据文件、数据卡及元数据，而非只下载 5,017 条训练说明或一种格式；生成相对路径、字节数、SHA-256 清单并复核文件数。保留下载时产生的元数据但将可重建缓存与源文件清单区分。

```bash
export SWE_DATA_REV=<通过Hub查询并记录的完整commit SHA>
test ! -e "$SWE_RUN/raw"
mkdir -p "$SWE_RUN/raw" "$SWE_RUN/metadata"
python - <<'PY'
import os
from huggingface_hub import snapshot_download
snapshot_download(repo_id='SWE-bench/SWE-smith-trajectories', repo_type='dataset',
                  revision=os.environ['SWE_DATA_REV'], local_dir=os.environ['SWE_RUN'] + '/raw')
PY
cd "$SWE_RUN/raw"
find . -type f ! -path './.cache/*' -print0 | sort -z | xargs -0 sha256sum > "$SWE_RUN/metadata/raw.sha256"
sha256sum -c "$SWE_RUN/metadata/raw.sha256"
```

以上命令会创建并下载数据，仅为后续执行模板；网络失败或空间不足时保留失败记录，重新校验，不用其他来源静默代替。记录数据本身、底层仓库代码及模型输出的许可证和再分发条件；数据卡 MIT 不自动证明每个源仓库许可证相同。

## 五、阶段 2：全量清点与多格式身份映射（待实现、待执行）

- [ ] 流式读取所有 parquet 行，不把整个数据集载入内存。为每行记录 revision、文件、行组/行号、格式分区、`traj_id`、`instance_id`、`resolved`、`model`、内容哈希、解析状态；字段不存在保留 null。逐文件汇总行数、字节、异常、ID 重复、缺字段、模型和 resolved 分布。
- [ ] 以可靠的 `traj_id`、`instance_id` 及元数据建立跨格式对应表；确认同 ID 不同内容是表达差异还是冲突。相同 `instance_id` 的所有轨迹及格式必须在同一划分；缺 ID、冲突或无可靠仓库归属的项目隔离待核查，不凭行号映射。严格重复和候选近重复分别统计；近重复审计覆盖率及不能证明的部分明确报告。
- [ ] 保留每一个原始行及格式，训练候选按经验证可转换的**一份轨迹表达**去重。不同格式无法互证时记录差异及覆盖而非扩大训练。报告原始总行数、唯一轨迹数、任务数、仓库数、各映射状态、候选数、隔离原因；每一步计数守恒。
- [ ] 测试：多格式重复、同 ID 内容冲突、缺 ID、坏行、不同源文件顺序、含大字段、内存受限流式处理；重复运行相同输出哈希。

**验收：**每个源行可追溯，三种格式覆盖关系有证据；全量来源未被筛选动作删除，数量对账成立。

## 六、阶段 3：全量划分与短测抽样（代码后续实现并保留）

- [ ] 先查任务元数据、仓库及基准测试的关联。默认按**仓库 group**分组划分，优先避免不同版本、同任务、多轨迹或多表达跨划分；仓库不能可靠解析时进入隔离。明确报告与已有 OpenHands/其他路径及将用的闭环任务的重叠检查范围，无法确认的不称零泄漏。
- [ ] 使用固定 seed `20261110` 和可复现的稳定哈希，对排序后的组分配 train/validation/test，初始目标比例 90/5/5；实际按组大小接近目标即可，记录每组及实际比例。禁止按行随机划分。已有旧划分若与新划分冲突，以新 manifest 为本次唯一依据并报告，旧数据不覆盖。
- [ ] 先固定全量划分，再从对应划分中固定种子抽取约 200/20/20 **轨迹**；优先增加不同仓库和任务覆盖，重复轨迹不混入。若有效数或分组不够，报告实际数并停止宣称达到目标，不跨集合借样。每条轨迹可能展开多个训练决策，分别统计。
- [ ] 保存 `full_manifest.jsonl`（每源行及归属）、`group_assignments.jsonl`、`smoke_manifest.jsonl`、`excluded.jsonl`、`split_report.json`、源代码、参数、依赖和哈希；测试集只作最终锁定评测，验证用于调参。短测测试轨迹属于全量测试集，不能回流训练。
- [ ] 测试固定种子、输入乱序、仓库/任务/轨迹/跨格式不交叉、异常分组隔离、短测是相应全集子集、样本不足和清单对账。对源无 group、数据变化导致划分变化的情况有可见失败。

**验收：**划分代码和清单能从同一固定源重建；所有原始记录在清单中，处理失败与训练候选分开，保留完整数据供未来扩大训练。

## 七、阶段 4：工具语义与全量转换（待执行）

- [ ] 从源记录实际枚举工具名、参数、响应、退出状态、工作目录、编辑前后内容及提交语义；检查三种格式的转换关系、工具配置和生成版本。固定目标 SWE-Agent commit、配置、解析器、提示、SWE-ReX、容器镜像 digest，并保存各自哈希。先验证旧记录所用配置，版本不符即明确另设版本。
- [ ] 将源工具与目标工具逐项映射：命令执行、读取、文件编辑（匹配失败、路径、换行和多次命中）、提交/结束；工具结果、调用次序、工作目录、返回码和异常均须核对。模型 system 与调用格式须和运行时一致。不支持、无法证明等价的动作隔离，保留源行和原因；禁止执行来源的动态 `available_apis`。
- [ ] 在一次性隔离 Docker 临时仓库中用**人工构造**动作比较源/目标工具对同一初始快照的修改、输出和失败；进行真实解析器往返测试。覆盖引号、多行、空内容、路径、编辑失败及 submit；有状态语义不能转成无状态实现而不记录。
- [ ] 对全量候选按固定划分转换，保留 source ID、group、原格式、事件编号、目标、观察、loss 标记来源以及转换代码版本。无法恢复监督标记时标记未知并隔离，不能假定全部 assistant 内容可监督。对比官方 ADP 样例与已有转换结果，记录差异；不假定旧转换器兼容当前源 schema。
- [ ] 输出每个分区转换成功/失败/不支持的轨迹数与动作数、逐条排除和对应关系。处理全量时支持分批、断点和原子输出，断点恢复需校验输入及配置哈希，不能混入不同版本。

**验收：**有测试证据支持声明的工具子集和转换；预设动作通过不等于模型自主完成任务；未支持的记录完整留存。

## 八、阶段 5：LLaMA-Factory 适配和审计（待执行）

- [ ] 从经过审核的转换结果生成按决策拆分的 ShareGPT 数据，保留 system、任务、历史工具观察和当前动作；仅对可证明的当前目标计算 loss。训练、验证、测试隔离；分别产生全量和短测版本。全量源/转换结果不因上下文长度被删除。
- [ ] 使用真实 tokenizer 和 `qwen3_5_nothink` 测量 token 分布，分别报告 1024、2048、4096 等预算下有效/过长/截断比例；这些是测量候选，不承诺 24 GB 均可用。默认不截断；如因短测需要显式截断，生成独立版本，记录丢失轮次与字符并抽查任务完整性。不得以 1024 旧输出全部截断的结果推断可用。
- [ ] 复用已有 [LLaMA-Factory 命令说明](../../vault-swe-llamafactory.zh-CN.md) 中 `export`、`verify`、`audit` 的真实接口；确认新转换文件能满足旧输入 schema 后才执行。导出后用真实 loader 核对 token、监督 mask、角色、工具定义、实际截断和 train/validation 隔离，记录散列与逐条报告。测试集不输入训练或调参。
- [ ] 对截断关闭/开启、过长目标、未知动作、丢失观察、错误角色、重复轨迹、无监督目标和跨组冲突做测试；样本从每个源格式与状态分层抽查，记录数量及抽查结论。

**验收：**短测和全量数据各有独立 manifest、配置和报告；全量处理产出不等于全量全部可安全训练。

## 九、阶段 6：打包和传输到新服务器（待执行）

建议传输项目代码、固定版本上游代码、全量原始文件、划分和转换产物、短测/全量训练数据、清单、日志、依赖锁、模型及必要镜像。为节省重复传输可分成源数据包、处理结果包和代码/模型包，但每包必须有清单和哈希。确认允许迁移相关数据和仓库许可证。

以下为**路径示例**，先用真实服务器地址、用户名和目录替换，目标目录必须新建且可用空间足够；大型文件先检查磁盘空间、网络和断点恢复方案。示例不自动处理软链接指向的模型 blob；模型请复制实体或完整缓存并检查。

```bash
# 本机：在处理和校验完成后，生成包及哈希。
cd "$(dirname "$SWE_RUN")"
tar -cf /tmp/swe-full-transfer.tar "$(basename "$SWE_RUN")"
(cd /tmp && sha256sum swe-full-transfer.tar > swe-full-transfer.tar.sha256)
scp -4 /tmp/swe-full-transfer.tar /tmp/swe-full-transfer.tar.sha256 <用户>@<服务器>:<新建接收目录>/
```

```bash
# 新服务器：进入接收目录；确认该目录中 tar 包和目标解压目录名正确。
cd <新建接收目录>
sha256sum -c swe-full-transfer.tar.sha256
mkdir unpacked
tar -xf swe-full-transfer.tar -C unpacked
```

解包后以原清单重新 `sha256sum -c`，并重新运行划分、转换和导出结果校验；还需检查软链接、文件权限、项目未提交代码、固定模型 revision 及完整性。打包本身不改变源数据。若走新服务器从源重建，使用同一 revision、代码、seed 和参数，并逐项比对报告和哈希，系统差异导致字节不一致时对比语义并解释。

## 十、阶段 7：24 GB GPU 环境、短测和扩大训练（待执行）

- [ ] 新服务器记录 `nvidia-smi`、显卡型号、驱动、CUDA、CPU、内存、磁盘、Python、PyTorch、bitsandbytes、Transformers、PEFT 及 pip 依赖；新建独立环境。按 [现有说明](../../vault-swe-llamafactory.zh-CN.md) 安装 LLaMA-Factory 固定 commit 和约束，运行 `pip check` 与 CUDA 检查。24 GB 为预估，真实模型、长度和并发决定是否可运行。
- [ ] 核对模型实体和 revision；先对短测数据运行导出 `verify` 和真实 loader `audit`。选保留上下文较完整、显存有余量的长度；采用 QLoRA NF4、batch 1、梯度累积、梯度检查点作为候选，bf16 是否启用由 GPU 实测决定。
- [ ] 先用真实数据训练约 10 个优化器步骤，记录 loss 有限、可训练参数、权重更新、显存峰值、耗时与日志；检查实际输出目录并保存 Adapter。按已有文档的 `check-adapter` 重新加载基座和 Adapter、核对 logits 和差异。OOM 则记录并另开输出目录调整长度、累积或配置、重新审计；不得默认 24 GB 一定可运行 4096。
- [ ] 在独立验证集试运行评估和保存/恢复；再运行 50–100 步测吞吐。基于实际可训练决策数、有效 batch 和步速估算训练时长及空间；决定短测扩大或全量训练，不把短测完成当全量训练。用验证集调参，测试集保留至方案固定。
- [ ] 训练日志、配置、代码/模型/data revision、数据哈希、种子、硬件、随机性、峰值显存、检查点和重载报告一并保存；中断恢复要核验数据、配置与 checkpoint 的一致性。

已有具体命令及 `export` 参数见 [SWE LLaMA-Factory 使用说明](../../vault-swe-llamafactory.zh-CN.md)。该文件的旧路径和 1024 示例仅供接口参考；新数据的文件名、模型路径、训练配置在转换完成后按实测生成并核对，不能直接宣称已执行。

## 十一、阶段 8：真实 SWE-Agent 仓库评测（待执行）

- [ ] 固定 SWE-Agent commit、系统提示、工具包、模型服务、Docker 镜像 digest、环境依赖、资源限制和仓库初始 commit；将训练与推理模板的动作前缀逐 token 比对。旧模型服务存在空 `<think>` 前缀差异，修正或明确统一后重新建立基线。
- [ ] 构建版本化的小仓库任务：明确缺陷、基线测试失败、参考修复后通过、隐藏验证测试、独立容器和仓库快照。验证任务和最终测试任务不用于训练；检查仓库/任务重叠并报告检查局限。离线轨迹 test 和真实可重建任务不是同一概念，不把离线预测称为仓库修复成功。
- [ ] 同一套任务和参数分别运行基座与 Adapter：每次恢复干净快照，记录提示、响应、解析、工具调用、观察、最终 patch、测试退出状态、token、耗时与失败分类；先验证任务，锁定方案后才运行最终测试。报告任务数和成功数，不将单次预设调用或 loss 下降当成自主成功。
- [ ] 验证至少一个真实自主循环“模型动作 → 工具执行 → 观察 → 后续决策”；未完成则如实记录。保留两个模型各自完整日志并输出可复核对照表。

## 十二、执行顺序与验收交接

1. 本文件只定义计划；下一步单独进行阶段 0–5 的全量数据处理，优先本机完成；先核对许可证、空间和版本，再下载与开发，不覆盖旧成果。
2. 检查全量源行、去重、隔离、分组划分和短测均可由代码重建；来源、数量、哈希、失败原因和未验证语义完整可查。
3. 迁移或重建到新服务器并校验，随后执行真实 10 步训练、Adapter 重载、验证和受控扩大训练。
4. 准备相互独立的仓库任务，在统一 SWE-Agent 环境下完成基座/Adapter 对照；按实际证据填写结果，不将计划里的示例命令或历史结果写成新服务器成功记录。

## 十三、阶段 1–3 本机执行记录与复刻（2026-11-10）

### 13.1 官方数据和校验

官方数据仓库 `SWE-bench/SWE-smith-trajectories` 固定 revision `08e109b4a59eaeebf80e4675cd125d42e7ac99a4`。本机在 2026-11-10 通过 Hub API 保存了文件名、大小、Git blob ID 和 LFS SHA-256，实际下载了完整快照：34 个文件共 4,223,845,920 字节，包括 32 个 Parquet 和 `.gitattributes`、`README.md`。**注意**：数据卡中只列 `tool`、`xml`、`ticks`，仓库还存在 `train` 八个文件，本次全部保留。下载脚本按官方 LFS SHA-256 或 Git blob ID 检查每个文件；后续清点程序再次计算并核验，`raw.sha256` 保存全部文件的 SHA-256。

本机目录：

```text
/root/EverOS/.memo-vault/agent-training/sweagent/full-08e109b4/
├── raw/                         # 官方原始文件，不修改
│   ├── .gitattributes
│   ├── README.md
│   └── data/                    # ticks、tool、train、xml，各 8 个 parquet
├── metadata/source.json         # Hub 固定 revision 与官方清单
├── splits-v1/
│   ├── raw.sha256               # 从 raw/ 目录执行 sha256sum -c
│   ├── full_manifest.jsonl      # 每个源行对应文件、行号、ID、分组和划分
│   ├── smoke_manifest.jsonl     # 对应短测的所有格式源行
│   ├── excluded.jsonl           # 不可分组或 ID 元数据冲突的源行
│   ├── group_assignments.json   # 仓库组 -> train/validation/test
│   └── report.json              # 数量、划分、已知局限
└── datasets-v1/
    ├── full/{train,validation,test}/data/   # 全量划分后的 parquet
    ├── smoke/{train,validation,test}/data/  # 短测划分后的 parquet
    ├── counts.json
    └── datasets.sha256           # 各输出 parquet 的 SHA-256
```

本机中断时曾产生 `datasets-v1-incomplete/`，它不是有效交付物，不要用于训练；有效产物应具有 `datasets-v1/counts.json` 并按 13.4 核对。

如果需要在另一机器重新下载，先在独立 Python 环境安装 Hugging Face Hub（本机 API 查询使用 `huggingface_hub 0.26.2`），再通过固定 revision 下载完整快照：

```bash
export SWE_BASE=/root/EverOS/.memo-vault/agent-training/sweagent/full-08e109b4
export SWE_REV=08e109b4a59eaeebf80e4675cd125d42e7ac99a4
# 此命令要求 raw 目录尚不存在；现有目录不要覆盖。
test ! -e "$SWE_BASE/raw"
mkdir -p "$SWE_BASE/metadata"
python - <<'PY'
import datetime, json, os
from pathlib import Path
from huggingface_hub import HfApi, snapshot_download
base = Path(os.environ['SWE_BASE'])
rev = os.environ['SWE_REV']
repo = 'SWE-bench/SWE-smith-trajectories'
i = HfApi().dataset_info(repo, revision=rev, files_metadata=True, timeout=30)
assert i.sha == rev
files = [{'path': s.rfilename, 'size': s.size, 'git_blob': s.blob_id,
          'lfs_sha256': getattr(s.lfs, 'sha256', None)} for s in i.siblings]
(base / 'metadata/source.json').write_text(json.dumps({
    'repo_id': i.id, 'revision': i.sha,
    'retrieved_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'files': files}, indent=2) + '\n')
snapshot_download(repo_id=repo, repo_type='dataset', revision=rev,
                  local_dir=str(base / 'raw'), max_workers=4)
PY
```

本机 `snapshot_download` 因 HTTP 元数据问题失败，实际使用 `curl -4 -L` 下载固定 revision 的各文件、断点续传并逐文件按官方哈希核验。若目标机遇到相同问题，可从本机直接迁移 `raw/` 和 `metadata/source.json`，然后执行下文校验；不能把失败的 snapshot 命令当作下载成功。

### 13.2 清点、分组及短测抽样

清点以 `pyarrow` 读取源文件中的 `instance_id`、`traj_id`、`resolved`、`model`，其余完整字段保留在原始 Parquet 和划分数据中。每条记录按源路径和从 0 开始的行号定位；每个文件输出行数与 Parquet 元数据核对。不同表达通过同 `traj_id` 和元数据关联，**不表示内容语义已核验等价**。

| 上游文件组 | 行数 |
|---|---:|
| ticks | 25,826 |
| tool | 24,100 |
| train | 26,076 |
| xml | 26,076 |
| 合计 | 102,078 |

按 `instance_id` 中的 `owner__repository.<8–40位十六进制版本>.…` 提取仓库组，支持仓库名含点号；无法唯一解析的记录隔离。所有同仓库、同任务及同轨迹的不同格式进入同一划分。seed 为 `20261110`，`sha256(seed + NUL + 仓库组)` 的整数对 10000 取模：0–8999 训练，9000–9499 验证，9500–9999 测试。由于按仓库分组，实际比例不保证恰好 90/5/5。

| 划分 | 仓库组 | 不同轨迹 ID | 源行数 | 短测不同轨迹 ID |
|---|---:|---:|---:|---:|
| train | 114 | 46,510 | 94,922 | 200 |
| validation | 6 | 1,401 | 2,969 | 20 |
| test | 9 | 1,986 | 4,187 | 20 |
| 合计 | 129 | 49,897 | 102,078 | 240 |

本次清点无因身份不明或同轨迹元数据冲突而隔离的行；检查仓库组和 `instance_id` 跨集合均为 0。短测在各自集合里按稳定哈希轮流抽取不同仓库，每个轨迹 ID 的全部源格式都保留。**200/20/20 是轨迹 ID 数，不是 Parquet 行数或训练决策数。短测实际分别为 423、45、41 行；所有输出 Parquet 行数已与逐行 manifest 对账。**

同一格式中存在额外 4,510 条重复 `traj_id` 记录；并未比较重复行内容是否完全相同，已全部保留，不可直接把此划分当成去重后的训练样本。仓库组是从 ID 推断，尚未与官方任务元数据独立核验；语义近重复、跨数据源泄漏、不同格式语义等价和工具语义尚未核验。原始 `train` 只是上游文件组名，和本次 `full/train` 划分不是同一概念。

### 13.3 关键代码与复刻命令

可执行代码随项目一并迁移：

- `src/everos/agent_training/swe_full_split.py`：固定版本和文件清单校验、流式标识清点、仓库分组、全量划分、短测、报告与 manifest。
- `src/everos/agent_training/swe_materialize_split.py`：依据 manifest 逐批读取原始 Parquet，按划分输出全量和短测 Parquet，原始文件不修改。
- `tests/unit/test_agent_training/test_swe_full_split.py`：仓库名、确定性抽样及写出内容测试。

核心划分和抽样：

```python
# 原实现位于 swe_full_split.py；固定 seed，仓库组不跨划分。
def split_for(group):
    n = int(rank(group), 16) % 10000
    return 'train' if n < 9000 else 'validation' if n < 9500 else 'test'
# rank(value) = SHA256('20261110' + NUL + value) 的十六进制摘要。
# 短测：每个划分按仓库组轮流抽取，组内按相同稳定哈希排序。
```

本机 Python 3.12.3、PyArrow 16.1.0；复刻前迁移项目源码（本机文件可能尚未提交）及 `raw/`、`metadata/source.json`。输出目录必须不存在。下面以 `v2` 为例，不覆盖 `v1`：

```bash
export SWE_PROJECT=/root/EverOS
export SWE_BASE="$SWE_PROJECT/.memo-vault/agent-training/sweagent/full-08e109b4"
cd "$SWE_PROJECT"
export PYTHONPATH="$SWE_PROJECT/src"
python3 -m everos.agent_training.swe_full_split \
  --base "$SWE_BASE" --output "$SWE_BASE/splits-v2"
python3 -m everos.agent_training.swe_materialize_split \
  --base "$SWE_BASE" --index "$SWE_BASE/splits-v2" \
  --output "$SWE_BASE/datasets-v2"
```

### 13.4 检查和使用

```bash
export SWE_BASE=/root/EverOS/.memo-vault/agent-training/sweagent/full-08e109b4
cd "$SWE_BASE/raw"
sha256sum -c "$SWE_BASE/splits-v1/raw.sha256"
cat "$SWE_BASE/splits-v1/report.json"
cat "$SWE_BASE/datasets-v1/counts.json"
cd "$SWE_BASE/datasets-v1"
sha256sum -c datasets.sha256
cd /root/EverOS
PYTHONPATH=src python3 -m pytest -c /dev/null --noconftest \
  tests/unit/test_agent_training/test_swe_full_split.py -q
```

`--noconftest` 是因为本机全局 Python 未安装项目夹具所需的 `pydantic_settings`；仅运行上述独立数据处理测试，不等于项目全量测试通过。完整和短测 Parquet 保留四组上游格式、原始消息、patch 和元数据；它们尚未转换为 LLaMA-Factory 可训练样本。

## 十四、新服务器 SWE-Agent 和容器最小部署（按用户要求）

此节代替第七节中大规模工具语义测试要求：只准备固定源码和一个容器，检查命令能否启动。以下是**新服务器待执行命令**，本次未运行；既不需要模型密钥，也不声称验证了模型闭环或训练数据动作与运行工具的语义等价。

### 14.1 检查系统、Python 和 Git

下面的 Docker 安装命令针对 **64 位 Ubuntu 22.04、24.04 或其他 Docker 官方文档当前支持的 Ubuntu 版本**，使用 `apt` 和 `systemd`。Debian 请按 [Docker 官方 Debian 安装说明](https://docs.docker.com/engine/install/debian/)操作；旧版 Ubuntu（例如 18.04）不要直接套用，应先检查 [官方支持的系统版本](https://docs.docker.com/engine/install/ubuntu/#os-requirements)。命令需要 root 权限；若已登录 root 且没有 `sudo`，去掉命令中的 `sudo`。

```bash
cat /etc/os-release
uname -m
command -v git && git --version
command -v python3 && python3 --version
command -v docker && docker --version
```

若 Git、curl 等缺失，在受支持的 Ubuntu 上安装：

```bash
sudo apt update
sudo apt install -y git curl ca-certificates
```

SWE-Agent 需要 **Python 3.11 或更高版本及 venv 支持**。例如 Ubuntu 24.04 可以使用系统 Python 3.12；Ubuntu 22.04 默认 `python3` 通常是 3.10，不能仅因 `python3` 存在就继续。下面先选择服务器已有的 Python 3.11+ 可执行文件；如只有 3.10，先由服务器管理员提供 3.11+（例如已有的 Conda 环境），不要覆盖系统 Python。

```bash
# 根据服务器实际路径修改；也可以是 /path/to/conda/env/bin/python。
export SWE_PYTHON=python3
"$SWE_PYTHON" -c 'import sys; print(sys.executable, sys.version); assert sys.version_info >= (3, 11), "Python 需要 3.11 或更高版本"'
```

如果使用 Ubuntu 系统 Python 且 `venv` 不可用，可安装对应的 venv 包，例如系统默认版本对应 `sudo apt install -y python3-venv`；若选择的是另一个 Python 版本，须安装与该版本匹配的 venv 包或使用该环境自带的 venv。

### 14.2 安装 Docker Engine（新服务器待执行）

如果 `docker info` 已成功，直接跳至 14.3，避免覆盖现有 Docker 配置。以下依据 [Docker 官方 Ubuntu 安装说明](https://docs.docker.com/engine/install/ubuntu/)添加官方 APT 源并安装 Engine；会更改服务器系统软件，先确认当前用户有相应权限及服务器网络可访问 `download.docker.com`。先检查是否已有冲突包；如果有，先查明其用途并按官方说明处理，**不要直接删除现有镜像、容器或数据卷**。

```bash
# 查看是否存在可能冲突的发行版软件包。
dpkg -l docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc 2>/dev/null || true

sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# 适用于官方支持的 Ubuntu；将系统代号和架构写入 APT 源。
sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo systemctl is-active docker
sudo docker version
sudo docker info
```

`systemctl is-active` 应输出 `active`；`docker version` 应同时显示 Client 和 Server。若当前用户没有访问 Docker 的权限，下文 Docker 命令使用 `sudo`，或者由管理员按服务器的账号管理方式配置权限。此安装选择仓库当前可用版本；为了以后复刻，应记录实际版本：

```bash
mkdir -p /root/vault-swe-run
sudo docker version > /root/vault-swe-run/docker-version.txt
```

如果新服务器不是 root 用户，请将 `/root/vault-swe-run` 改为当前用户有写权限的绝对路径，并在本节后续命令中一致替换。Docker 软件安装成功不代表 GPU 或 SWE-Agent 工具链已验证。

### 14.3 安装固定版本 SWE-Agent

参考项目内先前记录的 SWE-Agent 1.1.0 commit `3ea751c087f32b16e039a2233dd6eefecef325d5`。`$SWE_RUN/sweagent-venv` 和 `$SWE_RUN/SWE-agent` 须不存在，避免覆盖旧环境。使用上面检查过的 `$SWE_PYTHON` 新建隔离环境：

```bash
export SWE_RUN=/root/vault-swe-run
mkdir -p "$SWE_RUN"
"$SWE_PYTHON" -m venv "$SWE_RUN/sweagent-venv"
source "$SWE_RUN/sweagent-venv/bin/activate"
git clone https://github.com/SWE-agent/SWE-agent.git "$SWE_RUN/SWE-agent"
git -C "$SWE_RUN/SWE-agent" checkout --detach 3ea751c087f32b16e039a2233dd6eefecef325d5
git -C "$SWE_RUN/SWE-agent" rev-parse HEAD
python -m pip install -e "$SWE_RUN/SWE-agent"
python -m pip check
python -m pip freeze > "$SWE_RUN/sweagent-environment-freeze.txt"
sweagent --help
```

### 14.4 下载镜像并做最小容器测试

SWE-Agent 的 `config/human/human_demo.yaml` 使用 `python:3.11` 容器；下面下载该示例镜像并保存实际 digest。tag 会变化，所以**首次拉取后的 digest 要保存并在后续复刻时使用该 digest**，不能把本机旧的自建镜像 ID 视为可从 Docker Hub 拉取的镜像。

```bash
docker info
docker pull python:3.11
docker image inspect python:3.11 --format '{{json .RepoDigests}}' | tee "$SWE_RUN/python311-image-digests.json"
docker run --rm --network none --memory 1g --cpus 1 \
  --entrypoint python python:3.11 -c 'import sys; print("container OK", sys.version)'
```

出现 `container OK` 表示镜像已成功运行 Python；`sweagent --help` 表示框架命令可以启动。这两个检查不证明 SWE-Agent 已连接容器，也不证明模型能调用工具。正式实验前仍需用所选实际工具配置做一次最小连接和模型动作验证，不能把此处的 Docker 单独启动冒充自主任务成功。

## 十五、阶段 6：本机打包与新服务器传输命令

以下命令在**阶段 5 实际产物确认后**手动执行；本节只给出命令，不会自动打包、传输或连接服务器。用户、地址及目标目录均须按实际替换。先在两端用 `df -h` 检查空间，且目标目录须为新目录。原始快照、划分和脚本需一起迁移；若新增阶段 5 产物，须将实际目录加入下方清单。不要打包 `datasets-v1-incomplete`。项目中有未提交文件，不能假定 Git 克隆包含它们。

```bash
# 本机：替换为真实服务器用户名、IP 和服务器上可写的新目录。
export SWE_PROJECT=/root/EverOS
export SWE_REL=.memo-vault/agent-training/sweagent/full-08e109b4
export SWE_ARCHIVE_DIR=/root/swe-transfer-20261110
export SWE_REMOTE_USER=YOUR_SSH_USER
export SWE_REMOTE_HOST=YOUR_SERVER_IP
export SWE_REMOTE_DIR=/root/swe-transfer-20261111
export SWE_SSH_PORT=22

# 防止覆盖之前的打包结果。
if [ -e "$SWE_ARCHIVE_DIR" ]; then echo "目录已存在，请更换 SWE_ARCHIVE_DIR"; exit 1; fi
mkdir -p "$SWE_ARCHIVE_DIR"
cd "$SWE_PROJECT"
tar -cf "$SWE_ARCHIVE_DIR/swe-data.tar" \
  "$SWE_REL/raw" "$SWE_REL/metadata" \
  "$SWE_REL/splits-v1" "$SWE_REL/datasets-v1" \
  "$SWE_REL/lf-candidate-smoke-v1" "$SWE_REL/lf-candidate-full-v1" \
  "$SWE_REL/lf-smoke-provisional-4096-v1"
tar -cf "$SWE_ARCHIVE_DIR/swe-code.tar" \
  src/everos/__init__.py \
  src/everos/agent_training/swe_full_split.py \
  src/everos/agent_training/swe_materialize_split.py \
  src/everos/agent_training/swe_source_candidate.py \
  src/everos/agent_training/swe_train_run.py \
  src/everos/agent_training/swe_eval_run.py \
  src/everos/agent_training/openhands_inventory.py \
  src/everos/agent_training/openhands_qlora.py \
  src/everos/agent_training/swe_lora_pipeline.py \
  src/everos/agent_training/swe_llamafactory.py \
  tests/unit/test_agent_training/test_swe_full_split.py \
  tests/unit/test_agent_training/test_swe_source_candidate.py \
  tests/unit/test_agent_training/test_swe_run_scripts.py \
  scripts/train_swe_qlora_smoke.sh scripts/train_swe_qlora_scale.sh scripts/train_swe_qlora_full.sh \
  scripts/create_swe_eval_toy.sh scripts/serve_openhands_qwen35.sh \
  configs/swe/eval-qwen35.yaml \
  scripts/requirements-swe-llamafactory-constraints.txt \
  docs/superpowers/plans/2026-11-10-vault-sweagent-full-qwen35.zh-CN.md \
  docs/superpowers/plans/2026-11-09-vault-openhands-qwen35.zh-CN.md \
  docs/vault-swe-llamafactory.zh-CN.md \
  .memo-vault/agent-training/agentlab/qwen35-tokenizer-2fc06364
cd "$SWE_ARCHIVE_DIR"
sha256sum swe-data.tar swe-code.tar > SHA256SUMS
sha256sum -c SHA256SUMS

# 先确认远端目录是新的；SSH/SCP 是实际网络操作。
ssh -4 -p "$SWE_SSH_PORT" "$SWE_REMOTE_USER@$SWE_REMOTE_HOST" "mkdir '$SWE_REMOTE_DIR'"
scp -4 -P "$SWE_SSH_PORT" swe-data.tar swe-code.tar SHA256SUMS \
  "$SWE_REMOTE_USER@$SWE_REMOTE_HOST:$SWE_REMOTE_DIR/"
```

目标地址和路径必须由操作者填写，建议不含空格或 shell 特殊字符。模型权重、LLaMA-Factory 和 SWE-Agent 源码不在上述包内，需要按固定 revision 分别下载或单独迁移；tokenizer 不等于完整模型。上述代码包不自动包含以后新增的文件，应在最终传输前更新清单。大文件网络不稳定时可改用支持续传的 `rsync`，但迁移结束仍需校验。

```bash
# 新服务器：进入实际接收目录，确认包完好后解压到新的工作目录。
cd /root/swe-transfer-20261111  # 按实际服务器目录修改
sha256sum -c SHA256SUMS || { echo "校验失败，停止解包"; exit 1; }
if [ -e unpacked ]; then echo "unpacked 已存在，请更换目录"; exit 1; fi
mkdir unpacked
tar -xf swe-code.tar -C unpacked
tar -xf swe-data.tar -C unpacked
export SWE_RECEIVED="$PWD/unpacked/.memo-vault/agent-training/sweagent/full-08e109b4"
cd "$SWE_RECEIVED/raw"
sha256sum -c "$SWE_RECEIVED/splits-v1/raw.sha256"
cd "$SWE_RECEIVED/datasets-v1"
sha256sum -c datasets.sha256
```

此处只核验源文件和已划分 Parquet。阶段 5 的 LLaMA-Factory 配置包含本机绝对路径；迁移后不能直接沿用，必须重新生成配置和对应哈希、再运行导出器 `verify`/`audit`，并将 `model_name_or_path` 指向新服务器固定 revision 的完整模型。

## 十六、阶段 5 本机进展和未完成项（2026-11-10）

**状态：阶段 5 尚未完成。** 已完成全量训练/验证轨迹的候选转换，以及短测候选的真实 LLaMA-Factory 模板/标签核验；尚未完成全量决策编码和加载器核验。以下全部是**待审核候选，不是已验收的可训练数据**。源记录没有可直接确认的原始 loss 掩码；当前候选标记 `loss_provenance: unknown`，模型动作语义与目标框架也未完成逐项确认。不能因为可以编码或加载就假定源监督标记、工具语义正确。原始全量与测试集保持不变。

### 16.1 源格式盘点与候选转换

对阶段 1–3 的训练/验证轨迹按 ID 盘点：上游 `ticks` 的 25,826 个 ID 均不在 `train` 文件组中；因此不能把 `train` 文件组视为全部轨迹。当前转换只选择可用的单条 `xml`、`train` 或 `tool` 表达，并要求消息为 system 开头、后续 user/assistant 交替的纯文本且至少含一个支持的 XML 动作；`ticks`、选定格式重复或消息结构不符的轨迹隔离。按这一规则，生成结果如下（数字是不同轨迹 ID）：

| 范围 | 集合 | 候选 | 消息格式不符 | 无可用唯一支持格式 |
|---|---|---:|---:|---:|
| 全量 | train | 21,262 | 8,563 | 16,685 |
| 全量 | validation | 642 | 217 | 542 |
| 短测 | train | 87 | 43 | 70 |
| 短测 | validation | 12 | 3 | 5 |

输出目录：

```text
/root/EverOS/.memo-vault/agent-training/sweagent/full-08e109b4/
├── lf-candidate-full-v1/       # 全量训练/验证候选；train.jsonl 约 2.2 GiB
├── lf-candidate-smoke-v1/      # 短测训练/验证候选
└── lf-smoke-provisional-4096-v1/  # 短测模板编码与加载器核验
```

每个候选目录包含 `train.jsonl`、`validation.jsonl`、`excluded.jsonl`、`report.json` 和 `SHA256SUMS`；原始测试划分没有进入候选训练或验证文件。源代码为 `src/everos/agent_training/swe_source_candidate.py`，测试为 `tests/unit/test_agent_training/test_swe_source_candidate.py`。候选优先级 `xml → train → tool` 只是当前解析选择，不证明各格式语义等价。当前排除记录的相同 ID 可通过原始 `full_manifest.jsonl` 找到完整源位置。

复刻候选转换时输出目录必须是新的：

```bash
cd /root/EverOS
export PYTHONPATH="$PWD/src"
export SWE_BASE="$PWD/.memo-vault/agent-training/sweagent/full-08e109b4"
python3 -m everos.agent_training.swe_source_candidate \
  --base "$SWE_BASE" --scope smoke --output "$SWE_BASE/lf-candidate-smoke-v2"
python3 -m everos.agent_training.swe_source_candidate \
  --base "$SWE_BASE" --scope full --output "$SWE_BASE/lf-candidate-full-v2"
PYTHONPATH=src python3 -m pytest -c /dev/null --noconftest \
  tests/unit/test_agent_training/test_swe_source_candidate.py \
  tests/unit/test_agent_training/test_swe_full_split.py -q
```

本机上述两组测试共 7 项通过。`--noconftest` 只跳过项目其他测试夹具，不代表项目完整测试套件通过。

### 16.2 短测 Qwen 模板与 LLaMA-Factory 真实加载器检查

数据处理使用本机已有的 LLaMA-Factory 0.9.5 和 Qwen3.5 tokenizer/processor（revision `2fc06364715b967f1860aea9cf38778875588b17`），模板为 `qwen3_5_nothink`。该目录只有 tokenizer / processor 配置，没有完整模型权重。数据处理环境继承了系统包，`pip check` 存在依赖缺失和冲突，仅适用于本次编码核验，**不是已验收的训练环境**。

以 4,096 token、不截断为例：

| 集合 | 轨迹候选 | 动作决策 | 保留 | 超长排除 | 截断 |
|---|---:|---:|---:|---:|---:|
| train | 87 | 2,723 | 222 | 2,501 | 0 |
| validation | 12 | 334 | 26 | 308 | 0 |

在这些短测候选中，按完整 token 长度统计：1,024 token 下 train/validation 均为 0；2,048 token 下分别为 132/18；4,096 token 下为 222/26。这个统计不代表全量数据的长度分布。通过真实 LLaMA-Factory 加载器逐条比对，222 条训练决策和 26 条验证决策的 token 与标签哈希一致；这是模板和加载一致性检查，不验证源 loss 掩码的来源。

本机复刻命令（新目录，不运行训练）：

```bash
cd /root/EverOS
export SWE_BASE="$PWD/.memo-vault/agent-training/sweagent/full-08e109b4"
export SWE_TOKENIZER="$PWD/.memo-vault/agent-training/agentlab/qwen35-tokenizer-2fc06364"
export SWE_LF_PY="$PWD/.memo-vault/agent-training/agentlab/lf-data-adapt-py312-v1/bin/python"
export PYTHONPATH="$PWD/src"
# 本机 CUDA 动态库加载需要；新服务器按自己的环境设置，不应盲目照搬。
export LD_LIBRARY_PATH=/root/anaconda3/lib/python3.12/site-packages/nvidia/nvjitlink/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
"$SWE_LF_PY" -m everos.agent_training.swe_llamafactory export \
  --train-input "$SWE_BASE/lf-candidate-smoke-v1/train.jsonl" \
  --validation-input "$SWE_BASE/lf-candidate-smoke-v1/validation.jsonl" \
  --model "$SWE_TOKENIZER" --revision 2fc06364715b967f1860aea9cf38778875588b17 \
  --local-only --max-tokens 4096 --max-steps 10 \
  --output-dir "$SWE_BASE/lf-smoke-provisional-4096-v2" \
  --train-output-dir "$SWE_BASE/NOT-A-TRAINING-RUN-v2"
"$SWE_LF_PY" -m everos.agent_training.swe_llamafactory verify \
  --prepared-dir "$SWE_BASE/lf-smoke-provisional-4096-v2"
"$SWE_LF_PY" -m everos.agent_training.swe_llamafactory audit \
  --prepared-dir "$SWE_BASE/lf-smoke-provisional-4096-v2"
```

### 16.3 仍需完成

1. 核查 `ticks`、工具调用结构和非交替消息，建立不会改变工具语义的转换；核验同 ID 重复内容，避免把多格式或冲突行静默当独立轨迹。
2. 核实上游监督标记来源并决定可监督目标；未知项继续隔离，不能把当前候选直接训练。对工具接口的验证范围与用户要求的最小部署测试分开记录。
3. 实现适合全量的流式、可断点的逐决策编码与审计；现有 `swe_llamafactory export` 会把输入完整加载入内存，而且训练候选 JSONL 约 2.2 GiB，不能直接假设适合全量运行。完成后报告全量 1,024/2,048/4,096 的长度分布及排除，运行实际 loader 核验。
4. 在新服务器安装独立、依赖一致的训练环境，准备完整模型；迁移后重新生成带新路径的配置及校验哈希。当前本机仅完成数据处理检查，未训练 Adapter，也未验证 24 GB 显存可运行任何特定长度。

## 十七、24 GB 服务器训练脚本与命令（待服务器执行）

**状态：脚本已编写，本机未运行真实 SWE 训练；阶段 5 的全量编码和数据验收仍未完成。** 当前 `lf-smoke-provisional-4096-v1` 仅能用 `--technical-probe` 运行 10 步技术短测；其结果不能作为正式训练效果。扩大训练要求数据经过审核，准备报告中 `training_ready` 为 `true`。这一标记必须在完成监督来源、去重、工具语义和加载器核验后由数据处理流程产生，不能只为绕过检查手工添加。

| 文件 | 功能 |
|---|---|
| `scripts/train_swe_qlora_smoke.sh` | 10 个优化器步骤，允许显式使用未验收数据进行技术短测。 |
| `scripts/train_swe_qlora_scale.sh` | 50–100 步的扩大短测，要求已验收数据，定期计算验证 loss。 |
| `scripts/train_swe_qlora_full.sh` | 按 epoch 扩大训练，要求已验收数据。 |
| `src/everos/agent_training/swe_train_run.py` | 检查数据哈希与基座 revision、生成独立训练 YAML、保存日志、状态及 Adapter。 |

### 17.1 环境和数据

先按第十四节准备 CUDA 驱动、Docker 和 SWE-Agent；独立准备 [LLaMA-Factory v0.9.5 训练环境](../../vault-swe-llamafactory.zh-CN.md)，验证 `pip check`、`torch.cuda.is_available()`。按第十五节迁移数据、代码和固定 revision 的**完整** `Qwen/Qwen3.5-0.8B` 模型；tokenizer 目录不含模型权重。实际安装路径可不同，下例需要替换。

```bash
export SWE_PROJECT=/root/EverOS
export SWE_RUN=/root/vault-swe-run
export SWE_MODEL="$SWE_RUN/model"
export SWE_PYTHON="$SWE_RUN/llamafactory-venv/bin/python"
export SWE_CANDIDATES="$SWE_PROJECT/.memo-vault/agent-training/sweagent/full-08e109b4/lf-candidate-smoke-v1"
export SWE_PREPARED="$SWE_RUN/swe-smoke-prepared-4096"
export PYTHONPATH="$SWE_PROJECT/src"
cd "$SWE_PROJECT"
nvidia-smi
"$SWE_PYTHON" -c 'import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available()); assert torch.cuda.is_available()'
"$SWE_PYTHON" -m pip check
```

迁移后须使用服务器上的完整模型及新输出目录重导出，避免旧配置中的本机绝对路径；下方不启动训练，不开启截断。实际保留数量和显存是否足够以服务器检查为准：

```bash
"$SWE_PYTHON" -m everos.agent_training.swe_llamafactory export \
  --train-input "$SWE_CANDIDATES/train.jsonl" \
  --validation-input "$SWE_CANDIDATES/validation.jsonl" \
  --model "$SWE_MODEL" --revision 2fc06364715b967f1860aea9cf38778875588b17 \
  --local-only --max-tokens 4096 --max-steps 10 \
  --output-dir "$SWE_PREPARED" --train-output-dir "$SWE_RUN/export-placeholder"
"$SWE_PYTHON" -m everos.agent_training.swe_llamafactory verify --prepared-dir "$SWE_PREPARED"
"$SWE_PYTHON" -m everos.agent_training.swe_llamafactory audit --prepared-dir "$SWE_PREPARED"
```

上述 `export-placeholder` 只是导出配置里的未创建路径。`audit` 必须在正式短测前通过；对全量数据不能直接把当前 2.2 GiB JSONL 交给会整体加载输入的导出器，需先完成第 16.3 节的流式编码和审核。

### 17.2 10 步技术短测

```bash
cd "$SWE_PROJECT"
SWE_PYTHON="$SWE_PYTHON" scripts/train_swe_qlora_smoke.sh \
  "$SWE_PREPARED" "$SWE_MODEL" "$SWE_RUN/swe-smoke-10" \
  --technical-probe --gradient-accumulation 4
```

脚本固定 4-bit NF4、Qwen3.5、LoRA 和与数据相同的 cutoff；输出独立目录下的 `training.yaml`、`training.log`、`run-report.json` 和 `adapter/`，检查训练退出码、步数、有限 loss、Adapter 文件及权重哈希。`technical_probe` 状态不表示数据已验收或 Adapter 已被证明有效。需根据日志和 `nvidia-smi` 记录显存峰值；脚本不自动测量 GPU 峰值。

训练完成后，使用第十六节已存在的 `swe_llamafactory check-adapter` 执行基座加 Adapter 重载；要将 `--prepared-dir` 指向服务器上通过 `verify` 的 `$SWE_PREPARED`，并注意原始 `export-placeholder` 仍须不存在：

```bash
"$SWE_PYTHON" -m everos.agent_training.swe_llamafactory check-adapter \
  --prepared-dir "$SWE_PREPARED" \
  --adapter-dir "$SWE_RUN/swe-smoke-10/adapter" \
  --report-path "$SWE_RUN/swe-smoke-10-reload.json" --local-only
```

### 17.3 50–100 步扩大短测

只有阶段 5 完成审核并产出 `training_ready: true` 的训练与独立验证数据后，设置 `SWE_REVIEWED_DATA` 为该目录；它不能是当前未审核候选。先对该目录执行 `verify`、`audit`，再运行：

```bash
export SWE_REVIEWED_DATA=<已验收并在服务器重导出的数据目录>
"$SWE_PYTHON" -m everos.agent_training.swe_llamafactory verify --prepared-dir "$SWE_REVIEWED_DATA"
"$SWE_PYTHON" -m everos.agent_training.swe_llamafactory audit --prepared-dir "$SWE_REVIEWED_DATA"
SWE_PYTHON="$SWE_PYTHON" scripts/train_swe_qlora_scale.sh \
  "$SWE_REVIEWED_DATA" "$SWE_MODEL" "$SWE_RUN/swe-scale-75" \
  --steps 75 --gradient-accumulation 4
```

根据 `training.log` 和实际步速估算全量训练时间，再决定最终 epoch 与资源。50–100 步只是吞吐探针。完成阶段 5 的全量编码及验收，并使用全量数据新生成 `$SWE_FULL_REVIEWED` 后，可以运行下面按 epoch 的训练；不要把当前 2.2 GiB 候选当成已验收数据：

```bash
export SWE_FULL_REVIEWED=<已验收的全量训练数据目录>
"$SWE_PYTHON" -m everos.agent_training.swe_llamafactory verify --prepared-dir "$SWE_FULL_REVIEWED"
"$SWE_PYTHON" -m everos.agent_training.swe_llamafactory audit --prepared-dir "$SWE_FULL_REVIEWED"
SWE_PYTHON="$SWE_PYTHON" scripts/train_swe_qlora_full.sh \
  "$SWE_FULL_REVIEWED" "$SWE_MODEL" "$SWE_RUN/swe-full-1epoch" \
  --epochs 1.0 --gradient-accumulation 4
```

OOM 时保留日志，用新目录重新生成合适长度并重新审计；不承诺 24 GB 一定能运行 4,096 token。

## 十八、SWE-Agent 基座 / Adapter 两次真实评测脚本（仅准备，未执行）

| 文件 | 功能 |
|---|---|
| `src/everos/agent_training/swe_eval_run.py` | `run` 执行单臂任务、隔离测试和结果记录；`compare` 检查两臂条件并汇总。 |
| `configs/swe/eval-qwen35.yaml` | 从本机旧实验 XML 配置整理出的模板；服务器路径、模型服务和工具安装仍需验证。 |
| `scripts/create_swe_eval_toy.sh` | 创建含已知缺陷、可重复运行的玩具 Git 仓库及任务描述。 |

两次运行分别加载同一基座和基座 + Adapter，使用**同一**任务、配置、提交、镜像、测试命令、资源限制与生成参数。脚本为每臂建立两份独立的干净 clone：先在一份 clone 的容器中确认原始测试退出码为 1，再让 SWE-Agent 在另一份 clone 修复、应用 patch，并在同一镜像测试；只有基线退出 1、Agent 退出 0、有已应用的 tracked patch、保护的测试文件未被修改且修复后测试退出 0 才记为成功。容器测试禁用网络。完整轨迹和日志保留；玩具任务测试文件可见，不能代表隐藏测试或 SWE-bench 成绩。

**前置：**第十四节的 SWE-Agent、Docker、固定完整模型、经重载检查的 Adapter 和独立模型服务环境。配置文件中的工具目录相对于 SWE-Agent 仓库；固定模型服务使用 `127.0.0.1:18080`。第十六节提示的模板前缀差异和真实模型/框架连接尚未验证，先做最小连接检查；不要将脚本存在当作端到端已通过。

```bash
export SWE_PROJECT=/root/EverOS
export SWE_RUN=/root/vault-swe-run
export SWE_AGENT_REPO="$SWE_RUN/SWE-agent"
export SWE_AGENT_BIN="$SWE_RUN/sweagent-venv/bin/sweagent"
export SWE_CONFIG="$SWE_PROJECT/configs/swe/eval-qwen35.yaml"
export SWE_MODEL="$SWE_RUN/model"
export SWE_ADAPTER="$SWE_RUN/swe-smoke-10/adapter"
export SWE_TASK="$SWE_RUN/toy-add-task"
export PYTHONPATH="$SWE_PROJECT/src"
cd "$SWE_PROJECT"
scripts/create_swe_eval_toy.sh "$SWE_TASK"
export SWE_COMMIT=$(cat "$SWE_TASK/commit.txt")
# 将第 14.4 节拉取到的 python:3.11 的实际 RepoDigest 填入，必须包含 @sha256:。
export SWE_IMAGE='<python:3.11 的完整 RepoDigest>'
export SWE_TEST='python -m unittest discover -s /workspace -p test_calc.py'
```

每个工作终端均需设置上述环境变量，模型服务需要固定 LLaMA-Factory v0.9.5 GPU 环境。先在独立终端启动**基座服务**，确认 `curl -fsS http://127.0.0.1:18080/v1/models` 可访问；该脚本使用相同 `qwen3_5_nothink` 模板：

```bash
cd "$SWE_PROJECT"
OH_PYTHON="$SWE_RUN/llamafactory-venv/bin/python" \
  scripts/serve_openhands_qwen35.sh "$SWE_MODEL" base 18080
```

在另一终端运行基座评测：

```bash
cd "$SWE_PROJECT"
"$SWE_AGENT_BIN" --help
python3 -m everos.agent_training.swe_eval_run run --label base \
  --model-identity 'Qwen3.5-0.8B base revision 2fc06364715b967f1860aea9cf38778875588b17' \
  --repo "$SWE_TASK/repo" --commit "$SWE_COMMIT" --problem "$SWE_TASK/problem.md" \
  --config "$SWE_CONFIG" --sweagent "$SWE_AGENT_BIN" --sweagent-cwd "$SWE_AGENT_REPO" \
  --image "$SWE_IMAGE" --test-command "$SWE_TEST" --protected-path test_calc.py --output "$SWE_RUN/eval-toy-base"
```

停止基座服务，使用同一端口、基座和配置重启为 **Adapter 服务**：

```bash
cd "$SWE_PROJECT"
OH_PYTHON="$SWE_RUN/llamafactory-venv/bin/python" \
  scripts/serve_openhands_qwen35.sh "$SWE_MODEL" adapter 18080 "$SWE_ADAPTER"
```

确认服务可访问且启动日志显示已加载指定 Adapter 后，运行第二次：

```bash
cd "$SWE_PROJECT"
python3 -m everos.agent_training.swe_eval_run run --label adapter \
  --model-identity "Qwen3.5-0.8B + adapter $SWE_ADAPTER" \
  --repo "$SWE_TASK/repo" --commit "$SWE_COMMIT" --problem "$SWE_TASK/problem.md" \
  --config "$SWE_CONFIG" --sweagent "$SWE_AGENT_BIN" --sweagent-cwd "$SWE_AGENT_REPO" \
  --image "$SWE_IMAGE" --test-command "$SWE_TEST" --protected-path test_calc.py --output "$SWE_RUN/eval-toy-adapter"
python3 -m everos.agent_training.swe_eval_run compare \
  --base "$SWE_RUN/eval-toy-base" --adapter "$SWE_RUN/eval-toy-adapter"
```

各结果目录含 `result.json`、`sweagent.log`、`baseline-test.log`、`post-test.log`（实际运行后才产生）、`applied.patch` 和 SWE-Agent 轨迹；`compare` 验证任务提交、问题与配置哈希、镜像、测试命令、资源与超时一致。脚本不能自动证明远端服务实际上切换了模型：必须保存两次模型服务启动日志及 Adapter 哈希。运行出错时查看日志并记录失败，不能将缺失测试或框架启动错误计作修复成功。

## 十九、2026-11-11 新 24 GB 服务器操作速查

**当前可执行范围：**训练和评测脚本已编写，但本项目的真实训练、Adapter 重载和 SWE-Agent 自主评测尚未在新服务器验证。现在已有的是经过 LLaMA-Factory 加载器核对的**短测候选**；全量数据尚未完成编码、监督来源和工具语义审核，故完整训练不能直接开始。今天可以先迁移并运行明确标记的 10 步技术短测；扩大和完整训练必须等第 16.3 节完成并产出已验收的数据。

1. **准备服务器。** 检查 `nvidia-smi`、显存、磁盘、网络和 Python 版本；按照 **14.1–14.4 节**安装 Docker Engine、固定版本 SWE-Agent，拉取镜像并运行最小容器测试。该测试只证明 Docker 容器可启动，不证明模型与 SWE-Agent 已连接。

2. **迁移数据和代码。** 下方直接给出打包和 `scp` 命令；更完整的原方案见 **第十五节**。数据源目录为本机 `/root/EverOS/.memo-vault/agent-training/sweagent/full-08e109b4/`。2026-11-11 查看时的大小如下，实际打包大小以 `du -sh` 为准：

   | 要迁移的目录 | 用途 | 本机约占空间 |
   |---|---|---:|
   | `raw/`、`metadata/` | 官方完整源数据及来源版本，供以后重建 | 4.0 GiB |
   | `splits-v1/` | 全量及短测划分清单、原始数据哈希 | 35 MiB |
   | `datasets-v1/` | 已划分的全量和短测 Parquet | 2.5 GiB |
   | `lf-candidate-smoke-v1/` | 服务器重导出短测输入所需的训练/验证候选 | 11 MiB |
   | `lf-candidate-full-v1/` | 全量训练/验证候选及排除清单，供后续处理 | 2.2 GiB |
   | `lf-smoke-provisional-4096-v1/` | 本机已适配的短测候选、审计报告和配置 | 3.5 MiB |
   | 训练/评测源码、脚本、配置、本文档及 tokenizer | 重建数据、启动训练与评测 | 约 22 MiB 加源码 |

   完整模型权重、LLaMA-Factory 和 SWE-Agent 仓库不在数据包中，按后续第 3 步和第十四节准备。`lf-candidate-full-v1/` 还不是可直接用于完整训练的已验收数据；`datasets-v1-incomplete/` 是中断时的旧产物，不要迁移。

   **第一步：在本机打包。** 两台机器均需检查 `df -h`；本机需要额外空间保存约 9 GiB 的数据包，服务器还需要同时容纳压缩包、解包数据、模型和训练产物。下方是未压缩的 tar，适合后续直接核验；已有同名目录时换一个名称，不会覆盖。

```bash
# 本机执行；SWE_REMOTE_* 替换为新服务器的真实信息。
export SWE_PROJECT=/root/EverOS
export SWE_REL=.memo-vault/agent-training/sweagent/full-08e109b4
export SWE_ARCHIVE_DIR=/root/swe-transfer-20261111
export SWE_REMOTE_USER=YOUR_SSH_USER
export SWE_REMOTE_HOST=YOUR_SERVER_IP
export SWE_REMOTE_DIR=/root/swe-transfer-20261111
export SWE_SSH_PORT=22

df -h /root
if [ -e "$SWE_ARCHIVE_DIR" ]; then echo "打包目录已存在，请更换名称"; exit 1; fi
mkdir -p "$SWE_ARCHIVE_DIR"
cd "$SWE_PROJECT"

tar -cf "$SWE_ARCHIVE_DIR/swe-data.tar" \
  "$SWE_REL/raw" "$SWE_REL/metadata" "$SWE_REL/splits-v1" \
  "$SWE_REL/datasets-v1" "$SWE_REL/lf-candidate-smoke-v1" \
  "$SWE_REL/lf-candidate-full-v1" "$SWE_REL/lf-smoke-provisional-4096-v1"

tar -cf "$SWE_ARCHIVE_DIR/swe-code.tar" \
  src/everos/__init__.py \
  src/everos/agent_training/swe_full_split.py \
  src/everos/agent_training/swe_materialize_split.py \
  src/everos/agent_training/swe_source_candidate.py \
  src/everos/agent_training/swe_lora_pipeline.py \
  src/everos/agent_training/swe_llamafactory.py \
  src/everos/agent_training/swe_train_run.py \
  src/everos/agent_training/swe_eval_run.py \
  src/everos/agent_training/openhands_inventory.py \
  src/everos/agent_training/openhands_qlora.py \
  tests/unit/test_agent_training/test_swe_full_split.py \
  tests/unit/test_agent_training/test_swe_source_candidate.py \
  tests/unit/test_agent_training/test_swe_run_scripts.py \
  scripts/train_swe_qlora_smoke.sh scripts/train_swe_qlora_scale.sh \
  scripts/train_swe_qlora_full.sh scripts/create_swe_eval_toy.sh \
  scripts/serve_openhands_qwen35.sh scripts/requirements-swe-llamafactory-constraints.txt \
  configs/swe/eval-qwen35.yaml \
  docs/superpowers/plans/2026-11-10-vault-sweagent-full-qwen35.zh-CN.md \
  docs/superpowers/plans/2026-11-09-vault-openhands-qwen35.zh-CN.md \
  docs/vault-swe-llamafactory.zh-CN.md \
  .memo-vault/agent-training/agentlab/qwen35-tokenizer-2fc06364

cd "$SWE_ARCHIVE_DIR"
sha256sum swe-data.tar swe-code.tar > SHA256SUMS
sha256sum -c SHA256SUMS
du -sh swe-data.tar swe-code.tar
```

   **第二步：从本机通过 `scp` 上传。** 将用户名、IP、端口及远端目录改为真实值；远端目录的父目录需已存在，且接收目录须不存在。示例使用 IPv4；`ssh -p` 和 `scp -P` 的大小写不同。若远端非 root，换成该用户有写权限的绝对路径。路径请使用不含空格、引号和特殊字符的目录名。

```bash
# 本机执行；先检查服务器空间，再创建新的接收目录。
ssh -4 -p "$SWE_SSH_PORT" "$SWE_REMOTE_USER@$SWE_REMOTE_HOST" 'df -h'
ssh -4 -p "$SWE_SSH_PORT" "$SWE_REMOTE_USER@$SWE_REMOTE_HOST" "mkdir '$SWE_REMOTE_DIR'"
cd "$SWE_ARCHIVE_DIR"
scp -4 -P "$SWE_SSH_PORT" swe-data.tar swe-code.tar SHA256SUMS \
  "$SWE_REMOTE_USER@$SWE_REMOTE_HOST:$SWE_REMOTE_DIR/"
```

   **第三步：登录新服务器，校验并解包。** 先确认校验成功再解压；每一条命令都在服务器终端执行。下方目录与上述默认值一致，如果上传到其他位置请修改。

```bash
cd /root/swe-transfer-20261111
sha256sum -c SHA256SUMS || { echo "传输校验失败，停止"; exit 1; }
if [ -e unpacked ]; then echo "unpacked 已存在，请更换目录"; exit 1; fi
mkdir unpacked
tar -xf swe-code.tar -C unpacked
tar -xf swe-data.tar -C unpacked

export SWE_PROJECT="$PWD/unpacked"
export SWE_BASE="$SWE_PROJECT/.memo-vault/agent-training/sweagent/full-08e109b4"
cd "$SWE_BASE/raw"
sha256sum -c "$SWE_BASE/splits-v1/raw.sha256"
cd "$SWE_BASE/datasets-v1"
sha256sum -c datasets.sha256
cd "$SWE_BASE/lf-candidate-smoke-v1"
sha256sum -c SHA256SUMS
cd "$SWE_BASE/lf-smoke-provisional-4096-v1"
sha256sum -c SHA256SUMS
cd "$SWE_PROJECT"
export PYTHONPATH="$SWE_PROJECT/src"
python3 -m everos.agent_training.swe_eval_run --help
```

   服务器上后续命令中的 `SWE_PROJECT` 应使用上面解包出的实际路径，不能继续假定 `/root/EverOS`。已适配短测文件包含本机绝对路径，服务器上仍需按 **17.1 节**用迁移的短测候选重新 `export`，再运行 `verify` / `audit`。


3. **准备模型和训练环境。** 按 [OpenHands 计划 6.12 节：准备完整模型](2026-11-09-vault-openhands-qwen35.zh-CN.md)下载 `Qwen/Qwen3.5-0.8B`，固定 revision 为 `2fc06364715b967f1860aea9cf38778875588b17`；该模型下载命令也适用于本 SWE 路径。按 [SWE LLaMA-Factory 使用说明第一节](../../vault-swe-llamafactory.zh-CN.md)安装固定 v0.9.5 及依赖。先检查 `pip check` 和 `torch.cuda.is_available()`。切勿把只含 tokenizer 的目录当成完整模型。

4. **准备新服务器上的短测数据。** 按 **17.1 节**设置 `SWE_PROJECT`、`SWE_RUN`、`SWE_MODEL`、`SWE_PYTHON` 等变量。因迁移后的配置含旧绝对路径，从已迁移的短测候选重新执行 `export`，再运行 `verify` 和 `audit`；不要直接使用旧路径下的 `smoke.yaml`。4,096 token 是否适合 24 GB 显卡须实测，不能保证。

5. **先运行 10 步短测。** 按 **17.2 节**执行。下面假定 17.1 节的变量已设置、数据已通过 `verify` / `audit`：

```bash
cd "$SWE_PROJECT"
time env SWE_PYTHON="$SWE_PYTHON" scripts/train_swe_qlora_smoke.sh \
  "$SWE_PREPARED" "$SWE_MODEL" "$SWE_RUN/swe-smoke-10" \
  --technical-probe --gradient-accumulation 4
```

训练日志写在 `$SWE_RUN/swe-smoke-10/training.log`，不会自动实时打印到启动训练的终端；可在**另一个终端**查看：

```bash
tail -F /root/vault-swe-run/swe-smoke-10/training.log
```

若 `SWE_RUN` 改过，请相应修改路径。日志何时刷新取决于训练进程；它可能显示 LLaMA-Factory 的步骤和速度，但当前包装脚本不保证实时进度条或准确的剩余总时间。训练子进程正常退出后，脚本会将实际训练耗时秒数写入 `run-report.json` 的 `seconds` 字段；`time` 在命令结束时另外显示整个命令的墙钟时间。若进程被强制中断，报告中的最终耗时可能缺失。短测完成后按 **17.2 节**执行 Adapter 重载检查；显存峰值需另行监测。

6. **扩大和完整训练。** 先完成 **16.3 节**中全量格式处理、监督来源核实、编码与审计。取得已验收数据后，按 **17.3 节**先运行 50–100 步吞吐测试，再用 `scripts/train_swe_qlora_full.sh` 按 epoch 训练；该节已有完整命令。当前候选未达到这一条件，不能通过手工修改报告来跳过检查。

7. **微调前后各评测一次。** 按 **第十八节**准备同一个固定版本的小仓库、任务、工具配置和镜像；启动基座服务运行一次，随后切换为同一基座加 Adapter 服务再运行一次。使用 `swe_eval_run compare` 对照两份结果和相同条件，保存服务日志、轨迹、patch、测试结果及 Adapter 哈希。脚本和命令已准备，真实闭环尚待服务器验证；示例玩具仓库不能替代正式基准评测。
