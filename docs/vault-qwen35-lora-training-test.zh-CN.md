# Qwen3.5-0.8B：本机 QLoRA 训练试验

日期：2026-11-06。**结论：在当前机器和低内存实验配置下，成功完成 4-bit QLoRA 的前向、反向、优化器更新、Adapter 保存、重新加载及输出比较。** 这是 2 条合成样本、3–5 步的冒烟测试，未使用真实 Agent 数据，不能证明模型能力提升、长时间训练稳定或正式环境已就绪。

## 模型、环境和实现

- 模型：`Qwen/Qwen3.5-0.8B`，本地 revision `2fc06364715b967f1860aea9cf38778875588b17`，GPU 为 4 GiB RTX 3050 Laptop。
- 训练试验环境：`.memo-vault/agent-training/envs/qwen35-training/`，Python 3.10.16，PyTorch 2.6.0 / CUDA 12.6，Transformers 5.20.2，PEFT 0.21.2，Accelerate 1.4.0，bitsandbytes 0.50.2，safetensors 0.8.0，tokenizers 0.23.3，huggingface-hub 2.4.0。
- 环境以 `--system-site-packages` 继承 `/root/anaconda3/envs/rag_1` 的 CUDA PyTorch 等包，局部安装上述训练库；不是完全隔离或自包含。pip 报告继承的 sentence-transformers、datasets 和部分 LangChain 等与本环境的新库存在版本冲突。本次验证了该脚本依赖的导入和训练路径，没有验证这些其他包；正式服务或训练框架应建干净锁定环境并做 `pip check`。
- 模型加载：4-bit NF4、double quant、FP16 计算，全部参数位于 `cuda:0`；未量化的冻结权重保持 FP16。普通 AdamW；batch=1；gradient checkpointing 非重入；仅语言模型 attention 层的 q_proj / v_proj 使用 LoRA（r=8、alpha=16、dropout=0），共 319,488 个可训练参数。其他线性注意力、视觉部分没有训练。
- 模型原权重只读；仅训练量化模型附加的 LoRA 参数。聊天模板中的上下文标签为 `-100`，只对合成助手目标计算 loss。每次试验固定种子 42。

**低内存设置的局限**：首次尝试使用 PEFT 的 `prepare_model_for_kbit_training`，其自动将未量化参数转成 FP32，词嵌入转换试图申请约 970 MiB 时 OOM。成功实验改为手动冻结基座、保留非量化权重 FP16 并开启 checkpointing；这并不完全等同标准 PEFT 准备流程。已检查短试验 loss 和 LoRA 梯度有限、非零，但尚未验证更长训练的数值稳定性。

## 实测结果

| 试验 | 真实 token | tensor 长度 | 训练步数 | 结果 |
|---|---:|---:|---:|---|
| 短样本 | 38 / 39 | 补齐至 128 | 5 | 成功；allocated 峰值 1,247.50 MiB，reserved 峰值 1,374 MiB，训练 6.311 秒 |
| 中间长度 | 158 / 159 | 补齐至 256 | 3 | 成功；allocated 峰值 1,676.26 MiB，reserved 峰值 1,982 MiB，训练 4.800 秒 |
| 更长输入、动态长度 | 278 / 279 | 278 / 279 | 3 | 成功；allocated 峰值 1,752.96 MiB，reserved 峰值 2,602 MiB，训练 3.432 秒 |
| 更长输入、固定补齐 | 278 / 279 | 补齐至 512 | 3 | 重跑成功；allocated 峰值 2,541.71 MiB，reserved 峰值 3,102 MiB，训练 4.243 秒。此前一次在交叉熵计算时报 OOM，重跑结果说明可用显存存在波动。 |

以上显存是本进程 PyTorch 分配器统计，非整个 GPU 系统使用量；失败时错误提示中剩余显存和分配失败之间存在差异，原因尚未定位。训练计时不含模型加载、保存和重载；短试验波动大，不能按每秒速度外推正式训练时间。尝试时其他图形进程仍在运行，未终止。

四个成功试验均检查 loss/梯度有限且梯度非零、24 个 LoRA 张量发生变化。每次保存 Adapter 后释放模型，重新加载原始量化基座与 Adapter，在同一固定输入上比较 logits，最大绝对差均为 0，argmax 一致；这仅验证该输入/当前配置的序列化一致性，不证明模型合并、跨设备一致性或任务效果。每个 Adapter 文件总量约 1.23 MiB。

## 可复现命令

需使用上方环境和已缓存的固定模型；每次指定一个**不存在**的输出目录。下面示例只使用 2 条合成消息，不会改动公开数据或模型。

```bash
cd /root/EverOS
/root/EverOS/.memo-vault/agent-training/envs/qwen35-training/bin/python \
  src/everos/agent_training/probe_qwen35_lora.py \
  --low-memory-setup --dynamic-padding --max-length 512 --context-repeats 20 \
  --steps 3 --output-dir .memo-vault/agent-training/probes/qwen35_qlora_dynamic_rerun
```

脚本：[probe_qwen35_lora.py](../src/everos/agent_training/probe_qwen35_lora.py)。输出目录包含 `result.json`、`adapter/`、`tokenizer/`，数据位于 Git 忽略目录。已生成的记录：`qwen35_qlora_128_v3`、`qwen35_qlora_256_v2`、`qwen35_qlora_dynamic_v1`、`qwen35_qlora_512_v2`；失败记录 `qwen35_qlora_128_v1`（FP32 upcast OOM）保留供诊断。512 首次 OOM 的 `qwen35_qlora_512_v1/result.json` 因中断为空，该失败信息来自当时进程输出；重跑 v2 成功。`qwen35_qlora_128_v2` 是模板返回类型检查失败，未进入训练。

重建当前实验环境的关键命令（依赖本机已有 `rag_1` 父环境，仅适用于本机）：

```bash
/root/anaconda3/envs/rag_1/bin/python -m venv --system-site-packages /root/EverOS/.memo-vault/agent-training/envs/qwen35-training
/root/EverOS/.memo-vault/agent-training/envs/qwen35-training/bin/python -m pip install \
  'transformers==5.20.2' 'accelerate==1.4.0' 'bitsandbytes==0.50.2' \
  'peft==0.21.2' 'safetensors==0.8.0' 'tokenizers==0.23.3' 'huggingface-hub==2.4.0'
```

前一次推理报告见 [Qwen3.5 本机推理测试](vault-qwen35-local-inference-test.zh-CN.md)。

## 下一步

确定第一条 Harness 的真实工具协议及训练/验证数据；用 Qwen3.5 tokenizer 重新生成适配该 Harness 的样本，统计真实长度并先选有充分上下文的短样本。先在干净依赖环境中进行小规模真实数据训练、较长步数的 loss/梯度稳定性与重载评测；针对词表较大导致的 loss 内存，在验证数值正确性后研究按目标位置计算或分块等实现，不得只截断必要上下文。最终通过真实工具闭环对比基座与 Adapter，不能以合成示例 loss 下降证明 Agent 能力改善。
