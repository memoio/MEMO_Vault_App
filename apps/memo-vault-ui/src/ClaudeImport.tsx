import { useEffect, useState } from 'react';
import {
  claudeRequest, commitImport, extractImport, type ClaudeContent, type ClaudeJob,
  type ImportBatch, type ImportPreview, type ProjectSpace,
} from './api';
const activeStates = new Set(['queued', 'downloading', 'decoding', 'validating']);
const stageNames: Record<string, string> = {
  queued: '排队中', downloading: '准备导出文件', decoding: '解析 Claude 数据',
  validating: '校验 UMIF 包', ready_for_preview: '待预览',
  committed: '已归档', failed: '需要处理', cancelled: '已取消',
};

function jobTitle(item: ClaudeJob): string {
  if (item.input_kind === 'local_bundle' || item.filename === 'local-bundle.json') {
    return `本地 ZIP 合集 · ${item.part_count ?? Object.keys(item.progress).filter(key => key !== 'report').length} 个文件`;
  }
  if (item.input_kind === 'manifest' || item.filename === 'manifest.json') return 'Claude Manifest';
  return item.filename;
}

function fileSize(bytes: number): string {
  if (bytes === 0) return '—';
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : bytes >= 1024 ? `${Math.round(bytes / 1024)} KB` : `${bytes} B`;
}

function jobTime(value?: string): string {
  if (!value) return '';
  const time = new Date(value.includes('T') ? value : `${value.replace(' ', 'T')}Z`);
  return Number.isNaN(time.getTime()) ? value : time.toLocaleString('zh-CN');
}

function eventTime(value: number | null): string {
  return value === null ? '未提供' : new Date(value).toLocaleString('zh-CN');
}

function documentStatus(status: string): string {
  if (status === 'delivered') return '已进入 Knowledge';
  if (status === 'approved') return '已批准，待投递';
  if (status === 'submitting') return '投递中';
  if (status === 'delivery_unknown') return '待核对投递结果';
  if (status === 'delivery_rejected') return '上次投递失败，可重试';
  return '保留在 Vault';
}

