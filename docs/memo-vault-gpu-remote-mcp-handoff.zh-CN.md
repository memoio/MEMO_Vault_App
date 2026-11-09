# MEMO Vault GPU 服务器远程 MCP 部署交接

适用对象：已完成 MEMO Vault GPU 服务器基础部署、需要在现有 HTTPS 域名上增加远程 MCP 的管理员。本文不要求重装模型或重建 Vault 数据库。

目标地址：`https://memo-vault-app-test-gpu.memolabs.net/mcp`

调用链：MCP 客户端 → HTTPS 入口 `/mcp` → MCP 服务 `127.0.0.1:8767` → 外部授权网关 `127.0.0.1:8766` → EverOS 核心 `127.0.0.1:8000`。Vault 管理服务 `8765` 用于创建授权和 Key。

## 0. 范围与现有配置

- 此前提供的 GPU 机器截图表明运行账户为 `memo-vault`、Vault 数据目录为 `/srv/memo-vault/data/vault`。下文假定代码位于 `/srv/memo-vault/app`；代码目录、服务名称、Nginx 配置路径和公网入口层数请以管理员实际环境为准。
- 保留现有 `MEMO_VAULT_UI_ORIGINS` 的两个来源：`https://memo-vault-app-test-gpu.memolabs.net`、`http://183.240.197.189:21803`。它们用于 Vault 管理页面和现有外部网关的来源/主机检查；MCP 服务的 `MEMO_VAULT_MCP_ALLOWED_HOSTS` 是独立配置。不要为了 MCP 将管理页面 Origins 改成其他域名，也不要覆盖现有的其他环境变量。
- 截图显示的进程环境只包含 HTTPS 来源，用户后续说明已配置 HTTPS 与 HTTP 两项。请以管理员检查到的**实际运行进程环境**为准，确认两个值是否确实生效。配置文件中的反斜杠转义写法取决于使用的是 systemd 环境文件、unit 文件还是 shell；以程序实际收到的值为准，正确的 Origin 是 `https://...` 和 `http://...`，而非含反斜杠的字符串。
- MCP 访问使用 HTTPS 域名；HTTP IP 地址仅是现有管理页面来源之一，不应把带 Key 的 MCP 流量发送到明文 HTTP。管理员应确认该 HTTP 管理入口的访问限制。
- 远程 MCP 所需代码必须已经提交并推送。管理员须按项目负责人确认的提交号部署；不要假定旧版 `841b11f`、旧交接包或某个本地未提交修改已经包含在 GitHub 中。固定 Pack 省略 `pack_id` 的兼容修复应单独核对是否包含在该提交。
- 若数据库为新建实例，原环境创建的授权 Key 不会自动存在；管理员和项目负责人需在新实例重新创建授权。

## 1. 更新代码后的检查

下列目录只是示例，先确认实际路径与部署提交：

```bash
cd /srv/memo-vault/app

git status --short
git log -1 --oneline
test -f src/everos/entrypoints/memo_vault_mcp/http_server.py \
  && echo '远程 MCP 代码存在' \
  || echo '缺少远程 MCP 代码，请核对交付版本'
.venv/bin/python -c 'import mcp; print("MCP 依赖可用")'
```

如果代码从无 `.git` 的交接包导入，使用交接包版本清单核对，不执行 Git 命令。如果依赖缺失，在管理员确认的项目虚拟环境中按项目的依赖锁文件安装 `mcp` 可选依赖；避免无计划地更换共享环境或重启共享 Ollama。

确认端口和服务：

```bash
sudo ss -ltnp | grep -E ':(8000|8765|8766|8767|3000)[[:space:]]'
sudo systemctl status memo-everos memo-vault memo-gateway --no-pager
```

如果网关服务不存在，按基础部署文档或现有运维方式先配置网关。Vault 管理服务与网关必须使用相同的 `MEMO_VAULT_DATA_DIR` 和预期的 EverOS 地址；MCP 使用服务端配置的网关地址。部署前确认无正在执行的重要任务，备份现有代码、配置和数据，安排重启窗口。

### 确认 Vault 认证和来源

现有管理服务应显式设置 `MEMO_VAULT_PUBLIC_DEMO=0`，确认未登录不能访问管理接口，合法管理员可以登录。检查来源值时，可先根据 `systemctl show -p MainPID --value memo-vault` 得到 PID，再由管理员仅查看该进程的 `MEMO_VAULT_UI_ORIGINS` 值；不要把完整环境、Key 或密码发到聊天。更新 Vault 或网关配置时保留其他已有配置，按维护流程重启并复核。

## 2. 配置 systemd MCP 服务

先检查端口 `8767` 和服务文件是否已被使用：

```bash
sudo systemctl cat memo-mcp
sudo ss -ltnp 'sport = :8767'
```

如服务文件已存在，先检查并按现有服务的维护流程更新；不要覆盖未知配置。如 `8767` 已被其他服务占用，换用管理员确认的空闲端口，并同步修改下面的 ExecStart、白名单和代理地址。

在确认 `/etc/systemd/system/memo-mcp.service` 不存在后，可使用以下示例创建；实际代码路径、用户须先核对：

