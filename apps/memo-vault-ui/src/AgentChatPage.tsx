import ChatSelect from './ChatSelect';
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react';
import { ownerFetch } from './ownerSession';
import type { ProjectSpace } from './api';
import './agentChat.css';

type Model = { id: string; name: string; agent_id: string; agent_name: string };
type Session = { id: string; project_id: string; title: string; updated_at: string; generating?: boolean | number };
type ContextSource = {
  id: string;
  source_type: 'memory' | 'knowledge';
  title: string;
  snippet: string;
  project_id: string;
  project_name: string;
  score: number;
  kind?: string;
  memory_type?: string | null;
  revision?: number | null;
  doc_id?: string | null;
  topic_id?: string | null;
  available?: boolean;
};
type ContextMeta = {
  status: 'disabled' | 'ready' | 'empty' | 'partial_failure' | 'failed';
  project_id: string;
  subtree_project_ids: string[];
  query: string;
  memory_status: string;
  knowledge_status: string;
  sources: ContextSource[];
  trimmed: boolean;
  error_message?: string | null;
};
type Message = {
  id: string;
  role: string;
  content: string;
  status: string;
  model_id: string | null;
  agent_id?: string | null;
  framework_version?: string | null;
  use_project_context?: boolean;
  context_status?: string | null;
  context_meta?: ContextMeta | null;
  events?: { type: string; content: string }[];
};
type Detail = Session & { messages: Message[] };
type Selection = { model: string; agent: string };
type UploadedAttachment = {
  attachment_id: string;
  filename: string;
  char_count: number;
  truncated: boolean;
  content: string;
  snippet: string;
};

const defaultSelection: Selection = { model: 'qwen', agent: 'default' };
const validModels = new Set(['qwen', 'qwen-1', 'qwen-2', 'qwen-3']);
const validAgents = new Set(['default', 'openhands', 'swe', 'agentlab']);
const selectionKey = (id: string) => `everos.agent-chat.selection.${id}`;
const USE_CONTEXT_KEY = 'everos.agent-chat.use-project-context';
const RAG_NOTICE_DISMISSED_KEY = 'everos.agent-chat.rag-notice-dismissed';

const modelLabels: Record<string, string> = {
  qwen: 'Qwen3.5-0.8B',
  'qwen-1': 'Qwen3.5-0.8B-o',
  'qwen-2': 'Qwen3.5-0.8B-s',
  'qwen-3': 'Qwen3.5-0.8B-a',
};
const eventLabels: Record<string, string> = {
  retrieving_context: '检索项目记忆',
  context_ready: '参考资料就绪',
  context_partial: '部分来源降级',
  context_failed: '项目检索失败',
  framework_started: '框架已启动',
  model_request: '请求本地模型',
  framework_completed: '运行完成',
};

function messageName(m: Message): string {
  const version = m.framework_version;
  if (version) {
    if (version.startsWith('OpenHands')) return 'OpenHands';
    if (version.startsWith('SWE-agent')) return 'SWE-agent';
    if (version.startsWith('AgentLab')) return 'AgentLab';
    return version;
  }
  return m.agent_id === 'openhands'
    ? 'OpenHands（历史模型调用）'
    : m.agent_id === 'swe'
      ? 'SWE（历史模型调用）'
      : m.agent_id === 'agentlab'
        ? 'AgentLab（历史模型调用）'
        : 'Default Agent';
}

function pendingStageText(m: Message): string {
  const last = m.events?.[m.events.length - 1];
  if (!last) return '任务已提交，正在后台处理或等待排队…';
  if (last.type === 'retrieving_context') return '正在检索当前项目及子项目的正式记忆与 Knowledge…';
  if (last.type === 'context_ready' || last.type === 'context_partial') return '已整理项目参考资料，正在等待本地模型回复…';
  if (last.type === 'model_request') return '正在等待本地模型生成回复…';
  return '后台生成中或等待排队，可切换会话或页面…';
}

