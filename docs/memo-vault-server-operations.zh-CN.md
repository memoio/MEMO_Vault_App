# MEMO Vault 服务器部署与运维速查

本手册针对测试服务器上的现有部署。所有命令在服务器执行，示例使用 root 用户；具体状态以服务器实际配置为准。

| 组件 | 运行方式 | 地址或位置 |
|---|---|---|
| 公网入口 | 管理员的 HTTPS 代理 | `https://memo-vault-app-test.memolabs.net/` |
| 前端 | Nginx 提供构建后的静态文件 | `/var/www/memo-vault/`，服务器监听 3000 |
| Vault 管理 API | systemd：`memo-vault` | `127.0.0.1:8765` |
| EverOS 核心 | systemd：`memo-everos` | `127.0.0.1:8000` |
| Ollama | Snap：`ollama.listener` | `127.0.0.1:11434` |
| 外部网关（可选） | systemd：`memo-gateway`，须先创建服务 | `127.0.0.1:8766` |

项目目录是 `/root/workspace/MEMO_Vault_App`；EverOS 数据目录是项目内的 `.everos-ollama/`，Vault 数据目录是 `.memo-vault/`。网关只监听本机，启动服务不等于开放公网访问。确认服务文件与实际部署一致：

```bash
systemctl cat memo-everos memo-vault
systemctl cat memo-gateway  # 网关尚未创建时会提示找不到服务
```

## 最常用：拉取新代码后统一重新部署

适用场景：新需求可能同时修改前后端，不确定具体改了哪些文件；服务器已配置 `memo-everos`、`memo-vault` 和 `memo-gateway`，Ollama 与 Nginx 配置无需调整。**前端是静态文件，需要重新构建和复制，没有单独的前端服务要重启。** 重启会中断正在进行的抽取；先等待任务结束并安排维护时间。

### 1. 检查代码和依赖变化

在新服务器执行：

```bash
cd /root/workspace/MEMO_Vault_App
git status --short
git branch --show-current
git log -1 --oneline
```

如有未提交修改（此前服务器的 `uv.lock`、README 或 `auth.py` 可能被修改），先妥善保存并处理；不要使用 `git reset --hard` 覆盖。确认当前分支、远程地址和要发布的版本后，才更新：

```bash
git remote -v
git pull --ff-only
```

`git pull` 失败时停止并处理，不要继续部署。若需要检查拉取涉及的依赖文件，在拉取**前**记录旧提交，在拉取成功后比较；例如将 `<旧提交>` 换成拉取前 `git log -1 --oneline` 显示的提交 ID：

```bash
git diff --name-only <旧提交> HEAD -- pyproject.toml uv.lock apps/memo-vault-ui/package.json apps/memo-vault-ui/package-lock.json
```

如果 Python 依赖声明或锁文件变化，按新版本说明更新依赖后再继续；不要盲目执行 `uv sync`，它可能移除手动安装的本地 reranker/MCP 依赖或换掉 CPU 版 PyTorch。检查方法见第四节。若有数据库迁移或新配置，也先按新版本说明处理。上线前应备份数据。

### 2. 构建并发布前端

以下命令中每一步成功才会继续下一步；失败时查看报错，修复后重新执行：

```bash
cd /root/workspace/MEMO_Vault_App/apps/memo-vault-ui
export PATH="/opt/node24/bin:$PATH"
node --version
npm ci && npm run build && cp -a dist/. /var/www/memo-vault/
```

无需重启 Vite：正式部署由 Nginx 提供静态文件。此复制不是原子发布，在复制过程中可能短暂出现新旧资源混用；需要严格无中断时应另行配置版本目录和原子切换。

### 3. 依次重启后端和网关

确认没有正在进行的任务，然后执行：

```bash
systemctl restart memo-everos
curl --retry 10 --retry-connrefused --retry-delay 2 --max-time 5 --fail http://127.0.0.1:8000/health
```

确认返回 `status: ok` 后，再执行：

```bash
systemctl restart memo-vault
systemctl restart memo-gateway
systemctl status memo-everos memo-vault memo-gateway --no-pager
```

