# Vault：Agent 微调资源方案与本机实测

更新日期：2026 年 11 月 4 日。配套阅读：[Vault 数据完整性与开发方案](vault-adp-development-proposal.zh-CN.md)。

## 论文数据、Agent 与结果（精简）

论文统一了 13 份数据集，总原始规模超过 130 万条：AgentInstruct、Code-Feedback、CodeActInstruct、Go-Browse、Mind2Web、Nebius SWE Trajectories、NNetNav-live、NNetNav-wa、openhands-feedback、Orca AgentInstruct、SWE-Gym、SWE-smith、Synatra。论文使用 LLaMA-Factory 进行监督微调（SFT）；OpenHands 和 SWE-Agent 按非网页数据采样约**3 万条**，AgentLab 使用网页数据约 **2 万条**，并非每次训练都使用全部数据。

| Agent 框架与评测 | Qwen2.5 结果 |
|---|---|
| SWE-Agent / SWE-Bench Verified | 7B：0.4% → 20.2%；14B：2.0% → 34.4%；32B：2.2% → 40.3% |
| OpenHands / SWE-Bench Verified | 7B：2.8% → 20.4%；14B：5.8% → 30.6%；32B：10.6% → 36.8% |
| AgentLab / WebArena | 7B：4.5% → 21.0%；14B：5.5% → 22.2%；32B：10.9% → 22.9% |
| OpenHands / AgentBench OS、GAIA | AgentBench OS：7B Coder-Instruct 3.5% → 27.1%；GAIA：7B Instruct 7.3% → 9.1% |

