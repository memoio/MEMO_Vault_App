# 阶段一：筛选与 Qwen 样本试制

本次执行步骤 3、4 的保守第一轮，产物是**待审核的训练候选**，尚未达到阶段一“至少三个来源”的验收要求，也没有启动微调。原始 ADP 仍保持 `training_ready=false`。

## 范围及方法

只读取 `paper_adp_v2/train/general` 的 466 条候选及其固定来源索引，完全不使用领域训练、验证或测试数据。对 AgentInstruct 和 CodeAct，校验原始 Parquet SHA-256 和原始 ID，再按记录索引读取原始对话。这是为了保留准确工具提示、消息原文及源 loss 标志；ADP 提供筛选来源和可追溯身份，不修改 ADP 文件。

- AgentInstruct：将原始 `human/gpt` 映射到 Qwen `user/assistant`，源 `loss=false` 助手示例仅放在历史上下文中，源 `loss=true` 助手消息才生成目标。缺失或未知 loss 不假定可训练。
- CodeAct：保留原始系统提示及 `<execute>` 工具协议，原始每个助手消息都可生成候选目标。工具执行结果作为历史 user 消息，不伪装成实时执行。
- SWE-smith：100 条暂缓。其工具调用语义、工具声明版本和 Qwen 运行接口仍须逐条核实，不能只按工具名称假定一致。
- Mind2Web：92 条暂缓。需要先确定 `backend_node_id` 对应的工具接口，以及长网页中如何保留目标元素和足够的任务上下文。

上述两种保留的源协议彼此不同，且尚未映射到统一 Vault 工具目录。该数据可供后续样本检查与试验设计，不代表它已经适配最终运行时。源对话可能包含模型生成的推理性文字；本次没有要求或合成隐藏思维链，也没有做额外清洗。

## 长上下文和标签

按每个合格的助手决策切分：保留到该决策为止的完整历史及系统/工具提示，以目标助手消息构造 Qwen 官方聊天模板。使用本地缓存模型 `Qwen/Qwen2.5-0.5B-Instruct` revision `7ae557604adf67be50417f59c2c2f167def9a775` 的 tokenizer，最大长度设为 2048 token。超过阈值的决策暂缓，**不截断任何必要上下文**；2048 只是筛选阈值，不代表 4 GiB 显卡已验证能以此长度训练。

输入 `input_ids`、`attention_mask`、`labels` 等长。上下文和助手起始标记对应的 labels 为 -100，不计算 loss；目标回复和其模板终止标记才计算 loss。若单独生成的前缀 token 与完整模板不是严格前缀，则不猜测边界，记录为 `qwen_chat_template_prefix_mismatch`。实际观察到部分回复开头的换行与模板换行被分词器合并，是发生该问题的一种情况。

## 本次实测

| 来源 | 通用候选轨迹 | 待审核训练样本 | 暂缓 |
|---|---:|---:|---|
| AgentInstruct | 124 | 484 | 无决策因长度或边界问题暂缓 |
| CodeActInstruct | 150 | 498 | 15 个决策超长；9 个决策模板边界不匹配 |
| SWE-smith | 100 | 0 | 100 条工具接口未验证 |
| Mind2Web | 92 | 0 | 92 条网页接口与长上下文未解决 |
| 合计 | 466 | 982 | 轨迹与决策是不同单位，不应相加 |

一条轨迹可生成多个助手决策样本；982 不表示 982 条独立轨迹。生成样本分别来自 124 和 150 条轨迹。样本按统一模板的实际 token 计数：

| 来源 | 最短 | 中位数 | P90 | 最长 | 超过 512 |
|---|---:|---:|---:|---:|---:|
| AgentInstruct | 549 | 805.5 | 1224 | 1881 | 484 / 484 |
| CodeActInstruct | 294 | 914 | 1526 | 2008 | 417 / 498 |

独立扫描确认 982 条的 `input_ids`、`attention_mask` 和 `labels` 长度一致，标记边界符合本脚本定义。该检查不证明样本语义正确、工具可执行、资源足够或训练有效。

## 复现及文件

从仓库根目录运行；更换输出目录以免覆盖已有结果：

```bash
LD_LIBRARY_PATH=/root/anaconda3/envs/rag_1/lib PYTHONPATH=src \
  /root/anaconda3/bin/python -m everos.agent_training.prepare_qwen_stage1 \
  --output-dir .memo-vault/agent-training/datasets/qwen_stage1_candidate_v2
./.venv/bin/python -m pytest -q tests/unit/test_agent_training
```

运行前要求模型 tokenizer 和原始数据已在本地，输出目录不得存在。本次输出在 `.memo-vault/agent-training/datasets/qwen_stage1_candidate_v2/`：

- `samples.jsonl`：逐决策的原消息、目标、token、labels、来源索引和待审核状态；它可能包含原始数据文本，不应随意公开。
- `decisions.jsonl`：被暂缓的轨迹或决策及原因。
- `report.json`：统计、输入 ADP/原始文件哈希、模型 revision 和参数。

初次执行因模板边界问题停止，留下的 `qwen_stage1_candidate_v1` 是不完整输出，不可用于训练。本次完整结果是 v2。

## 仍需完成

1. 确认数据许可、敏感信息、来源质量和跨来源近重复；逐条核查工具定义及源协议，明确训练时使用的统一工具格式和运行时兼容策略。
2. 对 SWE 和网页样本恢复并验证真实工具接口；设计可追溯的网页元素提取/上下文缩减策略，再争取达到至少 3 个来源的验收要求。
3. 对 24 个暂缓的 CodeAct 决策分别解决标签边界和长上下文；禁止静默截断或虚构结果。最终训练样本需要经过审核并绑定明确的训练配置。
4. 分别准备只从验证集派生的验证样本及固定闭环任务，保留测试集；进行实际 GPU 短试验后再确定上下文长度和训练参数。
