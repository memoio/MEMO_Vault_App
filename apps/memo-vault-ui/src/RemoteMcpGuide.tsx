import { useState } from 'react';
import type { Grant } from './gatewayApi';

type Client = 'claude' | 'codex' | 'cursor' | 'vscode' | 'desktop' | 'other';
const clients: { id: Client; name: string }[] = [
  { id: 'claude', name: 'Claude Code' }, { id: 'codex', name: 'Codex' },
  { id: 'cursor', name: 'Cursor' }, { id: 'vscode', name: 'VS Code' },
  { id: 'desktop', name: 'Claude Desktop' }, { id: 'other', name: '其他客户端' },
];

function configuration(client: Client, url: string, token: string): string {
  if (client === 'claude') return `claude mcp add --transport http memo-vault ${url} \\\n  --header "Authorization: Bearer ${token}"`;
  if (client === 'codex') return `[mcp_servers.memo-vault]\nurl = ${JSON.stringify(url)}\nhttp_headers = { Authorization = ${JSON.stringify(`Bearer ${token}`)} }`;
  if (client === 'desktop') return JSON.stringify({ mcpServers: { 'memo-vault': { command: 'npx', args: ['-y', 'mcp-remote', url, '--header', 'Authorization:${MEMO_VAULT_AUTH}'], env: { MEMO_VAULT_AUTH: `Bearer ${token}` } } } }, null, 2);
  const server = { url, headers: { Authorization: `Bearer ${token}` } };
  return JSON.stringify(client === 'vscode' ? { servers: { 'memo-vault': { type: 'http', ...server } } } : { mcpServers: { 'memo-vault': server } }, null, 2);
}