如果网关尚未创建或本次不使用，跳过网关命令。若前面的健康检查失败，先查看 `journalctl -u memo-everos -n 100 --no-pager`，不要继续。通常不需要重启 Ollama 或 Nginx；如服务文件发生变化，重启前先运行 `systemctl daemon-reload`。

### 4. 验证

```bash
curl --max-time 10 http://127.0.0.1:8766/health  # 网关已启动时
curl -i --max-time 15 -H 'Origin: https://memo-vault-app-test.memolabs.net' https://memo-vault-app-test.memolabs.net/api/v1/auth/session
```

浏览器刷新 `https://memo-vault-app-test.memolabs.net/`，检查页面、会话和 EverOS 在线状态，再用小批次验证新功能。Vault 重启后可能需要重新登录。网关健康检查只表示进程可达，不代表外部授权已启用。

## 一、修改后需要做什么

| 修改 | 操作 |
|---|---|
| 前端源码 | 重新构建并发布 `dist`；无需重启后端 |
| Vault 后端代码或运行环境变量 | 重启 `memo-vault` |
| EverOS 核心代码或 `.everos-ollama/everos.toml` | 重启 `memo-everos` |
| 网关代码或网关环境变量 | 重启 `memo-gateway`；须先安装该服务 |
| Nginx 配置 | `nginx -t` 成功后 `systemctl reload nginx` |
| systemd 服务文件 | 先 `systemctl daemon-reload`，再重启受影响的服务 |
| Ollama 模型或服务配置 | 按需操作 Ollama；安装模型不需要重启全部服务 |

重启后端会中断进行中的请求；先等抽取或导入结束。Vault 重启后，浏览器会话可能需要重新登录。若开启了免 token 演示模式，任何能访问管理页面的人都可能管理这份数据；应在可信访问范围内使用。

## 二、重新部署前

```bash
cd /root/workspace/MEMO_Vault_App
git status --short
git diff --stat
```

先确认当前分支、目标版本和未提交修改。服务器上的 `uv.lock`、README 或 `auth.py` 可能有本地更改；不要直接覆盖或盲目 `git pull`。按团队方式保存这些更改，并确保 `pyproject.toml`、`uv.lock` 来自同一版本。

部署前备份 `.everos-ollama/` 和 `.memo-vault/`；这些目录包含配置、记忆、运行状态及凭据。为得到一致的备份，停止写入服务并按组织的备份方式操作。不要删除它们，也不要把凭据提交到 Git。上线前确认恢复方式和备份位置。

## 三、只更新前端

```bash
cd /root/workspace/MEMO_Vault_App/apps/memo-vault-ui
export PATH="/opt/node24/bin:$PATH"
node --version
npm ci
npm run build
```

确认构建成功后发布：

```bash
cp -a dist/. /var/www/memo-vault/
```

刷新浏览器验证。`npm ci` 适用于锁文件与依赖声明一致的情况；若只修改源码且依赖没变，也可直接 `npm run build`。复制完成前不要把失败的构建当作已发布；此复制方式不清理旧资源，长期可改用带回滚的发布方式。

## 四、完整维护与重新启动

以下假设新版本代码已经按团队流程准备好、依赖已检查，且网关服务已经创建。维护期间先停止接收新的操作，并等待正在运行的抽取结束。

1. 停止写入服务；如未安装网关，跳过第一条：

```bash
systemctl stop memo-gateway
systemctl stop memo-vault
systemctl stop memo-everos
```

2. 如有需要，在服务停止后进行一致性备份、更新已确认的代码和依赖，然后按第三节构建并发布前端。不要把旧机器的 `.venv` 或 `node_modules` 直接复制过来。

后端依赖确实变更时，先核对锁文件，再按新版本的安装说明安装。`uv sync` 可能移除手动安装的 MCP 或本地 reranker 依赖，也可能改变 PyTorch 版本；同步后务必确认 CPU 版 PyTorch、`sentence-transformers` 和需要的可选依赖仍可用：