export default function ClaudeImport({ projects, initialProjectId, onBack, step, onStepChange }: {
  projects: ProjectSpace[]; initialProjectId: string; onBack: () => void;
  step: number; onStepChange: (step: number) => void;
}) {
  const [project, setProject] = useState(initialProjectId);
  const [jobs, setJobs] = useState<ClaudeJob[]>([]);
  const [job, setJob] = useState<ClaudeJob | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [batch, setBatch] = useState<Pick<ImportBatch, 'batch_id' | 'project_id'> | null>(null);
  const [content, setContent] = useState<ClaudeContent | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [documentText, setDocumentText] = useState<Record<string, { text: string; nextOffset: number; hasMore: boolean }>>({});
  const [merge, setMerge] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [mode, setMode] = useState<'local' | 'manifest'>('local');
  const [localFiles, setLocalFiles] = useState<File[]>([]);
  const [localManifestFile, setLocalManifestFile] = useState<File | null>(null);
  const [manifestFile, setManifestFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [showAllJobs, setShowAllJobs] = useState(false);
  const jobPath = job ? `import-jobs/${job.id}` : '';
  const batchPath = batch ? `imports/${batch.batch_id}` : '';

  useEffect(() => { void claudeRequest<ClaudeJob[]>('import-jobs').then(setJobs).catch(e => setError(String(e))); }, []);
  useEffect(() => {
    if (!job || !activeStates.has(job.status)) return;
    let disposed = false;
    const timer = window.setInterval(() => {
      void claudeRequest<ClaudeJob>(`import-jobs/${job.id}`).then(value => { if (!disposed) setJob(value); })
        .catch(e => { if (!disposed) setError(String(e)); });
    }, 1000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [job?.id, job?.status]);

  useEffect(() => {
    if (job?.status === 'committed' && job.batch_id) {
      setBatch({ batch_id: job.batch_id, project_id: job.project_id });
      void claudeRequest<ClaudeContent>(`imports/${job.batch_id}/claude-content`).then(setContent).catch(e => setError(String(e)));
    }
  }, [job?.id, job?.status]);
  useEffect(() => {
    if (job?.status === 'ready_for_preview') onStepChange(2);
    if (job?.status === 'committed' && job.batch_id) onStepChange(3);
  }, [job?.id, job?.status, job?.batch_id]);

  async function action(work: () => Promise<void>) {
    if (busy) return;
    setBusy(true); setError(null); setNotice(null);
    try { await work(); } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }
  function addLocalFiles(files: FileList | File[]) {
    const incoming = Array.from(files);
    const invalid = incoming.find(file => !file.name.toLowerCase().endsWith('.zip'));
    if (invalid) { setError(`请选择 ZIP 文件：${invalid.name}`); return; }
    const merged = [...localFiles];
    for (const file of incoming) {
      if (merged.some(item => item.name.toLowerCase() === file.name.toLowerCase())) {
        setError(`文件名重复：${file.name}。同一份导出中的 ZIP 文件名必须唯一。`);
        return;
      }
      merged.push(file);
    }
    setError(null); setLocalFiles(merged);
  }
  async function uploadPart(file: File) {
    const form = new FormData(); form.append('file', file);
    setJob(await claudeRequest<ClaudeJob>(`${jobPath}/parts`, 'POST', form));
  }
  async function startImport() {
    const form = new FormData(); form.append('project_id', project);
    if (mode === 'local') {
      if (!localFiles.length) return;
      localFiles.forEach(file => form.append('files', file, file.name));
      if (localManifestFile) form.append('manifest', localManifestFile, localManifestFile.name);
      setJob(await claudeRequest<ClaudeJob>('import-jobs/local', 'POST', form));
    } else {
      if (!manifestFile) return;
      form.append('file', manifestFile, manifestFile.name);
      setJob(await claudeRequest<ClaudeJob>('import-jobs', 'POST', form));
    }
    setLocalFiles([]); setLocalManifestFile(null); setManifestFile(null);
  }
  async function refreshContent() { setContent(await claudeRequest<ClaudeContent>(`${batchPath}/claude-content`)); }
  async function loadDocumentText(recordId: string, offset: number) {
    const result = await claudeRequest<{ text: string; next_offset: number; has_more: boolean }>(
      `${batchPath}/documents/${recordId}/text?offset=${offset}&limit=4000`
    );
    setDocumentText(previous => ({ ...previous, [recordId]: {
      text: offset === 0 ? result.text : (previous[recordId]?.text ?? '') + result.text,
      nextOffset: result.next_offset, hasMore: result.has_more,
    } }));
  }

  return <div className="import-card claude-page">
    <div className="card-heading"><div><h2>{step === 1 ? '上传与检查' : step === 2 ? '解析预览' : step === 3 ? '抽取与审核' : '完成'}</h2>
      <p className="subtle">Claude 导出 · {step === 1 ? '同一次导出的多个 ZIP 将合并为一个导入任务' : `目标项目 ${job?.project_id ?? batch?.project_id ?? project}`}</p></div></div>
    <div className="claude-workspace">
    <section className="claude-primary">
    {step === 1 && !job && <>
      <div className="claude-section-heading"><h3>选择导入方式</h3><span className="claude-recommended">推荐 · 本地 ZIP</span></div>
      <div className="claude-mode-switch" role="group" aria-label="导入方式">
        <button type="button" aria-pressed={mode === 'local'} className={mode === 'local' ? 'selected' : ''} onClick={() => setMode('local')}><strong>上传多个 ZIP</strong><small>无需 Claude 下载链接</small></button>
        <button type="button" aria-pressed={mode === 'manifest'} className={mode === 'manifest' ? 'selected' : ''} onClick={() => setMode('manifest')}><strong>上传 Manifest</strong><small>由服务端按链接下载</small></button>
      </div>
      <label className="claude-project-picker">目标项目<select disabled={busy} value={project} onChange={e => setProject(e.target.value)}>
        {projects.map(p => <option key={p.project_id} value={p.project_id}>{p.name} · {p.project_id}</option>)}
      </select><small>本次所有 ZIP 将进入同一个目标项目与导入批次。</small></label>
      {mode === 'local' ? <>
        <div className={`claude-dropzone ${dragging ? 'dragging' : ''}`} onDragOver={e => { e.preventDefault(); setDragging(true); }} onDragLeave={e => { e.preventDefault(); setDragging(false); }} onDrop={e => { e.preventDefault(); setDragging(false); addLocalFiles(e.dataTransfer.files); }}>
          <span className="claude-upload-symbol" aria-hidden="true">↑</span><strong>拖入同一份导出的全部 ZIP</strong>
          <p>也可以一次选择多个文件；文件会先列在下方，确认后才开始上传。</p>
          <label className="secondary-button claude-file-button">选择 ZIP 文件<input className="visually-hidden" type="file" accept=".zip" multiple disabled={busy} onChange={e => { if (e.target.files) addLocalFiles(e.target.files); e.target.value = ''; }} /></label>
        </div>
        {localFiles.length > 0 && <div className="claude-selected-files"><div className="claude-file-list-heading"><strong>待上传文件</strong><span>{localFiles.length} 个 ZIP · {fileSize(localFiles.reduce((sum, file) => sum + file.size, 0))}</span></div>
          {localFiles.map(file => <div className="claude-selected-row" key={file.name}><span className="claude-zip-icon">ZIP</span><span className="claude-filename">{file.name}</span><small>{fileSize(file.size)}</small><button type="button" aria-label={`移除 ${file.name}`} onClick={() => setLocalFiles(localFiles.filter(item => item.name !== file.name))}>移除</button></div>)}
        </div>}
        <div className="claude-optional-manifest"><div><strong>可选：使用 Manifest 核对 ZIP 是否齐全</strong><p>只比较文件名清单，不会访问 Claude 下载链接。</p></div><label className="secondary-button claude-file-button">{localManifestFile ? localManifestFile.name : '选择 JSON'}<input className="visually-hidden" type="file" accept=".json" disabled={busy} onChange={e => { setLocalManifestFile(e.target.files?.[0] ?? null); e.target.value = ''; }} /></label>{localManifestFile && <button className="claude-text-button" onClick={() => setLocalManifestFile(null)}>移除</button>}</div>
        <p className="claude-helper">请选齐同一次 Claude 导出中的 conversations、projects、memories、metadata 等 ZIP。系统会合并为一个 UMIF 包。</p>
      </> : <div className="claude-manifest-panel"><strong>上传 Claude Manifest JSON</strong><p>适用于下载链接仍可访问的情况。链接若失效，可在该任务中逐个补交原文件名的 ZIP。</p>
        <label className="secondary-button claude-file-button">{manifestFile ? manifestFile.name : '选择 Manifest JSON'}<input className="visually-hidden" type="file" accept=".json" disabled={busy} onChange={e => { setManifestFile(e.target.files?.[0] ?? null); e.target.value = ''; }} /></label>
      </div>}
      <button className="primary-button claude-start-button" disabled={busy || (mode === 'local' ? localFiles.length === 0 : !manifestFile)} onClick={() => void action(startImport)}>{busy ? '正在上传…' : mode === 'local' ? `上传并检查 ${localFiles.length} 个 ZIP` : '上传并检查 Manifest'}</button>
    </>}
    {job && step <= 2 && <>
      <div className="claude-section-heading"><h3>{jobTitle(job)}</h3><span className={`claude-job-state state-${job.status}`}>{stageNames[job.status] ?? job.status}</span></div>
      <p className="claude-job-meta">目标项目 {job.project_id} · {job.part_count ?? 1} 个 ZIP · 任务 {job.id.slice(0, 8)}{job.manifest_verified ? ' · 已按 Manifest 核对齐全' : ''}</p>
      <div className="claude-progress" role="status" aria-label="文件处理进度"><strong>文件进度</strong>{Object.entries(job.progress).filter(([name]) => name !== 'report').map(([name, value]) => {
        const part = value as { status?: string; bytes?: number };
        return <div className="claude-progress-row" key={name}><span className="claude-zip-icon">ZIP</span><span className="claude-filename">{name}</span><small>{part.status === 'failed' ? '下载失败' : part.status === 'downloaded' || part.status === 'uploaded' ? '已保存' : part.status === 'downloading' ? '下载中' : '等待处理'}</small><small>{fileSize(part.bytes ?? 0)}</small></div>;
      })}</div>
      {job.error_message && <p className="notice error" role="alert">{job.error_message} ({job.error_code})</p>}
      {activeStates.has(job.status) && <button className="secondary-button" disabled={busy} onClick={() => void action(async () => setJob(await claudeRequest<ClaudeJob>(`${jobPath}/cancel`, 'POST')))}>取消此任务</button>}
      {['failed', 'cancelled'].includes(job.status) && <div className="actions">
        <button className="secondary-button" disabled={busy} onClick={() => void action(async () => setJob(await claudeRequest<ClaudeJob>(`${jobPath}/retry`, 'POST')))}>使用已保存文件重试</button>
        {(job.input_kind === 'manifest' || job.filename === 'manifest.json') && <label className="secondary-button claude-file-button">补交缺失 ZIP<input className="visually-hidden" type="file" accept=".zip" disabled={busy} onChange={e => {
          const file = e.target.files?.[0]; e.target.value = '';
          if (file) void action(() => uploadPart(file));
        }} /></label>}
      </div>}
      {job.status === 'ready_for_preview' && !batch && step === 2 && <>
        <label className="claude-merge-choice"><input type="checkbox" checked={merge} onChange={e => setMerge(e.target.checked)} /> 若导出中包含多个 Claude 项目，将它们都映射到 {job.project_id}</label>
        <button className="primary-button" disabled={busy} onClick={() => void action(async () => setPreview(await claudeRequest<ImportPreview>(`${jobPath}/preview`, 'POST', JSON.stringify({ confirm_project_merge: merge }))))}>生成解析预览</button>
      </>}
    </>}
    {preview && !batch && step === 2 && <>
      <div className="claude-section-heading"><h3>解析结果</h3></div>
      <div className="stats-grid claude-preview-stats"><div><strong>{preview.conversation_count}</strong><span>会话</span></div><div><strong>{preview.message_count}</strong><span>消息</span></div><div><strong>{preview.tool_call_count}</strong><span>工具调用</span></div><div><strong>{preview.tool_result_count}</strong><span>工具结果</span></div><div><strong>{preview.umif_record_count}</strong><span>UMIF 记录</span></div><div><strong>{preview.duplicate_message_count}</strong><span>重复消息</span></div>
        {Object.entries(((job?.progress.report as { counts?: Record<string, number> } | undefined)?.counts) ?? {}).filter(([name]) => ['assets', 'documents', 'assertions'].includes(name)).map(([name, count]) => <div key={name}><strong>{count}</strong><span>{name === 'assets' ? '附件' : name === 'documents' ? '文档' : '平台记忆'}</span></div>)}
      </div>
      {preview.warnings.map(w => <p className="notice warning" key={w}>{w}</p>)}
      {Object.entries(preview.skipped_event_counts).map(([kind, count]) => <p className="claude-helper" key={kind}>未转换：{kind} · {count}</p>)}
      <h3>消息预览 <span className="subtle">最多展示前 20 条</span></h3>
      <div className="table-wrap"><table><thead><tr><th>角色</th><th>时间</th><th>内容</th></tr></thead><tbody>{preview.messages.map(m => <tr key={`${m.conversation_id}:${m.ordinal}`}><td><span className="role-tag">{m.role}</span></td><td>{eventTime(m.timestamp_ms)}</td><td><textarea className="message-content" readOnly rows={4} value={m.content} aria-label={`${m.role} 消息内容`} /></td></tr>)}</tbody></table></div>
      {preview.duplicate_batch_id && <p className="notice warning">这份 UMIF 包已归档至批次 {preview.duplicate_batch_id}。</p>}
      <button className="primary-button" disabled={busy || Boolean(preview.duplicate_batch_id) || preview.source_event_conflict_count > 0} onClick={() => void action(async () => {
        const saved = await commitImport(preview.preview_id, job!.project_id);
        setBatch(saved); setJob({ ...job!, status: 'committed', batch_id: saved.batch_id }); onStepChange(3); setContent(await claudeRequest<ClaudeContent>(`imports/${saved.batch_id}/claude-content`));
        setNotice('归档完成。你可以继续抽取对话候选、审核保存的记忆和投递项目文档。');
      })}>确认归档</button>
    </>}
    {batch && step === 3 && <>
      <div className="notice success">原文已归档，关闭页面也不会丢失。候选记忆仍需人工审核。</div><p className="claude-job-meta">批次 {batch.batch_id} · 项目 {batch.project_id}</p>
      <button className="primary-button" disabled={busy} onClick={() => void action(async () => {
        const result = await extractImport(batch.batch_id);
        setNotice(`对话抽取状态：${result.status}。候选仍需人工审核后才能发布。`);
      })}>抽取对话候选</button>
      <h3>Claude 保存的记忆</h3><p className="claude-helper">这些是 Claude 平台摘要，需要作为来源声明单独审核。</p>
      {content?.assertions.map(r => <article className="claude-content-card" key={r.record_id}><textarea aria-label={`审核 Claude 记忆摘要 ${r.record_id}`} rows={5} value={edits[r.record_id] ?? r.text} onChange={e => setEdits({ ...edits, [r.record_id]: e.target.value })} />
        <small>审核文本需为 1–4000 字符；原始摘要仍会保留。</small>
        <small>{r.status} · 来源项目：{r.source_project_id ?? '账号级'}</small>
        <button disabled={busy || r.status === 'review_pending' || !(edits[r.record_id] ?? r.text).trim() || (edits[r.record_id] ?? r.text).length > 4000} onClick={() => void action(async () => {
          await claudeRequest(`${batchPath}/assertions/${r.record_id}/review`, 'POST', JSON.stringify({ content: edits[r.record_id] ?? r.text })); await refreshContent();
          setNotice('已加入候选审核队列。');
        })}>加入候选审核</button></article>)}
      <h3>工具轨迹 <span className="subtle">共 {content?.agent_event_count ?? 0} 条，最多展示前 20 条；参数和结果已脱敏并截断</span></h3>
      <p className="subtle">工具调用与结果独立归档，不进入用户记忆抽取，也不会自动生成 Case / Skill。</p>
      {content?.agent_events.length ? <div className="table-wrap"><table><thead><tr><th>事件</th><th>工具 / 调用 ID</th><th>时间</th><th>参数或结果</th></tr></thead><tbody>{content.agent_events.map(event => <tr key={event.record_id}>
        <td>{event.record_type === 'event.tool_call' ? 'tool/call' : 'tool/result'} · #{event.sequence}</td>
        <td>{event.tool_name ?? '结果'}<br /><code>{event.call_id ?? '无调用 ID'}</code>{event.is_error && <small className="duplicate-label">调用失败</small>}</td>
        <td>{eventTime(event.timestamp_ms)}</td><td><pre className="trace-preview">{event.preview}</pre></td>
      </tr>)}</tbody></table></div> : <p className="subtle">本批次没有工具轨迹。</p>}
      <h3>项目知识文档</h3>
      <p className="subtle">原文件已保留在 Vault。按需选择文档进入项目 Knowledge；未操作的文档继续保留在 Vault。</p>
      {content?.documents.map(r => <article className="claude-content-card" key={r.record_id}><strong>{r.title}</strong><p>{documentStatus(r.status)}</p>
        <details><summary>查看文档</summary><div className="claude-document-text">{(documentText[r.record_id]?.text ?? r.text) || '此文件没有可显示的纯文本，投递后由 Knowledge 解析器处理。'}</div>
          {(documentText[r.record_id]?.hasMore ?? r.text_truncated) && <button className="secondary-button" disabled={busy} onClick={() => void action(() => loadDocumentText(r.record_id, documentText[r.record_id]?.nextOffset ?? 4000))}>继续加载正文</button>}</details>
        <button className="primary-button" disabled={busy || r.status === 'delivered'} onClick={() => void action(async () => {
          try {
            if (['submitting', 'delivery_unknown'].includes(r.status)) {
              const result = await claudeRequest<{ status: string; message?: string }>(`${batchPath}/documents/${r.record_id}/reconcile`, 'POST');
              setNotice(result.message ?? result.status);
            } else {
              await claudeRequest(`${batchPath}/documents/${r.record_id}/review`, 'POST', JSON.stringify({ decision: 'knowledge' }));
              await claudeRequest(`${batchPath}/documents/${r.record_id}/deliver`, 'POST', JSON.stringify({ confirmed: true }));
              setNotice('文档已投递到项目 Knowledge。');
            }
          } finally { await refreshContent(); }
        })}>{r.status === 'delivered' ? '已进入 Knowledge' : ['submitting', 'delivery_unknown'].includes(r.status) ? '核对投递结果' : '批准进入 Knowledge'}</button>
      </article>)}
      <div className="actions right"><button className="primary-button" onClick={() => onStepChange(4)}>查看完成状态</button></div>
    </>}
    {batch && step === 4 && <><h3>导入处理状态</h3><p>批次 {batch.batch_id} 已归档至 {batch.project_id}。候选记忆仍需在「候选审核」中确认。</p>
      <p>项目文档：{content?.documents.filter(item => item.status === 'delivered').length ?? 0} 已进入 Knowledge · {content?.documents.filter(item => item.status !== 'delivered').length ?? 0} 保留在 Vault，仍可返回选择投递。</p>
      <div className="actions"><button className="secondary-button" onClick={() => onStepChange(3)}>返回抽取与审核</button></div></>}
    </section>
      {step === 1 && !job ? <section className="claude-history"><h3>恢复已有导入任务</h3><p className="subtle">选择一个任务可继续预览、重试，或查看已归档内容。</p>
        {jobs.length ? <div className="claude-job-list">{(showAllJobs ? jobs : jobs.slice(0, 6)).map(item => <button type="button" className="claude-history-item" key={item.id} onClick={() => { setJob(item); setProject(item.project_id); setPreview(null); setBatch(null); setContent(null); setDocumentText({}); setMerge(false); setError(null); onStepChange(item.status === 'committed' ? 3 : item.status === 'ready_for_preview' ? 2 : 1); }}><span className="claude-history-main"><strong>{jobTitle(item)}</strong><em className={`claude-job-state state-${item.status}`}>{stageNames[item.status] ?? item.status}</em></span><span>{item.project_id} · {jobTime(item.created_at)} · #{item.id.slice(0, 8)}</span></button>)}</div> : <p className="claude-empty-history">尚无导入任务</p>}
        {jobs.length > 6 && <button className="claude-text-button" onClick={() => setShowAllJobs(!showAllJobs)}>{showAllJobs ? '收起任务' : `查看全部 ${jobs.length} 个任务`}</button>}
      </section> : null}
    </div>
    {notice && <p className="notice success" role="status">{notice}</p>}
    {error && <p className="notice error" role="alert">{error}</p>}
    <div className="actions claude-footer"><button className="secondary-button" disabled={busy} onClick={(step === 3 && Boolean(batch)) || step === 1 ? onBack : () => onStepChange(step - 1)}>{(step === 3 && Boolean(batch)) || step === 1 ? '返回选择来源' : '上一步'}</button>
      {job && <button className="secondary-button" disabled={busy} onClick={() => {
        setJob(null); setPreview(null); setBatch(null); setContent(null); setDocumentText({}); setMerge(false); onStepChange(1); void claudeRequest<ClaudeJob[]>('import-jobs').then(setJobs).catch(e => setError(String(e)));
      }}>开始新导入 / 查看其他任务</button>}</div>
  </div>;
}