表中的数字是论文在特定模型、框架和评测条件下的成绩；提升是数据组合、SFT、模型和框架共同作用的结果，不能仅归功于 ADP 格式。论文没有充分披露 GPU 型号、GPU 时长和训练参数，因此不能直接用论文结果计算 Vault 的训练成本。[论文表 1、表 3–5、附录 C](https://arxiv.org/pdf/2510.24702)

## 1. 先看结论

我们已经使用**本机 GPU 和本地真实预训练模型**，实际运行了 LoRA 微调：模型真实加载了已有权重，程序执行了前向计算、反向传播和参数更新。训练数据是手工构造的工具调用示例，没有使用用户私人数据。

| 你关心的问题 | 本次结论 |
|---|---|
| 论文是不是从零训练模型？ | 不是。[ADP 论文](https://arxiv.org/pdf/2510.24702)在现有基础模型上做监督微调（SFT），没有从零预训练大模型。 |
| 本机是什么设备？ | RTX 3050 Laptop，4 GiB 显存；测试开始前约 1 GiB 已被其他用途占用。 |
| 本机真正跑了什么？ | 加载 Qwen2.5-0.5B-Instruct 预训练权重，用 8 条合成工具调用样本做 LoRA 微调。 |
| 训练能否跑起来？ | 能。在每条补齐为 512 token、batch=1 的配置下，100 个训练步骤计时 24.14 秒，约 **4.14 个样本/秒**。 |
| 显存需要多少？ | 测试进程的 PyTorch 峰值显存分配约 **2.03 GiB**，缓存预留约 **2.65 GiB**。整卡还要加其他进程和系统开销。 |
| 训练 3 万条要多久？ | 按上述本机速度、每条补齐 512 token、每条每轮恰好产生 1 个样本、训练 3 轮，纯训练环节约 **6.0 小时**。这是同配置算术外推，实际数据更长或系统忙时可能不同。 |
| 这能推出 7B 要多少钱吗？ | 不能直接推出。本机没有实测 7B 微调；下面给出 7B 硬件选择和不同吞吐下的预算情景，正式预算需要目标 GPU 实测。 |

**给产品决策的建议**：第一阶段用现有 4 GiB 机器验证数据转换和 0.5B 微调闭环；第二阶段租用 24 GiB 或更大显存的 GPU，尝试 7B QLoRA 并先测真实吞吐、显存和效果。0.5B 在本机能运行，不意味着它一定足够聪明或满足业务需求。

上一版确实在本机运行了 GPU 预实验，但使用的是**随机初始化模型和随机 token**。这一版补充了真实预训练权重和可读的合成工具调用数据；两次实验都不等同于 Vault 的真实用户数据训练，也没有完成任务成功率评测。

## 2. 先弄清楚：我们要训练的是什么？

Vault 预期将用户允许用于训练的交互整理成：

```text
用户任务 + 可用工具信息 + 历史对话
        ↓
模型应该输出的动作（例如调用 weather，参数 city=北京）
        ↓
工具结果 + 后续上下文
        ↓
模型继续调用工具或回复用户
```

**微调**：下载或使用本地已有的大模型，拿上述示例继续训练，让它更贴近特定工具和任务。**从零训练**：先用大量通用文本从随机参数训练出语言模型，再做指令和工具调用微调，两者的数据、时间和算力不是一个量级。ADP 负责规范数据，训练仍需训练程序；本次微调用 PyTorch、Transformers 和 PEFT，不是论文的 LLaMA-Factory 实验复现。

| 方式 | 训练了什么 | 资源特征 | 是否适合首版 |
|---|---|---|---|
| LoRA | 基础模型大部分参数冻结，只更新少量新增参数 | 显存和产物较小 | **适合**，本次已实测 |
| QLoRA | 基础模型以低比特形式保存，并训练 LoRA 参数 | 进一步降低基座显存；速度和兼容性依实现变化 | **适合尝试 7B**；本次没有实测 |
| 全参数 SFT | 更新基础模型全部参数 | 梯度和优化器状态占用显著增加 | 后续按效果决定 |
| 从零预训练 | 从随机参数学习语言能力，再进行后续微调 | 需要大量通用语料、训练和评测资源 | 不建议首版 |

## 3. 本机真实微调：做了什么

### 3.1 测试条件

| 项目 | 测试值 |
|---|---|
| 测试时间 | 2026-11-04，本机时区 Asia/Shanghai |
| GPU | NVIDIA GeForce RTX 3050 Laptop，4,096 MiB 显存 |
| 机器内存 | 约 11 GiB；测试开始前 `nvidia-smi` 显示约 1,037 MiB 整卡占用，其他进程占用会变化 |
| 基础模型 | Qwen/Qwen2.5-0.5B-Instruct，本地缓存 revision `7ae557604adf67be50417f59c2c2f167def9a775` |
| 真实参数量 | 总参数 **494,573,440**；LoRA 可训练参数 **540,672** |
| 软件 | PyTorch 2.5.1+cu124、Transformers 4.44.2、PEFT 0.13.2；CUDA、SDPA 注意力 |
| 微调配置 | 基础权重 float16；LoRA rank 8、q_proj/v_proj；batch=1；AdamW；开启 gradient checkpointing |
| 训练数据 | 8 条人工构造的中文工具调用示例，使用本模型 tokenizer 和聊天模板；每条实际长度 52–57 token，平均 **54 token** |
| 学习目标 | 用户问题、工具说明和 padding 只作为上下文，不计算 loss；只对助手输出的工具名称与参数 JSON 计算 loss |
| 计时 | 每个配置预热 8 步，随后同步 GPU 并计时 100 个完整的前向、反向和优化器更新步骤；batch=1，因此 1 步处理 1 条样本 |

两组长度在**同一个模型进程中顺序执行**：先跑 256，再继续训练同一模型跑 512。每组重复使用 8 条样本，故训练损失会快速下降；512 组开始时模型已经经过 256 组训练，不能将两组 loss 当作独立对照。本测试只证明权重可加载、梯度可回传且 LoRA 参数被更新，不证明泛化能力或工具调用成功率。

### 3.2 实测结果

| 每条样本补齐长度 | 计时步数 | 用时 | 处理样本速度 | 包含 padding 的 token/s | PyTorch 峰值 allocated | 峰值 reserved |
|---:|---:|---:|---:|---:|---:|---:|
| 256 token | 100 | 16.94 秒 | **5.90 条/秒** | 1,511 | 1,620 MiB | 1,806 MiB |
| 512 token | 100 | 24.14 秒 | **4.14 条/秒** | 2,121 | 2,077 MiB | 2,712 MiB |

测试进程的 CPU 峰值 RSS 约 **2,615 MiB**；本地模型权重文件约 **943 MiB**。测试未测量训练产物大小、完整数据集占用、稳定功率、耗电量、检查点保存和任务评测时间。

**如何读这张表：**

- `allocated` 是 PyTorch 在 GPU 上实际分配给张量的峰值；`reserved` 是 PyTorch 向 GPU 缓存管理器申请的峰值。二者有包含关系，**不能相加**；整卡还存在 CUDA、图形桌面和其他进程的占用。
- 表中 token/s 把为了凑齐固定长度而添加的 padding 也算进去了。我们的示例平均只有 54 个真实 token：512 配置对应约 **224 个非 padding token/秒**。不要把 2,121 token/s 误当作真实对话内容的有效处理速度。
- 例子很短、数量很少，且都在 CPU 数据准备后放到了 GPU；真实 Vault 会话更长、内容更复杂，还需要数据读取、验证、保存、评测。本次短时测量不能保证数小时持续训练速度或长上下文不 OOM。
- 此处 512 意味着**训练输入补齐到 512 token**，不是训练了 512 条数据。若实际任务需要 2K、4K 或更长上下文，需要重新测显存和速度。

### 3.3 测试是否确实训练了？

代码加载了本地 `model.safetensors`，将预训练 Qwen 权重放在 GPU，构造带标签的助手工具调用输出，然后每步执行 `loss.backward()` 和 `optimizer.step()`。256 配置最初 4 步平均训练 loss 为 **1.234**，末尾 4 步为 **0.00041**；这反映模型快速记住了重复的少量训练样本，并非独立测试集上的效果提升。脚本参数、环境及复现方式见文末。

## 4. 用本机测得的速度，推算小模型需要多久

先解释两个概念：

- **训练样本**：一个目标决策点和其上下文。一段完整会话可能包含多个工具调用，因此可以拆成多个训练样本；会话数不一定等于样本数。
- **epoch**：把训练集完整遍历一轮。假设 1,000 条样本训练 3 轮，就会处理约 3,000 条次。

以本次真实模型实测为依据：**1 个训练步骤处理 1 条样本，512 token 配置下 100 步用了 24.14 秒**。

```text
纯训练时间（秒）
≈ 训练样本数 × 训练轮数 × (24.14 秒 ÷ 100 步)
```

| 训练样本数 | 每条样本长度假设 | 训练轮数 | 处理样本总次数 | 本机纯训练估算 | 额外加 30% 预留 |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 均补齐到 512 token | 3 | 3,000 | 约 12.1 分钟 | 约 15.7 分钟 |
| 10,000 | 均补齐到 512 token | 3 | 30,000 | 约 2.0 小时 | 约 2.6 小时 |
| 30,000 | 均补齐到 512 token | 3 | 90,000 | 约 6.0 小时 | 约 7.8 小时 |

例如 30,000 条：`30,000 × 3 × 24.14 ÷ 100 ÷ 3600 = 6.04 小时`。30% 只是粗略预留给初始化、读取、保存等，**不是**这些环节的本机实测；如果包含较慢的数据准备、完整评测、多次调参和反复训练，应另行计时及计费。

**这些数字只能用于估算同一台机器、同一 0.5B 模型、LoRA 配置和 512 补齐长度的示范场景。**真实数据如果很长、需要更大的上下文，或者训练时 GPU 被其他程序占用，速度和显存可能变化很大。本次每条真实内容只有约 54 token，只是通过 padding 补到 512；即使计算张量是 512 长，也不能声称已经验证真实的 512-token Agent 长轨迹。

**本机花费**：本次不需要支付额外云 GPU 租赁费；电费和设备折旧未实测，不能说成本为零。若想估算电费，应测量训练期间整机或 GPU 额外平均功率：`电费 = 平均功率(kW) × 运行小时 × 电价`；GPU 功率不等于整机功率。

## 5. 换成更有能力的 7B：需要什么资源？

### 5.1 为什么本次不能用本机直接给出 7B 实测？

本机总显存只有 4 GiB，且部分已经被桌面和其他进程占用。本次 **0.5B fp16 + LoRA** 的 PyTorch reserved 已到约 2.65 GiB。7B 模型参数比它多约 14 倍，还需要保存激活和训练状态。即使以 4-bit 理想存储粗略计算，7B 权重也有约 `7×10⁹ × 0.5 byte ≈ 3.5 GB`，还没计算量化元数据、激活、未量化层和运行时开销。**因此不建议把这台 4 GiB 电脑作为常规 7B QLoRA 训练设备**；不同 offload 方案的可运行性和性能本次没有验证。

本机已有一个 Qwen2.5 3B 的 Ollama 推理模型，但这次没有对它进行 LoRA 或 QLoRA 训练，不能将推理能运行等同于训练能运行。切换模型、显卡、精度或训练框架后，应重新测量。

### 5.2 建议的硬件起点

| 模型和方法 | 建议显存起点 | 基于什么 | 风险 |
|---|---|---|---|
| 0.5B + LoRA | 本机 4 GiB，短上下文 | **已实测** 512 补齐可运行 | 业务能力未验证；更长上下文、其他占用可能 OOM |
| 1.5B–3B + QLoRA | 12–24 GiB | **工程规划假设** | 本次未测；长上下文和配置可能需要更大显存 |
| 7B–8B + QLoRA | 24 GiB 起 | **工程规划假设** | 不是保证；建议准备 48 GiB 作为长轨迹的备选 |
| 14B + QLoRA | 48 GiB 起 | **工程规划假设** | 成本、上下文和显存都需目标设备实测 |
| 7B 全参数 SFT | 多卡或更大显存；先按 2×80 GiB 级别规划 | **粗略规划** | 与精度、分片、CPU offload 等强相关 |

[Hugging Face 显存说明](https://huggingface.co/docs/transformers/v4.46.0/en/model_memory_anatomy)给出一种典型混合精度 AdamW 全参数训练近似：约 `18 bytes × 参数量`，再加激活等。对 7B 是约 126 GB（十进制），这只是特定实现下的估算，并非所有训练方案固定使用量。

### 5.3 7B 云端租用：用情景而非假装实测

[Runpod GPU 定价页](https://www.runpod.io/pricing)在 2026-11-04 查询时列出 RTX 4090 24 GiB 约 **$0.89/卡·小时**、RTX 3090 24 GiB 约 **$0.50/卡·小时**、A100 PCIe 80 GiB 约 **$1.79/卡·小时**。实际价格和可用量随时间、地区、实例与供应变化；这里用 4090 举例，未实际租用。

假设一个 7B 单卡 QLoRA 任务：**30,000 条样本 × 每条 2,048 个处理 token × 3 轮 = 184,320,000 个处理 token**。这是为了演示如何估算，2,048 和 3 轮不是论文披露的训练参数，也不是本次本机实验参数。

```text
训练小时 = 184,320,000 ÷（目标机器实测 token/秒 × 3,600）
租用费用 = 训练小时 × 单卡每小时价格
```

| 假设目标机器实测吞吐 | 纯训练时间 | 加 30% 预留 | 按 $0.89/卡·小时的示意费用 |
|---:|---:|---:|---:|
| 200 token/秒 | 256 小时 | 332.8 小时 | 约 $296 |
| 500 token/秒 | 102.4 小时 | 133.1 小时 | 约 $118 |
| 1,000 token/秒 | 51.2 小时 | 66.6 小时 | 约 $59 |

**200、500、1,000 均是假设值，不是 4090 测量结果，也不是从本机 0.5B 测试直接换算出来的。**实际速度可能在区间外；上表也不包括独立评测、数据清洗、模型下载、存储、税费和多轮实验。不同 GPU 的价格和速度必须配对实测，不能直接用 4090 吞吐去乘 3090 价格。

## 6. 从零训练模型：能否覆盖？

从零训练并非仅靠用户导入的几千条工具轨迹：模型还需先学习语言、常识、代码等基础能力。通常需要大规模通用语料和数据处理，再进行指令及工具 SFT。粗略计算量可参考 `6 × 模型参数 × 训练 token`；这是模型计算量估算，不包含所有训练系统开销，参见 [Training Compute-Optimal Large Language Models](https://arxiv.org/abs/2203.15556)。

假设每张 GPU 的**实际有效算力**为 10 TFLOP/s（仅用于演示，并非本机测得，也非硬件峰值）：

| 假设任务 | 粗略计算量 | 在假设算力下的 GPU-hours | 按 $0.89/卡·小时的算术示意 |
|---|---:|---:|---:|
| 从零训练 0.5B、使用 10B token | 3×10¹⁹ FLOPs | 约 833 | 约 $741 |
| 从零训练 7B、使用 140B token | 5.88×10²¹ FLOPs | 约 163,333 | 约 $145,367 |

这些是假设下的量级，实际有效算力可能不同，数据清洗、通信、验证、失败重试、保存和后续 SFT 也没有计入。多卡能缩短墙钟时间，但 GPU-hours 不会因为增加卡数而等比例消失。**本次没有实测从零预训练，也无法据此保证任意 GPU 或数据集能得到可用模型。**首版应先验证微调是否解决用户问题，再考虑是否有必要预训练。

## 7. 后续如何得到可信的 Vault 项目预算？

1. **统计真实数据**：从获得训练授权的数据中，统计可用会话数、拆分后的样本数、缺失工具定义数量，使用目标模型 tokenizer 统计 p50/p90/p99 长度及真实 padding 比例。先按会话和任务切分训练集与测试集，防止同一轨迹泄漏。
2. **先做 0.5B 端到端试验**：数据导出 → SFT 格式转换 → 样本审查 → 模型微调 → 保存适配器 → 加载适配器 → 沙箱评测。记录全部阶段耗时、磁盘和显存；检查授权、版本、可撤销策略。
3. **租目标 GPU 跑 7B 探针**：固定模型版本、量化方式、框架、LoRA rank、batch、上下文长度。预热后运行至少 100–300 个步骤并同步计时；同时记录 GPU 型号、卡数、总卡占用、allocated、reserved、CPU 内存和总处理 token。
4. **拿真实吞吐重新算账**：先用 `数据实际 token × 轮数 ÷ 实测总吞吐` 得到训练时间，再加数据准备、评测、存储和合理重试预算。多卡时，吞吐若是所有卡合计，要用墙钟小时 × GPU 张数计算 GPU-hours。
5. **对照效果和成本**：同一独立测试集分别测原模型与微调模型的工具选择、参数正确率、执行成功率、错误恢复和普通对话回归；任务执行采用模拟工具或沙箱。达到产品设定阈值，再扩大训练规模。
6. **实现资源控制**：训练使用独立 worker，支持数据集快照、授权校验、GPU 配额、时间与预算限制、进度、取消、失败原因、适配器和指标版本管理。预计每次训练都要能追溯使用的数据与配置。

## 8. 本次实验的完整复现材料

测试脚本保存在以下代码块，可保存为 `/tmp/vault_real_sft_probe.py`。依赖现有本地模型缓存和指定 Python 环境；没有依赖下载。代码仅构造示例数据，不读取用户轨迹；执行会短时占用 GPU。缓存可能变化或不存在，失败时需先检查依赖和权重。

```bash
LD_LIBRARY_PATH=/root/anaconda3/envs/rag_1/lib /root/anaconda3/bin/python /tmp/vault_real_sft_probe.py
```

注意：此机器的 PyTorch 默认导入存在 CUDA 动态库冲突，因此本次只在该命令进程内设置 `LD_LIBRARY_PATH`。该路径是本机环境配置，其他机器需要使用自身兼容的 CUDA 依赖。

```python
import json, os, time, statistics, resource
import torch, transformers, peft
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import LoraConfig, get_peft_model, TaskType

torch.manual_seed(2026)
torch.set_num_threads(4)
name='/root/.cache/huggingface/hub/models--Qwen--Qwen2.5-0.5B-Instruct/snapshots/7ae557604adf67be50417f59c2c2f167def9a775'
tokenizer=AutoTokenizer.from_pretrained(name,local_files_only=True)
base=AutoModelForCausalLM.from_pretrained(name,torch_dtype=torch.float16,local_files_only=True,low_cpu_mem_usage=True).to('cuda')
model=get_peft_model(base,LoraConfig(r=8,lora_alpha=16,lora_dropout=0.0,target_modules=['q_proj','v_proj'],task_type=TaskType.CAUSAL_LM))
model.gradient_checkpointing_enable();model.enable_input_require_grads();model.train()
optimizer=torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],lr=1e-4)
samples=[('北京天气怎么样？','weather','{"city":"北京"}'),('查询上海的天气。','weather','{"city":"上海"}'),('帮我查询深圳的天气。','weather','{"city":"深圳"}'),('查询广州天气情况。','weather','{"city":"广州"}'),('搜索机器学习入门教程。','search','{"query":"机器学习入门教程"}'),('查找 Python 文档。','search','{"query":"Python 文档"}'),('搜索数据分析课程。','search','{"query":"数据分析课程"}'),('查找深度学习书籍。','search','{"query":"深度学习书籍"}')]
examples=[]
for question,tool,args in samples:
 prompt=tokenizer.apply_chat_template([{'role':'system','content':'你是助手。可用工具: weather(city), search(query)。需要工具时输出 JSON。'},{'role':'user','content':question}],tokenize=False,add_generation_prompt=True)
 target=json.dumps({'name':tool,'arguments':json.loads(args)},ensure_ascii=False)+tokenizer.eos_token
 a=tokenizer(prompt,add_special_tokens=False)['input_ids']; b=tokenizer(target,add_special_tokens=False)['input_ids']
 examples.append((a,b))
print(json.dumps({'model':'Qwen/Qwen2.5-0.5B-Instruct','revision':'7ae557604adf67be50417f59c2c2f167def9a775','device':torch.cuda.get_device_name(0),'total_params':sum(p.numel() for p in model.parameters()),'trainable_params':sum(p.numel() for p in model.parameters() if p.requires_grad),'torch':torch.__version__,'transformers':transformers.__version__,'peft':peft.__version__,'attention':model.config._attn_implementation,'unique_synthetic_examples':len(examples),'actual_lengths':[len(a)+len(b) for a,b in examples],'base_dtype':'float16','optimizer':'AdamW fp32 adapter','gradient_checkpointing':True,'rank':8,'batch':1},ensure_ascii=False),flush=True)
for seq in [256,512]:
 try:
  prepared=[]
  for a,b in examples:
   length=len(a)+len(b); assert length<=seq
   input_ids=torch.tensor([a+b+[tokenizer.pad_token_id]*(seq-length)],device='cuda')
   mask=torch.tensor([[1]*length+[0]*(seq-length)],device='cuda')
   labels=torch.tensor([[-100]*len(a)+b+[-100]*(seq-length)],device='cuda')
   prepared.append((input_ids,mask,labels))
  def step(i):
   ids,mask,labels=prepared[i%len(prepared)];optimizer.zero_grad(set_to_none=True)
   loss=model(input_ids=ids,attention_mask=mask,labels=labels,use_cache=False).loss
   if not torch.isfinite(loss):raise RuntimeError('nonfinite loss')
   loss.backward();optimizer.step();return loss.detach().item()
  losses=[step(i) for i in range(8)]
  torch.cuda.synchronize();torch.cuda.reset_peak_memory_stats();start=time.perf_counter()
  for i in range(100):losses.append(step(i))
  torch.cuda.synchronize();duration=time.perf_counter()-start
  print(json.dumps({'sequence_padded':seq,'warmup_steps':8,'timed_steps':100,'seconds':duration,'steps_per_second':100/duration,'padded_tokens_per_second':100*seq/duration,'mean_real_tokens_per_example':statistics.mean(len(a)+len(b) for a,b in examples),'nonpad_tokens_per_second':100*statistics.mean(len(a)+len(b) for a,b in examples)/duration,'peak_allocated_mib':torch.cuda.max_memory_allocated()/2**20,'peak_reserved_mib':torch.cuda.max_memory_reserved()/2**20,'loss_initial_mean4':statistics.mean(losses[:4]),'loss_final_mean4':statistics.mean(losses[-4:]),'process_peak_rss_mib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024},ensure_ascii=False),flush=True)
 except torch.cuda.OutOfMemoryError as e:
  print(json.dumps({'seq':seq,'error':'CUDA OOM','detail':str(e)[:300]}),flush=True);break

```
