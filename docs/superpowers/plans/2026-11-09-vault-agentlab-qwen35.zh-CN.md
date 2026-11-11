# AgentLab + BrowserGym + Qwen3.5-0.8B 网页任务开发与复刻计划

> 更新：2026-11-11。**状态：进行中；AgentLab QLoRA 启动器、模型服务适配、真实闭环脚本和 base/Adapter 配对评测代码已实现，尚未在 GPU 上训练或对真实 Qwen 服务运行最终评测。** 已下载并划分官方数据，完成只读审计、训练候选导出代码和 BrowserGym 预设动作短测；网页元素语义映射、经审核的正式训练样本和真实模型闭环结果仍待验证。

**目标：**以 Mind2Web 为数据源，适配固定的 AgentLab / BrowserGym 网页动作和观察，分别完成 Qwen3.5-0.8B 的 QLoRA、Adapter 保存重载，以及在可复现本地网页上的基座和 Adapter 对照。

**架构：**ADP 已转换的 AgentLab SFT → 审计源任务、原划分、网页观察和动作 → 有证据的逐条审核 → LLaMA-Factory 数据及 QLoRA → 固定的 AgentLab 模型接口 → BrowserGym 浏览器动作及观察反馈。离线 SFT 不需要启动网页浏览器；框架评测需要 BrowserGym、Chromium、AgentLab、模型服务和任务页面。

**模型与训练：**`Qwen/Qwen3.5-0.8B`，revision `2fc06364715b967f1860aea9cf38778875588b17`；LLaMA-Factory 0.9.5（commit `7af909522a951e3ad9f022ea6f88b6755257eaa5`）；4-bit NF4 QLoRA，batch 1，梯度检查点；训练三个框架时本路径从相同基座独立训练一个 Adapter。

## 一、现状与约束

1. 原始计划要求观察、点击、输入的真实网页任务，Mind2Web 的 `backend_node_id` 与 BrowserGym 的 `browsergym_id` 不能假设相同。原始网站不保证可重放，不能将离线历史快照当成已经验证的实时页面。
2. ADP 官方已发布 `mind2web/full_sft/full_sft_agentlab.jsonl`，可以避免从原始 HTML 重新生成所有 SFT，但上游发布的转换结果仍需验证元素和动作语义；本项目保留按证据回溯原始快照或使用固定上游转换器重建的选择，不静默替换数据。
3. AgentLab 最新检出中的通用 Agent 有其输出解析和提示，ADP SFT 的目标是 `{"thought": ..., "action": ...}` JSON。模型服务、AgentLab 解析器、训练模板的输出协议必须对齐，不能假定现有默认配置支持该 JSON。
4. 用户已确定 Qwen3.5-0.8B；测试集维持保留，按网站 / 原任务分组隔离。初期只验证代码与少量有效轨迹；不承诺论文规模或任务效果。
5. 现有 GPU 为 RTX 3050 Laptop 4 GiB，尚无本路径训练成功结果。此前 2060 SUPER 8 GiB 在现有 QLoRA 配置的 1,024 token 合成单步 OOM。训练长度要在目标机器实测；不得把目标预算当作已验证容量。

## 二、固定来源与当前文件

| 内容 | 固定版本、位置和状态 |
|---|---|
| 官方数据集 | `neulab/agent-data-collection` revision `31a76bfb0124d77ae7322eabbb0171bf11ee2c67`，`mind2web/full_sft/full_sft_agentlab.jsonl`，SHA-256 `9b5fe4336aebb3e7f84143e1705785376b5180b919fea37adb6a976454274120`，418,877,279 字节。发布数据的具体转换器提交未知。 |
| AgentLab 源码 | `https://github.com/ServiceNow/AgentLab` commit `8488c564eea0525aaff5dea189bb03a9b529546e`；源码已下载；在继承系统包的实验环境中通过预设回复驱动的 AgentLab 动作短测，依赖冲突及真实模型兼容性仍待解决。 |
| BrowserGym 源码 | `https://github.com/ServiceNow/BrowserGym` commit `27a90ce815098f43414e93491e06907af468e45b`，core 版本 `0.14.4`；单独浏览器环境已验证基础动作。它是候选组合，不声称与 AgentLab 全依赖及 ADP 发布版本已完全兼容。 |
| 原候选清单 | `.memo-vault/agent-training/manifests/harness_audit_20261109/agentlab.jsonl`，183 条训练、20 条验证、19 条测试候选轨迹。 |
| 下载脚本 | `scripts/download_agentlab_adp_sft.sh`：固定文件与 SHA-256，不覆盖已有文件。 |
| 来源与动作审计 | `src/everos/agent_training/agentlab_data_audit.py`：只读检查，产生逐条报告，默认所有记录 `training_ready=false`。 |
| 训练数据导出 | `src/everos/agent_training/agentlab_llamafactory.py`：只接受有证据的逐条审核、原训练 / 验证映射以及可识别的动作。测试集、未匹配来源、错误 ID 和不支持的动作不会进入训练；不执行训练。 |
| JSON 动作适配 | `src/everos/agent_training/agentlab_action.py`：无执行地解析受限 JSON，只接受当前观察中的 ID 和 `click` / `fill` / `select_option`。尚未接入 AgentLab。 |
| 基础浏览器短测 | `scripts/agentlab_browsergym_smoke.py`：使用内存本地页面和预设动作检查观察、输入、点击及结果，**不调用模型或 AgentLab**。 |
| 单元测试 | `tests/unit/test_agent_training/test_agentlab_data_audit.py`、`test_agentlab_llamafactory.py`。 |

`.memo-vault/` 存放数据、上游源码、虚拟环境和报告，通常不随 Git 提交。项目代码及本文也须提交后才能在新服务器通过 Git 获取；单独迁移原候选清单、模型和审计证据，核验哈希。

## 三、已执行的数据审计

官方文件 **9,060 条决策**；按决策 ID 的轨迹前缀与原候选清单做精确匹配：训练 1,462、验证 155、测试 167、未匹配 7,276。222 条旧候选轨迹中 201 条至少匹配到一个决策，另有 21 条未匹配。ID 对应不等于来源 revision、任务内容或质量已经证明一致；未匹配数据不可默认加入训练。

| 文件中的动作 | 数量 | 当前判断 |
|---|---:|---|
| `click` | 5,999 | 5,378 条在该条当前无障碍树中没有唯一的目标 ID 匹配；剩余 621 条只是文本匹配，仍不能证明运行时映射。 |
| `type` | 878 | 当前固定 BrowserGym 动作空间使用 `fill`；不能不验证语义地直接改名。 |
| `select` | 298 | 当前 BrowserGym 使用 `select_option`；必须先验证选项值和参数语义。 |
| `goto` | 942 | 可作协议检查，实际评测不得默认访问原始网站。 |
| `send_msg_to_user` | 942 | 结束消息也需要核对任务来源和监督目标。 |
| 无法解析 | 1 | 保持排除，检查上游原文与原因。 |

计数中的问题可能重叠。`bid` 检查只搜索 `# Current page Accessibility Tree` 与历史动作段之间的方括号标识，它是**初筛启发式，不是网页节点绑定证明**。当前审计对所有记录标记 `training_ready=false`，原因包括来源版本 / 监督与实时元素对应尚未证明。

报告：`.memo-vault/agent-training/agentlab/audit-20261109-v1/report.json`；每条原因：同目录 `records.jsonl`。其中 `source_line` 是固定发布文件的行号，`partition` 是按轨迹 ID 与旧清单匹配的候选划分。

## 四、实际完成的 BrowserGym 预设动作验证

在本机独立 Python 3.12 虚拟环境，从固定源码安装 BrowserGym core 0.14.4，Playwright 1.44，下载 Chromium 125。运行本地内存页面：从无障碍树分别识别输入框与按钮的 `browsergym_id`，将预设 JSON 通过本项目受限解析器转换为 `fill`、`click` 并执行，页面观察中出现 `Hello Ada`；本次输入 ID 为 `4`、按钮 ID 为 `5`（每次运行可能变化）。

该结果只证明 BrowserGym 环境与两条预设动作可运行，不证明 Mind2Web 的历史 ID 对应正确，也不证明 AgentLab、Qwen3.5 或 Adapter 已完成自主闭环。浏览器依赖记录在 `.memo-vault/agent-training/agentlab/browser-requirements-20261109.txt`，实际运行依赖及 Chromium 构建仍应迁移或重新安装并校验。

## 五、新服务器复刻：数据、源码和环境

先确保 Git 工作区包含本计划和新增脚本，Linux / WSL、Python 3.11 或 3.12 可用。下列命令中的路径按服务器修改；不覆盖既有目录。

```bash
cd /path/to/EverOS
export VAULT_PROJECT="$PWD"
export VAULT_RUN=/root/vault-agentlab-run
export VAULT_DATA="$VAULT_RUN/data/official-sft-31a76bfb"
export VAULT_MANIFEST="$VAULT_RUN/data/agentlab-candidates.jsonl"
export VAULT_MODEL="$VAULT_RUN/model"
mkdir -p "$VAULT_RUN/data" "$VAULT_RUN/upstream"

# 仅下载已转换 AgentLab SFT，并校验固定 SHA-256。
bash scripts/download_agentlab_adp_sft.sh "$VAULT_DATA"
# 将现有 agentlab.jsonl 清单从旧机器复制到 $VAULT_MANIFEST；Git 不包含它。

# 固定源码：如目录已存在，先确认状态，不覆盖。
git clone https://github.com/ServiceNow/AgentLab.git "$VAULT_RUN/upstream/AgentLab"
git -C "$VAULT_RUN/upstream/AgentLab" checkout 8488c564eea0525aaff5dea189bb03a9b529546e
git clone https://github.com/ServiceNow/BrowserGym.git "$VAULT_RUN/upstream/BrowserGym"
git -C "$VAULT_RUN/upstream/BrowserGym" checkout 27a90ce815098f43414e93491e06907af468e45b

git -C "$VAULT_RUN/upstream/AgentLab" rev-parse HEAD
git -C "$VAULT_RUN/upstream/BrowserGym" rev-parse HEAD
```

下述命令只建立并测试 BrowserGym，不会安装完整 AgentLab：

```bash
python3 -m venv "$VAULT_RUN/browser-env"
"$VAULT_RUN/browser-env/bin/python" -m pip install "$VAULT_RUN/upstream/BrowserGym/browsergym/core"
"$VAULT_RUN/browser-env/bin/python" -m pip check
export PLAYWRIGHT_BROWSERS_PATH="$VAULT_RUN/playwright-browsers"
"$VAULT_RUN/browser-env/bin/python" -m playwright install chromium
PYTHONPATH="$VAULT_PROJECT/src" "$VAULT_RUN/browser-env/bin/python" scripts/agentlab_browsergym_smoke.py
"$VAULT_RUN/browser-env/bin/python" -m pip freeze > "$VAULT_RUN/browser-requirements.txt"
```

预期短测输出 `"ok": true`、`"result": "Hello Ada"`。Playwright / Chromium 系统库、图形支持和离线服务器下载渠道依机器而异；失败需保存日志。AgentLab 的完整依赖安装与固定锁文件尚待在隔离环境验证，不能用以上 BrowserGym 成功替代。

## 六、执行步骤与验收

### 1. 数据审计与训练 / 验证 / 测试隔离

```bash
export PYTHONPATH="$VAULT_PROJECT/src"
python -m everos.agent_training.agentlab_data_audit \
  --input "$VAULT_DATA/mind2web.agentlab.jsonl" \
  --manifest "$VAULT_MANIFEST" \
  --output-dir "$VAULT_RUN/audit-v1"
```

输出目录必须不存在。检查 `report.json` 与 `records.jsonl` 的来源、划分、动作、ID 和排除原因；校验轨迹内容、来源文件和 website group，而非只依赖同名 ID。未匹配的 7,276 条决策要先确认来源和旧测试隔离，不能当作训练；跨网站 / 原任务冲突为零后才能划分。21 条未匹配旧轨迹应检查数据版本、转换排除与缺失原因。

### 2. 建立可验证的网页元素与动作映射

- [ ] 从**训练划分**抽取可复核的轨迹，关联原始快照及动作，检查目标节点、上下文和浏览器状态是否支持映射到 BrowserGym 的 `browsergym_id`。必须记录源 ID、转换 ID、上下文、操作、证据及失败原因。
- [ ] 分别验证 `click`、`type` / `fill`、`select` / `select_option` 在动作时序、参数、焦点、值及反馈上的语义；只对经实际测试等价的记录转换，绝不简单批量改名。找不到映射则排除。
- [ ] 观察的生成或裁剪不得读取目标元素、动作或答案来选择上下文；保留目标和必要网页状态，记录截断前后信息。此路径当前导出器**不支持自动截断**，默认保留完整内容，超限排除；可控截断尚需设计和单测。
- [ ] 重新运行审计，按来源和原因输出训练 / 验证 / 测试清单。测试集保留，仅在最终方案固定后评测。不可因网页上的 ID 文本唯一便宣称已经验证真实映射。

### 3. 审核并导出 LLaMA-Factory 数据

`agentlab_llamafactory.py` 只读取逐条审核文件，审核文件每行为 JSON，例如：

```json
{"id":"原始轨迹ID-步骤号","approved":true,"source_sha256":"完整SFT文件SHA256","evidence":"可复核的来源、元素/动作及监督审核证据路径"}
```