```bash
sudo tee /etc/systemd/system/memo-mcp.service >/dev/null <<'UNIT'
[Unit]
Description=MEMO Vault remote MCP
After=network-online.target memo-everos.service memo-vault.service memo-gateway.service
Wants=network-online.target

[Service]
Type=simple
User=memo-vault
Group=memo-vault
WorkingDirectory=/srv/memo-vault/app
Environment=MEMO_VAULT_MCP_GATEWAY_URL=http://127.0.0.1:8766
Environment=MEMO_VAULT_MCP_ALLOWED_HOSTS=memo-vault-app-test-gpu.memolabs.net,127.0.0.1:8767,localhost:8767
ExecStart=/srv/memo-vault/app/.venv/bin/uvicorn everos.entrypoints.memo_vault_mcp.http_server:app --host 127.0.0.1 --port 8767 --workers 1 --no-access-log
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
```

此服务读取的是网关 URL 和 Host 白名单，不在 unit 中保存客户端 Key。不同网关监听地址或网络命名空间需要按实际调整。`After` 只是启动顺序，不表示网关已经就绪。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now memo-mcp
sudo systemctl status memo-gateway memo-mcp --no-pager
sudo journalctl -u memo-mcp -n 50 --no-pager
```

本机无 Key 检查：

```bash
curl --noproxy '*' -sS -o /dev/null \
  -w 'HTTP %{http_code}\n' \
  http://127.0.0.1:8767/mcp
```

预期 `HTTP 401`。它只证明本机端点对该无凭据请求拒绝，不证明网关授权或公网可用。

## 3. 在现有 HTTPS 路径上代理 /mcp

由管理员确认 GPU 域名实际经过的 HTTPS 入口、Nginx 站点配置和代理链。备份现有配置，在**正确站点的 `server` 块内**增加路由，不替换整个站点；如已有 `/mcp` 路由先检查冲突：

```nginx
location = /mcp {
    proxy_pass http://127.0.0.1:8767;
    proxy_http_version 1.1;
    proxy_set_header Host memo-vault-app-test-gpu.memolabs.net;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-Forwarded-Proto https;
    proxy_buffering off;
    proxy_read_timeout 120s;
    proxy_send_timeout 120s;
    access_log off;
}
```

若前面另有 HTTPS 入口，需让它将 `/mcp` 保持原路径转发到上述站点，并保留 `Authorization`、`Host`、`Accept`、`Content-Type`、MCP 协议请求及响应头；确认代理允许必要的 HTTP 方法。上游访问日志也不得记录凭据。检查无误后：

```bash
sudo nginx -t
```

仅当检查通过时，再按管理员维护流程执行 `sudo systemctl reload nginx`。不要公开 8000、8765、8766、8767 等内部端口；管理页面的外部入口按现有 HTTPS/访问控制架构配置。

## 4. 创建授权 Key

1. 在 GPU 服务器对应的 Vault 管理页面登录，进入「授权与 MCP」。
2. 确认外部访问已恢复。外部网关健康检查正常不等于授权策略已经开放。
3. 创建测试授权，限制项目、操作与有效期，使用测试数据。授权创建后按页面提示安全保存一次性显示的 `mv_...` 外部 Key。
4. 不要使用 Vault 管理员登录凭据作为 MCP Key，也不要在聊天、日志或截图中发送真实 Key。

如果交付版本包含前端改动，还需在项目的 `apps/memo-vault-ui` 中构建，并由管理员按 GPU 服务器原有发布方式部署 `dist`；仅拉取代码不会更新线上静态页面。不要假定旧服务器的 `/var/www/memo-vault` 是 GPU 服务器当前静态目录。

## 5. 从服务器外验收

从外部电脑测试 HTTPS 地址：

```bash
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  https://memo-vault-app-test-gpu.memolabs.net/mcp
```

无 Key 预期拒绝（通常为 `401`），但反向代理本身也可能产生 `401`；还须验证后续有效 Key 工具调用，才能确认完整链路。若返回前端 HTML，应核对 HTTPS 入口和 `/mcp` 代理。

用支持 **Streamable HTTP + Bearer 请求头**的 MCP 客户端配置：

```text
URL: https://memo-vault-app-test-gpu.memolabs.net/mcp
Authorization: Bearer <新实例的外部授权 Key>
```

完成以下检查：

- 有效 Key 能连接、发现获授权的工具并读取测试数据；确认不同项目和工具权限不会交叉。
- 无效、过期或已吊销 Key 被拒绝；测试结束后吊销临时授权。
- 固定 Pack 先用 `get_pack_info` 检查。`read_memory_pack` 一旦成功交付，会消耗单次授权，不要自动重试。
- 用所选真实客户端复测；MCP Inspector 是可选测试客户端，不是服务器依赖。当前方案采用静态 Bearer Key，没有 OAuth；浏览器网页直接跨域请求需要额外 CORS 支持，不能保证任意在线测试网站可用。

如失败，检查 `sudo journalctl -u memo-mcp -n 50 --no-pager`、`sudo journalctl -u memo-gateway -n 50 --no-pager` 及代理日志，提交排障信息前移除 Key、个人记忆内容和其他敏感信息。修改、重启及回滚由管理员按维护流程执行。