export default function RemoteMcpGuide({ grant, url, revealedToken }: { grant: Grant | undefined; url: string; revealedToken?: string }) {
  const [client, setClient] = useState<Client>('claude');
  const [copyError, setCopyError] = useState('');
  async function copy(value: string) {
    try { await navigator.clipboard.writeText(value); setCopyError(''); }
    catch { setCopyError('复制失败，请检查浏览器剪贴板权限。'); }
  }
  const credential = revealedToken ?? '';
  const display = configuration(client, url, credential ? `mv_••••••••••••${credential.slice(-4)}` : '<YOUR_TOKEN>');
  const full = configuration(client, url, credential);
  const copyReady = !!grant && grant.status === 'active' && !!credential;
  const example = `在已授权的项目空间中，搜索与“项目计划”相关的记忆。`;
  const clientName = clients.find(item => item.id === client)?.name ?? '客户端';
  const details: Record<Client, { title: string; intro: string; steps: string[]; verify: string }> = {
    claude: { title: '确认授权与 Token', intro: '无需浏览器登录；Token 已写入请求头。', steps: ['在 Claude Code 会话中输入 /mcp', '列表中找到 memo-vault，检查连接状态', '如使用项目级配置，按提示批准该服务器'], verify: '在终端运行命令检查服务器；若连接失败，请检查 Vault、网关及 MCP 服务。' },
    codex: { title: '重启会话并确认', intro: '保存配置后重新打开 Codex 会话。', steps: ['重新打开 Codex CLI 或 IDE 插件会话', '在会话中输入 /mcp，检查 memo-vault', '查看工具列表中该授权允许的操作'], verify: '在终端运行命令，确认 memo-vault 已列出并启用。' },
    cursor: { title: '在 Cursor 中启用', intro: '保存配置后，在 Cursor 中检查服务器状态。', steps: ['打开 Cursor 设置 → MCP（Tools & MCP）', '找到并启用 memo-vault', '确认连接状态及可用工具'], verify: '在 Agent 对话中提问；首次调用工具时按客户端提示确认。' },
    vscode: { title: '启动服务器并确认', intro: '在 VS Code 中启动 MCP 服务器。', steps: ['打开 mcp.json，点击 memo-vault 上方的“启动”', '或通过命令面板运行 MCP: List Servers 查看状态', '在 Copilot Chat 中切到 Agent 模式，检查工具列表'], verify: '在 Agent 模式下提问；首次调用工具时按提示确认。' },
    desktop: { title: '重启并确认', intro: '保存后完全退出并重新打开 Claude Desktop。', steps: ['完全退出 Claude Desktop（包括托盘或菜单栏）后重新打开', '在设置中查看 memo-vault 的运行状态', '检查对话输入框的工具菜单'], verify: '在新对话中提问；首次调用工具时按提示允许。' },
    other: { title: '填入服务地址与 Token', intro: '服务地址和认证请求头均需配置，Token 不要放入 URL。', steps: ['传输方式：Streamable HTTP', `服务地址：${url}`, '请求头：Authorization: Bearer 加上该授权的 Token'], verify: '刷新客户端工具列表，检查 memo-vault 提供的工具。' },
  };
  const detail = details[client];
  return <div className="remote-mcp-guide">
    <p className="muted">选择客户端，按步骤添加服务并验证连接。凭证决定实际授权范围。</p>
    <div className="remote-client-tabs" role="group" aria-label="选择 MCP 客户端">{clients.map(item => <button key={item.id} type="button" aria-pressed={client === item.id} className={client === item.id ? 'primary-button' : 'secondary-button'} onClick={() => setClient(item.id)}>{item.name}</button>)}</div>
    {!grant ? <p>请先选择授权。</p> : <>
      {grant.status !== 'active' && <p className="notice warning">当前授权未生效，请选择有效授权。</p>}
      {!credential && grant.status === 'active' && <p className="remote-token-hint">此授权创建于旧版本。请在连接测试中输入已保存的凭证，或更换凭证；验证后即可自动使用。</p>}
      {copyError && <p className="remote-copy-error" role="alert">{copyError}</p>}
      <ol className="remote-mcp-steps">
        <li><h3>添加 MCP Server</h3><p>{client === 'claude' ? '在终端运行命令，通过 HTTP 连接远程 MCP 服务。默认仅当前项目生效；可加 --scope user 对所有项目生效。' : client === 'codex' ? '将以下配置追加到 ~/.codex/config.toml（项目级为 .codex/config.toml）；CLI 和 IDE 插件共用该配置。' : client === 'cursor' ? '将以下配置合并到 ~/.cursor/mcp.json 或项目的 .cursor/mcp.json。' : client === 'vscode' ? '将以下配置合并到项目的 .vscode/mcp.json。' : client === 'desktop' ? '安装 Node.js，并将以下配置合并到 Claude Desktop 配置文件；这里使用 mcp-remote 桥接。' : '在支持 Streamable HTTP 和自定义请求头的客户端中填写以下配置。'}</p>
          <div className="remote-mcp-code"><pre>{display}</pre><button className="secondary-button" disabled={!copyReady} onClick={() => void copy(full)}>复制</button></div>
          <small className="muted">复制内容包含完整凭证，请保存在私密位置，勿提交到代码仓库。</small>
        </li>
        <li><h3>{detail.title}</h3><p>{detail.intro}</p><strong>你将看到</strong><ol>{detail.steps.map(step => <li key={step}>{step}</li>)}</ol></li>
        <li><h3>验证连接</h3><p>{detail.verify}</p>{(client === 'claude' || client === 'codex') && <div className="remote-mcp-code"><pre>{client === 'claude' ? 'claude mcp list' : 'codex mcp list'}</pre><button className="secondary-button" onClick={() => void copy(client === 'claude' ? 'claude mcp list' : 'codex mcp list')}>复制</button></div>}
          <div className="remote-mcp-example"><strong>在 {clientName} 中试试</strong><p>{example}</p><button className="secondary-button" onClick={() => void copy(example)}>复制</button></div>
        </li>
      </ol>
    </>}
  </div>;
}