function contextSummaryLabel(meta: ContextMeta): string {
  if (meta.status === 'disabled') return '未启用项目记忆';
  if (meta.status === 'failed') return '项目记忆检索失败';
  const available = meta.sources.filter((s) => s.available !== false);
  const memCount = available.filter((s) => s.source_type === 'memory').length;
  const knCount = available.filter((s) => s.source_type === 'knowledge').length;
  if (meta.status === 'partial_failure') {
    return available.length > 0
      ? `部分来源失败 · 已参考 ${available.length} 条`
      : '部分来源检索失败 · 无可用参考';
  }
  if (available.length === 0) return '未命中相关项目资料';
  const parts: string[] = [];
  if (memCount > 0) parts.push(`${memCount} 条正式记忆`);
  if (knCount > 0) parts.push(`${knCount} 条 Knowledge`);
  return `已参考 ${parts.join(' · ')}`;
}

function saveSelection(id: string, selection: Selection) {
  try {
    localStorage.setItem(selectionKey(id), JSON.stringify(selection));
  } catch {
    /* Storage may be unavailable. */
  }
}

function readSelection(session: Detail): Selection {
  try {
    const raw = localStorage.getItem(selectionKey(session.id));
    if (raw) {
      const saved = JSON.parse(raw);
      if (validModels.has(saved.model) && validAgents.has(saved.agent)) return saved;
    }
  } catch {
    /* Fall back to message history. */
  }
  const last = [...session.messages]
    .reverse()
    .find((m) => m.model_id && m.agent_id && validModels.has(m.model_id) && validAgents.has(m.agent_id));
  return last ? { model: last.model_id!, agent: last.agent_id! } : defaultSelection;
}

function readBoolStorage(key: string, fallback: boolean): boolean {
  try {
    const val = localStorage.getItem(key);
    if (val === 'true') return true;
    if (val === 'false') return false;
  } catch {
    /* ignore */
  }
  return fallback;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await ownerFetch(`/api/v1/agent-chat${path}`, init);
  const b = await r.json();
  if (!r.ok) throw new Error(typeof b.detail === 'string' ? b.detail : '请求失败');
  return b.data as T;
}

