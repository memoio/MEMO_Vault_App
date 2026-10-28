# ChatGPT 账户导入

在 MEMO Vault 的统一五步导入向导中选择“ChatGPT 导出”，选定目标项目，并上传 ChatGPT 账户导出的原始 ZIP。系统先进行 ZIP 安全检查，读取 `conversations.json`，将其转换为 UMIF；用户确认预览后，原 ZIP、UMIF 和投影记录归档到 Vault。

解析器沿每个会话的 `current_node`/`parent` 链确认当前分支。当前分支中可见的用户与助手 `text` 消息可进入候选抽取；其他可见分支保留在 UMIF/Vault，但标记为不参与记忆抽取。`is_do_not_remember` 会禁止该会话进入抽取。隐藏的 `thoughts`、`reasoning_recap`、系统内容和未知内容类型保留在原 ZIP，不展示或抽取其正文。明确的工具调用与结果进入独立 Agent 轨迹，不进入聊天记忆。

抽取候选和归档是两个动作。候选经人工审核发布后才成为 EverOS 正式记忆，并可出现在记忆库和 Memory Pack 中。ZIP 内若有可精确定位的附件字节，系统将其作为待审批文档保留在 Vault；仅用户点击“批准进入 Knowledge”后才投递到目标项目 Knowledge。只有已投递的文档可从 Knowledge 导出中心下载。未能定位的附件引用不生成文档。

2026-10-26 提供的样本 ZIP 含 3 个会话、9 条可见文本消息，无可确认工具调用。`library_files.json` 为空，ZIP 不含项目附件字节或项目映射字段，因此该样本只能验收聊天至 Memory Pack 的路径，不能用于验证项目附件投递。

单 ZIP 上传沿用 Vault 的文件大小、解压大小、文件数及压缩比限制。导出包中未知成员始终保留在原 ZIP，解析报告记录保留文件清单；重复导入不会重复归档。当前不提供 ChatGPT 账号登录或在线下载。
