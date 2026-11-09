# 三条 Harness 数据清单与待解决问题

2026-11-09。清单位于 `.memo-vault/agent-training/manifests/harness_audit_20261109/`：`openhands.jsonl`、`swe_agent.jsonl`、`agentlab.jsonl` 和 `report.json`。它们是审计清单，不是 SFT 输入。每行记录目标路径、原划分、来源仓库和 revision、来源文件哈希和行号、来源任务分组及任务哈希、原动作数和暂缓原因。全部 `training_ready=false`。不覆盖原数据，不将验证/测试移入训练。

| 路径 | 训练候选轨迹 | 验证候选轨迹 | 测试候选轨迹 |
|---|---:|---:|---:|
| OpenHands | 366 | 36 | 33 |
| SWE-Agent | 200 | 20 | 20 |
| AgentLab / BrowserGym | 183 | 20 | 19 |

这些是 897 条已选记录的分配，不是筛选通过的数量。使用已有 `task_sha256` 全局比较（包括跨来源）未发现跨 train/validation/test 的相同哈希；按来源加 group 检查亦未发现跨划分冲突。哈希检查不证明语义不重复，也不证明 few-shot 示例未泄漏；原始 `train/general` 和 `train/domain` 同属 train。已检查每份清单的来源及 ID 唯一、原划分一致、来源版本和暂缓原因非空。

OpenHands 需固定框架及工具，验证 bash/Python 协议、示范 loss 标记、依赖及动作质量；SWE-Agent 需核验来源与目标工具版本和编辑/提交语义；网页路径需建立可验证的元素映射，处理快照长度并避免依赖目标裁剪。三者均需逐条质量、许可、语义近重复与跨来源重复审查，完成目标模板与上下文测长后才能计算有效决策数。现有验证和测试只是候选且规模有限，筛选后可能不足。清单中的原因是已知尚未完成的检查，不意味着每条都已独立确认存在对应缺陷。

建议每个路径先审查 20–50 条训练轨迹以验证流程，之后根据有效决策数、类别覆盖和验证表现扩大；这不是保证效果的数据量。训练步数按决策样本数、epoch、每设备 batch 和梯度累积计算，不能将原动作数直接等同训练样本数。最终测试集保留到方案确定后。

## Qwen3.5-0.8B 长度与资源试验

共同基座固定为 `Qwen/Qwen3.5-0.8B`，revision `2fc06364715b967f1860aea9cf38778875588b17`。使用该模型 tokenizer 和聊天模板、`enable_thinking=False`，仅对旧 `train/general` 的 982 个决策候选重新测长：AgentInstruct 484 个，中位数 808.5、P90 1,225、全部超过 512 token；CodeAct 498 个，中位数 948.5、P90 1,569、422 个超过 512、4 个超过 2,048。零前缀不匹配。这些不是最终 Harness 模板，也不覆盖 domain、SWE 或网页；原先已排除的旧候选未计入。结果和每条索引位于 `.memo-vault/agent-training/audits/qwen35_old_candidate_lengths_20261109_v2/`。首次扫描错误地按 BatchEncoding 的字段数计长，位于无 `v2` 的目录，不可使用。

2026-11-09 在 RTX 3050 Laptop 4 GiB 上，4-bit NF4 / double quant、FP16 计算、batch 1、gradient checkpointing、LoRA r=8（仅六个 full-attention 层的 q/v 投影，319,488 可训练参数）的合成短测：768 补齐（有效输入仅 278–279 token），5 个 optimizer step 耗时 13.686 秒，即 2.737 秒/step；allocated 峰值 3,403.79 MiB、reserved 峰值 4,250 MiB（PyTorch 分配器统计，不等于显卡物理占用），保存重载 logits 一致。训练开始前 PyTorch 报告空闲 3,300 MiB。该短测使用大量 padding，不证明真实 768 token、长于 768 token 或其他 LoRA 配置可训练；显存波动及分配器/系统统计差异需后续核验。日志、参数及适配器在 `.memo-vault/agent-training/probes/qwen35_qlora_768_20261109/`，重现使用 `src/everos/agent_training/probe_qwen35_lora.py` 的 `--max-length 768 --context-repeats 20 --steps 5 --low-memory-setup` 和训练环境。此前 512 补齐、3 步用时 4.243 秒，即 1.414 秒/step，峰值 reserved 3,102 MiB，同样是合成测试。

**条件估算示例，不是正式训练承诺：**若每条路径最终得到 1,000 个决策样本、训练 2 epoch、batch 1、不丢弃样本，共约 2,000 次前反向；按上述 512 补齐短测约 47 分钟，按 768 补齐短测约 91 分钟，仅为训练循环外推，不含加载、验证、保存和失败重试。梯度累积会改变 optimizer step 数，但不会消除每个样本的前反向计算。真实输入长度、数据读取、GPU 负载和参数设置均会改变速度，5 步短测尤其不足以稳定预测；须在已审核的目标 Harness 数据上进行较长短测后分别重新估算。当前没有任何路径可给出有效训练样本数及可靠完成时间。
