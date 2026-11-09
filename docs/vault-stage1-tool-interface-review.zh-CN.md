# 阶段一：样本示例与 SWE / Mind2Web 接口核查

## 已生成的样本长什么样

以下示例来自 `qwen_stage1_candidate_v2/samples.jsonl`，仅节选和说明；省略的上下文在实际样本中保留。一个样本是某次助手决策，包含 `messages`（此前系统/用户/助手消息）、`target`（本次要学习的助手回复）、`input_ids`、`attention_mask`、`labels`、来源索引等。模型看到由 Qwen 聊天模板编码的上下文；训练时上下文标签为 `-100`，只对目标回复计算 loss。

**AgentInstruct：** `os_0` 的第 7 条原始消息是一个训练目标。上下文先展示工具规则与不参与 loss 的历史示范，然后用户提出：筛选当前目录配置文件的有效配置，找出现次数最多的配置项。目标为：

````text
Think: First, locate .conf files in the current directory.

Act: bash

```bash
ls *.conf
```
````

这条样本共 565 token；其中前 538 token 是上下文，不计入 loss。它使用来源原有的 `Act: bash` 语法，不是统一的 Vault JSON 工具协议。

**CodeAct：** 系统消息说明用 `<execute>` 包裹 Python 调用；用户问题是 `If 5^x=100, what is the value of 5^{x+2}?`。目标节选如下：

```text
<execute>
import sympy
x = sympy.solve(5**x - 100, x)[0]
print(5**(x+2))
</execute>
```

这条样本共 348 token，前 267 token 是上下文。这是原轨迹的首个动作；代码中 `x` 未定义，后续原始观测记录了 `NameError`。因此样本可能用于学习“试错过程”，但它的存在不证明每个训练目标都是正确动作，也说明还要审核样本质量。这里保留的 `<execute>` 与 AgentInstruct 协议不同，未来运行时需要对应解析器或经过验证的统一转换。

## 为什么此前没有直接用于训练

SWE 原始数据记录调用名、参数、调用 ID 和反馈，但保存的 system 文本没有完整工具声明。来源仓库当前的 [SWE-smith 工具配置](https://github.com/SWE-bench/SWE-smith/blob/main/agent/swesmith_infer.yaml) 描述了 `bash`、`str_replace_editor`、`submit`；它的当前 `main` 不一定是原始轨迹生成时的版本。单凭名称相同不能证明所有参数和行为一致。

[Mind2Web 数据定义](https://github.com/OSU-NLP-Group/Mind2Web#data-fields)明确动作前有 `raw_html`，动作有 `CLICK`、`TYPE`、`SELECT` 和元素 `backend_node_id`。我们保存的是这一原生标识，不能冒充上游其他浏览器接口使用的 XPath；快照本身也不是可操作的线上浏览器。

## 本次执行的代码与结果

运行：

```bash
PYTHONPATH=src /root/anaconda3/bin/python -m everos.agent_training.audit_tool_interfaces \
  --verify-raw-swe --output-dir .memo-vault/agent-training/audits/tool_interfaces_v2
./.venv/bin/python -m pytest -q tests/unit/test_agent_training
```

输出目录必须不存在。`actions.jsonl` 保存每个动作的轨迹 ID、源文件索引、内容下标、检查结果；`report.json` 保存汇总及输入哈希。脚本不会执行 Bash、编辑仓库或点击网页。

- SWE：100 条轨迹先核对原始文件 SHA-256，再从原始消息重新转换，与已保存内容及调用链接逐条比较，100 条一致。2,164 个动作通过本次有限字段/结果链接检查；其中 2,065 个链接到环境结果，99 个是轨迹最后的 `submit`。工具名计数：938 次 bash、1,035 次 editor、191 次 submit。**这些检查不证明当前上游声明就是采集时的版本，也不证明完整语义、工具可运行或样本适合训练。**
- Mind2Web：92 条轨迹的 610 个动作中，610 个均在其紧邻的动作前 HTML 中找到唯一对应的 `backend_node_id`；其中 518 个后面还有下一次动作前的快照，92 个没有下一快照。后续快照也不自动证明动作成功。这次没有把 ID 映射为 XPath，也没有使用目标元素裁剪 HTML。

## 后续实现方案

1. **固定工具来源**：寻找 SWE 数据采集时的工具定义及版本；将实际配置保存版本与哈希，逐个验证命令、必填/可选参数、结果和终止语义。当前配置仅作为对照，版本无法确认时显式标注并继续暂缓。
2. **SWE 训练转换**：在版本确定后，保留源调用和 ID，将工具声明、任务、此前动作及对应结果转为统一的 Qwen 训练上下文；每次调用输出作为目标。处理多个调用/结果及无结果终止动作时要遵循原始关联，不将普通终止当失败。完整上下文过长时先设计保留任务、声明、关键历史与观测的策略，不能简单截断成缺上下文的样本。
3. **网页训练转换**：定义可核验的快照动作表示，保留原始 ID、操作和前置 HTML；如要转换到其他运行接口，先建立并测试有证据的元素映射。缩减 HTML 必须在模型决策前仅根据可见信息进行，不能用标注的正确目标位置反向裁剪，否则会泄露答案。记录快照缺失、映射失败、长度等排除原因。
4. **真实闭环另行建设**：离线训练使用记录反馈。仓库工具闭环需隔离的临时仓库和命令执行环境；网页闭环需本地可重复的浏览器和页面。对运行系统的参数、结果格式和轨迹工具规范建立一致性测试；不要把离线快照检查称为真实执行。
5. **进入训练前**：审核两类既有样本的原始协议、许可、敏感信息与质量；分别准备验证/测试流程，并完成 GPU 短试验。上述接口验证和转换是待做工作，本次未生成第三来源 Qwen 样本。