```bash
cd /root/workspace/MEMO_Vault_App
.venv/bin/python -c "import torch, sentence_transformers; print('torch:', torch.__version__); print('reranker dependencies OK')"
```

如需 stdio MCP，还应检查：

```bash
.venv/bin/python -c "import mcp; print('MCP dependency OK')"
```

3. 如果编辑了 systemd 服务文件，先执行：

```bash
systemctl daemon-reload
```

4. 确认 Ollama 正常，再依次启动核心、管理 API 和可选网关：

```bash
snap services ollama
curl --max-time 10 http://127.0.0.1:11434/api/tags
systemctl start memo-everos
curl --max-time 15 http://127.0.0.1:8000/health
systemctl start memo-vault
systemctl start memo-gateway  # 未安装或不需要网关时跳过
```

如果 EverOS 健康检查失败，先看日志并修复，再继续。若 Nginx 配置也有变化，检查并重载：

```bash
nginx -t
systemctl reload nginx
```

5. 按第六节验证。维护完成后恢复用户访问。Ollama 和 Nginx 平时无需为了更新项目代码而重启。

## 五、常用服务管理命令

```bash
# 状态
systemctl status memo-everos memo-vault nginx --no-pager
systemctl status memo-gateway --no-pager  # 可选
snap services ollama

# 查看日志（-f 实时跟随；Ctrl+C 只退出查看）
journalctl -u memo-everos -n 100 --no-pager
journalctl -u memo-vault -f
journalctl -u memo-gateway -f  # 可选
snap logs ollama -n=100

# 按需重启
systemctl restart memo-everos
systemctl restart memo-vault
systemctl restart memo-gateway  # 可选

# Ollama：仅在需要重启时执行，会中断当前模型请求
snap restart ollama

# 开机启动状态
systemctl is-enabled memo-everos memo-vault nginx
systemctl is-enabled memo-gateway  # 可选
```

网关服务若已创建但未启用，可用 `systemctl enable --now memo-gateway` 启用并启动。网关启动后，还须在“授权与管理”中启用外部访问并创建授权；`/health` 通过不代表已有可用授权。

## 六、部署后验证

```bash
curl --max-time 10 http://127.0.0.1:11434/api/tags
curl --max-time 15 http://127.0.0.1:8000/health
curl --max-time 10 http://127.0.0.1:8766/health  # 网关启用时
ss -lntp | grep -E ':(3000|8000|8765|8766|11434)\b'
```

检查公网入口的会话接口：

```bash
curl -i --max-time 15 \
  -H 'Origin: https://memo-vault-app-test.memolabs.net' \
  https://memo-vault-app-test.memolabs.net/api/v1/auth/session
```

应返回 HTTP 200；`authenticated` 的值取决于实际配置的认证模式。使用浏览器检查页面、登录或演示模式、EverOS 在线状态，再用小批次验证导入与抽取。生产前端不会自动填写所有者 token。勿将 token、私人导入数据或完整敏感日志发到聊天中。

断开 SSH 不会停止 systemd、Nginx 或 Snap 服务；服务器重启、网络或上游代理故障、服务失败和维护重启均可能导致页面或部分功能暂时不可用。服务器无 GPU，抽取可能较慢；不要连续重复提交同一任务。


## 七、日志窗口观察进程

终端 A：Vault 后端

```bash
journalctl -u memo-vault -f -o short-iso
```
这里可以观察网页请求是否到达 Vault，以及 Vault 最终返回什么状态。

终端 B：EverOS 核心
```bash
journalctl -u memo-everos -f -o short-iso
```

这里可以观察抽取请求、错误和部分处理日志。具体日志字段取决于新服务器上的代码版本；请求的访问日志通常在请求完成后才出现。

终端 C：Ollama 模型
```bash
snap logs -f ollama
```

这里最适合观察模型实际工作：输入处理进度、生成速度、请求完成状态。

这三个命令中的 -f 都是持续跟踪日志；在对应终端按 Ctrl+C 只是退出日志查看，不会停止服务。
