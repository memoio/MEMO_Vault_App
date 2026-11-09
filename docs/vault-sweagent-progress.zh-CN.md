# SWE-Agent 与 Qwen3.5 本机实施进度

2026-11-09；对应计划 `docs/superpowers/plans/2026-11-06-vault-sweagent-qwen35.zh-CN.md`。工作仍在进行，不能视为框架闭环、训练或评测已完成。

- 官方 SWE-Agent 1.1.0 固定提交 `3ea751c087f32b16e039a2233dd6eefecef325d5`，隔离安装路径 `.memo-vault/agent-training/sweagent/upstream/SWE-agent`，Python 环境 `.memo-vault/agent-training/sweagent/env`，依赖版本 `environment-freeze-20261109.txt`。SWE-Agent 许可证 MIT；默认配置使用原生 function calling，当前验证改用实际提供的 `xml_function_calling` 和 `tools/edit_anthropic`、`tools/submit`、内置 bash。尚未固定完成真实运行配置。
- ADP 固定提交 `ab1180306a3d4d27e9195448c90f030607eba981` 位于并列的 `upstream/agent-data-protocol`。原版转换器在禁用来源提供的 `available_apis` 动态执行路径且传入空 API 签名时，成功转换 240 条现有 SWE 轨迹；输出在 `upstream_conversion_20261109`。这不是模型训练数据。
- 240 条均从固定哈希源文件重建为相同 ADP 内容及工具链接。训练 200、验证 20、测试 20，源 ID、源行、group 和任务哈希无跨划分冲突。5,811 个工具链接未发现有限结构错误，明细与清单在 `data_audit_20261109`；语义近重复和工具运行行为未全面证明。
- 使用真实 XMLFunctionCallingParser 以及三种目标工具声明，全部 5,811 个动作均被解析，生成的 shell 命令/参数与源记录按离线比较一致，统计在 `parser_audit_20261109`。该检查未执行轨迹命令，不证明源与目标工具的行为完全等价。
- Docker 引擎可用；Docker Hub 和 mirror.gcr.io 均超时。通过 Ubuntu 官方最小根文件系统成功导入并启动容器，隔离运行镜像构建仍在进行。BusyBox 最小镜像的 `--network none` 测试成功，但不是 SWE-Agent 真实工具闭环。

后续需完成：固定工具及运行镜像、在隔离仓库通过真实框架执行并返回结果；构造目标模型样本与可选截断并验证 mask；接入本机 Qwen3.5、建立未训练基线；真实轨迹训练和保存重载；在相同任务比较基座与 Adapter。不可将现有转换结果或解析器检查当成训练及真实闭环。

## 真实工具、训练与初步对比

- 真正的 SWE-Agent `ToolHandler`、XML 解析器和 SWE-ReX 1.4.0 运行在固定镜像 `vault-swe-rex:20261109`（镜像 ID `sha256:8cacceef5796b479e9ea41ba23ef3b3d9224c135a2b89d8c2a47eac8db3bf71a`）。容器接入内部网络、限制内存 1 GiB / 2 CPU；实验时本机回环代理转发到内部容器，不对外开放服务。工具配置使用 registry、edit_anthropic、submit 和 bash；离线实验副本省略编辑器可选 tree-sitter 安装，且关闭 filemap。预设动作在临时仓库成功读取、查看、编辑和提交；详情见 `framework_tool_smoke_20261109_v3.json`。这是预设动作验证，不是模型自主完成的任务。
- Qwen3.5 非 thinking、贪心解码，输入上限 1,536 token（超长左截）、最多生成 128 token；基线任务的输入为 992 token，没有截断。任务仓库初始提交 `cd320ed6f28d81afb7ea174119ea94910d935bd2`。基座只返回普通代码块，XML 格式失败；Adapter 返回 XML 但缺少 `command`。两者均 `exit_format`、无补丁、模型调用 1 次、成功修复 0/1。轨迹分别在 `baseline_toy_add_v1/` 和 `adapter_toy_add_v1/`；自动提交不代表模型发起的有效工具调用。
- 使用 3 条训练轨迹拆出的 9 个决策（bash/editor/submit 各 3）训练，全部左截断至 256 token；因此工具声明及任务可能被丢弃，仅作为管线试验。512 token 的真实样本首次反向 OOM；256 token 9 步成功，训练循环 6.137 秒，峰值 allocated 1,676.30 MiB / reserved 1,982 MiB；梯度非零、24 个 LoRA 张量变化，重新加载前后 logits 最大绝对差 0。`qwen35_swe_qlora_256_9step_20261109/result.json` 含配置、每条来源及实验结果；清单和 adapter 哈希见 `run_manifest_20261109.json`。9 条不足以判断微调效果。
- 使用既有 PEFT 脚本进行短测作为实验性替代：LLaMA-Factory v0.9.5（提交 `7af909522a951e3ad9f022ea6f88b6755257eaa5`）声明支持 Qwen3.5，但其依赖上限 Transformers 5.6.0、PEFT 0.18.1，现有训练环境分别是 5.20.2 和 0.21.2；未修改既有环境，也尚未在独立兼容环境实际试跑 LLaMA-Factory，不能声称其不可用。

**尚未完成：**模型尚未生成一个可执行动作，因此模型→框架→工具→模型的自主闭环未通过；只有一个验证玩具任务，尚未建设多个独立任务与最终测试，也没有完整 200 轨迹的目标决策数据集或正式训练。应先解决输出格式/长度和上下文处理问题，再以更多任务及相同规则重新验证，不把预设动作或训练 loss 当作任务成功。
