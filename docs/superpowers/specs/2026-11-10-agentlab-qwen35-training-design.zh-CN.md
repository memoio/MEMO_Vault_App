# AgentLab + Qwen3.5 基座闭环与 QLoRA 训练设计

状态：待用户核阅。此设计以 AgentLab 计划第 10、11 节的现有版本和数据产物为依据；OpenHands 文档只提供步骤组织方式。

## 意图

为 Mind2Web 的 AgentLab 实验准备可在新服务器复刻的模型服务、真实 AgentLab 基座闭环、QLoRA 短测和扩大训练入口。训练仍由固定版 LLaMA-Factory 执行；AgentLab + BrowserGym 负责模型基线和真实浏览器动作验证。目标是保持已适配的数据行数与内容不变，并防止 LLaMA-Factory 因 `cutoff_len` 静默裁剪样本。

## 已核对的现状

- Qwen3.5-0.8B tokenizer / processor revision 为 `2fc06364715b967f1860aea9cf38778875588b17`；LLaMA-Factory 为 0.9.5，commit `7af909522a951e3ad9f022ea6f88b6755257eaa5`；AgentLab 为 commit `8488c564eea0525aaff5dea189bb03a9b529546e`。
- `agentlab_untruncated.py` 已按 `qwen3_5_nothink` 模板生成 ShareGPT 候选；没有按 token 长度筛行，也没有截断。短测是 train/validation/test = 200/28/23，全量是 7,464/670/926。每条候选仍是 `training_ready=false`，因为原始监督标记、来源证据及部分网页动作语义尚未核实。
- 短测 train 最长 128,584 tokens、validation 最长 12,308；全量 train 最长 203,676、validation 最长 81,675。固定 Qwen 配置中的文本上下文上限是 262,144。LLaMA-Factory 0.9.5 的 SFT 处理器在样本超过 `cutoff_len` 时会缩短 source 和/或 target；因此训练入口必须扫描完整 manifest，并要求 cutoff 覆盖所有 train / validation 样本。拟用短测 `131072`、全量 `262144`，均不减行、不截断。若硬件无法承载该上下文长度，训练应在预检失败并记录资源不足，不得自动降 cutoff 或删行。
- 已用真实 LLaMA-Factory 加载器在 `cutoff_len=262144` 下逐条验证全量 train 7,464 条和 validation 670 条，token / label 哈希均与适配 manifest 一致。短测 200/28 条此前已在 `131072` 下逐条核验。test 不登记为训练或验证数据。
- `agentlab_llamafactory.py` 是另一个“显式逐条审核后导出”的工具；它只导出审核记录，且超过给定 `max_tokens` 的审核项会被排除。它不是当前不截断候选的生成入口，不能拿它的筛选行为替代本设计。
- OpenHands QLoRA 训练器复用了通用 LLaMA-Factory 训练调用，但其中的数据来源、dataset 名称、manifest/report 字段和 AgentLab 无法直接通用。OpenHands 基座评测器则调用 OpenHands，不适用于 AgentLab。OpenAI API 请求兼容层可以复用其字段映射思路：AgentLab 的 `OpenAIModelArgs` 会发送 `max_completion_tokens`，而 LLaMA-Factory 0.9.5 API schema 使用 `max_tokens`，需通过仅监听 loopback 的轻量代理转换。

## 目标与边界

