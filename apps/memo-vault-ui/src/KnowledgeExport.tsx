import { useEffect, useState } from 'react';
import { ownerFetch } from './ownerSession';
import type { ProjectSpace } from './api';

interface DocumentRow {
  doc_id: string; title: string; category_id: string; topic_count: number;
  source_name: string | null; source_type: string | null; created_at: string;
  original_available: boolean; original_bytes: number;
  derived_bytes?: number;
  import_status?: string; review_status?: string;
}
interface DocumentPage { documents: DocumentRow[]; total: number; page: number; page_size: number }
interface Envelope<T> { data: T }

async function jsonData<T>(url: string): Promise<T> {
  const response = await ownerFetch(url);
  if (!response.ok) throw new Error(`Knowledge 请求失败：HTTP ${response.status}`);
  return (await response.json() as Envelope<T>).data;
}
async function saveDownload(response: Response, fallback: string) {
  if (!response.ok) throw new Error(`下载失败：HTTP ${response.status}`);
  const blob = await response.blob();
  const match = response.headers.get('content-disposition')?.match(/filename\*?=(?:UTF-8''|\")?([^\";]+)/i);
  const filename = match ? decodeURIComponent(match[1].replace(/\"$/, '')) : fallback;
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob); link.download = filename;
  link.click(); window.setTimeout(() => URL.revokeObjectURL(link.href), 60_000);
}

export default function KnowledgeExport({ projects }: { projects: ProjectSpace[] }) {
  const [projectId, setProjectId] = useState('personal');
  const [category, setCategory] = useState('');
  const [titleInput, setTitleInput] = useState('');
  const [titleQuery, setTitleQuery] = useState('');
  const [sourceType, setSourceType] = useState('');
  const [fileType, setFileType] = useState('');
  const [createdFrom, setCreatedFrom] = useState('');
  const [createdTo, setCreatedTo] = useState('');
  const [page, setPage] = useState(1);
  const [result, setResult] = useState<DocumentPage | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [includeDerived, setIncludeDerived] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (projects.length && !projects.some(item => item.project_id === projectId)) setProjectId(projects[0].project_id);
  }, [projects, projectId]);
  useEffect(() => {
    if (!projectId || !projects.some(item => item.project_id === projectId)) return;
    const controller = new AbortController();
    const params = new URLSearchParams({ project_id: projectId, page: String(page), page_size: '20' });
    if (category) params.set('category_id', category);
    if (titleQuery) params.set('title_query', titleQuery);
    if (sourceType) params.set('source_type', sourceType);
    if (fileType) params.set('file_type', fileType);
    if (createdFrom) params.set('created_from', createdFrom);
    if (createdTo) params.set('created_to', createdTo);
    setError(null); setResult(null);
    void jsonData<DocumentPage>(`/api/v1/knowledge-export/documents?${params}`).then(data => {
      if (!controller.signal.aborted) setResult(data);
    }).catch(reason => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [projectId, category, titleQuery, sourceType, fileType, createdFrom, createdTo, page, projects]);

  async function download(path: string, filename: string, body?: object) {
    if (busy) return;
    setBusy(true); setError(null);
    try {
      const response = await ownerFetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : undefined);
      await saveDownload(response, filename);
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }
  const chosen = result?.documents.filter(item => selected.includes(item.doc_id)) ?? [];
  const missing = chosen.filter(item => !item.original_available).length;
  const size = chosen.reduce((total, item) => total + item.original_bytes + (includeDerived ? item.derived_bytes ?? 0 : 0), 0);
  function changeProject(value: string) { setProjectId(value); setSelected([]); setPage(1); }
  function changeFilter(setter: (value: string) => void, value: string) { setter(value); setSelected([]); setPage(1); }

  return <section className="knowledge-export">
    <div className="card-heading"><div><h2>Knowledge 文档</h2><p className="subtle">按项目选择已进入 Knowledge 的原始文件。Vault 中仅归档的文件不会混入下载。</p></div></div>
    <div className="knowledge-filters">
      <label>项目空间<select value={projectId} onChange={event => changeProject(event.target.value)}>{projects.map(item => <option key={item.project_id} value={item.project_id}>{item.name} · {item.project_id}</option>)}</select></label>
      <label>分类<input value={category} onChange={event => changeFilter(setCategory, event.target.value)} placeholder="全部分类" /></label>
      <label>来源<input value={sourceType} onChange={event => changeFilter(setSourceType, event.target.value)} placeholder="例如 claude" /></label>
      <label>文件类型<input value={fileType} onChange={event => changeFilter(setFileType, event.target.value)} placeholder="例如 pdf" /></label>
      <label>创建日期起<input type="date" value={createdFrom} onChange={event => changeFilter(setCreatedFrom, event.target.value)} /></label>
      <label>创建日期止<input type="date" value={createdTo} onChange={event => changeFilter(setCreatedTo, event.target.value)} /></label>
      <label>标题搜索<div className="knowledge-search"><input value={titleInput} onChange={event => setTitleInput(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') changeFilter(setTitleQuery, titleInput.trim()); }} placeholder="输入关键词" /><button className="secondary-button" onClick={() => changeFilter(setTitleQuery, titleInput.trim())}>搜索</button></div></label>
    </div>
    {error && <p className="notice error" role="alert">{error}</p>}
    {!result && !error && <p>正在读取项目文档…</p>}
    {result && <><p className="subtle">共 {result.total} 篇 · 第 {page} 页</p>
      <div className="knowledge-document-list">{result.documents.map(item => <article className="knowledge-document-row" key={item.doc_id}>
        <label><input type="checkbox" disabled={!item.original_available} checked={selected.includes(item.doc_id)} onChange={event => setSelected(event.target.checked ? [...selected, item.doc_id] : selected.filter(id => id !== item.doc_id))} /><span><strong>{item.title}</strong><small>{item.source_name || '无原文件名'} · {item.category_id} · {item.topic_count} 个 Topic · {item.source_type || '未知来源'} · {new Date(item.created_at).toLocaleDateString()}</small><small>入库：{item.import_status === 'delivered' ? 'Vault 已投递' : 'Knowledge 现有文档'} · 审核：{item.review_status === 'knowledge' ? '已批准' : '非 Vault 导入'}</small></span></label>
        <span>{item.original_available ? `${(item.original_bytes / 1024).toFixed(1)} KB` : '原件缺失'}</span>
        <button className="secondary-button" disabled={!item.original_available || busy} onClick={() => void download(`/api/v1/knowledge-export/documents/${item.doc_id}/original?project_id=${encodeURIComponent(projectId)}`, item.source_name || `${item.doc_id}.bin`)}>下载原件</button>
      </article>)}</div>
      {!result.documents.length && <p className="subtle">此范围暂无 Knowledge 文档。</p>}
      <div className="actions"><button className="secondary-button" disabled={page <= 1} onClick={() => { setSelected([]); setPage(page - 1); }}>上一页</button><button className="secondary-button" disabled={page * result.page_size >= result.total} onClick={() => { setSelected([]); setPage(page + 1); }}>下一页</button></div>
    </>}
    <div className="knowledge-export-summary"><h3>ZIP 导出预览</h3><p>{chosen.length} 篇原件 · 文件约 {(size / 1024 / 1024).toFixed(2)} MB · {missing} 篇缺失 · 包含 manifest.json{includeDerived ? '、摘要和 Topic Markdown' : ''}</p>
      <label><input type="checkbox" checked={includeDerived} onChange={event => setIncludeDerived(event.target.checked)} /> 附带 EverOS 生成的摘要与 Topic Markdown</label>
      <p className="subtle">一次最多 100 篇、512 MiB。此处仅选择当前页中的文档。</p>
      <button className="primary-button" disabled={busy || !chosen.length || missing > 0} onClick={() => void download('/api/v1/knowledge-export/zip', 'knowledge-export.zip', { project_id: projectId, doc_ids: selected, include_derived: includeDerived })}>{busy ? '正在下载…' : '下载所选 ZIP'}</button>
    </div>
  </section>;
}
