import { useEffect, useRef, useState } from 'react';
import type { PackKind, ProjectSpace } from './api';
import { text as t } from './authorization.zh';
import { callGateway, createGrant, deleteGrant, gatewayStatus, getGrantAudit, listGrants, pauseGateway, revokeGrant, rotateGrant, type AuditRow, type FixedDraft, type GatewayOperation, type GatewayStatus, type Grant, type GrantInput } from './gatewayApi';

const liveOps: GatewayOperation[] = ['search_memory', 'get_memory', 'build_context', 'get_evidence'];
const kinds: PackKind[] = ['fact', 'episode', 'profile'];
const pageSize = 20;
const initial: GrantInput = { subject: '', purpose: '', mode: 'live_scope', project_ids: [], kinds,
  operations: ['search_memory', 'get_memory', 'build_context'], expires_in_hours: 24, token_budget: 4000,
  evidence_mode: 'none', requests_per_minute: 30, confirm_transfer: false, confirm_future_allow: false };
const message = (reason: unknown) => reason instanceof Error ? reason.message : String(reason);

export default function AuthorizationMcpPage({ projects, draft, onOpenPack, onDraftUsed }: {
  projects: ProjectSpace[]; draft: FixedDraft | null; onOpenPack: () => void; onDraftUsed: () => void;
}) {
  const [grants, setGrants] = useState<Grant[]>([]);
  const [grantTotal, setGrantTotal] = useState(0);
  const [grantBucket, setGrantBucket] = useState<'all' | 'active' | 'history'>('all');
  const [grantOffset, setGrantOffset] = useState(0);
  const [status, setStatus] = useState<GatewayStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [connectionError, setConnectionError] = useState('');
  const [notice, setNotice] = useState('');
  const [selectedId, setSelectedId] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState<GrantInput>(initial);
  const [tab, setTab] = useState<'api' | 'mcp'>('mcp');
  const [token, setToken] = useState('');
  const [revealed, setRevealed] = useState<{ grantId: string; token: string } | null>(null);
  const [operation, setOperation] = useState<GatewayOperation>('search_memory');
  const [project, setProject] = useState('');
  const [query, setQuery] = useState('');
  const [memoryId, setMemoryId] = useState('');
  const [evidenceId, setEvidenceId] = useState('');
  const [result, setResult] = useState('');
  const [audit, setAudit] = useState<{ grantId: string; rows: AuditRow[]; loading: boolean } | null>(null);
  const testRequest = useRef<AbortController | null>(null);
  const sequence = useRef(0);
  const creationRequest = useRef({ payload: '', key: '' });
  const previousPause = useRef<boolean | undefined>(undefined);
  const selected = grants.find(g => g.id === selectedId);

  async function refresh(offset = grantOffset, bucket = grantBucket) {
    setLoading(true); setError('');
    try {
      const [page, state] = await Promise.all([listGrants(bucket, pageSize, offset), gatewayStatus()]);
      let items = page.items;
      let actualOffset = offset;
      if (!items.length && offset > 0 && offset >= page.total) {
        actualOffset = Math.max(0, Math.ceil(page.total / pageSize) - 1) * pageSize;
        items = (await listGrants(bucket, pageSize, actualOffset)).items;
      }
      setGrantOffset(actualOffset); setGrantTotal(page.total); setGrants(items); setStatus(state);
      setSelectedId(current => items.some(g => g.id === current) ? current : items[0]?.id ?? '');
    } catch (reason) { setError(message(reason)); }
    finally { setLoading(false); }
  }
  useEffect(() => { void refresh(); return () => { testRequest.current?.abort(); sequence.current++; }; }, []);
  useEffect(() => {
    testRequest.current?.abort(); sequence.current++; setBusy(false); setToken(''); setRevealed(current => current?.grantId === selectedId ? current : null); setResult(''); setConnectionError(''); setAudit(null);
    const current = grants.find(g => g.id === selectedId);
    setProject(current?.project_ids[0] ?? ''); setOperation(current?.operations[0] ?? 'search_memory');
  }, [selectedId]);
  useEffect(() => {
    if (!draft) return;
    setForm({ ...initial, mode: 'approved_pack', project_ids: draft.project_ids, kinds: draft.kinds,
      operations: ['get_pack_info', 'read_memory_pack'], token_budget: draft.token_budget,
      evidence_mode: draft.evidence_mode, draft_id: draft.draft_id });
    setShowForm(true);
  }, [draft]);

  useEffect(() => {
    if (previousPause.current === false && status?.paused === true) {
      clearSecrets(); setBusy(false);
    }
    previousPause.current = status?.paused;
  }, [status?.paused]);
  useEffect(() => {
    if (selected && ['revoked', 'expired', 'stale'].includes(selected.status)) {
      clearSecrets(); setBusy(false);
    }
  }, [selected?.status]);

  function clearSecrets() { testRequest.current?.abort(); sequence.current++; setToken(''); setRevealed(null); setResult(''); setConnectionError(''); }
  async function copy(value: string) { try { await navigator.clipboard.writeText(value); setNotice(t.copied); } catch { setError('Clipboard unavailable. Select and copy the text manually.'); } }
  function beginCreate(previous?: Grant) {
    if (previous?.status === 'active' && !window.confirm(t.duplicateConfirm)) return;
    setForm(previous ? { ...initial, mode: previous.mode, subject: previous.subject, purpose: previous.purpose,
      project_ids: previous.project_ids, kinds: previous.kinds, operations: previous.mode === 'live_scope' ? previous.operations : initial.operations,
      evidence_mode: previous.evidence_mode, token_budget: previous.token_budget, expires_in_hours: 24 } : initial);
    creationRequest.current = { payload: '', key: '' };
    setShowForm(true); setError('');
  }
  async function submit() {
    setBusy(true); setError('');
    try {
      const payload = JSON.stringify(form);
      if (creationRequest.current.payload !== payload) creationRequest.current = { payload, key: crypto.randomUUID() };
      const created = await createGrant(form, creationRequest.current.key);
      await refresh(); setSelectedId(created.id); setShowForm(false); onDraftUsed();
      // Selection changes clear prior secrets; a separate one-time reveal remains
      // attached to the newly created record after that transition.
      queueMicrotask(() => setRevealed({ grantId: created.id, token: created.token! }));
    } catch (reason) { setError(message(reason)); }
    finally { setBusy(false); }
  }
  async function mutate(action: 'revoke' | 'rotate' | 'delete', grant: Grant) {
    const confirmation = action === 'revoke' ? t.revokeConfirm : action === 'delete' ? t.deleteConfirm : t.rotateConfirm;
    if (!window.confirm(confirmation)) return;
    setBusy(true); setError(''); clearSecrets();
    try {
      if (action === 'revoke') await revokeGrant(grant.id);
      else if (action === 'delete') {
        await deleteGrant(grant.id);
        if (selectedId === grant.id) setSelectedId('');
      } else { const rotated = await rotateGrant(grant.id); setSelectedId(grant.id); setRevealed({ grantId: grant.id, token: rotated.token! }); }
      await refresh();
    } catch (reason) { setError(message(reason)); }
    finally { setBusy(false); }
  }
  async function togglePause() {
    if (!status) return; setBusy(true); if (!status.paused) clearSecrets();
    try { await pauseGateway(!status.paused); await refresh(); }
    catch (reason) { setError(message(reason)); }
    finally { setBusy(false); }
  }
  async function showAudit(grant: Grant) {
    setAudit({ grantId: grant.id, rows: [], loading: true });
    try { setAudit({ grantId: grant.id, rows: await getGrantAudit(grant.id), loading: false }); }
    catch (reason) { setAudit(null); setError(message(reason)); }
  }
  useEffect(() => {
    if (!audit) return;
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === 'Escape') setAudit(null); };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [audit]);
  async function runTest() {
    if (!selected || !status || !token) return;
    if (operation === 'read_memory_pack' && !window.confirm(t.consumeConfirm)) return;
    testRequest.current?.abort(); const controller = new AbortController(); testRequest.current = controller;
    const serial = ++sequence.current; setBusy(true); setError(''); setResult(''); setConnectionError('');
    const started = performance.now();
    try {
      const body: Record<string, unknown> = selected.mode === 'approved_pack' ? { pack_id: selected.pack_id } : { project_ids: [project] };
      if (operation === 'search_memory' || operation === 'build_context') body.query = query;
      if (operation === 'get_memory' || operation === 'get_evidence') body.memory_id = memoryId;
      if (operation === 'get_evidence') body.evidence_id = evidenceId;
      const response = await callGateway(status.gateway_url, token, operation, body, controller.signal);
      if (serial !== sequence.current) return;
      setResult(JSON.stringify({ status: response.status, elapsed_ms: Math.round(performance.now() - started), receipt: response.receipt, result: response.result }, null, 2).slice(0, 20000));
      if (response.status === 401 || response.status === 403) { setToken(''); setRevealed(null); }
      if (operation === 'read_memory_pack' && response.ok) { setToken(''); setRevealed(null); }
      await refresh();
    } catch (reason) {
      if (serial === sequence.current && !controller.signal.aborted) {
        setConnectionError(reason instanceof TypeError
          ? t.gatewayFetchFailure(status.gateway_url)
          : message(reason));
      }
    }
    finally { if (serial === sequence.current) setBusy(false); }
  }
  const template = selected && status ? (tab === 'mcp' ? JSON.stringify({ mcpServers: { 'memo-vault': {
    command: 'everos', args: ['vault-mcp', '--gateway', status.gateway_url], env: { MEMO_VAULT_TOKEN: '<YOUR_TOKEN>' },
  } } }, null, 2) : `curl -X POST '${status.gateway_url}/api/v1/external/${selected.mode === 'approved_pack' ? `packs/${selected.pack_id}/deliver` : 'context/build'}' \\\n  -H 'Authorization: Bearer <YOUR_TOKEN>' \\\n  -H 'Content-Type: application/json' \\\n  -d '${JSON.stringify(selected.mode === 'approved_pack' ? {} : { project_ids: selected.project_ids, query: '<KEYWORD>', token_budget: Math.min(4000, selected.token_budget) })}'`) : '';

  return <section className="authorization-page">
    <div className="authorization-toolbar"><p>{t.intro}</p><div className="actions">
      {status && <span className={`status-chip ${status.gateway_online === undefined ? 'muted-chip' : status.gateway_online ? '' : 'offline-chip'}`}>{status.gateway_online === undefined ? t.gatewayUnknown : status.gateway_online ? t.running : t.gatewayStopped}</span>}
      {status && <span className={`status-chip ${status.paused ? 'offline-chip' : 'muted-chip'}`}>{status.paused ? t.paused : t.accessAllowed}</span>}
      {status && <code>{status.gateway_url}</code>}
      <button className="secondary-button" disabled={busy || !status} onClick={() => void togglePause()}>{status?.paused ? t.resume : t.pause}</button>
      <button className="primary-button" disabled={busy} onClick={() => beginCreate()}>{t.newGrant}</button>
    </div></div>
    {error && <p className="error-banner" role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    <div className="grant-list-toolbar"><div className="actions" role="group" aria-label={t.status}>
      {(['all', 'active', 'history'] as const).map(bucket => <button key={bucket} className={grantBucket === bucket ? 'primary-button' : 'secondary-button'} aria-pressed={grantBucket === bucket} onClick={() => { setGrantBucket(bucket); setGrantOffset(0); void refresh(0, bucket); }}>{bucket === 'all' ? t.allGrants : bucket === 'active' ? t.activeGrants : t.historyGrants}</button>)}
    </div><span className="muted">{t.pageSummary(grantTotal ? grantOffset + 1 : 0, Math.min(grantOffset + grants.length, grantTotal), grantTotal)}</span></div>
    <div className="panel grant-table-scroll"><table className="grant-table"><thead><tr>
      {[t.subject,t.projects,t.operations,t.expires,t.purpose,t.status,''].map((label, i) => <th key={i}>{label}</th>)}
    </tr></thead><tbody>
      {grants.map(grant => <tr key={grant.id} className={grant.id === selectedId ? 'grant-selected' : ''}>
        <td><button className="grant-subject" onClick={() => setSelectedId(grant.id)}>{grant.subject}</button><small>{grant.mode === 'live_scope' ? t.live : t.fixed}</small><code>{grant.fingerprint}</code>{grant.last_used_at && <small>{t.lastUsed}: {new Date(grant.last_used_at).toLocaleString()}</small>}</td>
        <td>{grant.project_ids.map(id => <div key={id}>{projects.find(p => p.project_id === id)?.name ?? id}</div>)}</td>
        <td>{grant.operations.map(op => <div key={op}><code>{op}</code></div>)}</td>
        <td>{new Date(grant.expires_at).toLocaleString()}<small>{grant.status === 'active' && new Date(grant.expires_at).getTime() - Date.now() < 86400000 ? '< 24h' : ''}</small></td>
        <td>{grant.purpose}</td><td><span className="status-chip muted-chip">{t[grant.status]}</span></td>
        <td><div className="actions grant-actions">
          {grant.status === 'active' && <><button disabled={busy} className="grant-action grant-action-danger" onClick={() => void mutate('revoke',grant)}>{t.revoke}</button><button disabled={busy} className="grant-action" onClick={() => void mutate('rotate',grant)}>{t.rotate}</button></>}
          {grant.status !== 'active' && <button disabled={busy} className="grant-action grant-action-danger" onClick={() => void mutate('delete',grant)}>{t.deleteGrant}</button>}
          <button disabled={busy} className="grant-action" onClick={() => beginCreate(grant)}>{t.recreate}</button>
          <button className="grant-action" onClick={() => void showAudit(grant)}>{t.audit}</button>
        </div></td>
      </tr>)}
      {!grants.length && <tr><td colSpan={7}>{loading ? t.loading : t.empty}</td></tr>}
    </tbody></table><div className="grant-pagination"><button className="secondary-button" disabled={loading || grantOffset === 0} onClick={() => void refresh(Math.max(0, grantOffset - pageSize))}>{t.previousPage}</button><button className="secondary-button" disabled={loading || grantOffset + grants.length >= grantTotal} onClick={() => void refresh(grantOffset + pageSize)}>{t.nextPage}</button><button className="secondary-button" disabled={loading} onClick={() => void refresh()}>{t.refresh}</button></div></div>

    {revealed?.grantId === selectedId && <div className="panel secret-reveal"><strong>{t.tokenOnce}</strong><code>{revealed.token}</code><div className="actions"><button className="secondary-button" onClick={() => void copy(revealed.token)}>{t.revealCopy}</button><button className="secondary-button" onClick={() => { setToken(revealed.token); setRevealed(null); }}>{t.connection}</button></div></div>}
    <div className="authorization-cards">
      <section className="panel integration-panel"><div className="actions" role="tablist">
        <button role="tab" aria-selected={tab === 'mcp'} className={tab === 'mcp' ? 'primary-button' : 'secondary-button'} onClick={() => setTab('mcp')}>{t.mcpConfig}</button>
        <button role="tab" aria-selected={tab === 'api'} className={tab === 'api' ? 'primary-button' : 'secondary-button'} onClick={() => setTab('api')}>{t.apiConfig}</button>
      </div><label>{t.choose}<select value={selectedId} onChange={e => setSelectedId(e.target.value)}><option value="">—</option>{grants.map(g => <option key={g.id} value={g.id}>{g.subject} · {g.project_ids.join(', ')} · {g.id.slice(0,6)}</option>)}</select></label>
        {template ? <pre tabIndex={0}>{template}</pre> : <p>{t.noSelection}</p>}
        <button className="secondary-button" disabled={!template} onClick={() => void copy(template)}>{t.copy}</button><p className="muted">{t.templateHint}</p>{tab === 'mcp' && <p className="muted">{t.install}</p>}
      </section>
      <section className="panel connection-panel"><h2>{t.connection}</h2><p className="muted">{t.testHint}</p>
        <label>{t.subject}<input value={selected?.subject ?? ''} readOnly /></label>
        <label>{t.token}<input disabled={busy} type="password" value={token} autoComplete="off" placeholder="mv_…" onChange={e => { setToken(e.target.value); setResult(''); setConnectionError(''); }} /></label><small>{t.tokenHint}</small>
        <label>{t.tool}<select aria-label={t.tool} disabled={busy} value={operation} onChange={e => { setOperation(e.target.value as GatewayOperation); setResult(''); setConnectionError(''); }}>{(selected?.operations ?? ['search_memory']).map(op => <option key={op} value={op}>{op === 'get_pack_info' ? t.checkingAccess : op === 'read_memory_pack' ? t.actualDelivery : op}</option>)}</select></label>
        {selected?.mode === 'live_scope' && <label>{t.projects}<select aria-label={t.projects} disabled={busy} value={project} onChange={e => { setProject(e.target.value); setResult(''); setConnectionError(''); }}>{selected.project_ids.map(id => <option key={id}>{id}</option>)}</select></label>}
        {['search_memory','build_context'].includes(operation) && <label>{t.query}<input disabled={busy} value={query} onChange={e => { setQuery(e.target.value); setResult(''); setConnectionError(''); }} /></label>}
        {['get_memory','get_evidence'].includes(operation) && <label>{t.memory}<input disabled={busy} value={memoryId} onChange={e => { setMemoryId(e.target.value); setResult(''); setConnectionError(''); }} /></label>}
        {operation === 'get_evidence' && <label>{t.evidence}<input disabled={busy} value={evidenceId} onChange={e => { setEvidenceId(e.target.value); setResult(''); setConnectionError(''); }} /></label>}
        <button className="primary-button" disabled={busy || !selected || !token || !status || !status.gateway_online || status.paused || selected.status !== 'active' || (['search_memory','build_context'].includes(operation) && !query.trim()) || (['get_memory','get_evidence'].includes(operation) && !memoryId) || (operation === 'get_evidence' && !evidenceId)} onClick={() => void runTest()}>{t.run}</button>
        {connectionError && <div className="connection-error" role="alert"><strong>{t.connectionFailed}</strong><p>{connectionError}</p></div>}
        <p className="muted">{t.deniedHint}</p>{result && <><h3>{t.result}</h3><pre role="status">{result}</pre></>}
      </section>
    </div>
    {audit && <div className="modal-backdrop audit-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) setAudit(null); }}><section className="panel grant-audit-dialog" role="dialog" aria-modal="true" aria-labelledby="audit-title">
      <div className="grant-audit-heading"><div><h2 id="audit-title">{t.auditTitle}</h2><span className="muted">{grants.find(grant => grant.id === audit.grantId)?.subject ?? audit.grantId}</span></div><button className="secondary-button" aria-label={t.close} onClick={() => setAudit(null)}>{t.close}</button></div>
      {audit.loading ? <p>{t.loading}</p> : audit.rows.length ? <div className="grant-audit-scroll"><table className="grant-table"><thead><tr><th>{t.auditOperation}</th><th>{t.auditTransport}</th><th>{t.auditTime}</th><th>{t.auditOutcome}</th><th>{t.auditReason}</th></tr></thead><tbody>{audit.rows.map(row => <tr key={row.id}><td><code>{row.operation}</code></td><td>{row.transport}</td><td>{new Date(row.created_at).toLocaleString()}</td><td>{row.outcome}</td><td>{row.reason || '—'}{row.metadata_json && row.metadata_json !== '{}' && <details><summary>{t.auditDetails}</summary><pre>{row.metadata_json}</pre></details>}</td></tr>)}</tbody></table></div> : <p>{t.auditEmpty}</p>}
    </section></div>}
    {showForm && <div className="modal-backdrop"><form className="panel grant-dialog" role="dialog" aria-modal="true" aria-labelledby="grant-title" onSubmit={e => { e.preventDefault(); void submit(); }}>
      <h2 id="grant-title">{t.newGrant}</h2>
      <label>{t.subject}<input required maxLength={120} value={form.subject} onChange={e => setForm({ ...form, subject:e.target.value })} placeholder="agent:dev-assistant" /></label>
      <label>{t.purpose}<input required maxLength={500} value={form.purpose} onChange={e => setForm({...form,purpose:e.target.value})} /></label>
      <label>{t.mode}<select value={form.mode} disabled={!!draft} onChange={e => setForm({...form,mode:e.target.value as GrantInput['mode']})}><option value="live_scope">{t.live}</option><option value="approved_pack">{t.fixed}</option></select></label>
      {form.mode === 'approved_pack' && !form.draft_id ? <div><p>{t.fixedHint}</p><button type="button" className="secondary-button" onClick={onOpenPack}>{t.openPack}</button></div> : <>
        {form.mode === 'approved_pack' && <p role="status">{t.draftReady} ({draft?.included_count})</p>}
        <fieldset disabled={form.mode === 'approved_pack'}><legend>{t.projects}</legend>{projects.map(p => <label className="grant-check" key={p.project_id}><input type="checkbox" checked={form.project_ids.includes(p.project_id)} onChange={e => setForm({...form,project_ids:e.target.checked ? [...form.project_ids,p.project_id] : form.project_ids.filter(id => id !== p.project_id)})} />{p.name}</label>)}</fieldset>
        <fieldset disabled={form.mode === 'approved_pack'}><legend>{t.kinds}</legend>{kinds.map(kind => <label className="grant-check" key={kind}><input type="checkbox" checked={form.kinds.includes(kind)} onChange={e => setForm({...form,kinds:e.target.checked ? [...form.kinds,kind] : form.kinds.filter(k => k !== kind)})} />{t[kind]}</label>)}</fieldset>
        {form.mode === 'live_scope' && <fieldset><legend>{t.operations}</legend>{liveOps.map(op => <label className="grant-check" key={op}><input type="checkbox" checked={form.operations.includes(op)} onChange={e => setForm({...form,operations:e.target.checked ? [...form.operations,op] : form.operations.filter(o => o !== op), ...(e.target.checked && op === 'get_evidence' ? {evidence_mode:'snippet' as const} : {})})} />{op}</label>)}<small>{t.planned}</small></fieldset>}
        <div className="grant-form-row"><label>{t.hours}<input required type="number" min={1} max={720} value={form.expires_in_hours} onChange={e => setForm({...form,expires_in_hours:Number(e.target.value)})} /></label><label>{t.budget}<input required type="number" min={100} max={32000} disabled={form.mode === 'approved_pack'} value={form.token_budget} onChange={e => setForm({...form,token_budget:Number(e.target.value)})} /></label></div>
        <label>{t.rate}<input type="number" required min={1} max={120} value={form.requests_per_minute} onChange={e => setForm({...form,requests_per_minute:Number(e.target.value)})} /></label>
        <label>{t.evidencePermission}<select value={form.evidence_mode} disabled={form.mode === 'approved_pack'} onChange={e => setForm({...form,evidence_mode:e.target.value as GrantInput['evidence_mode'],operations:e.target.value === 'snippet' ? form.operations : form.operations.filter(o => o !== 'get_evidence')})}><option value="none">{t.none}</option><option value="id">{t.ids}</option><option value="snippet">{t.snippets}</option></select></label>
        {form.mode === 'live_scope' && <label className="grant-check"><input type="checkbox" checked={form.confirm_future_allow} onChange={e => setForm({...form,confirm_future_allow:e.target.checked})} />{t.future}</label>}
        <label className="grant-check"><input type="checkbox" checked={form.confirm_transfer} onChange={e => setForm({...form,confirm_transfer:e.target.checked})} />{t.transfer}</label>
      </>}
      <div className="actions right"><button type="button" className="secondary-button" disabled={busy} onClick={() => { setShowForm(false); onDraftUsed(); }}>{t.cancel}</button><button className="primary-button" disabled={busy || !form.confirm_transfer || !form.project_ids.length || !form.kinds.length || !form.operations.length || (form.mode === 'live_scope' && !form.confirm_future_allow) || (form.mode === 'approved_pack' && !form.draft_id)}>{t.create}</button></div>
      {error && <p className="error-banner" role="alert">{error}</p>}
    </form></div>}
  </section>;
}