- 保持现有短测和全量候选文件原样；不重抽、不丢弃长样本、不分块扩充行数、不截断。
- 提供 AgentLab 专用的 Qwen 基座/adapter 本地 API 服务入口、`MostBasicAgent` + BrowserGym 自主闭环脚本，以及 QLoRA smoke / scale / full 训练入口。
- 评测分为两类：本地单页 base/adapter 闭环烟测只确认模型接入和动作执行链路；最终结果使用相同的 Mind2Web 保留测试清单分别评估微调前 base 和微调后 adapter，并对齐逐条结果比较。
- Mind2Web 离线评测使用同一 test prompt 调用两个模型，统计 JSON/action 可解析率、动作类型准确率、规范化动作 exact match 和按 trajectory 汇总的全动作匹配率。结果定义为“对发布 SFT 目标的下一动作模仿”，不是实时网站任务成功率；由于原始 loss 标记仍未知，此限制随结果报告。
- QLoRA 预检验证模型与 tokenizer 哈希、LLaMA-Factory 版本、数据 SHA-256、manifest 顺序、train/validation 计数、每条 token 长度和 `training_ready` 状态；训练配置不加入 test。
- smoke 只允许显式的 `probe_only` 技术探针标记，可用现有 200/28 候选验证依赖、反向传播和 adapter 保存，不把结果称为正式训练结论。
- scale / full 只接受审核后 `training_ready=true` 的 AgentLab 数据。当前候选不满足此门槛时，脚本应清楚失败并打印原因。
- 不实现 AgentLab 自有 Trainer；AgentLab 是动作执行和评测框架，SFT / QLoRA 继续由已固定的 LLaMA-Factory 完成。
- 本机模型权重缺失，且本地 GPU 只有 4 GB；此阶段生成和验证脚本，不在本机启动 QLoRA。训练是否可执行取决于新服务器显存、CUDA 与长上下文支持。

## 实现选择

1. **AgentLab 专用入口，复用 LLaMA-Factory（推荐）**：最小化重复训练逻辑，同时按 AgentLab manifest / report 做严格校验，并使用 AgentLab + BrowserGym 闭环基线。保持 200/28 与 7,464/670 行数；用不截断 cutoff，不满足资源或数据门槛则停止。
2. **仅复制 OpenHands 脚本并替换文件名**：不能满足，因为 OpenHands runner 硬编码 `openhands_train`、来源类型和报告结构，基线也使用 OpenHands 运行时。
3. **在 AgentLab 内新建训练器**：不选，因为它重复 LLaMA-Factory，扩大依赖和维护面，也与已固定训练栈不一致。

## 主要产物

- AgentLab Qwen 基座与 adapter 服务启动脚本，以及转换 `max_completion_tokens` 到 `max_tokens` 的 loopback API 兼容入口。
- `scripts/agentlab_qwen_loop.py`：从第 10.5 节现有示例抽出并收敛成可复刻脚本，记录每次模型响应、浏览器动作和结果；分别对 base 与 adapter 执行时使用独立结果目录。
- `scripts/eval_agentlab_mind2web.py` 与比较器：对同一组 926 个 held-out test decisions 分别运行 base / adapter，记录逐样本输出和测试清单哈希，比较 action-level 指标。此离线模仿评测与本地 BrowserGym 闭环烟测分开命名、分开报告。
- AgentLab 专用 QLoRA runner 与 smoke / scale / full shell wrappers。runner 对全量 manifest 做长度检查；不提供可静默绕过的 truncation 选项。smoke 默认保留全部 200 train / 28 validation；scale/full 保留审核数据中的所有 train / validation 行。
- AgentLab 计划第 10、11 节后增加服务、基座闭环、QLoRA 预检和新服务器复刻命令，明确数据计数、cutoff 和 readiness 门槛。

## 验收

- short/full 数据哈希不变；预检报告显示训练/验证计数与输入报告一致，test 不在 registry/config 中。
- cutoff 小于任一 train/validation 样本时，在准备训练配置前失败；使用推荐 cutoff 时全量加载器的 token / label 哈希逐条通过。
- smoke 输出 `probe_only=true` 且明示训练数据未完成语义审核；scale/full 对当前 `training_ready=false` 候选拒绝启动。
- AgentLab 基座脚本确认服务模型后，真实模型响应经 `MostBasicAgent` 解析并触发 BrowserGym 动作；API 参数转换测试覆盖 `max_completion_tokens` 映射。
- 训练脚本做 prepare-only 预检和参数测试，不在本机缺少模型权重、4 GB 显存条件下声称 QLoRA 已完成。
