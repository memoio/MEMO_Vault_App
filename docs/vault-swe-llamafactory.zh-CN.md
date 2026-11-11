# SWE-Agent + Qwen3.5-0.8B：LLaMA-Factory SFT 路径

本路径参考 ADP 论文：SWE-smith 轨迹 → ADP → SWE-Agent 对话 → LLaMA-Factory SFT → SWE-Agent 实际任务评测。现阶段仅用 SWE-smith 候选和 QLoRA，不能称为复现论文的多来源混合实验或论文成绩。训练不启动 SWE-Agent 或 Docker；真实任务评测需要固定 SWE-Agent、工具、仓库和 Docker 环境。

训练路径使用 [LLaMA-Factory v0.9.5](https://github.com/hiyouga/LlamaFactory/releases/tag/v0.9.5)，提交 `7af909522a951e3ad9f022ea6f88b6755257eaa5`。其声明 Python >=3.11、Transformers <=5.6.0、PEFT <=0.18.1。先前建议的 Transformers 5.20.2 / PEFT 0.21.2 不满足此版本声明，**不要在原环境直接降级或覆盖**，请为本路径创建独立环境。本文件中的依赖和 GPU 训练尚需服务器实测；安装成功不表示显存或模型训练已验证。

## 一、准备独立环境与资源

迁移此前准备的模型、`paper_adp_v2` 及固定提交 `ab1180306a3d4d27e9195448c90f030607eba981` 的 ADP 上游仓库到服务器，校验哈希。Git 仓库不包含 `.memo-vault` 下的数据和模型。保留训练、验证和测试的原始划分，测试轨迹不用于训练或调参。

在服务器准备 Python 3.11 或 3.12 独立虚拟环境，并获取固定版本的 LLaMA-Factory。服务器仅支持 IPv4；如 GitHub 访问不便，可在本机克隆并通过 `scp -4` 迁移。以下示例会安装依赖，安装前确认目录和磁盘空间：

```bash
export VAULT_RUN=/root/vault-swe-run
export VAULT_PROJECT=/root/workspace/MEMO_Vault_App
/root/miniconda3/bin/python -m venv "$VAULT_RUN/llamafactory-venv"
source "$VAULT_RUN/llamafactory-venv/bin/activate"
git clone --branch v0.9.5 https://github.com/hiyouga/LlamaFactory.git "$VAULT_RUN/LlamaFactory"
git -C "$VAULT_RUN/LlamaFactory" rev-parse HEAD
# 应为 7af909522a951e3ad9f022ea6f88b6755257eaa5
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
  -c "$VAULT_PROJECT/scripts/requirements-swe-llamafactory-constraints.txt" \
  -e "$VAULT_RUN/LlamaFactory" bitsandbytes
python -m pip check
python -c 'import torch, transformers, peft; from importlib.metadata import version; print("torch", torch.__version__, "CUDA", torch.cuda.is_available(), "transformers", transformers.__version__, "peft", peft.__version__, "llamafactory", version("llamafactory"))'
```

安装命令会解析上游声明的依赖；GPU 驱动、PyTorch CUDA 构建与 bitsandbytes 是否可用以实际检查为准。不要把 `pip check` 失败或 CUDA 不可用当作训练环境已就绪。

Ubuntu 18.04 使用 glibc 2.27；上游依赖 PyAV 时，pip 可能选择没有兼容 wheel 的较新版本并尝试从源码编译，报缺少 `libavformat` 等库。约束文件固定 `av==14.2.0`，它提供 Python 3.12 的 manylinux2014 wheel，且符合 v0.9.5 的 PyAV 版本范围。服务器应使用包含此约束的最新文件，安装时也可加 `--prefer-binary` 优先选择预编译包。

## 二、转换数据

在项目根目录，设置实际路径。沿用已有的 `convert`；转换目录必须不存在。若服务器已有相同固定版本和哈希的转换产物，可直接复用，并核对其 `report.json` 和 `excluded.jsonl`。

```bash
cd "$VAULT_PROJECT"
export PYTHONPATH="$VAULT_PROJECT/src"
export VAULT_ADP="$VAULT_RUN/data/paper_adp_v2"
export VAULT_UPSTREAM="$VAULT_RUN/upstream/agent-data-protocol"
export VAULT_MODEL="$VAULT_RUN/model"
export VAULT_REV=2fc06364715b967f1860aea9cf38778875588b17

python -m everos.agent_training.swe_lora_pipeline convert \
  --adp-dir "$VAULT_ADP" --upstream-dir "$VAULT_UPSTREAM" \
  --output-dir "$VAULT_RUN/converted-v1"
```

原候选为训练 200、验证 20、测试 20 条轨迹；实际转换数以报告为准。转换器固定上游提交，并拒绝源数据中提供的动态 `available_apis`。

## 三、导出 LLaMA-Factory 训练数据

在上述独立环境中运行。`export` 调用固定版本 LLaMA-Factory 的 Qwen3.5 模板及 SFT 预处理器，按助手工具动作生成 ShareGPT 格式决策样本。`mask_history: true`、`train_on_prompt: false` 仅对当前动作计算 loss；这里选择逐决策训练，不能假定与论文完整训练配置完全相同。

```bash
python -m everos.agent_training.swe_llamafactory export \
  --train-input "$VAULT_RUN/converted-v1/train_general.jsonl" "$VAULT_RUN/converted-v1/train_domain.jsonl" \
  --validation-input "$VAULT_RUN/converted-v1/validation_validation.jsonl" \
  --model "$VAULT_MODEL" --revision "$VAULT_REV" --local-only \
  --max-tokens 1024 --truncate --max-steps 10 \
  --output-dir "$VAULT_RUN/lf-prepared-1024-v1" \
  --train-output-dir "$VAULT_RUN/lf-smoke-1024-v1"
```

`--truncate` 默认关闭。开启后，先移除最早的完整用户/助手历史轮次；如果仍超长，再从最后一条用户消息的左侧截断，保留至少一个字符、完整 system 和目标动作。如果仍过长则排除。截断可能移除最初的任务描述或工具反馈，需要查看 `manifest.jsonl` 中的删除轮次和字符数。导出器同时以实际 cutoff 和无限长编码，要求输入和标签一致，以防 LLaMA-Factory 再次截断。1024 只是短测候选，不保证显存够用，也不保证有效样本充足；重新导出可改长度或关闭开关。

查看 `report.json`、`manifest.jsonl`、`excluded.jsonl`，确认轨迹数、决策数、有效数、截断数、排除原因和来源；还需人工抽查任务及工具定义是否充分。代码检查轨迹 ID、已有 source group 与任务哈希的训练/验证交叉，**未证明语义近重复已排除**。`validation.jsonl` 保存独立验证样本，但短测配置没有自动运行验证集评估；`test_test.jsonl` 不参与导出。

## 四、人工启动 10 步训练短测

准备并检查报告后，先验证输出未被修改且训练目录不存在：

```bash
python -m everos.agent_training.swe_llamafactory verify --prepared-dir "$VAULT_RUN/lf-prepared-1024-v1"
python -m everos.agent_training.swe_llamafactory audit --prepared-dir "$VAULT_RUN/lf-prepared-1024-v1"
llamafactory-cli train "$VAULT_RUN/lf-prepared-1024-v1/smoke.yaml"
```

`audit` 使用 LLaMA-Factory 的实际数据加载器重新处理训练和验证数据，逐条对比导出时记录的 token 与标签哈希；这一步不进行模型训练。只有核对通过后才运行训练命令。

`smoke.yaml` 使用 4-bit NF4、LoRA、batch 1、10 个优化器步数及 FP16。观察 loss 是否有限、实际最大显存、速度及是否保存 Adapter。按 LLaMA-Factory 输出记录实际可训练参数，检查目标模块是否符合预期；本配置与旧脚本的目标模块名称类似，但不能假定两者匹配到的层完全相同。

训练成功后，用训练目录实际保存 Adapter 的位置进行 GPU 重载检查：

```bash
python -m everos.agent_training.swe_llamafactory check-adapter \
  --prepared-dir "$VAULT_RUN/lf-prepared-1024-v1" \
  --adapter-dir "$VAULT_RUN/lf-smoke-1024-v1" \
  --report-path "$VAULT_RUN/lf-smoke-1024-v1-reload.json" --local-only
```

重载检查加载同一基座和 Adapter，检查有限 logits，并记录相对于禁用 Adapter 时的 logits 差异。它不是 SWE-Agent 闭环评测，也不证明模型能够完成仓库任务。若显存不足，重新导出新的较短长度并重新短测；每次使用新的数据和训练输出目录。配置和数据受 `verify` 哈希保护，修改后应重新导出并重新检查；不要直接把短测配置改成正式训练后绕过审计。

## 五、扩大训练与评测

只有短测及报告通过后，才重新导出新目录，并根据实测显存设置长度和训练步数。训练时间依据实际步速估算。短测默认不自动对验证集求 loss；正式实验可在重新导出时设置 `--eval-steps N`，令 LLaMA-Factory 每 N 个优化器步骤使用独立验证集计算 loss。使用 `--save-steps N` 可按间隔保存检查点；两者默认为 0，分别表示关闭定期验证和在最后一个配置步数保存。启用验证时必须有有效验证样本。定期验证和从检查点恢复尚未实测，应先进行小规模检查。

在新服务器部署独立 SWE-Agent 评测环境、固定工具及 Docker 仓库镜像，先对基座做固定任务基线，再对基座加 Adapter 使用完全相同的任务、仓库快照、上下文与生成参数评测。记录动作解析、执行、测试与任务成功情况。评测不需要运行 EverOS 前后端。当前尚无新服务器的训练或 SWE-Agent 评测通过记录。

**模板对齐尚待完成：**本路径使用 LLaMA-Factory `qwen3_5_nothink` 模板，原轨迹中的助手文本（包括已有思考文本）作为普通文本保留。此前本机实验服务使用 Hugging Face `apply_chat_template(..., enable_thinking=False)`，其生成提示会额外加入空的 `<think>` 段，不能直接认为与本训练模板一致。服务器评测前需要调整或明确评测的提示模板，逐 token 核对训练与推理的动作前缀，并在相同设置下重新建立基座和 Adapter 基线。