此示例仅展示结构，不是可用审核结果。每条记录必须实际经过来源、动作、当前观察和监督检查。只使用能证明对应关系的训练 / 验证决策，不将原始内容未核验的样本伪造为已审核。导出器保留原 system、用户观察和助手 JSON 目标；限制当前观察内 ID 唯一、支持的动作及训练 / 验证划分，默认没有截断，超限记录排除。人工证据的真实性仍需要审核者核对，程序不能自动证明它。

训练环境按 [SWE LLaMA-Factory 使用说明](../../vault-swe-llamafactory.zh-CN.md) 的固定版本单独准备，注意模型与数据需要迁移。仅在已有真实审核文件后执行：

```bash
source "$VAULT_RUN/llamafactory-venv/bin/activate"
export PYTHONPATH="$VAULT_PROJECT/src"
python -m everos.agent_training.agentlab_llamafactory export \
  --input "$VAULT_DATA/mind2web.agentlab.jsonl" \
  --manifest "$VAULT_MANIFEST" --review "$VAULT_RUN/reviewed-v1.jsonl" \
  --model "$VAULT_MODEL" --local-only --max-tokens 2048 --max-steps 10 \
  --output-dir "$VAULT_RUN/prepared-v1" \
  --train-output-dir "$VAULT_RUN/train-smoke-v1"
python -m everos.agent_training.agentlab_llamafactory verify --prepared-dir "$VAULT_RUN/prepared-v1"
```

输出 `train.jsonl`、`validation.jsonl`、`dataset_info.json`、`smoke.yaml`、`manifest.jsonl`、`excluded.jsonl` 和 `report.json`。其中训练标签使用真实 LLaMA-Factory 0.9.5 的 `qwen3_5_nothink` 编码器；导出代码会比较完整编码和 cutoff 编码，防止训练器二次截断。此代码目前只经过合成单元测试，**尚未在 LLaMA-Factory 环境用真实网页样本端到端验证**。还需要实现 / 执行实际 LLaMA-Factory 数据加载器逐条 token 和 label 哈希对照；在此验证前不得称训练数据审计完成。

### 4. 训练、保存与重载

- [ ] 先核对训练与推理模板、环境、GPU 空闲显存、长度及完整样本和标签；确保有独立验证数据。现有 4 GB 不承诺能运行 2,048 token。
- [ ] 用以上 `smoke.yaml` 在合适 GPU 上运行 10 个优化器步骤，检查有限 loss、参数更新、显存峰值与保存产物：

```bash
llamafactory-cli train "$VAULT_RUN/prepared-v1/smoke.yaml"
python -m everos.agent_training.swe_llamafactory check-adapter \
  --prepared-dir "$VAULT_RUN/prepared-v1" \
  --adapter-dir "$VAULT_RUN/train-smoke-v1" \
  --report-path "$VAULT_RUN/reload-smoke-v1.json" --local-only
```

该 Adapter 重载命令复用通用的基座 / PEFT 检查；本路径尚未实际执行或验证。短测通过后基于相同真实长度分布测 50～100 步，记录速度，才决定扩大数据和训练步数。`smoke.yaml` 默认只执行训练，验证集不会自动运行 loss，需另行实现并测试配置后才能报告验证 loss。

### 5. AgentLab + BrowserGym 模型评测

- [ ] 在独立环境安装固定 AgentLab 及与 BrowserGym 兼容的依赖，记录锁文件；核验动作空间 `click` / `fill` / `select_option`、当前观察结构和页面 ID。
- [ ] 将已有受限 JSON 动作解析器接入并验证固定 AgentLab 模型接口和 BrowserGym action parser；不要把 ADP JSON 直接当成现成 AgentLab 默认标签格式。模型服务使用相同 Qwen revision、模板、停止条件、工具配置和生成参数。
- [ ] 在本地可控网页准备独立的查看、点击、输入任务；先对基座建立基线，再加载本路径 Adapter，在同一个任务快照比较解析率、有效工具动作、观察反馈和任务成功。
- [ ] 至少记录一个真实自主循环：模型输出 → 解析 → 浏览器执行 → 更新观察 → 模型继续决策。预设动作成功、静态数据检查和 loss 不可替代自主任务通过。固定方案后才使用保留测试任务。

## 七、当前验证与已知缺口

