# 远程 MCP（本地验证版）

在运行 EverOS、Vault 管理服务和外部网关后，安装 `pip install -e '.[mcp]'`，开启外部访问并创建外部授权。管理端凭据不能作为 MCP Key。使用单独的受限外部授权凭据，凭据仅在创建或轮换时显示一次。

本机启动：

```bash
MEMO_VAULT_MCP_GATEWAY_URL=http://127.0.0.1:8766 .venv/bin/uvicorn everos.entrypoints.memo_vault_mcp.http_server:app --host 127.0.0.1 --port 8767 --no-access-log
```

本机 MCP 地址为 `http://127.0.0.1:8767/mcp`，请求头为 `Authorization: Bearer <外部授权凭据>`。该服务依赖 8766 外部网关和网关启用状态；每个 MCP 请求均检查授权并计入现有网关限流。已有本地 STDIO 接入不变。

正式部署时使用 HTTPS 反向代理，推荐地址 `https://memo-vault-app-test.memolabs.net/mcp`。确认目标 HTTPS 入口和路径未被其他服务使用，授权网络管理员配置将 `/mcp` 原路径代理到 `127.0.0.1:8767`，保留 Authorization、Host、Accept、Content-Type 和 MCP 协议相关请求及响应头，不记录凭据。MCP 服务设置环境变量 `MEMO_VAULT_MCP_ALLOWED_HOSTS=memo-vault-app-test.memolabs.net`；如果实际代理改写 Host，须以实际接收到的 Host 配置白名单。仅支持规定的来源，浏览器调用需配置 `MEMO_VAULT_MCP_ALLOWED_ORIGINS`。不得直接公网暴露 8767、8766 或 8000。外网访问前实测 TLS、无 Key 拒绝、有效 Key 调用、撤销立即拒绝和不同授权的隔离。

客户端必须支持 Streamable HTTP 和自定义 Bearer 请求头；本版本未实现 OAuth，不能保证只接受 OAuth 的客户端兼容。协议版本由所安装 SDK 和客户端协商，不能照搬其他服务的版本号。地址或代理未实际部署时，页面显示的连接信息不意味着服务已经上线。