export default function AgentChatPage({ projects }: { projects: ProjectSpace[] }) {
  const [project, setProject] = useState('');
  const [models, setModels] = useState<Model[]>([]);
  const [model, setModel] = useState('qwen');
  const [agent, setAgent] = useState('default');
  const [useProjectContext, setUseProjectContext] = useState<boolean>(() =>
    readBoolStorage(USE_CONTEXT_KEY, true),
  );
  const [showRagNotice, setShowRagNotice] = useState<boolean>(
    () => !readBoolStorage(RAG_NOTICE_DISMISSED_KEY, false),
  );
  const [retrievalAlert, setRetrievalAlert] = useState('');
  const [sessions, setSessions] = useState<Session[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [text, setText] = useState('');
  const [search, setSearch] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [renaming, setRenaming] = useState(false);
  const [titleDraft, setTitleDraft] = useState('');
  const [renameBusy, setRenameBusy] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<UploadedAttachment[]>([]);
  const [uploadingFile, setUploadingFile] = useState(false);

  const textAreaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const listSequence = useRef(0);
  const selectionSequence = useRef(0);
  const activeProject = useRef(project);
  activeProject.current = project;
  const lastAlertedMessageId = useRef<string | null>(null);

  useLayoutEffect(() => {
    const el = textAreaRef.current;
    if (el) {
      el.style.height = 'auto';
      el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
    }
  }, [text]);

  const current = models.find((m) => m.id === model);
  const pending = !!detail?.messages.some((m) => m.status === 'pending');
  const activeSession = useRef<string | null>(null);
  activeSession.current = detail?.id ?? null;

  const refresh = useCallback(async () => {
    const sequence = ++listSequence.current;
    if (!project) {
      setSessions([]);
      return;
    }
    const q = new URLSearchParams({ project_id: project });
    if (search) q.set('q', search);
    const result = await api<{ sessions: Session[] }>(`/sessions?${q}`);
    if (sequence === listSequence.current && activeProject.current === project) setSessions(result.sessions);
  }, [project, search]);

  useEffect(() => {
    if (!project && projects.length) {
      setProject(projects.find((p) => p.project_id === 'personal')?.project_id ?? projects[0].project_id);
    }
  }, [projects, project]);

  useEffect(() => {
    api<{ models: Model[] }>('/models')
      .then((x) => setModels(x.models))
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    refresh().catch((e) => setError(e.message));
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void refresh().catch(() => {});
    }, 3000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (!pending || !detail?.id) return;
    const id = detail.id;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void api<Detail>(`/sessions/${id}`)
        .then((result) => {
          if (!cancelled && activeSession.current === id) {
            setDetail(result);
            const last = result.messages[result.messages.length - 1];
            if (
              last &&
              last.role === 'assistant' &&
              last.status === 'failed' &&
              last.context_status === 'failed' &&
              lastAlertedMessageId.current !== last.id
            ) {
              lastAlertedMessageId.current = last.id;
              setRetrievalAlert(
                last.context_meta?.error_message ||
                  last.content ||
                  '项目记忆检索失败，已中止本轮回答。可重试或关闭「使用项目记忆」后继续。',
              );
            }
          }
        })
        .catch(() => {});
    }, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [detail?.id, pending]);

  function toggleProjectContext() {
    setUseProjectContext((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(USE_CONTEXT_KEY, String(next));
      } catch {
        /* ignore */
      }
      return next;
    });
  }

  function dismissRagNotice() {
    setShowRagNotice(false);
    try {
      localStorage.setItem(RAG_NOTICE_DISMISSED_KEY, 'true');
    } catch {
      /* ignore */
    }
  }

  async function create() {
    if (!project || busy) return;
    const sequence = ++selectionSequence.current;
    try {
      const created = await api<Detail>('/sessions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: project }),
      });
      if (sequence === selectionSequence.current) {
        setRenaming(false);
        setModel(defaultSelection.model);
        setAgent(defaultSelection.agent);
        setDetail(created);
        setRetrievalAlert('');
      }
      await refresh();
      setError('');
    } catch (e) {
      setError(String(e));
    }
  }

  async function select(id: string) {
    if (busy) return;
    const sequence = ++selectionSequence.current;
    try {
      const s = await api<Detail>(`/sessions/${id}`);
      if (sequence !== selectionSequence.current) return;
      const selection = readSelection(s);
      setModel(selection.model);
      setAgent(selection.agent);
      setDetail(s);
      setProject(s.project_id);
      setRenaming(false);
      setRetrievalAlert('');
      setError('');
    } catch (e) {
      if (sequence === selectionSequence.current) setError(String(e));
    }
  }

  async function removeSession(session: Session) {
    if (busy || deleteBusy) return;
    if (!window.confirm(`确定删除会话“${session.title}”吗？该会话的所有消息也将删除，无法撤销。`)) return;
    setDeleteBusy(session.id);
    setError('');
    try {
      await api(`/sessions/${session.id}`, { method: 'DELETE' });
      setSessions((previous) => previous.filter((item) => item.id !== session.id));
      try {
        localStorage.removeItem(selectionKey(session.id));
      } catch {
        /* Storage may be unavailable. */
      }
      if (detail?.id === session.id) {
        setDetail(null);
        setRenaming(false);
        setRetrievalAlert('');
      }
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : '删除会话失败');
    } finally {
      setDeleteBusy(null);
    }
  }

  async function rename() {
    if (!detail || renameBusy) return;
    const title = titleDraft.trim();
    if (!title || title.length > 100) {
      setError('会话名称须为 1 到 100 个字符');
      return;
    }
    setRenameBusy(true);
    try {
      const result = await api<Detail>(`/sessions/${detail.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
      });
      setDetail(result);
      setRenaming(false);
      setError('');
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : '重命名失败');
    } finally {
      setRenameBusy(false);
    }
  }

  async function handleFileSelect(file: File | undefined) {
    if (!file) return;
    setError('');
    setUploadingFile(true);
    try {
      const form = new FormData();
      form.append('file', file);
      const res = await ownerFetch('/api/v1/agent-chat/attachments', {
        method: 'POST',
        body: form,
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detailObj = typeof body.detail === 'object' ? body.detail : undefined;
        throw new Error(detailObj?.message || (typeof body.detail === 'string' ? body.detail : '附件解析失败'));
      }
      setAttachments((prev) => [...prev, body.data as UploadedAttachment]);
    } catch (e) {
      setError(e instanceof Error ? e.message : '附件上传失败');
    } finally {
      setUploadingFile(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  }

  async function send() {
    if (!text.trim() || !current || !project || busy || pending) return;
    const value = text;
    const currentAttachments = attachments;
    setBusy(true);
    setError('');
    setRetrievalAlert('');
    setText('');
    let s = detail;
    try {
      if (!s || s.project_id !== project) {
        s = await api<Detail>('/sessions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_id: project }),
        });
      }
      saveSelection(s.id, { model, agent });
      setDetail({
        ...s,
        messages: [
          ...s.messages,
          { id: 'user-pending', role: 'user', content: value, status: 'completed', model_id: model },
          {
            id: 'assistant-pending',
            role: 'assistant',
            content: useProjectContext
              ? '正在检索当前项目及子项目的正式记忆与 Knowledge…'
              : '任务已提交，正在后台生成或等待排队…',
            status: 'pending',
            model_id: model,
            use_project_context: useProjectContext,
          },
        ],
      });
      const result = await api<Detail>(`/sessions/${s.id}/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          content: value,
          model_id: model,
          agent_id: agent,
          request_id: crypto.randomUUID(),
          use_project_context: useProjectContext,
          attached_materials: currentAttachments.map((att, idx) => ({
            ref_id: `M${idx + 1}`,
            source_type: 'file_attachment',
            title: att.filename,
            source_url: '',
            content: att.content,
          })),
        }),
      });
      setDetail(result);
      setAttachments([]);
      await refresh();
    } catch (e) {
      setError(String(e));
      setText(value);
      if (s) {
        try {
          setDetail(await api<Detail>(`/sessions/${s.id}`));
        } catch {
          /* retain visible error */
        }
      }
    } finally {
      setBusy(false);
    }
  }

  function key(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  }

  return (
    <section className="agent-chat-workspace">
      <div className="agent-chat-toolbar">
        <strong>对话空间</strong>
        <ChatSelect
          label="选择项目空间"
          prefix="Chat in"
          placement="below"
          value={project}
          disabled={busy || !projects.length}
          options={projects.map((p) => ({ value: p.project_id, label: p.name }))}
          onChange={(value) => {
            selectionSequence.current++;
            setSessions([]);
            setProject(value);
            setDetail(null);
            setModel(defaultSelection.model);
            setAgent(defaultSelection.agent);
            setRenaming(false);
            setRetrievalAlert('');
          }}
        />
        <span>本地模型 · 文本对话 · 支持项目记忆 RAG</span>
      </div>
      {error && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}
      <div className="agent-chat-grid">
        <aside className="agent-chat-sessions">
          <div className="agent-sessions-heading">
            <strong>Sessions</strong>
            <small>对话记录</small>
          </div>
          <button className="agent-new" disabled={!project || busy} onClick={() => void create()}>
            ＋ 新建对话
          </button>
          <input
            aria-label="搜索会话标题"
            placeholder="搜索会话标题"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <div className="agent-session-list">
            {sessions.length === 0 ? (
              <p>暂无对话记录</p>
            ) : (
              sessions.map((s) => (
                <div className={`agent-session-row ${detail?.id === s.id ? 'active' : ''}`} key={s.id}>
                  <button
                    className="agent-session-open"
                    onClick={() => void select(s.id)}
                    disabled={!!deleteBusy}
                  >
                    <strong>{s.title}</strong>
                    <small>
                      {projects.find((p) => p.project_id === s.project_id)?.name ?? s.project_id} · {s.updated_at}
                      {!!s.generating && <span className="agent-session-running"> · 后台生成中</span>}
                    </small>
                  </button>
                  <button
                    className="agent-session-delete"
                    aria-label={`删除会话 ${s.title}`}
                    title={s.generating ? '会话正在生成，完成后可删除' : '删除会话'}
                    disabled={busy || !!deleteBusy || !!s.generating}
                    onClick={() => void removeSession(s)}
                  >
                    <svg
                      width="16"
                      height="16"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.8"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      aria-hidden="true"
                    >
                      <path d="M4 7h16M10 11v6m4-6v6M5.5 7l1 13h11l1-13M9 7V4h6v3" />
                    </svg>
                  </button>
                </div>
              ))
            )}
          </div>
        </aside>
        <div className="agent-chat-main">
          <div className="agent-conversation-heading">
            <div className="agent-title-group">
              {renaming && detail ? (
                <form
                  className="agent-rename-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void rename();
                  }}
                >
                  <input
                    aria-label="会话名称"
                    autoFocus
                    maxLength={100}
                    value={titleDraft}
                    onChange={(e) => setTitleDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Escape') setRenaming(false);
                    }}
                  />
                  <button type="submit" disabled={renameBusy}>
                    保存
                  </button>
                  <button type="button" onClick={() => setRenaming(false)}>
                    取消
                  </button>
                </form>
              ) : (
                <>
                  <strong>{detail?.title ?? '新对话'}</strong>
                  {detail && (
                    <button
                      className="agent-rename"
                      aria-label="重命名会话"
                      title="重命名会话"
                      disabled={busy}
                      onClick={() => {
                        setTitleDraft(detail.title);
                        setRenaming(true);
                        setError('');
                      }}
                    >
                      <svg
                        width="15"
                        height="15"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.8"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        aria-hidden="true"
                      >
                        <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4z" />
                      </svg>
                    </button>
                  )}
                </>
              )}
            </div>
            <span>会话保存在本机</span>
          </div>
          <div className="agent-messages">
            {!detail?.messages.length ? (
              <div className="agent-welcome">
                <div className="agent-welcome-icon">✦</div>
                <h2>对话 Agent</h2>
                <p>选择项目和模型，开始一段结合项目记忆的本地对话。</p>
                <small>开启「使用项目记忆」后，将自动检索当前项目及子项目下已确认的正式记忆与 Knowledge。</small>
              </div>
            ) : (
              detail.messages.map((m, index) => {
                const meta = m.context_meta;
                const memorySources = meta?.sources.filter((s) => s.source_type === 'memory') ?? [];
                const knowledgeSources = meta?.sources.filter((s) => s.source_type === 'knowledge') ?? [];
                return (
                  <article key={m.id} className={`agent-message ${m.role}`}>
                    {m.role !== 'user' && <div className="agent-message-name">{messageName(m)}</div>}
                    <div className={`agent-message-body ${m.status}`}>
                      {m.content || (m.status === 'pending' ? pendingStageText(m) : '')}
                    </div>
                    {m.role === 'assistant' && m.status !== 'pending' && meta && (
                      <details className={`agent-context-details status-${meta.status}`}>
                        <summary>
                          <span className="agent-context-icon" aria-hidden="true">
                            ◈
                          </span>
                          <span>本次提供给模型的参考内容</span>
                          <span className="agent-context-badge">{contextSummaryLabel(meta)}</span>
                          <svg className="agent-run-chevron" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                            <path
                              d="m4 6 4 4 4-4"
                              stroke="currentColor"
                              strokeWidth="1.6"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                            />
                          </svg>
                        </summary>
                        <div className="agent-context-body">
                          {meta.error_message && <p className="agent-context-note warn">{meta.error_message}</p>}
                          {meta.status === 'disabled' && (
                            <p className="agent-context-note">本轮未启用项目记忆，模型仅基于对话内容回答。</p>
                          )}
                          {meta.status === 'empty' && (
                            <p className="agent-context-note">
                              在当前项目及子项目空间中未命中相关正式记忆或 Knowledge。
                            </p>
                          )}
                          {memorySources.length > 0 && (
                            <div className="agent-context-group">
                              <div className="agent-context-group-title">正式记忆（{memorySources.length}）</div>
                              {memorySources.map((src) => (
                                <div
                                  className={`agent-context-item ${src.available === false ? 'unavailable' : ''}`}
                                  key={`mem-${src.id}`}
                                >
                                  <div className="agent-context-item-head">
                                    <strong>{src.title}</strong>
                                    <span className="agent-context-project">{src.project_name}</span>
                                    {src.available === false && (
                                      <span className="agent-context-unavailable">来源已变更或不可用</span>
                                    )}
                                  </div>
                                  <p>{src.snippet}</p>
                                </div>
                              ))}
                            </div>
                          )}
                          {knowledgeSources.length > 0 && (
                            <div className="agent-context-group">
                              <div className="agent-context-group-title">
                                Knowledge 文档（{knowledgeSources.length}）
                              </div>
                              {knowledgeSources.map((src) => (
                                <div
                                  className={`agent-context-item ${src.available === false ? 'unavailable' : ''}`}
                                  key={`kn-${src.id}`}
                                >
                                  <div className="agent-context-item-head">
                                    <strong>{src.title}</strong>
                                    <span className="agent-context-project">{src.project_name}</span>
                                    {src.available === false && (
                                      <span className="agent-context-unavailable">来源已变更或不可用</span>
                                    )}
                                  </div>
                                  <p>{src.snippet}</p>
                                </div>
                              ))}
                            </div>
                          )}
                          {meta.trimmed && (
                            <p className="agent-context-footnote">部分低优先级参考片段已按模型上下文预算裁剪。</p>
                          )}
                        </div>
                      </details>
                    )}
                    {!!m.events?.some((event) => event.type !== 'model_output') && (
                      <details className="agent-run-details">
                        <summary>
                          <span className="agent-run-icon" aria-hidden="true">
                            ⌁
                          </span>
                          <span>运行详情</span>
                          <svg className="agent-run-chevron" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                            <path
                              d="m4 6 4 4 4-4"
                              stroke="currentColor"
                              strokeWidth="1.6"
                              strokeLinecap="round"
                              strokeLinejoin="round"
                            />
                          </svg>
                        </summary>
                        <div className="agent-run-events">
                          {m.events
                            .filter((event) => event.type !== 'model_output')
                            .map((event, i) => (
                              <div className="agent-run-event" key={i}>
                                <span className="agent-run-dot" aria-hidden="true" />
                                <span className="agent-run-label">{eventLabels[event.type] ?? event.type}</span>
                                <span className="agent-run-value">{event.content}</span>
                              </div>
                            ))}
                        </div>
                      </details>
                    )}
                    {m.status === 'failed' && m.context_status !== 'failed' && (
                      <button
                        className="agent-retry"
                        onClick={() =>
                          setText(
                            detail.messages
                              .slice(0, index)
                              .reverse()
                              .find((item) => item.role === 'user')?.content ?? '',
                          )
                        }
                      >
                        重新编辑并发送
                      </button>
                    )}
                  </article>
                );
              })
            )}
          </div>
          {showRagNotice && (
            <div className="agent-rag-banner" role="note">
              <div className="agent-rag-banner-text">
                <strong>项目记忆已默认开启：</strong>
                对话时会自动检索当前项目及其子项目下<strong>已确认的正式记忆</strong>与{' '}
                <strong>Knowledge 文档</strong>作为参考，不会读取未审核候选、其他无关项目或 Web
                归档；可随时点击下方开关关闭。
              </div>
              <button type="button" className="agent-rag-banner-close" onClick={dismissRagNotice}>
                我知道了
              </button>
            </div>
          )}
          {retrievalAlert && (
            <div className="agent-rag-alert" role="alert">
              <span>{retrievalAlert}</span>
            </div>
          )}
          <div className="agent-composer">
            {attachments.length > 0 && (
              <div className="agent-attachment-chips">
                {attachments.map((att) => (
                  <span key={att.attachment_id} className="agent-attachment-chip">
                    <span>
                      📎 {att.filename} ({att.char_count} 字{att.truncated ? '，已截断' : ''})
                    </span>
                    <button
                      type="button"
                      aria-label={`移除附件 ${att.filename}`}
                      onClick={() =>
                        setAttachments((prev) => prev.filter((item) => item.attachment_id !== att.attachment_id))
                      }
                    >
                      ×
                    </button>
                  </span>
                ))}
              </div>
            )}
            <textarea
              ref={textAreaRef}
              rows={2}
              aria-label="输入消息"
              placeholder="输入消息…（Enter 发送，Shift + Enter 换行）"
              value={text}
              disabled={busy}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={key}
            />
            <div className="agent-composer-actions">
              <ChatSelect
                label="选择模型"
                icon="✦"
                value={model}
                disabled={busy}
                options={models.map((m) => ({ value: m.id, label: modelLabels[m.id] ?? m.name }))}
                onChange={(value) => {
                  setModel(value);
                  if (detail) saveSelection(detail.id, { model: value, agent });
                }}
              />
              <ChatSelect
                label="选择 Agent"
                icon="◇"
                value={agent}
                disabled={busy}
                options={[
                  { value: 'default', label: 'Default Agent' },
                  { value: 'openhands', label: 'OpenHands' },
                  { value: 'swe', label: 'SWE' },
                  { value: 'agentlab', label: 'AgentLab' },
                ]}
                onChange={(value) => {
                  setAgent(value);
                  if (detail) saveSelection(detail.id, { model, agent: value });
                }}
              />
              <div className="agent-rag-toggle-group">
                <button
                  type="button"
                  role="switch"
                  aria-checked={useProjectContext}
                  className={`agent-rag-switch ${useProjectContext ? 'is-on' : 'is-off'}`}
                  disabled={busy}
                  onClick={toggleProjectContext}
                  title={useProjectContext ? '已开启：检索当前项目及子项目记忆与 Knowledge' : '已关闭：仅使用普通对话'}
                >
                  <span className="agent-rag-switch-track" aria-hidden="true">
                    <span className="agent-rag-switch-thumb" />
                  </span>
                  <span className="agent-rag-switch-label">使用项目记忆</span>
                </button>
                <button
                  type="button"
                  className="agent-rag-help"
                  aria-label="查看项目记忆说明"
                  title="查看项目记忆说明"
                  onClick={() => setShowRagNotice((v) => !v)}
                >
                  ?
                </button>
              </div>
              <input
                ref={fileInputRef}
                type="file"
                hidden
                accept=".txt,.md,.markdown,.json,.csv,.tsv,.yaml,.yml,.toml,.xml,.log,.html,.htm,.docx,.pdf,.py,.ts,.tsx,.js,.jsx,.java,.go,.rs,.c,.cpp,.sql,.sh,.css"
                onChange={(e) => void handleFileSelect(e.target.files?.[0])}
              />
              <button
                type="button"
                className="agent-attachment"
                title={uploadingFile ? '正在解析附件…' : '上传附件（支持 txt / md / docx / html / json / csv / 代码文件）'}
                aria-label="上传附件"
                disabled={busy || uploadingFile}
                onClick={() => fileInputRef.current?.click()}
              >
                <svg
                  width="17"
                  height="17"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <path d="M21 11.5 12 20.4a5.5 5.5 0 0 1-7.8-7.8l9.2-9.2a3.7 3.7 0 0 1 5.2 5.2l-9.2 9.2a1.9 1.9 0 0 1-2.7-2.7l8.5-8.5" />
                </svg>
              </button>
              <button
                className="agent-send"
                aria-label="发送消息"
                disabled={busy || pending || !text.trim() || !current}
                title={pending ? '当前会话正在生成，可切换到其他会话' : '发送消息'}
                onClick={() => void send()}
              >
                ➤
              </button>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