- `PYTHONPATH=src python -m pytest -c /dev/null --noconftest tests/unit/test_agent_training/test_agentlab_data_audit.py tests/unit/test_agent_training/test_agentlab_llamafactory.py tests/unit/test_agent_training/test_agentlab_action.py -q`：本次针对性测试通过；不代表项目全量测试通过。
- 真实 BrowserGym 本地页面预设 `fill` / `click` 测试通过。固定 AgentLab 环境中也已用临时 OpenAI 兼容 fixture 跑通自定义 ADP JSON Agent 的两步链路：观察 Confirm、解析 JSON、执行点击、观察更新后的 Continue 和新 bid、再次调用模型、完成页面任务。该 fixture 不是 Qwen，不能证明 Qwen 自主选择正确动作。干净 AgentLab 环境安装、真实 Qwen 闭环、Mind2Web 历史网页元素映射、真实数据的完整 LLaMA-Factory 加载审核、QLoRA 训练、Adapter 重载及正式模型评测仍**尚未通过**。
- 数据和环境来源：[ADP 数据集说明](https://huggingface.co/datasets/neulab/agent-data-collection/blob/31a76bfb0124d77ae7322eabbb0171bf11ee2c67/README.md)、[AgentLab 官方仓库](https://github.com/ServiceNow/AgentLab)、[BrowserGym 官方仓库](https://github.com/ServiceNow/BrowserGym)。项目总方案见 [个人 Agent 开发计划](../../vault-personal-agent-development-plan.zh-CN.md)。


## 八、数据处理执行记录及新服务器复刻命令（2026-11-10）

本节记录实际执行的数据处理，供从头复刻。用户确定后续范围为 **ADP 官方发布的 Mind2Web → AgentLab SFT 全文件**；尽可能利用其中可核验并适合训练的数据。全文件 9,060 条是**逐次决策**，不是 9,060 个独立任务；一个原任务轨迹可包含多条决策。原文件完整保留，不因短测或审计删除；不意味着所有记录都已证明可用于训练。此前旧清单仅覆盖部分数据，不是全量数据的最终划分。

本次只重新审计、生成短测**候选**并核验，未启动训练、未生成审核批准文件、未声称 BrowserGym 映射已验证。旧划分按网站分组，本次选择完整轨迹，短测只用原划分匹配上的数据；未匹配的 7,276 条原样保留，后续先补全来源及测试隔离再决定用途。网站和任务哈希在既有清单中跨划分无冲突，仅是已匹配子集的检查，不证明全量无语义重复或版本对应。

### 8.0 本机数据存放位置与迁移

下表的路径均为本机实际位置（项目根目录 `/root/EverOS`）。这些数据位于 `.memo-vault/`，通常不随 Git 克隆；新服务器需要单独复制，或按照后续固定版本和校验步骤重新下载。注意区分原始 Mind2Web、ADP 中间数据和官方已转换的 AgentLab SFT，它们不是同一个文件。

| 内容 | 本机绝对路径 | 用途与当前状态 |
|---|---|---|
| **官方 AgentLab SFT 全量数据** | `/root/EverOS/.memo-vault/agent-training/agentlab/official-sft-31a76bfb/mind2web.agentlab.jsonl` | 本项目后续全量处理的输入：9,060 条决策，418,877,279 字节，固定 SHA-256 见 8.2。原文件完整保留。 |
| 原始 Mind2Web 文件 | `/root/EverOS/.memo-vault/agent-training/datasets/raw/mind2web/` | 本机现有 `README.md` 和 `data/train/` 下 `train_0.json` 至 `train_10.json` 共 11 个训练文件，完整原始 train 已按固定 SHA-256 核验；原始测试文件未在此目录中确认。 |
| 此前保存的 ADP 中间数据 | `/root/EverOS/.memo-vault/agent-training/datasets/paper_adp_v2/` | 其中 `train/general/mind2web.jsonl`、`train/domain/mind2web.jsonl`、`validation/validation/mind2web.jsonl`、`test/test/mind2web.jsonl` 分别有 92、91、20、19 行。这些是旧候选来源，不是 9,060 条官方 AgentLab SFT 全文件。 |
| 旧候选来源与划分清单 | `/root/EverOS/.memo-vault/agent-training/manifests/harness_audit_20261109/agentlab.jsonl` | 222 条候选轨迹，记录原始文件、网站组、任务哈希、旧划分及审核状态；不是训练数据。新服务器应复制此文件并按 8.1 核验。 |
| 本次全量审计 | `/root/EverOS/.memo-vault/agent-training/agentlab/audit-20261110-v1/` | `report.json` 是汇总，`records.jsonl` 是逐决策来源、划分和原因；可按 8.3 重建。原先的 `audit-20261109-v1/` 是上一次审计，保留作历史记录。 |
| 短测候选 | `/root/EverOS/.memo-vault/agent-training/agentlab/smoke-candidates-20261110-v1/` | `train.jsonl`、`validation.jsonl`、`test.jsonl` 是从官方 SFT 原样抽取的候选；对应 `.audit.jsonl` 和 `report.json` 保存审计与校验信息。均尚未验收为可训练数据，可按 8.4 重建。 |

如两台机器能通过 SSH 互访，可在**旧机器**执行以下示例，把必要文件复制到新服务器。先将地址及目标路径改成实际值；确保目标目录不存在或为空，避免覆盖已有文件。`scp` 需要新服务器可登录，传输方式也可换成移动硬盘等。复制原始 Mind2Web 和 ADP 中间数据是为了后续溯源；即使复制成功，也不代表缺失的原始文件或审核证据已经齐全。

```bash
# 在旧机器上：替换成新服务器的登录地址和专用空目录
export AL_NEW_HOST=user@new-server
export AL_NEW_ROOT=/path/to/agentlab-transfer
ssh "$AL_NEW_HOST" "mkdir -p '$AL_NEW_ROOT'"
scp -r /root/EverOS/.memo-vault/agent-training/agentlab/official-sft-31a76bfb "$AL_NEW_HOST:$AL_NEW_ROOT/"
scp -r /root/EverOS/.memo-vault/agent-training/agentlab/audit-20261110-v1 "$AL_NEW_HOST:$AL_NEW_ROOT/"
scp -r /root/EverOS/.memo-vault/agent-training/agentlab/smoke-candidates-20261110-v1 "$AL_NEW_HOST:$AL_NEW_ROOT/"
scp /root/EverOS/.memo-vault/agent-training/manifests/harness_audit_20261109/agentlab.jsonl "$AL_NEW_HOST:$AL_NEW_ROOT/"
scp -r /root/EverOS/.memo-vault/agent-training/datasets/raw/mind2web "$AL_NEW_HOST:$AL_NEW_ROOT/mind2web-raw"
scp -r /root/EverOS/.memo-vault/agent-training/datasets/paper_adp_v2 "$AL_NEW_HOST:$AL_NEW_ROOT/"
```

在新服务器按 8.1 设置 `$AL_RUN` 后，将传输目录下 `agentlab.jsonl` 放到 `$AL_MANIFEST`、`official-sft-31a76bfb/` 放到 `$AL_DATA`（确保目标不存在），再执行 8.1、8.2 的 SHA-256 核验。若只想从头重建审计和短测，不必复制这两个输出目录；原始 Mind2Web 与 ADP 中间数据建议保留目录结构用于后续追溯。项目源码、模型和环境也不会自动包含在上述数据中。

### 8.1 准备目录和项目代码

需 Linux、Python 3、Git、curl、sha256sum、网络及约 420 MB 数据空间；浏览器和 GPU 在以下数据审计步骤不是必需。脚本、模块和本文当前可能尚未提交，先在旧机器确认已纳入 Git，或连同项目代码一并复制；`.memo-vault` 默认不随 Git 迁移。旧候选清单须从旧机器单独迁移，其对应的原始来源数据及审核证据也应保留。

在新服务器设置路径，按实际情况修改 `AL_RUN`，后续均在项目根目录运行：

```bash
cd /path/to/EverOS
export AL_PROJECT="$PWD"
export AL_RUN=/path/to/agentlab-run
export AL_DATA="$AL_RUN/data/official-sft-31a76bfb"
export AL_SOURCE="$AL_DATA/mind2web.agentlab.jsonl"
export AL_MANIFEST="$AL_RUN/data/agentlab-candidates.jsonl"
export AL_AUDIT="$AL_RUN/audit-20261110-v1"
export AL_SMOKE="$AL_RUN/smoke-candidates-20261110-v1"
mkdir -p "$AL_RUN/data"
python3 --version
ls scripts/download_agentlab_adp_sft.sh src/everos/agent_training/agentlab_data_audit.py
```

将旧机器的 `.memo-vault/agent-training/manifests/harness_audit_20261109/agentlab.jsonl` 复制到 `$AL_MANIFEST`；不要用旧清单代替官方全量文件。复制后核验：

```bash
printf '%s  %s\n' '1c4264295d5d158343301512868befc3cfc4ba24a96625d0b00bb8cce8af9023' "$AL_MANIFEST" | sha256sum -c -
wc -l "$AL_MANIFEST"
```

哈希不一致则停止，检查复制内容和版本。该清单的内容仍需来源核验，哈希一致只说明与本次文件一致。

### 8.2 下载并核对官方全量文件

```bash
bash scripts/download_agentlab_adp_sft.sh "$AL_DATA"
printf '%s  %s\n' '9b5fe4336aebb3e7f84143e1705785376b5180b919fea37adb6a976454274120' "$AL_SOURCE" | sha256sum -c -
wc -lc "$AL_SOURCE"
```

固定数据集 `neulab/agent-data-collection` revision `31a76bfb0124d77ae7322eabbb0171bf11ee2c67`，上游文件为 `mind2web/full_sft/full_sft_agentlab.jsonl`。预期 9,060 行、418,877,279 字节。下载脚本对已有文件只核哈希，不覆盖；下载失败或哈希不符需排查，不能继续以不同文件冒充固定版本。

### 8.3 逐条审计与结果检查

输出目录必须不存在；重新运行请换一个新目录。使用项目源码，不需要 LLaMA-Factory 或显卡：

```bash
PYTHONPATH="$AL_PROJECT/src" python3 -m everos.agent_training.agentlab_data_audit \
  --input "$AL_SOURCE" --manifest "$AL_MANIFEST" --output-dir "$AL_AUDIT"
cat "$AL_AUDIT/report.json"
wc -l "$AL_AUDIT/records.jsonl"
sha256sum "$AL_AUDIT/records.jsonl"
```

本机本次结果：训练候选 1,462、验证候选 155、测试候选 167、未匹配 7,276；按动作 `goto` 942、`select` 298、`click` 5,999、`type` 878、`send_msg_to_user` 942、解析失败 1。所有记录仍标记 `training_ready=false`。`records.jsonl` SHA-256 为 `bc30ed599de52b6161e6de7576427c47139971ce6ccec816ae45392ba947e484`。逐条报告包含源文件行号、ID、旧候选划分、动作和原因；原因可能重叠，不能直接相加。审计中的 ID 文本检查不是运行时网页元素映射证明。

### 8.4 生成可复刻的短测候选

本机按 `agentlab-smoke-20261110-v1` 与轨迹 ID 的 SHA-256 排序，在各自旧划分内依次加入**完整轨迹**，达到或超过目标决策数即停止；不把同一轨迹拆到不同文件。抽取前核查已匹配候选的轨迹、网站组和任务文本哈希均未跨划分；这不替代对来源、语义近重复和全量数据的审核。测试候选仅保留供后续评测，不能注册为训练集。此方法不是随机代表性抽样，不能用短测指标推断全量效果。

下列脚本写入运行目录，在新服务器完整执行；`$AL_SMOKE` 必须不存在。脚本复核源文件哈希、审计数量和被选行的 ID，并保存原样 SFT 行与对应审计行；不会生成训练格式或启动训练。

```bash
cat > "$AL_RUN/make-smoke-candidates.py" <<'PY'
import collections, hashlib, json, os
from pathlib import Path

source = Path(os.environ['AL_SOURCE'])
audit = Path(os.environ['AL_AUDIT'])
out = Path(os.environ['AL_SMOKE'])
expected = '9b5fe4336aebb3e7f84143e1705785376b5180b919fea37adb6a976454274120'
def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()
report = json.loads((audit / 'report.json').read_text())
if sha(source) != expected or report['source_sha256'] != expected:
    raise SystemExit('官方文件哈希不匹配')
records = [json.loads(line) for line in (audit / 'records.jsonl').open(encoding='utf-8')]
if len(records) != report['records'] or report['records'] != 9060:
    raise SystemExit('审计记录数量不匹配')
groups = collections.defaultdict(list)
partitions = collections.defaultdict(set)
for r in records:
    if r['partition'] in ('train', 'validation', 'test'):
        groups[(r['partition'], r['trajectory_id'])].append(r)
        for field in ('trajectory_id', 'source_group', 'task_sha256'):
            if not r.get(field): raise SystemExit('缺少分组依据: ' + field)
            partitions[(field, r[field])].add(r['partition'])
if any(len(v) > 1 for v in partitions.values()):
    raise SystemExit('轨迹、网站或任务哈希跨划分冲突')
chosen = {}
for split, target in [('train', 200), ('validation', 20), ('test', 20)]:
    items = [(tid, rows) for (p, tid), rows in groups.items() if p == split]
    items.sort(key=lambda x: hashlib.sha256(('agentlab-smoke-20261110-v1:' + x[0]).encode()).hexdigest())
    selected, count = [], 0
    for tid, rows in items:
        if count >= target: break
        selected.extend(rows)
        count += len(rows)
    if count < target: raise SystemExit('候选不足: ' + split)
    chosen[split] = selected
out.mkdir(parents=True, exist_ok=False)
by_line = {r['source_line']: (split, r) for split, rows in chosen.items() for r in rows}
handles = {s: (out / (s + '.jsonl')).open('xb') for s in chosen}
try:
    with source.open('rb') as f:
        for n, line in enumerate(f, 1):
            if n in by_line:
                split, row = by_line[n]
                if json.loads(line)['id'] != row['id']: raise ValueError('审计行号和源 ID 不一致')
                handles[split].write(line)
finally:
    for f in handles.values(): f.close()
for split, rows in chosen.items():
    with (out / (split + '.audit.jsonl')).open('x', encoding='utf-8') as f:
        for row in sorted(rows, key=lambda r: r['source_line']): f.write(json.dumps(row, ensure_ascii=False) + '\n')
result = {'source_sha256': expected, 'audit_records_sha256': sha(audit / 'records.jsonl'), 'seed': 'agentlab-smoke-20261110-v1', 'training_ready': False, 'splits': {}}
for split, rows in chosen.items():
    result['splits'][split] = {'decisions': len(rows), 'trajectories': len({r['trajectory_id'] for r in rows}), 'source_groups': len({r['source_group'] for r in rows}), 'sha256': sha(out / (split + '.jsonl')), 'audit_sha256': sha(out / (split + '.audit.jsonl'))}
(out / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(result, ensure_ascii=False, indent=2))
PY
python3 "$AL_RUN/make-smoke-candidates.py"
cat "$AL_SMOKE/report.json"
wc -l "$AL_SMOKE"/{train,validation,test}.jsonl
sha256sum "$AL_SMOKE"/{train,validation,test}.jsonl
```

本机实际结果如下；验证和测试超过目标是因为最后一条轨迹完整保留：

| 划分 | 决策数 | 轨迹数 | 网站组数 | 原样 JSONL SHA-256 |
|---|---:|---:|---:|---|
| train | 200 | 25 | 15 | `65f316a0dd11731af19d908941bdb7831414ee25e278d5d47da232feacd5b38f` |
| validation | 28 | 3 | 1 | `b4026eabef8c6d3dc156935c74be6636b5e9e8034a22fe774603eb6984d876aa` |
| test | 23 | 3 | 2 | `0afa402bc5fed69b09cbd8af1e6e603400b42baaeafd110b1e60a63adb252e81` |

每个划分还输出对应 `.audit.jsonl`；`report.json` 保存种子、来源和输出哈希。短测目录位于本机 `.memo-vault/agent-training/agentlab/smoke-candidates-20261110-v1/`。各文件只是从原文件选出的原样候选，`training_ready=false`，不可直接当 LLaMA-Factory 训练文件。

### 8.5 当时记录的后续关口

本节是短测候选生成时的历史记录；完整原始 train 已补齐，全量来源与项目划分的最新结果见第九节。

1. 以完整官方文件为范围，追溯未匹配的 7,276 条决策对应的原任务、网站和官方划分，核验来源版本及任务内容，建立可复核的全量分组；与保留测试集、同任务及相近任务隔离，不能将未匹配项默认归为训练。保留无法证明来源的记录并写明原因。
2. 对短测的 train / validation 候选先核验观察、目标、原快照和动作语义，形成第六节要求的逐条证据和审核 JSONL。测试候选单独保留；对于不能证明节点映射、`type` / `fill` 或 `select` / `select_option` 语义的记录，先排除或修复并复核，不能伪造批准。
3. 准备固定 Qwen 模型和 LLaMA-Factory 环境后，再按第六节第 3 项用真实审核文件导出。导出器尚不生成测试文件，须独立设计评测数据及隔离检查；真实加载器逐条 token/label 对照尚未实现或验证。训练与模型评测均须在这些关口完成后另行记录实际结果。

若短测中通过审核的条数少于目标，以审核后的实际数量为准；可从同一已确认的训练 / 验证划分继续按规则选取候选并复核，不能为凑数降低审核标准。本次未改变全量数据，也未将短测候选视为已完成的数据适配。


## 九、Mind2Web 全量溯源与项目划分（2026-11-10）

**最新结果：已完成项目内部任务级划分，尚未完成训练数据验收。** 之前仅持有四个原始文件、将 6,524 条决策隔离的结果已被本节替代，不要将历史 `restored-candidates-20261110-v3` 当作最新划分。原始训练数据来自 `osunlp/Mind2Web` revision `17ece8eb89862368edc0cc806acee6fca5163474`，本机现有 11 个 train 文件，均核对固定 SHA-256。原始 train 共 1,009 个任务，已发布 SFT 共 942 条轨迹、9,060 条决策；942 条轨迹均匹配到原任务 ID，全部决策的目标段包含对应任务文本，67 个原始任务未出现在该 SFT，原因尚未查明。ID 与文本对应不证明所有网页动作或监督语义正确。

| 项目划分 | SFT 决策 | 轨迹 | 网站组 | JSONL SHA-256 |
|---|---:|---:|---:|---|
| train | 7,464 | 773 | 60 | `7b221adf7b787b6811cdf974d6e3adbe6458de22832ddfe658c0149f8a6b43d0` |
| validation | 670 | 80 | 5 | `4ad2d2e0e1242bc6bf89e59a15292661aaefda11e69c500411716f855ad87877` |
| test | 926 | 89 | 7 | `0d253f233c6e803bee3a11afd07debf0d78e8c8310daed9410d273c101ee92b0` |

划分规则：保留旧清单中已有网站的项目划分；新增网站按固定种子 `mind2web-site-split-20261110-v1` 的 SHA-256 分桶，阈值 80/10/10；若不同网站存在相同规范化任务文本，将这些网站合并到同一分组再分配。已有网站按旧清单固定，因此实际比例不保证恰好 80/10/10。规范化为 Unicode NFKC、大小写折叠和压缩空白。完整原始任务集用于查找相同任务文本，已分配 SFT 的轨迹 ID、网站和规范化任务哈希跨划分冲突均为 0。没有做语义近重复人工审查或跨其他来源查重；这三份是**本项目划分**，不是 Mind2Web 原有测试集，也不是 ADP 论文指定的三份划分。

第 8 节的 200 / 28 / 23 短测候选按此前旧清单抽样；它是历史候选，保留旧网站归属，但不是本节全量结果自动生成的最终训练格式。正式试验前仍需审查监督策略、动作语义、浏览器元素映射、训练与推理格式，并检查有效样本和测试隔离；原 SFT 有一条动作解析异常，详见旧审计。SFT 的显式原始 loss 标记未恢复，不应凭本次划分假定监督规则。

### 9.1 数据与关键代码位置

本机原始文件：`/root/EverOS/.memo-vault/agent-training/datasets/raw/mind2web/data/train/train_0.json` 至 `train_10.json`。官方 SFT：`/root/EverOS/.memo-vault/agent-training/agentlab/official-sft-31a76bfb/mind2web.agentlab.jsonl`。结果目录：`/root/EverOS/.memo-vault/agent-training/agentlab/full-split-20261110-v1/`。

结果目录的 `train.jsonl`、`validation.jsonl`、`test.jsonl` 是原样 SFT 决策；`manifest.jsonl` 记录逐条源行、行哈希、源任务、原始文件、网站和划分；`raw-task-manifest.jsonl` 记录原始任务及其是否在 SFT 中；`report.json` 记录数量和哈希；`quarantine.jsonl` 本次为空，不代表动作或监督审核已经完成。目录默认不由 Git 管理，新服务器需另行复制或重新生成。

主要代码 `src/everos/agent_training/agentlab_full_split.py` 的 `assign_sites()` 将相同规范化任务关联的网站合并，检查旧划分冲突，随后对新增组按固定种子分桶；`main()` 核对 SFT、旧清单、全部 11 个原始文件的 SHA-256，流式索引原始任务，再逐条对照 ID、目标文本和旧清单，写出分组文件与报告。依赖 `src/everos/agent_training/agentlab_restore_split.py` 中的哈希和匹配函数；两个文件及相关测试均须随项目代码迁移或先提交到 Git。测试为 `tests/unit/test_agent_training/test_agentlab_full_split.py`。

主要分桶逻辑如下（完整实现还负责合并相同任务的网站组、验证既有划分冲突及逐条来源核验；下面片段不可脱离完整模块直接运行）：

```python
# sites 是已合并的同任务网站组；reserved 是旧清单的网站 -> 划分。
fixed = {reserved[site] for site in sites if site in reserved}
if len(fixed) > 1:
    raise ValueError("reserved partition conflict for identical task or website")
if fixed:
    partition = next(iter(fixed))
else:
    key = seed + ":" + "\n".join(sorted(sites))
    bucket = int(hashlib.sha256(key.encode()).hexdigest(), 16) % 100
    partition = "train" if bucket < 80 else "validation" if bucket < 90 else "test"
```

### 9.2 新服务器复刻完整划分

先按 8.1–8.3 准备官方 SFT、旧清单、全量审计；从旧机复制全部 11 个原始 train 文件到 `$AL_RAW`，或从固定 revision 下载并校验。可用 `ijson==3.3.0` 的 Python 环境。输出目录必须不存在，脚本不覆盖。路径应按服务器修改：

```bash
cd /path/to/EverOS
export AL_PROJECT="$PWD"
export AL_RUN=/path/to/agentlab-run
export AL_SOURCE="$AL_RUN/data/official-sft-31a76bfb/mind2web.agentlab.jsonl"
export AL_MANIFEST="$AL_RUN/data/agentlab-candidates.jsonl"
export AL_RAW="$AL_RUN/data/mind2web-raw/data/train"
export AL_AUDIT="$AL_RUN/audit-20261110-v1"
export AL_FULL="$AL_RUN/full-split-20261110-v1"
export AL_PYTHON=/path/to/python

"$AL_PYTHON" -c 'import ijson; print("ijson", ijson.__version__)'
ls "$AL_SOURCE" "$AL_MANIFEST" "$AL_AUDIT/records.jsonl" "$AL_RAW"/train_{0..10}.json
PYTHONPATH="$AL_PROJECT/src" "$AL_PYTHON" -m everos.agent_training.agentlab_full_split \
  --input "$AL_SOURCE" --raw-dir "$AL_RAW" --legacy-manifest "$AL_MANIFEST" \
  --audit "$AL_AUDIT" --output-dir "$AL_FULL"
cat "$AL_FULL/report.json"
wc -l "$AL_FULL"/{train,validation,test,quarantine,manifest}.jsonl
sha256sum "$AL_FULL"/{train,validation,test}.jsonl
```

复制整个结果目录到新服务器时，三份 JSONL 应与上表哈希一致；复制原始文件后可运行上述脚本，其内置官方哈希检查会拒绝不匹配的文件。相关单元测试：

```bash
PYTHONPATH="$AL_PROJECT/src" "$AL_PYTHON" -m pytest -c /dev/null --noconftest \
  "$AL_PROJECT/tests/unit/test_agent_training/test_agentlab_full_split.py" \
  "$AL_PROJECT/tests/unit/test_agent_training/test_agentlab_restore_split.py" -q
```

## 十、固定 AgentLab 与网页动作协议（2026-11-10）

### 10.1 当前实际状态与版本

本机源码**已经下载**，不是框架整体尚未下载：

| 组件 | 本机位置 | 固定版本 | 实际验证 |
|---|---|---|---|
| AgentLab | `/root/EverOS/.memo-vault/agent-training/agentlab/upstream/AgentLab` | Git commit `8488c564eea0525aaff5dea189bb03a9b529546e` | 已在继承系统包的测试环境安装并通过预设响应驱动的 AgentLab 浏览器动作短测；该环境 pip check 有系统包冲突，真实模型接口和 WebArena 尚未验证。 |
| BrowserGym | `/root/EverOS/.memo-vault/agent-training/agentlab/upstream/BrowserGym` | Git commit `27a90ce815098f43414e93491e06907af468e45b` | 单独安装的 `browsergym-core==0.14.4` 已通过预设动作短测；不等同于完整 BrowserGym 基准集。 |
| ADP 转换源码 | `/root/agent-data-protocol` | 当前 Git commit `040a279b46b2388ae42b43449f8645b9781c7bf7` | 仅读取当前 `agents/agentlab` 转换器；不能证明它就是产生固定 9,060 条发布 SFT 的源码版本。 |
| 现有浏览器环境 | `/root/EverOS/.memo-vault/agent-training/agentlab/browser-env` | Python 3.12；`browsergym-core==0.14.4`、`playwright==1.44.0` | 本机 2026-11-10 重跑预设 `fill` / `click`，输出 `Hello Ada`；并未调用 AgentLab 或模型。 |

AgentLab 源码声明 Python `>=3.11,<3.13`，其依赖是 `browsergym>=0.7.1` 而不是仅 `browsergym-core`。固定提交只是本项目**待验证的候选组合**，不是 ADP 论文和已发布文件对应版本的证明。浏览器环境不要当作已安装 AgentLab。

### 10.2 新服务器下载固定源码

先确认 Git、网络、磁盘可用；将 `AL_PROJECT` 指向已经包含项目脚本的 EverOS 仓库，将 `AL_RUN` 改成服务器上的新目录。脚本对已存在的源码目录不执行 `checkout`、不覆盖；HEAD 不符或工作区有改动时停止，由操作人员先核查。

```bash
cd /path/to/EverOS
export AL_PROJECT="$PWD"
export AL_RUN=/path/to/agentlab-run
export AL_AGENTLAB="$AL_RUN/upstream/AgentLab"
export AL_BROWSERGYM="$AL_RUN/upstream/BrowserGym"
export AL_ADP="$AL_RUN/upstream/agent-data-protocol"
mkdir -p "$AL_RUN/upstream"

fetch_fixed_repo() {
  local url="$1" dest="$2" commit="$3"
  if [ ! -e "$dest" ]; then
    git clone "$url" "$dest" || return 1
    git -C "$dest" checkout --detach "$commit" || return 1
  fi
  test -d "$dest/.git" || { echo "不是 Git 目录: $dest"; return 1; }
  test "$(git -C "$dest" rev-parse HEAD)" = "$commit" || { echo "提交不匹配: $dest"; return 1; }
  test -z "$(git -C "$dest" status --porcelain)" || { echo "工作区有变更，请先核查: $dest"; return 1; }
  git -C "$dest" rev-parse HEAD
}
fetch_fixed_repo https://github.com/ServiceNow/AgentLab.git "$AL_AGENTLAB" 8488c564eea0525aaff5dea189bb03a9b529546e || return 1 2>/dev/null || exit 1
fetch_fixed_repo https://github.com/ServiceNow/BrowserGym.git "$AL_BROWSERGYM" 27a90ce815098f43414e93491e06907af468e45b || return 1 2>/dev/null || exit 1
fetch_fixed_repo https://github.com/neulab/agent-data-protocol.git "$AL_ADP" 040a279b46b2388ae42b43449f8645b9781c7bf7 || return 1 2>/dev/null || exit 1
```

仅下载源码不会安装依赖。ADP `agents/agentlab/std_to_sft.py` 和 `suffix.txt` 定义当前检出的转换协议：观察及历史动作作为上下文，助手目标为含 `thought`、`action` 的 JSON。当前转换器文件 SHA-256：`std_to_sft.py` 为 `3a5bdac08bdc85465cb01d7d9528531822f3a53d6cae264d072607d14ae30460`，`suffix.txt` 为 `dbf0c4d2b9be2ec08dcabe1f5cce34d0173c99542857e5c1de97675dd8a457b0`。可查看并核验：

```bash
sha256sum "$AL_ADP/agents/agentlab/"{std_to_sft.py,suffix.txt}
cat "$AL_ADP/agents/agentlab/suffix.txt"
```

当前的项目解析器 `src/everos/agent_training/agentlab_action.py` 只接受有限 JSON 动作，尚未接入 AgentLab；已发布 Mind2Web SFT 中有 `type`、`select` 等动作，其语义与当前 `fill`、`select_option` 的兼容必须另行核验。固定本节提交和 JSON 格式不表示实际模型请求、解析与浏览器闭环已跑通。

### 10.3 单独验证 BrowserGym（可选的底层排障）

此步骤仅用于排查浏览器、页面观察及动作执行是否可用；它不安装 AgentLab，也不验证模型。用户希望验证框架调用工具时，应继续执行 10.4，不能以此步骤代替。

以下环境供可控本地页面的预设动作短测；仅在新环境中安装。`playwright` 在目标平台的依赖和 Chromium 下载仍需要联网，系统缺库则按实际错误处理。依赖未提供完整跨平台锁定，新机器的 `pip freeze` 用于记录实际解析结果。

```bash
export AL_BROWSER_ENV="$AL_RUN/browser-env"
export PLAYWRIGHT_BROWSERS_PATH="$AL_RUN/playwright-browsers"
test ! -e "$AL_BROWSER_ENV" || { echo '环境已存在，请先核查'; return 1 2>/dev/null || exit 1; }
python3.12 -m venv "$AL_BROWSER_ENV"
"$AL_BROWSER_ENV/bin/python" -m pip install "$AL_BROWSERGYM/browsergym/core" 'playwright==1.44.0'
"$AL_BROWSER_ENV/bin/python" -m pip check
"$AL_BROWSER_ENV/bin/python" -m playwright install chromium
PYTHONPATH="$AL_PROJECT/src" "$AL_BROWSER_ENV/bin/python" "$AL_PROJECT/scripts/agentlab_browsergym_smoke.py"
"$AL_BROWSER_ENV/bin/python" -m pip freeze > "$AL_RUN/browser-requirements.txt"
```

预期输出 `"ok": true`、`"result": "Hello Ada"`，元素 ID 每次可能变化。本机依赖快照在 `/root/EverOS/.memo-vault/agent-training/agentlab/browser-requirements-20261109.txt`。完整 AgentLab 安装、BrowserGym 全部依赖版本与 WebArena 服务需要单独在隔离环境验证并记录；不要将本节预设动作成功表述成模型自主执行成功。


### 10.4 AgentLab 真实接口调用浏览器工具短测（2026-11-10）

**10.2 只下载源码，不能单独证明 AgentLab 已安装或可调用工具。** 在本机新建测试环境后，运行 AgentLab 自带的 `MostBasicAgent.get_action()`：它读取 BrowserGym 页面观察、调用一个脚本模拟的聊天模型、解析返回的代码块并给出动作；BrowserGym 分别执行 `fill` 和 `click`，页面返回 `Hello Ada`。该测试调用了 AgentLab 实际类和浏览器，但回复由脚本预先提供，**未调用 Qwen、未验证模型自主选择动作或 ADP JSON 适配**。

版本依据：固定 AgentLab 提交的 `uv.lock` 使用 `browsergym==0.14.2`、`browsergym-core==0.14.2`、`browsergym-experiments==0.14.2` 和 `playwright==1.44.0`。10.3 使用的源码 core 0.14.4 是另一套单独浏览器测试，**不要混用两套安装环境**。本次 AgentLab 依赖安装固定了锁文件中若干关键版本，并未完整采用锁文件的全部版本。

本机使用 `/root/EverOS/.memo-vault/agent-training/agentlab/agentlab-validation-env-20261110/`，通过 `--system-site-packages` 借用了主机 Python 的 torch 2.5.1 等包。该环境中 AgentLab 的版本是 `0.0.1.dev1+g8488c564e`；`pip check` 检出继承的 `streamlit` 与 packaging / protobuf / rich，以及 `s3fs` 与 fsspec 的冲突。尝试修正这些包时出现新的系统包冲突，因此该环境**仅作为一次动作链路实验，不是干净、完整或可直接迁移的生产锁环境**。本机本地安装包记录为 `.memo-vault/agent-training/agentlab/agentlab-validation-requirements-local-20261110.txt`，依赖检查记录为同目录 `agentlab-validation-pip-check-20261110.txt`，二者不包含可完整复刻的系统包锁。

本机已验证的动作测试脚本为 `scripts/agentlab_framework_smoke.py`，需要随项目代码迁移。若要在具有相同依赖的现有环境复跑：

```bash
cd /root/EverOS
export AL_AGENT_ENV="$PWD/.memo-vault/agent-training/agentlab/agentlab-validation-env-20261110"
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.memo-vault/agent-training/agentlab/playwright-browsers"
"$AL_AGENT_ENV/bin/python" -m pip check
"$AL_AGENT_ENV/bin/python" scripts/agentlab_framework_smoke.py
```

本机输出 `ok: true`，动作 `fill('4', 'Ada')`、`click('5')`，结果 `Hello Ada`；ID 可能变化。第一条 `pip check` 当前会报告上述冲突，不能当作通过。

以下是本机本次使用的**安装实验命令**，不是已验证的干净新服务器安装方案；其中复用服务器全局包，不同服务器可能安装失败或产生不同冲突。先执行 10.2 设置 `AL_AGENTLAB`、`AL_RUN`，并确认 Python 3.12、pip、CUDA / torch 与空间；实验输出目录必须不存在：

```bash
export AL_AGENT_ENV="$AL_RUN/agentlab-validation-env"
test ! -e "$AL_AGENT_ENV" || { echo '环境已存在，请先核查'; return 1 2>/dev/null || exit 1; }
python3.12 -m venv --system-site-packages "$AL_AGENT_ENV"
"$AL_AGENT_ENV/bin/python" -m pip install --prefer-binary -e "$AL_AGENTLAB" \
  'browsergym==0.14.2' 'playwright==1.44.0' 'anthropic==0.62.0' \
  'litellm==1.75.3' 'gradio==5.41.1' 'mcp==1.10.1' \
  'pydantic==2.11.7' 'browsergym-workarena==0.4.3' 'weblinx-browsergym==0.0.3'
"$AL_AGENT_ENV/bin/python" -m pip check
export PLAYWRIGHT_BROWSERS_PATH="$AL_RUN/playwright-browsers"
"$AL_AGENT_ENV/bin/python" -m playwright install chromium
"$AL_AGENT_ENV/bin/python" "$AL_PROJECT/scripts/agentlab_framework_smoke.py"
```

后续须在干净隔离环境根据固定源码锁文件重建并通过 `pip check`，再验证 Qwen 服务接入、模型输出解析、浏览器实际执行和反馈闭环；WebArena 的服务部署和任务评测也尚未完成。不要将本次脚本预设回复当成模型自主调用成功。


### 10.5 新服务器：Qwen + AgentLab 自主闭环集成短测

这里的“闭环”是**集成短测**，不是最终评测：真实模型读取页面观察，AgentLab 的 `Agent` 接口内使用 ADP JSON 动作适配器解析输出，BrowserGym 执行动作，再把改变后的观察送回模型决定下一步。ADP 训练目标是 JSON，而固定版本的 `MostBasicAgent` 期待代码块，因此此脚本使用与训练目标一致的自定义 Agent；它仍调用 AgentLab 聊天模型接口和 BrowserGym action set。脚本在本地内存网页上要求模型先点击 Confirm，再根据新出现的 Continue 按钮继续操作；不访问外网网站，也不使用预设模型回答。它验证运行时链路，不计算 Mind2Web 的最终分数。

可执行代码已纳入项目：scripts/agentlab_qwen_loop.py。它只接受当前观察中唯一可见的目标按钮 ID 与单个 click 调用；模型输出不符合动作约束、浏览器报错或最后页面没有 Task complete 时，均以失败退出并保留结果 JSON。结果目录不能覆盖已有记录。执行前先按 10.2–10.4 准备 AgentLab、BrowserGym 和 Chromium；在另外的终端启动第十二节的 Qwen 服务及 loopback API 兼容代理。兼容代理将 AgentLab 请求的 max_completion_tokens 转成 LLaMA-Factory 0.9.5 所需的 max_tokens。闭环脚本会把 `127.0.0.1` 和 `localhost` 加入 `NO_PROXY` / `no_proxy`，避免服务器继承的 HTTP 代理拦截本地模型 API。

```bash
cd /path/to/EverOS
export AL_PROJECT="$PWD"
export AL_AGENT_ENV=/path/to/agentlab-python-env
export OPENAI_BASE_URL=http://127.0.0.1:18082/v1
export OPENAI_API_KEY=local
export AL_MODEL_NAME=qwen35
export AL_MODEL_REVISION=2fc06364715b967f1860aea9cf38778875588b17
export AL_MODEL_FINGERPRINT=从第十二节模型指纹命令取得的完整值
export AL_LOOP_DIR=/path/to/results/agentlab-loop-base
mkdir -p "$AL_LOOP_DIR"

curl --noproxy '*' --fail --silent --show-error "$OPENAI_BASE_URL/models"
"$AL_AGENT_ENV/bin/python" "$AL_PROJECT/scripts/agentlab_qwen_loop.py"
cat "$AL_LOOP_DIR/agentlab-qwen-loop-result.json"
```

通过时，结果 JSON 的 ok 为 true、steps 有两次由真实服务返回的 click 动作；第一步结果记录 BrowserGym 执行后的 Continue 按钮和新 bid，第二次模型请求使用更新后的无障碍树，最终观察包含 Task complete。请先对基座运行，再停止基座服务、启动 Adapter 服务，用不同的 AL_LOOP_DIR 重跑同一个集成短测。任何失败都保留原始 JSON 和服务日志。**这不是第十二节的最终 Mind2Web 测试集评测，也不能证明网站任务成功率。**

```json
{
  "evaluation_type": "agentlab_browsergym_integration_smoke",
  "model_name": "qwen35",
  "model_revision": "2fc06364715b967f1860aea9cf38778875588b17",
  "model_fingerprint": "记录的基座或 Adapter 完整文件指纹",
  "steps": [
    {"observed_button": "Confirm", "action": "click(实际观察ID)"},
    {"observed_button": "Continue", "action": "click(更新观察中的实际ID)"}
  ],
  "ok": true
}
```

## 十一、Qwen3.5 / LLaMA-Factory 不截断数据适配（2026-11-10）

本次对第九节的全量项目划分和第八节的短测候选，使用 Qwen3.5-0.8B 固定 revision `2fc06364715b967f1860aea9cf38778875588b17` 的 tokenizer / processor 与 LLaMA-Factory **0.9.5**（commit `7af909522a951e3ad9f022ea6f88b6755257eaa5`）的真实 `qwen3_5_nothink` 编码器生成 ShareGPT 候选。原 system、用户观察、助手 JSON 内容逐条原样保留；每条发布的助手消息作为一个候选目标，输入上下文标签被屏蔽。**原始 loss 标记无法恢复；这是适配候选的监督方式，不代表原始训练监督配置已证明一致。**

**没有截断，也没有按 token 长度删除样本。** 程序逐条检查原始 SFT 行哈希和第九节清单中的 ID / 划分，以足够大的编码预算记录完整 token、标签哈希；每条标记 `training_ready=false`。这一步没有训练、没有验证真实网页 ID 或动作语义，也没有批准将候选作为正式训练集。原审计中的 `source_id_unmatched` 是“未匹配旧清单”的历史原因，不意味着第九节全量原任务匹配失败。

| 数据 | train | validation | test | 排除 |
|---|---:|---:|---:|---:|
| 短测候选 | 200 | 28 | 23 | 0 |
| 全量候选 | 7,464 | 670 | 926 | 0 |

| 数据 | 划分 | 最长 token | 超过 2,048 | 超过 4,096 | 超过 8,192 | 超过 32,768 |
|---|---|---:|---:|---:|---:|---:|
| 短测 | train | 128,584 | 150 | 148 | 124 | 14 |
| 全量 | train | 203,676 | 5,912 | 5,697 | 4,516 | 411 |
| 全量 | validation | 81,675 | 511 | 492 | 351 | 92 |
| 全量 | test | 92,658 | 754 | 717 | 462 | 116 |

适配阶段不截断不代表训练器也不会截断：实际训练仍须设定 `cutoff_len`，并根据硬件和监督目标单独审核。上表最长 203,676 token，不能假定现有 4 GB 或其他机器能够训练；若设置低于样本长度的 cutoff，LLaMA-Factory 可能裁剪内容，不符合本次不截断目标。后续如需短测可选择完整且符合预算的样本，或另行讨论训练资源，不能静默裁剪。

**验证覆盖：**独立逐条对照了两套输入和输出的 system / user / assistant 内容与输出文件 SHA-256，全部一致。真实 LLaMA-Factory 数据加载器使用不低于被检查样本长度的 cutoff：短测 train **200/200**、validation **28/28** 的 token 和 label 哈希通过；全量 train 和 validation 各抽查前 **32** 条通过，**全量并非逐条加载核验**。test 仅保留独立文件，没有注册在 `dataset_info.json` 中，也未用作训练加载检查。测试用大 cutoff 仅用于 CPU 加载验证，不证明 GPU 可训练。

**仍待处理：**全量训练候选中包含 1 条动作表达式语法异常，以及 `type` / `select`、网页目标 ID 等尚待验证的样本；它们在本次无截断候选中保留，并在 `manifest.jsonl` / `report.json` 记录原审计原因，不视为已通过审核。正式训练前须确认监督策略、动作 / 元素语义、近重复隔离、训练与推理协议和硬件预算。`excluded.jsonl` 为空表示格式适配没有排除，不表示数据语义已验收。

### 11.1 文件位置与环境

| 内容 | 本机绝对路径 |
|---|---|
| 短测适配候选 | `/root/EverOS/.memo-vault/agent-training/agentlab/lf-smoke-untruncated-20261110-v1/` |
| 全量适配候选 | `/root/EverOS/.memo-vault/agent-training/agentlab/lf-full-untruncated-20261110-v1/` |
| Qwen 固定 revision 的 tokenizer / processor 配置 | `/root/EverOS/.memo-vault/agent-training/agentlab/qwen35-tokenizer-2fc06364/` |
| LLaMA-Factory 源码 | `/root/EverOS/.memo-vault/agent-training/agentlab/upstream/LLaMA-Factory/` |
| 本机数据处理 Python | `/root/EverOS/.memo-vault/agent-training/agentlab/lf-data-adapt-py312-v1/bin/python` |

各候选目录有 `train.jsonl`、`validation.jsonl`、`test.jsonl`、`manifest.jsonl`、`excluded.jsonl`、`dataset_info.json` 和 `report.json`。其中 `report.json` 的 `output_sha256` 记录文件校验值，`manifest.jsonl` 保存源行、任务、网站、token 数、标签哈希和审计原因。上述 tokenizer 目录**不含模型权重**；后续训练还需准备完整 Qwen 固定 revision 模型。`dataset_info.json` 只登记 train / validation；短测或正式训练不要把 test 写入训练配置。

本机数据处理环境使用 Python 3.12，继承了系统包，仅为本次编码和加载核验使用；`pip check` 报告缺少部分 LLaMA-Factory 非本任务依赖及继承包版本冲突，不是可直接用于 GPU 训练的完整、干净环境。局部依赖快照及 `pip check` 输出分别在 `.memo-vault/agent-training/agentlab/lf-data-adapt-py312-v1.local-freeze.txt` 和 `.memo-vault/agent-training/agentlab/lf-data-adapt-py312-v1.pip-check.txt`。本机系统 CUDA 库路径需要临时将 `/root/anaconda3/lib/python3.12/site-packages/nvidia/nvjitlink/lib` 置于 `LD_LIBRARY_PATH` 前面，否则导入 torch 出现 `__nvJitLinkComplete_12_4` 未定义；这属于本机环境差异，不是数据文件要求。

主要代码：`src/everos/agent_training/agentlab_untruncated.py`；加载器核验：`src/everos/agent_training/agentlab_untruncated_loader.py`；编码器复用 `src/everos/agent_training/swe_llamafactory.py`。这些代码及依赖模块须随项目提交或复制到新服务器，数据目录不会随 Git 自动迁移。

### 11.2 新服务器复刻适配与检查

先按第八、九节准备短测、全量原始划分与全量 `manifest.jsonl`，准备固定 LLaMA-Factory 0.9.5 环境，并确保 `pip check` 和模型 processor 兼容性在新服务器已核验。以下 `AL_PY` 是该环境的 Python。示例将固定 revision 的 tokenizer / processor 文件下载到单独目录；运行前确保目标路径和输出路径没有已有同名结果。

```bash
cd /path/to/EverOS
export AL_PROJECT="$PWD"
export AL_PY=/path/to/llamafactory-venv/bin/python
export AL_DATA=/path/to/agentlab-data
export AL_FULL="$AL_DATA/full-split-20261110-v1"
export AL_SMOKE="$AL_DATA/smoke-candidates-20261110-v1"
export AL_TOKENIZER="$AL_DATA/qwen35-tokenizer-2fc06364"
export AL_FULL_OUT="$AL_DATA/lf-full-untruncated-v1"
export AL_SMOKE_OUT="$AL_DATA/lf-smoke-untruncated-v1"

"$AL_PY" -c 'from importlib.metadata import version; print("LLaMA-Factory", version("llamafactory"))'
"$AL_PY" -m pip check
export AL_QWEN_TOKENIZER_DIR="$AL_TOKENIZER"
"$AL_PY" - <<'PY'
import os
from huggingface_hub import snapshot_download
snapshot_download(repo_id='Qwen/Qwen3.5-0.8B',
                  revision='2fc06364715b967f1860aea9cf38778875588b17',
                  local_dir=os.environ['AL_QWEN_TOKENIZER_DIR'],
                  allow_patterns=['*.json', '*.jinja', '*.txt'])
PY
sha256sum "$AL_TOKENIZER"/{tokenizer.json,tokenizer_config.json}
```

期望 `tokenizer.json` SHA-256 为 `5f9e4d4901a92b997e463c1f46055088b6cca5ca61a6522d1b9f64c4bb81cb42`，`tokenizer_config.json` 为 `49e2b6e395f959f077f1e992b338919c0d4a9732fc6e613995e06557f843500c`；与预期不一致时停止。仅下载配置不会下载模型权重。

先适配短测，再适配全量；输出目录必须不存在：

```bash
PYTHONPATH="$AL_PROJECT/src" "$AL_PY" -m everos.agent_training.agentlab_untruncated \
  --input-dir "$AL_SMOKE" --split-dir "$AL_FULL" --output-dir "$AL_SMOKE_OUT" \
  --model "$AL_TOKENIZER" --local-only
PYTHONPATH="$AL_PROJECT/src" "$AL_PY" -m everos.agent_training.agentlab_untruncated \
  --input-dir "$AL_FULL" --split-dir "$AL_FULL" --output-dir "$AL_FULL_OUT" \
  --model "$AL_TOKENIZER" --local-only
cat "$AL_SMOKE_OUT/report.json"
cat "$AL_FULL_OUT/report.json"
wc -l "$AL_SMOKE_OUT"/{train,validation,test,excluded}.jsonl
wc -l "$AL_FULL_OUT"/{train,validation,test,excluded}.jsonl
```

核对真实加载器：`131072` 覆盖本次短测训练和验证的最大长度；`262144` 覆盖本次全量所有记录，但命令只抽前 32 条 train / validation。若输入版本变化，必须先检查报告最大长度并适当调整；这里的 cutoff 不适用于实际训练配置。

```bash
PYTHONPATH="$AL_PROJECT/src" "$AL_PY" -m everos.agent_training.agentlab_untruncated_loader \
  --prepared-dir "$AL_SMOKE_OUT" --model "$AL_TOKENIZER" \
  --max-samples 1000 --cutoff-len 131072 > "$AL_DATA/lf-smoke-loader.log" 2>&1
tail -n 1 "$AL_DATA/lf-smoke-loader.log"
PYTHONPATH="$AL_PROJECT/src" "$AL_PY" -m everos.agent_training.agentlab_untruncated_loader \
  --prepared-dir "$AL_FULL_OUT" --model "$AL_TOKENIZER" \
  --max-samples 32 --cutoff-len 262144 > "$AL_DATA/lf-full-loader.log" 2>&1
tail -n 1 "$AL_DATA/lf-full-loader.log"
```

期望分别为 `{"train": 200, "validation": 28}` 与 `{"train": 32, "validation": 32}`。读取 `report.json` 中的 SHA-256 与 `sha256sum` 比对后，也可将完整候选目录复制到新服务器；本机目录与虚拟环境默认不随 Git 提交。训练与评测应在上述待处理事项完成后另行配置和记录，不应直接把本候选当作已验收训练集。

## 十二、QLoRA 短测、基座与 Adapter 配对评测（2026-11-11）

本节提供 AgentLab 专用训练和评测命令。评测分成两个互补部分：

1. **AgentLab + BrowserGym 本地自主闭环集成短测**：调用真实模型服务，让自定义 AgentLab Agent 按 ADP JSON 协议解析并执行动作，随后把浏览器反馈送回模型。第 10.5 节脚本对基座和 Adapter 各运行一次。这是运行时集成检查，不是 Mind2Web 最终分数。
2. **Mind2Web 保留 test 的离线下一动作模仿评测**：分别评测相同固定 revision 的基座模型和训练后的 LoRA Adapter，对同一份全量 test.jsonl、同一顺序的 926 条决策使用 temperature=0、max_completion_tokens=256。比较 JSON/动作解析率、动作类型准确率、规范化动作完全匹配率、整条轨迹全部动作匹配率。它不启动浏览器、不执行生成的动作、不代表实时网站任务成功率，也无法恢复原发布数据未知的 loss mask。

短测候选仍是 200/28/23 条，且所有样本 training_ready=false。短测只允许显式传 --probe-unverified-data，并应标记为技术性训练探针；不得将其 Adapter 或测试结果当作正式模型效果。scale/full 必须先有经过审核、报告和逐样本 manifest 均标记 training_ready=true 的数据。**当前全量报告仍为 training_ready=false，因而现在执行 scale/full 会被 runner 拒绝。** 不使用 test 训练或调参。

### 12.1 固定脚本、版本与环境

项目新增的核心文件：

- scripts/train_agentlab_qlora_smoke.sh、train_agentlab_qlora_scale.sh、train_agentlab_qlora_full.sh：短测、扩展和全量 QLoRA 启动器。
- src/everos/agent_training/agentlab_qlora.py：核验数据报告、候选文件哈希、train/validation manifest 顺序与样本数、token cutoff 和 training_ready；生成 training.yaml 与 run-report.json。任何训练/验证样本超 cutoff 时立即失败，不删样本，不允许截断参数。
- scripts/serve_agentlab_qwen35.sh：校验固定 Qwen tokenizer/config 身份、LLaMA-Factory 版本和 Adapter 文件，并将服务限制在 loopback。
- scripts/serve_agentlab_lf_api_proxy.sh 与 src/everos/agent_training/agentlab_api_compat.py：只转发 loopback 的 models / chat completions 路由，并转换 max_completion_tokens 参数。
- scripts/eval_agentlab_mind2web.py 与 src/everos/agent_training/agentlab_mind2web_eval.py：只读取 test.jsonl 和 test manifest，逐条保存模型响应并计算指标。
- scripts/compare_agentlab_mind2web.py：检查两次结果的完成状态、test 数据及 manifest 哈希、决策 ID 顺序和生成参数完全相同后才输出差值报告。
- scripts/agentlab_qwen_loop.py：第 10.5 节实际 AgentLab Agent + ADP JSON 动作适配器 + BrowserGym 模型闭环。

本文记录脚本用途、路径和运行命令；完整 Python / shell 源码保存在项目文件中，并未全部内嵌在本 Markdown。**本机检查时上述 AgentLab 新增文件仍未被 Git 跟踪**，单纯在新服务器 `git clone` 当前已提交版本不会获得这些文件。迁移前需将它们纳入项目版本管理，或连同项目源码一起复制；只复制本文不能运行训练和评测。可用下面的命令检查迁移前的状态：

```bash
cd /root/EverOS
git status --short -- docs/superpowers/plans/2026-11-09-vault-agentlab-qwen35.zh-CN.md scripts/*agentlab* src/everos/agent_training/agentlab_*.py
```

以下路径按新服务器修改。AL_SMOKE_OUT 和 AL_FULL_OUT 指向第十一节导出的、报告及文件 SHA-256 已核对的适配目录；完整权重目录必须下载固定 Qwen revision，不能使用只有 tokenizer 的目录。LLaMA-Factory 服务/训练用其训练环境；BrowserGym 闭环用装有固定 AgentLab 的环境；评测程序只用 Python 标准库，可用独立 control Python。

```bash
cd /path/to/EverOS
export AL_PROJECT="$PWD"
export AL_RUN=/path/to/agentlab-run
mkdir -p "$AL_RUN"
export AL_DATA="$AL_RUN/data/agentlab"
export AL_SMOKE_OUT="$AL_DATA/lf-smoke-untruncated-20261110-v1"
export AL_FULL_OUT="$AL_DATA/lf-full-untruncated-20261110-v1"
export AL_MODEL="$AL_RUN/model/qwen35-0.8b-2fc06364"
export AL_LF="$AL_RUN/upstream/LLaMA-Factory"
export AL_TRAIN_PY="$AL_RUN/llamafactory-venv/bin/python"
export AL_PYTHON="$AL_TRAIN_PY"
export AL_CONTROL_PYTHON="$AL_TRAIN_PY"
export AL_AGENT_ENV="$AL_RUN/agentlab-env"
export PYTHONPATH="$AL_PROJECT/src"

# 保存到 AL_RUN；后续每开一个终端，先 source 该文件恢复相同路径和环境。
cat > "$AL_RUN/agentlab-run.env" <<EOF
export AL_PROJECT="$AL_PROJECT"
export AL_RUN="$AL_RUN"
export AL_DATA="$AL_DATA"
export AL_SMOKE_OUT="$AL_SMOKE_OUT"
export AL_FULL_OUT="$AL_FULL_OUT"
export AL_MODEL="$AL_MODEL"
export AL_LF="$AL_LF"
export AL_TRAIN_PY="$AL_TRAIN_PY"
export AL_PYTHON="$AL_PYTHON"
export AL_CONTROL_PYTHON="$AL_CONTROL_PYTHON"
export AL_AGENT_ENV="$AL_AGENT_ENV"
export PYTHONPATH="$PYTHONPATH"
EOF
```

LLaMA-Factory 固定版本是 0.9.5，源码 commit 为 7af909522a951e3ad9f022ea6f88b6755257eaa5；AgentLab 固定 commit 为 8488c564eea0525aaff5dea189bb03a9b529546e。若还没有下载 LLaMA-Factory 源码：

```bash
mkdir -p "$AL_RUN/upstream"
git clone https://github.com/hiyouga/LLaMA-Factory.git "$AL_LF"
git -C "$AL_LF" checkout --detach 7af909522a951e3ad9f022ea6f88b6755257eaa5
git -C "$AL_LF" rev-parse HEAD
```

按 docs/vault-swe-llamafactory.zh-CN.md 准备与服务器 CUDA 匹配的 PyTorch、bitsandbytes 等依赖，再安装该固定源码并检查版本。不要在已有 GPU 环境中盲目升级 torch：

```bash
python3.12 -m venv "$AL_RUN/llamafactory-venv"
# 先按目标 CUDA 安装匹配的 PyTorch，再装固定 LLaMA-Factory。
"$AL_TRAIN_PY" -m pip install -e "$AL_LF"
"$AL_TRAIN_PY" -m pip check
"$AL_TRAIN_PY" -c 'from importlib.metadata import version; print(version("llamafactory")); assert version("llamafactory") == "0.9.5"'
```

完整 AgentLab 安装与干净锁环境仍需在新服务器实测；第 10.4 节列出的继承系统包实验环境不是干净部署方案。训练服务与 AgentLab 运行时可分开建环境，通过本机 OpenAI 兼容 API 通信。

### 12.2 下载并核对完整 Qwen3.5 模型

目标模型目录必须不存在；tokenizer 单独下载目录不能用于训练或服务。下载整个固定 revision：

```bash
test ! -e "$AL_MODEL" || { echo "model directory already exists; inspect it first: $AL_MODEL" >&2; exit 1; }
mkdir -p "$AL_MODEL"
"$AL_TRAIN_PY" - <<'PY'
import os
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id="Qwen/Qwen3.5-0.8B",
    revision="2fc06364715b967f1860aea9cf38778875588b17",
    local_dir=os.environ["AL_MODEL"],
)
PY
PYTHONPATH="$AL_PROJECT/src" "$AL_TRAIN_PY" -m everos.agent_training.agentlab_qwen_model --model "$AL_MODEL"
```

末条命令核对已记录的 tokenizer SHA-256、Qwen3.5 架构配置与权重文件，并打印覆盖该目录全部文件的 fingerprint。评测时将完整 fingerprint 原样记录。当前数据最大训练样本为 203,676 tokens；262,144 token QLoRA 对显存要求很高。必须在目标 GPU 实测显存、吞吐和 OOM 情况；本机 4 GiB 环境不运行训练。

### 12.3 短测 QLoRA（技术探针）

输出目录必须是新的目录。先用 prepare-only 核对数据文件哈希、200/28/23 条数量、token cutoff 和模型身份，不启动优化器：

```bash
export AL_SMOKE_RUN="$AL_RUN/runs/agentlab-smoke-probe-20261111"
bash scripts/train_agentlab_qlora_smoke.sh "$AL_SMOKE_OUT" "$AL_MODEL" "$AL_SMOKE_RUN" --probe-unverified-data --prepare-only
cat "$AL_SMOKE_RUN/run-report.json"
cat "$AL_SMOKE_RUN/training.yaml"
```

检查报告 counts 为 train=200、validation=28、test=23，cutoff_len=131072、probe_only=true、truncation_allowed=false、test_used_for_training=false 后，另指定一个新目录执行 10 个 optimizer steps，并每 5 步在 validation 上评估：

```bash
export AL_SMOKE_RUN="$AL_RUN/runs/agentlab-smoke-train-20261111"
bash scripts/train_agentlab_qlora_smoke.sh "$AL_SMOKE_OUT" "$AL_MODEL" "$AL_SMOKE_RUN" --probe-unverified-data
cat "$AL_SMOKE_RUN/run-report.json"
tail -n 80 "$AL_SMOKE_RUN/training.log"
```

脚本会要求 Adapter 含 adapter_config.json 和权重文件，trainer_state 有预期 global_step 与有限 training/eval loss；run-report.json 记录状态和 SHA-256。失败时保留目录及日志，用新的 run 目录重跑。AL_SMOKE_STEPS、AL_EVAL_STEPS 可改步数和验证频率。这个 Adapter 只用于检验依赖、显存、保存/加载和评测代码，不能用于正式效果结论。

runner 对完整样本 token 数做硬性 cutoff 检查；超限立即失败，不删除样本，也没有 allow-truncation 逃生开关。当前短测 train 最长 128,584，cutoff 为 131,072。数据更新后若有超限样本，先重新确认长度和资源预算，不能让训练器悄悄裁剪目标。

### 12.4 scale/full readiness gate

当前短测和全量候选报告均标记 training_ready=false，因为网页目标 ID/动作语义、发布文件来源和原始 loss mask 尚未全部核实。所以下列命令现在会被 readiness gate 拒绝，这是预期行为，不要加绕过选项。

完成逐样本审核、更新报告及 manifest 的 training_ready 标记、重新生成并独立核验文件哈希后，才运行 scale（50–100 steps）：

```bash
export AL_SCALE_RUN="$AL_RUN/runs/agentlab-scale-reviewed-v1"
AL_SCALE_STEPS=50 AL_EVAL_STEPS=10 bash scripts/train_agentlab_qlora_scale.sh "$AL_FULL_OUT" "$AL_MODEL" "$AL_SCALE_RUN"
```

full 使用 train=7,464、validation=670；test=926 只留给最终评测。当前 train 最大长度 203,676，cutoff 使用 262,144，不截断：

```bash
export AL_FULL_RUN="$AL_RUN/runs/agentlab-full-reviewed-v1"
AL_FULL_EPOCHS=1 AL_EVAL_STEPS=100 bash scripts/train_agentlab_qlora_full.sh "$AL_FULL_OUT" "$AL_MODEL" "$AL_FULL_RUN"
```

scale/full 只接受训练划分与验证划分每行都标记 training_ready=true 的数据，并会核验所有候选文件 SHA-256 和 token manifest。配置只注册 agentlab_train 与 agentlab_validation；test 没有数据集配置。Adapter 默认保存在 RUN_DIR/adapter。

### 12.5 启动模型服务和 AgentLab 闭环

模型 API 服务绑定 127.0.0.1:18081，兼容代理绑定 127.0.0.1:18082。分别在两个终端启动。第一终端先服务基座：

```bash
source /path/to/agentlab-run/agentlab-run.env
export AL_PYTHON="$AL_TRAIN_PY"
export AL_API_MODEL_NAME=qwen35
export AL_API_KEY=local
bash scripts/serve_agentlab_qwen35.sh "$AL_MODEL" base 18081
```

第二终端启动兼容代理：

```bash
source /path/to/agentlab-run/agentlab-run.env
export AL_CONTROL_PYTHON="$AL_TRAIN_PY"
bash scripts/serve_agentlab_lf_api_proxy.sh --upstream http://127.0.0.1:18081 --listen-host 127.0.0.1 --listen-port 18082
```

在 AgentLab 环境的第三终端核对 /v1/models 后，运行基座闭环短测。fingerprint 用上一节的命令计算；每次输出目录必须唯一：

```bash
source /path/to/agentlab-run/agentlab-run.env
export AL_PROJECT=/path/to/EverOS
export AL_AGENT_ENV=/path/to/agentlab-env
export OPENAI_BASE_URL=http://127.0.0.1:18082/v1
export OPENAI_API_KEY=local
export AL_MODEL_NAME=qwen35
export AL_MODEL_REVISION=2fc06364715b967f1860aea9cf38778875588b17
export AL_MODEL_FINGERPRINT=$("$AL_TRAIN_PY" -m everos.agent_training.agentlab_qwen_model --model "$AL_MODEL" | "$AL_CONTROL_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["fingerprint"])')
export AL_LOOP_DIR="$AL_RUN/integration/base"
mkdir -p "$AL_LOOP_DIR"
curl --noproxy '*' --fail --silent --show-error "$OPENAI_BASE_URL/models"
"$AL_AGENT_ENV/bin/python" "$AL_PROJECT/scripts/agentlab_qwen_loop.py"
```

第一轮成功后保存结果，然后停止基座服务。用已训练且经过审核的 full Adapter 启动服务；若只在验证接口时使用短测 Adapter，结果须标记为技术探针：

```bash
# 模型服务终端：停止基座服务后执行
source /path/to/agentlab-run/agentlab-run.env
export AL_FULL_RUN="$AL_RUN/runs/agentlab-full-reviewed-v1"
export AL_ADAPTER_DIR="$AL_FULL_RUN/adapter"
export AL_PYTHON="$AL_TRAIN_PY"
export AL_API_MODEL_NAME=qwen35
export AL_API_KEY=local
bash scripts/serve_agentlab_qwen35.sh "$AL_MODEL" adapter 18081 "$AL_ADAPTER_DIR"
```

在 AgentLab 终端对同一个本地页面再次运行，使用新的结果目录和包含 Adapter 文件哈希的 fingerprint：

```bash
source /path/to/agentlab-run/agentlab-run.env
export AL_PROJECT=/path/to/EverOS
export AL_AGENT_ENV=/path/to/agentlab-env
export OPENAI_BASE_URL=http://127.0.0.1:18082/v1
export OPENAI_API_KEY=local
export AL_MODEL_NAME=qwen35
export AL_MODEL_REVISION=2fc06364715b967f1860aea9cf38778875588b17
export AL_FULL_RUN="$AL_RUN/runs/agentlab-full-reviewed-v1"
export AL_ADAPTER_DIR="$AL_FULL_RUN/adapter"
export AL_MODEL_FINGERPRINT=$("$AL_TRAIN_PY" -m everos.agent_training.agentlab_qwen_model --model "$AL_MODEL" --adapter "$AL_ADAPTER_DIR" | "$AL_CONTROL_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["fingerprint"])')
export AL_LOOP_DIR="$AL_RUN/integration/adapter-full"
mkdir -p "$AL_LOOP_DIR"
"$AL_AGENT_ENV/bin/python" "$AL_PROJECT/scripts/agentlab_qwen_loop.py"
cat "$AL_LOOP_DIR/agentlab-qwen-loop-result.json"
```

两次结果的 `ok` 都为 true，且模型指纹分别指向基座与 Adapter，才算两套运行时链路均通过。这个对照仍是本地集成短测，不替代保留 Mind2Web test 上的最终配对评测。

**本机接口验证记录（2026-11-11）：**在已有固定 AgentLab / BrowserGym 环境中，通过临时 OpenAI 兼容 fixture 服务完成两次请求、两次实际 BrowserGym 点击、更新观察和最终页面断言；报告 `ok=true`，第二次请求前观察到新 Continue bid。此 fixture 返回预设 JSON，只验证 AgentLab 接口、JSON 解析、浏览器执行和反馈线路，**不是 Qwen 推理，也没有比较基座与 Adapter 的模型效果**。新服务器仍须分别按上面的命令接入真实基座和 Adapter 服务。

### 12.6 Mind2Web test：微调前后配对评测

正式评测对全量 test.jsonl 的 926 条决策逐一调用服务；只使用 test manifest 生成 ID/轨迹清单，提示只包含每条原始 system 与 user observation，gold action 只在本地评分。评测器不会执行预测的 Python，也不会用 train/validation 提示模型。

评测器完整性已在本机临时 API 上验证：base 和 adapter 流程各处理 926 条（合计 1,852 次 fixture 请求），比较器确认测试数据、manifest、ID 顺序与生成参数一致并生成成对报告。fixture 不代表模型效果；真实 Qwen 基线、训练后 Adapter 评测尚未运行。

先启动基座服务和代理，再计算基座 fingerprint 并评测。结果目录必须不存在：

```bash
source /path/to/agentlab-run/agentlab-run.env
export OPENAI_BASE_URL=http://127.0.0.1:18082/v1
export OPENAI_API_KEY=local
export AL_MODEL_NAME=qwen35
export AL_BASE_FINGERPRINT=$("$AL_TRAIN_PY" -m everos.agent_training.agentlab_qwen_model --model "$AL_MODEL" | "$AL_CONTROL_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["fingerprint"])')
AL_CONTROL_PYTHON="$AL_CONTROL_PYTHON" bash scripts/run_agentlab_base_baseline.sh "$AL_FULL_OUT" "$AL_RUN/eval/base-full-test" "$AL_BASE_FINGERPRINT"
```

然后停止基座服务，在模型服务终端启动加载 full Adapter 的服务：

```bash
source /path/to/agentlab-run/agentlab-run.env
export AL_FULL_RUN="$AL_RUN/runs/agentlab-full-reviewed-v1"
export AL_ADAPTER_DIR="$AL_FULL_RUN/adapter"
export AL_PYTHON="$AL_TRAIN_PY"
export AL_API_MODEL_NAME=qwen35
export AL_API_KEY=local
bash scripts/serve_agentlab_qwen35.sh "$AL_MODEL" adapter 18081 "$AL_ADAPTER_DIR"
```

在另一个评测终端等待 Adapter 服务就绪；各终端环境变量不共享，因此这里重新设置路径并计算包含 Adapter 文件哈希的 fingerprint，再对相同 test 评测和比较：

```bash
source /path/to/agentlab-run/agentlab-run.env
export OPENAI_BASE_URL=http://127.0.0.1:18082/v1
export OPENAI_API_KEY=local
export AL_MODEL_NAME=qwen35
export AL_FULL_RUN="$AL_RUN/runs/agentlab-full-reviewed-v1"
export AL_ADAPTER_DIR="$AL_FULL_RUN/adapter"
export AL_ADAPTER_FINGERPRINT=$("$AL_TRAIN_PY" -m everos.agent_training.agentlab_qwen_model --model "$AL_MODEL" --adapter "$AL_ADAPTER_DIR" | "$AL_CONTROL_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["fingerprint"])')
"$AL_CONTROL_PYTHON" scripts/eval_agentlab_mind2web.py --data-dir "$AL_FULL_OUT" --model-tag adapter --api-url "$OPENAI_BASE_URL" --api-key "$OPENAI_API_KEY" --model-name "$AL_MODEL_NAME" --model-fingerprint "$AL_ADAPTER_FINGERPRINT" --output-dir "$AL_RUN/eval/adapter-full-test"
"$AL_CONTROL_PYTHON" scripts/compare_agentlab_mind2web.py --base-report "$AL_RUN/eval/base-full-test/report.json" --adapter-report "$AL_RUN/eval/adapter-full-test/report.json" --output "$AL_RUN/eval/base-vs-adapter-full-test.json"
cat "$AL_RUN/eval/base-vs-adapter-full-test.json"
```

两份 report 都必须 status=complete，且 test.jsonl 哈希、完整 manifest 哈希、ordered_test_ids 和生成参数必须相同，否则 compare 拒绝比较。predictions.jsonl 保存每条原始响应及 usage；report.json 保存模型 fingerprint、模型名、revision、测试哈希、无效 JSON/动作、解析率和匹配率。Gold action 解析失败会计入 invalid_target_count，不会静默从记录删除。API 中断会保留已完成预测及 failed 报告；请换新的输出目录重跑完整评测，配对比较不会接受失败报告。

指标分母须一起阅读：`action_type_accuracy` 的分母是 gold 和预测动作均可解析的决策数 `scored_decision_count`，因此须同时查看覆盖全部测试决策的 `action_parse_rate`；`action_exact_match` 的分母是可解析的 gold 数，无法解析的预测不算匹配。不要只看条件动作类型准确率而忽略解析失败。

### 12.7 如何解释两块结果

Mind2Web test 上的 base 与 full Adapter 离线评分是微调前/后的**最终配对评测**，必须在方案与 Adapter 固定后运行；第 10.5 节基座与 Adapter 的 AgentLab + BrowserGym 本地页闭环是**运行时工具集成检查**。前者回答“微调后模型对相同保留决策的下一动作模仿表现相对基座如何”，后者回答“模型能否按训练协议输出动作，经 AgentLab Agent 接口解析、由 BrowserGym 执行，并利用更新观察继续行动”。两者都不自动证明真实网站完整任务成功率；若要报告 live benchmark，仍需建立经验证的任务重放、页面/动作 ID 映射和任务成功判据。

### 12.8 训练与评测的耗时、进度和可视化

现有脚本已分别记录完成后的 `elapsed_seconds`，但**没有自动汇总完整实验的总墙钟时间或评测剩余时间**：

| 阶段 | 结果位置 | 计时范围 |
|---|---|---|
| 训练 | `$AL_FULL_RUN/run-report.json` 的 `elapsed_seconds` | 大约从启动 LLaMA-Factory 子进程至其退出并初步检查训练结果，包含该进程内的模型加载、训练和验证；不包含启动前的数据及模型核验。正常退出或子进程返回错误后记录。 |
| 基座评测 | `$AL_RUN/eval/base-full-test/report.json` 的 `elapsed_seconds` | 测试数据加载完成后，发送全部请求并计算指标的时间；不包含服务启动和测试数据加载。普通请求异常时的失败报告也有耗时。 |
| Adapter 评测 | `$AL_RUN/eval/adapter-full-test/report.json` 的 `elapsed_seconds` | 与基座评测使用相同计时范围。 |

`--prepare-only` 不启动训练，因此没有训练耗时；运行中的训练报告尚无最终 `elapsed_seconds`。进程被强制终止、训练启动前核验失败，或评测在创建报告之前失败时，可能没有最终耗时，不能把缺失值当作零。比较报告目前不携带两次耗时，需读取各自的 `report.json`。

如需记录整个实验区间的墙钟时间，可在新实验目录准备好、开始训练之前执行下面第一条命令，在基座和 Adapter 两次评测及比较都完成后执行第二条。它们分别记录 UTC 时间戳；两次之间的等待、服务启动、暂停也会计入。重复执行会覆盖对应标记，请针对一次实验使用独立 `AL_RUN`：

```bash
date -u +%s > "$AL_RUN/experiment-start.epoch"
```

两次评测及比较都完成后，再执行：

```bash
date -u +%s > "$AL_RUN/experiment-end.epoch"
```

训练期间可在另一个终端观察日志。固定 LLaMA-Factory 的 `adapter/trainer_log.jsonl` 在实际开始训练并产生日志后逐条写入 `current_steps`、`total_steps`、`percentage`、`elapsed_time`、`remaining_time` 及 loss；剩余时间是按当前平均速度计算的估计值。`training.log` 也包含完整训练输出：

```bash
source /path/to/agentlab-run/agentlab-run.env
export AL_FULL_RUN="$AL_RUN/runs/agentlab-full-reviewed-v1"
tail -F "$AL_FULL_RUN/adapter/trainer_log.jsonl"
# 另一个终端也可查看完整日志：tail -F "$AL_FULL_RUN/training.log"
```

短测时将 `AL_FULL_RUN` 改为对应短测运行目录。评测运行时，`predictions.jsonl` 每完成一条就刷新；文件出现后在另一个终端查看当前行数，完整 test 应为 926 行。此方法显示完成数量，当前评测脚本不计算实时 ETA：

```bash
source /path/to/agentlab-run/agentlab-run.env
watch -n 5 wc -l "$AL_RUN/eval/base-full-test/predictions.jsonl"
# Adapter 评测改看 "$AL_RUN/eval/adapter-full-test/predictions.jsonl"
```

训练和两次评测结束后，以下命令把已有报告整理成 CSV。默认读取 full 训练目录；如需汇总短测，将 `AL_TIMING_TRAIN_REPORT` 指向该短测目录的 `run-report.json`。命令保留未完成/缺失状态，只有三项都完成且都存在耗时时才输出阶段耗时之和；该和仍不包括下载、准备和服务启动时间。若手动保存了上述开始、结束标记，另输出两次标记之间的 `experiment_wall`。重复执行会覆盖这个派生的 CSV：

```bash
source /path/to/agentlab-run/agentlab-run.env
export AL_TIMING_TRAIN_REPORT="$AL_RUN/runs/agentlab-full-reviewed-v1/run-report.json"
"$AL_CONTROL_PYTHON" - <<'PY'
import csv
import json
import os
from pathlib import Path

run = Path(os.environ["AL_RUN"])
paths = [
    ("training", Path(os.environ["AL_TIMING_TRAIN_REPORT"])),
    ("base_eval", run / "eval/base-full-test/report.json"),
    ("adapter_eval", run / "eval/adapter-full-test/report.json"),
]
rows = []
for phase, path in paths:
    report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    seconds = report.get("elapsed_seconds")
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        seconds = None
    rows.append({"phase": phase, "status": report.get("status", "missing"),
                 "elapsed_seconds": seconds if seconds is not None else "",
                 "elapsed_minutes": round(seconds / 60, 2) if seconds is not None else ""})
if all(row["status"] == "complete" and row["elapsed_seconds"] != "" for row in rows):
    total = sum(row["elapsed_seconds"] for row in rows)
    rows.append({"phase": "measured_stage_sum", "status": "complete",
                 "elapsed_seconds": round(total, 2), "elapsed_minutes": round(total / 60, 2)})
start_path = run / "experiment-start.epoch"
end_path = run / "experiment-end.epoch"
if start_path.exists() and end_path.exists():
    wall = int(end_path.read_text().strip()) - int(start_path.read_text().strip())
    if wall < 0:
        raise ValueError("experiment end precedes start; check timestamps")
    rows.append({"phase": "experiment_wall", "status": "complete",
                 "elapsed_seconds": wall, "elapsed_minutes": round(wall / 60, 2)})
output = run / "timing-summary.csv"
with output.open("w", encoding="utf-8", newline="") as stream:
    writer = csv.DictWriter(stream, fieldnames=["phase", "status", "elapsed_seconds", "elapsed_minutes"])
    writer.writeheader()
    writer.writerows(rows)
print(output)
for row in rows:
    print(row)
PY
```

CSV 可直接导入电子表格；如环境中有 `matplotlib`，也可用以下命令生成 `timing-summary.png`。图展示已有耗时的各阶段和可选的墙钟区间，不把阶段和单独作为一栏；标题和标签使用英文以避免服务器缺少中文字体。没有耗时数据时不会生成图，重复执行会覆盖派生的 PNG：

```bash
"$AL_CONTROL_PYTHON" - <<'PY'
import csv
import os
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

run = Path(os.environ["AL_RUN"])
with (run / "timing-summary.csv").open(encoding="utf-8", newline="") as stream:
    rows = [row for row in csv.DictReader(stream)
            if row["phase"] != "measured_stage_sum" and row["elapsed_seconds"]]
if not rows:
    print("No measured durations yet; no chart generated.")
else:
    fig, ax = plt.subplots(figsize=(7, 4))
    bars = ax.bar([row["phase"] for row in rows], [float(row["elapsed_minutes"]) for row in rows])
    ax.bar_label(bars, fmt="%.2f min")
    ax.set_ylabel("Minutes")
    ax.set_title("AgentLab training and evaluation duration")
    fig.tight_layout()
    output = run / "timing-summary.png"
    fig.savefig(output, dpi=160)
    plt.close(fig)
    print(output)
PY
```

## 十三、24 GB 新服务器：AgentLab 微调与评测速查（2026-11-11）

本节按执行顺序汇总；固定版本、完整环境说明和故障处理仍以对应章节为准。**目前尚未在这台新服务器上执行训练或评测。**短测是未审核数据的技术探针；全量数据报告仍为 `training_ready=false`，正式 scale/full 命令目前会拒绝运行，需先完成第 12.4 节的审核和门禁要求。24 GB 不保证能够容纳不截断的长样本：短测 train 最长约 128,584 token，全量 train 最长约 203,676 token；先实测显存，OOM 时保留日志，不能静默截断或删行。

### 13.1 从本机传输已适配数据

**在旧机器 `/root/EverOS` 上执行。**将用户名、IP 和新服务器绝对路径替换为实际值；示例路径不要包含空格或 shell 特殊字符。新服务器需能通过 SSH 登录；非默认端口分别给 `ssh` 加 `-p 端口`、给 `scp` 加 `-P 端口`。两份目录合计约 416 MiB，不包含模型或项目源码：

```bash
export AL_NEW_HOST=user@NEW_SERVER_IP
export AL_NEW_RUN=/data/agentlab-run

# 预先创建父目录；若目标数据目录已存在则停止，先检查，避免混合旧文件。
ssh "$AL_NEW_HOST" "mkdir -p '$AL_NEW_RUN/data/agentlab' && test ! -e '$AL_NEW_RUN/data/agentlab/lf-smoke-untruncated-20261110-v1' && test ! -e '$AL_NEW_RUN/data/agentlab/lf-full-untruncated-20261110-v1'" || { echo '目标目录已存在或 SSH 失败；请检查后停止'; return 1 2>/dev/null || exit 1; }

scp -r /root/EverOS/.memo-vault/agent-training/agentlab/lf-smoke-untruncated-20261110-v1 "$AL_NEW_HOST:$AL_NEW_RUN/data/agentlab/"
scp -r /root/EverOS/.memo-vault/agent-training/agentlab/lf-full-untruncated-20261110-v1 "$AL_NEW_HOST:$AL_NEW_RUN/data/agentlab/"
```

若传输中断，先检查目标上的不完整目录；确认后清理或换新的目标目录再重传。传输的是适配后的 train / validation / test、manifest、报告及数据集配置。短测为 **200 / 28 / 23** 条，全量为 **7,464 / 670 / 926** 条；test 只用于固定方案后的最终评测。

项目源码也必须迁移：第 12.1 节列出的 AgentLab 脚本和 Python 模块目前在旧机器是 Git 未跟踪文件。将这些文件纳入版本管理后拉取，或复制包含它们的完整 EverOS 项目源码；仅传数据和 Markdown 无法运行脚本。

### 13.2 新服务器：设置路径并核验传输

先确认 `nvidia-smi` 能看到约 24 GB 显存、磁盘空间充足。按**第 12.1 节**准备项目目录、固定 LLaMA-Factory 0.9.5 环境和 `agentlab-run.env`；其中 `AL_RUN` 必须设为上面的 `/data/agentlab-run`，`AL_DATA` 为 `$AL_RUN/data/agentlab`，并按实际情况设置已有模型路径 `AL_MODEL`。每个新终端先 `source /data/agentlab-run/agentlab-run.env`。

在**新服务器**执行下列核验。报告哈希对应本次从旧机器复制的原始候选；如果之后经过审核而生成了新的报告，应改用审核产物自己的哈希，不要继续套用这两个旧值：

```bash
source /data/agentlab-run/agentlab-run.env
nvidia-smi
df -h "$AL_RUN"

printf '%s  %s\n' \
  b68be305e7f0b0a5a7dfdcbe500d227a9eb8bb1801712c3f29714f636e47f769 "$AL_SMOKE_OUT/report.json" \
  b7b2e8399c2b6f0adaa5583d25d6abd142651e71cc041aef99f15366df0f94e9 "$AL_FULL_OUT/report.json" | sha256sum -c -

"$AL_CONTROL_PYTHON" - <<'PY'
import hashlib
import json
import os
from pathlib import Path

for key in ("AL_SMOKE_OUT", "AL_FULL_OUT"):
    directory = Path(os.environ[key])
    report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    for name, expected in report["output_sha256"].items():
        digest = hashlib.sha256()
        with (directory / name).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise SystemExit(f"SHA-256 不匹配: {directory / name}")
    print(key, "hashes OK", "training_ready=", report.get("training_ready"),
          "counts=", {k: v["samples"] for k, v in report["counts"].items()})
PY
```

### 13.3 Qwen3.5 和 AgentLab 环境

**Qwen3.5：**新服务器已经有完整 `Qwen/Qwen3.5-0.8B` 固定 revision 时不重新下载，把第 12.1 节的 `AL_MODEL` 指向该目录；没有时按**第 12.2 节**下载。以下命令核验已记录的 tokenizer 哈希和模型配置，并检查权重分片是否齐全；模型完整权重本身的固定 revision 仍须结合下载来源确认：

```bash
PYTHONPATH="$AL_PROJECT/src" "$AL_TRAIN_PY" -m everos.agent_training.agentlab_qwen_model --model "$AL_MODEL"
"$AL_TRAIN_PY" - <<'PY'
import json
import os
from pathlib import Path
model = Path(os.environ["AL_MODEL"])
index = model / "model.safetensors.index.json"
if index.is_file():
    names = set(json.loads(index.read_text(encoding="utf-8"))["weight_map"].values())
    if not names or any(not (model / name).is_file() for name in names):
        raise SystemExit("模型权重分片缺失；请检查已有模型目录")
    print("weight shards present:", len(names))
elif (model / "model.safetensors").is_file():
    print("single model.safetensors present")
else:
    raise SystemExit("模型权重缺失；请按 12.2 下载完整模型")
PY
```

**AgentLab / BrowserGym：**按**第 10.2 节**下载并核对固定源码；源码下载不等于安装。按**第 10.4 节**准备环境、Chromium 并运行 `pip check`；该节安装命令是本机实验配方，干净新服务器的依赖解析仍需实测，不能将继承系统包环境当作已锁定环境。第 10.3 节仅供浏览器底层排障。

**框架工具短测：**第 10.4 节有完整测试代码，假设使用该节的环境路径，在新服务器执行：

```bash
export AL_AGENT_ENV="$AL_RUN/agentlab-validation-env"
export PLAYWRIGHT_BROWSERS_PATH="$AL_RUN/playwright-browsers"
"$AL_AGENT_ENV/bin/python" "$AL_PROJECT/scripts/agentlab_framework_smoke.py"
```

预期 `ok: true`、结果 `Hello Ada`；这是预设回复驱动的 AgentLab + BrowserGym 测试。真实 Qwen 自主动作 → 浏览器反馈的集成短测，按**第 10.5、12.5 节**分别对基座和训练后的 Adapter 执行。

### 13.4 短测与完整训练

按**第 12.3 节**先核对配置，再运行短测；先执行 `--prepare-only`，实际训练必须使用新的输出目录：

```bash
source /data/agentlab-run/agentlab-run.env
cd "$AL_PROJECT"
export AL_PYTHON="$AL_TRAIN_PY"
export AL_SMOKE_PREP="$AL_RUN/runs/agentlab-smoke-prepare-20261111"
bash scripts/train_agentlab_qlora_smoke.sh "$AL_SMOKE_OUT" "$AL_MODEL" "$AL_SMOKE_PREP" --probe-unverified-data --prepare-only
cat "$AL_SMOKE_PREP/run-report.json"

export AL_SMOKE_RUN="$AL_RUN/runs/agentlab-smoke-train-20261111"
bash scripts/train_agentlab_qlora_smoke.sh "$AL_SMOKE_OUT" "$AL_MODEL" "$AL_SMOKE_RUN" --probe-unverified-data
cat "$AL_SMOKE_RUN/run-report.json"
```

默认短测 10 个优化器步骤；`--probe-unverified-data` 只允许技术短测，不代表数据审核通过。短测通过并完成审核后，再按**第 12.4 节**先做 scale（50–100 步）再全量训练。下面完整训练命令需要全量报告及 train / validation 的 manifest 都有经审核的 `training_ready=true`，当前候选会被拒绝：

```bash
export AL_FULL_RUN="$AL_RUN/runs/agentlab-full-reviewed-v1"
AL_FULL_EPOCHS=1 AL_EVAL_STEPS=100 bash scripts/train_agentlab_qlora_full.sh "$AL_FULL_OUT" "$AL_MODEL" "$AL_FULL_RUN"
cat "$AL_FULL_RUN/run-report.json"
```

最终 Adapter 路径为 `$AL_FULL_RUN/adapter`；训练日志、运行时间、loss 和进度见**第 12.8 节**。如果要统计整个实验的墙钟时间，训练前先按该节记录开始标记。

### 13.5 最终评测：基座与微调后 Adapter

方案和 Adapter 确定后，按**第 12.5、12.6 节**在独立终端启动模型服务和代理：先用基座服务评测完整 test；停止基座服务，改用相同基座 + full Adapter；对相同 926 条 test 再评测并比较。两次都必须使用相同数据、生成参数及顺序，test 不用于训练或调参。以下是核心评测命令；两个服务与代理的启动命令、各终端完整环境变量设置见第 12.5–12.6 节：

```bash
source /data/agentlab-run/agentlab-run.env
cd "$AL_PROJECT"
export OPENAI_BASE_URL=http://127.0.0.1:18082/v1
export OPENAI_API_KEY=local
export AL_MODEL_NAME=qwen35
export AL_FULL_RUN="$AL_RUN/runs/agentlab-full-reviewed-v1"
export AL_ADAPTER_DIR="$AL_FULL_RUN/adapter"

# 基座服务就绪时执行；结果目录必须不存在。
export AL_BASE_FINGERPRINT=$("$AL_TRAIN_PY" -m everos.agent_training.agentlab_qwen_model --model "$AL_MODEL" | "$AL_CONTROL_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["fingerprint"])')
bash scripts/run_agentlab_base_baseline.sh "$AL_FULL_OUT" "$AL_RUN/eval/base-full-test" "$AL_BASE_FINGERPRINT"
```

**先停止基座服务并按第 12.6 节启动 Adapter 服务**，确认服务就绪，再在评测终端执行：

```bash
export AL_ADAPTER_FINGERPRINT=$("$AL_TRAIN_PY" -m everos.agent_training.agentlab_qwen_model --model "$AL_MODEL" --adapter "$AL_ADAPTER_DIR" | "$AL_CONTROL_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["fingerprint"])')
"$AL_CONTROL_PYTHON" scripts/eval_agentlab_mind2web.py --data-dir "$AL_FULL_OUT" --model-tag adapter --api-url "$OPENAI_BASE_URL" --api-key "$OPENAI_API_KEY" --model-name "$AL_MODEL_NAME" --model-fingerprint "$AL_ADAPTER_FINGERPRINT" --output-dir "$AL_RUN/eval/adapter-full-test"
"$AL_CONTROL_PYTHON" scripts/compare_agentlab_mind2web.py --base-report "$AL_RUN/eval/base-full-test/report.json" --adapter-report "$AL_RUN/eval/adapter-full-test/report.json" --output "$AL_RUN/eval/base-vs-adapter-full-test.json"
cat "$AL_RUN/eval/base-vs-adapter-full-test.json"
```

结果是保留集的离线下一动作配对评测，不是实时网站任务成功率。完成后按**第 12.8 节**记录实验结束时间，并导出训练 / 两次评测的耗时 CSV 和可选 PNG。test 中最长样本也超过 9 万 token，24 GB 服务能否完成推理同样需要实际验证。
