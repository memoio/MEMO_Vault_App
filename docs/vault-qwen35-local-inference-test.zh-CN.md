# Qwen3.5-0.8B 本机推理测试

测试日期：2026-11-06。结论：本机以 bitsandbytes 4-bit NF4、FP16 计算在 GPU 上完成了两次短文本推理；一次 FP16 全模型加载报 OOM。4-bit 短推理成功不证明长上下文、并发、框架集成或 LoRA/QLoRA 训练可行。

## 模型与设备

- 模型：`Qwen/Qwen3.5-0.8B`，本地快照 revision `2fc06364715b967f1860aea9cf38778875588b17`，权重文件约 1.7 GiB。只从本地加载，未额外下载模型。
- GPU：RTX 3050 Laptop，4,096 MiB，驱动 KMD 617.14，CUDA capability 8.6。实验前 `nvidia-smi` 显示占用 1,168 MiB、空闲 2,795 MiB；过程中 `torch.cuda.mem_get_info` 报告实验前约 3,300 MiB，二者统计口径/时间不同，不应混为同一项。显存会随其他图形进程变化。
- 不杀进程、不改变现有项目虚拟环境。

## 依赖与环境

测试用环境：`.memo-vault/agent-training/envs/qwen35-inference/`，Python 3.10.16；复用 `/root/anaconda3/envs/rag_1` 的系统包，**不是完全独立自包含环境**，删除父环境或升级父包可能影响复现。

| 依赖 | 实测版本 |
|---|---|
| PyTorch / 编译 CUDA | 2.6.0 / 12.6 |
| Transformers | 5.20.2 |
| Accelerate | 1.4.0 |
| bitsandbytes | 0.50.2 |
| huggingface-hub | 2.4.0 |
| safetensors / tokenizers | 0.8.0 / 0.23.3 |
| PEFT | 未安装 |

由于继承父环境，pip 报告 `sentence-transformers`、`datasets` 等与新 Transformers/huggingface-hub 的版本冲突。本次仅验证模型推理，不应把该环境作为已验证的完整训练环境；后续应建立经过锁定和检查的训练环境。Qwen3.5 模型推理提示未安装 `causal_conv1d` 和 `flash-linear-attention`，当前使用参考实现，可运行但可能慢；未验证安装加速内核的兼容性。

## 实测结果

| 配置 | 输入/输出 | 结果 |
|---|---|---|
| 模型全部参数 FP16，尝试整体搬到 CUDA | 未运行到生成 | 加载时 CUDA OOM；PyTorch 峰值 allocated 1,474 MiB / reserved 1,524 MiB。错误报告同时称还有约 1.73 GiB 空闲，具体原因未确定，不能据此声称 FP16 在所有情况下不可能运行。 |
| 4-bit NF4、double quant、FP16 计算；整个模型设备 `cuda:0` | 输入 24 token，生成 9 token | 返回“2加3等于5。”；加载 3.36 秒，生成 2.72 秒；峰值 allocated 831 MiB / reserved 876 MiB。 |
| 同上 | 输入 365 token，生成 25 token | 返回 JSON 形式文本，加载 2.96 秒，生成 2.83 秒；峰值 allocated 869 MiB / reserved 878 MiB。模型把 `name` 输出为“北京”，不符合应调用 `weather` 的要求，因此该条**工具选择失败**。 |

训练前的工具能力未通过系统性评估；第二例是自行构造的单条提示，没实际执行天气工具，不能代表模型总体能力。峰值数字为本进程 PyTorch CUDA 分配器统计，不等于 GPU 全系统占用；加载时间包含本次设备和缓存状态，生成时间包含输入处理及输出，不能直接外推吞吐。

## 重现

```bash
/root/EverOS/.memo-vault/agent-training/envs/qwen35-inference/bin/python \
  /root/EverOS/.memo-vault/agent-training/probes/qwen35_inference_4bit_probe.py

/root/EverOS/.memo-vault/agent-training/envs/qwen35-inference/bin/python \
  /root/EverOS/.memo-vault/agent-training/probes/qwen35_inference_tool_probe.py
```

脚本和输出 JSON 位于 `.memo-vault/agent-training/probes/`；输出目录在 Git 忽略范围，重跑会覆盖对应结果 JSON。FP16 失败过程也保存在该目录。复现时可用显存、依赖、进程及温度会影响结果。

## 后续训练验证

应另建并锁定支持 Qwen3.5 的训练环境，验证 PEFT 与量化版本兼容，在不结束其他用户进程的条件下尝试 LoRA 前向、反向、优化器一步、保存和重载；从短序列逐步增长并记录系统显存及 PyTorch 峰值。若 OOM，降低长度或调整资源配置，不能将本次推理峰值当作训练显存。
