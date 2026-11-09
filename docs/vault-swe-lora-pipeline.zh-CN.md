# SWE-Agent Qwen3.5 QLoRA 脚本使用说明

脚本：`src/everos/agent_training/swe_lora_pipeline.py`。它包含 `convert`（ADP 到上游 SWE-Agent XML 会话）、`prepare`（按决策生成 token 和标签）和 `train`（单 GPU 4-bit QLoRA、保存及重载检查）。它不启动 Docker、不执行轨迹中的工具，也不做真实闭环评测。

**状态：**已用本机固定上游代码转换 240 条轨迹，并用一条轨迹和 Qwen3.5 tokenizer 检查数据准备；训练子命令尚未执行和端到端验证。曾完成的 9 步和 90 步训练来自先前的实验脚本，不能视为新脚本已通过训练验证。

## 输入和环境

- 四个原始 ADP JSONL 位于 `paper_adp_v2/{train/general,train/domain,validation/validation,test/test}/swe_smith.jsonl`，其目录本身为下方 `VAULT_ADP`。原数据需单独迁移；`.memo-vault/` 被 Git 忽略。
- 官方 ADP 仓库须检出固定提交 `ab1180306a3d4d27e9195448c90f030607eba981`，本脚本检查 HEAD。只使用可信且未被修改的检出；脚本会拒绝源轨迹的 `available_apis`，不执行源数据提供的动态工具声明。
- 基座为 `Qwen/Qwen3.5-0.8B`，revision `2fc06364715b967f1860aea9cf38778875588b17`；本地已有快照或先自行下载。模型目录、ADP 目录和上游检出均需迁移或重新准备，不能期待 Git 拉取自动获取。
- 单 NVIDIA GPU 和匹配 CUDA 的 PyTorch、Transformers、PEFT、bitsandbytes、Accelerate 以及上游转换器所需 Pydantic。以本机曾实测的环境为参考：PyTorch 2.6.0/CUDA 12.6、Transformers 5.20.2、PEFT 0.21.2、bitsandbytes 0.50.2、Accelerate 1.4.0；新服务器兼容性必须重新验证。

## 命令示例

下面只展示命令；`VAULT_ADP`、`VAULT_UPSTREAM`、`VAULT_MODEL` 改为新服务器的实际路径，并在项目根目录运行。每次 `--output-dir` 必须是不存在的新目录，避免覆盖。

```bash
export PYTHONPATH=src
export VAULT_ADP=/path/to/paper_adp_v2
export VAULT_UPSTREAM=/path/to/agent-data-protocol
export VAULT_MODEL=/path/to/Qwen3.5-0.8B-snapshot
export VAULT_REV=2fc06364715b967f1860aea9cf38778875588b17

# 1. 离线转换已固定划分的 ADP 轨迹。
python -m everos.agent_training.swe_lora_pipeline convert \
  --adp-dir "$VAULT_ADP" --upstream-dir "$VAULT_UPSTREAM" \
  --output-dir ./swe-converted-v1

# 2. 分别读取训练和验证轨迹；默认超过上限的决策排除，不截断。
python -m everos.agent_training.swe_lora_pipeline prepare \
  --train-input ./swe-converted-v1/train_general.jsonl ./swe-converted-v1/train_domain.jsonl \
  --validation-input ./swe-converted-v1/validation_validation.jsonl \
  --model "$VAULT_MODEL" --revision "$VAULT_REV" --local-only \
  --max-tokens 2048 --output-dir ./swe-prepared-v1

# 3. 确认报告后，在 GPU 上先做少量 optimizer step；需要人工执行。
python -m everos.agent_training.swe_lora_pipeline train \
  --train-data ./swe-prepared-v1/train.jsonl \
  --validation-data ./swe-prepared-v1/validation.jsonl \
  --model "$VAULT_MODEL" --revision "$VAULT_REV" --local-only \
  --epochs 1 --gradient-accumulation 1 --max-steps 10 \
  --output-dir ./swe-lora-smoke-v1
```

如需截断，在 **prepare** 命令加 `--truncate`；它从上下文左侧截断，完整保留助手目标和至少一个上下文 token，记录被截断 token 数；目标过长仍会排除。截断可能移除任务和工具定义。更大显存服务器可提高 `--max-tokens` 并保留默认不截断。`--base-setup peft` 是训练默认；此前 4 GiB 实验使用手动冻结方案，若要复现该实验可显式指定 `--base-setup manual-fp16`，但其数值稳定性仅经短测。`--max-steps` 计 optimizer 步数，0 表示跑完指定 epoch；batch 为 1，梯度累积通过 `--gradient-accumulation` 设置。

转换结果含 200 条训练、20 条验证和 20 条测试候选；示例只将训练和验证输入 `prepare`，测试集需保留到方案确定后的最终评估。训练只保存 Adapter 和 tokenizer，不改变基座。输出包含 `adapter/`、`tokenizer/` 和 `run.json`；报告记录来源数据哈希、依赖、参数、loss、显存、训练时间及保存重载结果。脚本只检查轨迹 ID、可用 source group 和任务哈希的跨训练/验证冲突，不保证语义近重复已排除。

Adapter 不是独立模型，部署时必须配合同一基座及量化配置；保存重载验证和离线 validation loss 不等于 SWE-Agent 真实仓库任务成功。训练后还要在相同工具、任务和参数下比较基座与 Adapter，并将测试任务保留到调参结束。
