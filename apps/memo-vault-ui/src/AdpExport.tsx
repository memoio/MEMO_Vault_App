import { useCallback, useEffect, useState } from "react";
import { ownerFetch } from "./ownerSession";

type Trace = { id: string; session_id: string; title: string; source: string; project_id: string; tool_call_count: number; tool_result_count: number; consent: boolean; eligible: boolean };
type Redaction = { category: string; path: string; snippet: string };
type Result = { id: string; valid: boolean; error?: string; redaction_count?: number; redactions?: Redaction[]; trajectory?: unknown };
type ExportFormat = "adp" | "atif";
type Validation = { valid: boolean; format: ExportFormat; traces: Result[]; preview_sha256: string | null };

const redactionGroups = [
  { id: "message", label: "消息" },
  { id: "tool_arguments", label: "工具参数" },
  { id: "tool_result", label: "工具结果" },
  { id: "metadata", label: "元数据" },
];

function MaskedSnippet({ text }: { text: string }) {
  return <>{text.split("[REDACTED]").map((part, index) => <span key={index}>{index > 0 && <mark>[REDACTED]</mark>}{part}</span>)}</>;
}

function RedactionDetails({ redactions }: { redactions: Redaction[] }) {
  if (!redactions.length) return <p>未发现规则匹配的敏感信息，请仍检查完整内容。</p>;
  return <div className="adp-redactions">
    <p>以下仅展示替换后的内容。同一信息可能同时存在于正文和元数据中，因此会分别计数。</p>
    {redactionGroups.map(group => {
      const entries = redactions.filter(item => item.category === group.id);
      if (!entries.length) return null;
      return <details key={group.id} open><summary>{group.label} · {entries.length} 处</summary>
        <ol>{entries.map((item, index) => <li key={`${item.path}-${index}`}>
          <code className="adp-redaction-path">{item.path}</code>
          <div className="adp-redaction-snippet"><MaskedSnippet text={item.snippet} /></div>
        </li>)}</ol>
      </details>;
    })}
    <small>位置以 JSON 路径表示；$json 表示字符串内解析出的 JSON 内容。</small>
  </div>;
}

async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await ownerFetch(`/api/v1/adp/${path}`, body === undefined ? undefined : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data?.detail?.message ?? `请求失败 (${response.status})`);
  return data.data as T;
}

export default function AdpExport() {
  const [traces, setTraces] = useState<Trace[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [format, setFormat] = useState<ExportFormat>("adp");
  const [result, setResult] = useState<Validation | null>(null);
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const refresh = useCallback(async () => {
    setLoading(true);
    try { setTraces(await request<Trace[]>("traces")); setError(""); }
    catch (e) { setError(e instanceof Error ? e.message : "读取轨迹失败"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);

  async function consent(ids: string[], value: boolean) {
    setBusy(true); setError(""); setResult(null);
    try { await request("consent", { trace_ids: ids, consent: value }); await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : "更新授权失败"); }
    finally { setBusy(false); }
  }
  async function validate(ids: string[], showPreview = false) {
    setBusy(true); setError(""); setResult(null);
    try {
      const data = await request<Validation>("validate", { trace_ids: ids, format });
      if (showPreview) setPreviewId(ids[0]);
      else { setPreviewId(null); setResult(data); }
      if (showPreview) setResult(data);
    } catch (e) { setError(e instanceof Error ? e.message : "校验失败"); }
    finally { setBusy(false); }
  }
  async function download(target: ExportFormat) {
    if (!result?.valid || !result.preview_sha256 || previewId || result.format !== target) return;
    setBusy(true); setError("");
    try {
      const response = await ownerFetch("/api/v1/adp/export", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ trace_ids: selected, format: target, preview_sha256: result.preview_sha256 }) });
      if (!response.ok) { const data = await response.json(); throw new Error(data?.detail?.message ?? "下载失败"); }
      const url = URL.createObjectURL(await response.blob());
      const a = document.createElement("a"); a.href = url; a.download = `${target}-candidates.jsonl`; a.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { setResult(null); setError(e instanceof Error ? e.message : "下载失败"); }
    finally { setBusy(false); }
  }
  function toggle(id: string) { setSelected(old => old.includes(id) ? old.filter(x => x !== id) : [...old, id]); setResult(null); setPreviewId(null); }

  return <section className="adp-workspace">
    <div className="adp-notice">Beta：导出 ADP 或 ATIF-v1.7 候选 JSONL 并进行本地子集 Schema 校验，不执行训练。每个完整会话对应一条轨迹。</div>
    <div className="adp-notice adp-info">训练授权仅代表你本人的意愿。轨迹可能包含他人信息，请检查脱敏预览；自动脱敏不能保证发现全部敏感信息。</div>
    <div className="adp-actions"><button className="secondary-button" disabled={!selected.length || busy} onClick={() => void consent(selected, true)}>对所选轨迹允许训练</button><button className="secondary-button" disabled={!selected.length || busy} onClick={() => void consent(selected, false)}>撤销所选授权</button><button className="secondary-button" disabled={busy || loading} onClick={() => void refresh()}>刷新</button><span>仅将授权写入 training_consent 字段，不代表已进行训练。</span></div>
    {error && <p role="alert" className="adp-error">{error}</p>}
    <div className="adp-table-wrap"><table className="adp-table"><thead><tr><th>选择</th><th>轨迹</th><th>来源 / 项目</th><th>工具调用</th><th>观察结果</th><th>训练授权</th><th>脱敏预览</th></tr></thead><tbody>
      {traces.map(t => <tr key={t.id} className={!t.eligible ? "adp-disabled" : ""}><td><input aria-label={`选择 ${t.title}`} type="checkbox" disabled={!t.eligible || busy} checked={selected.includes(t.id)} onChange={() => toggle(t.id)} /></td><td><strong>{t.title}</strong><br/><small>{t.session_id}</small>{!t.eligible && <div>无真实工具调用，不能导出</div>}</td><td>{t.source === "deepseek_harness" ? "DeepSeek Harness" : "Claude"}<br/><small>{t.project_id}</small></td><td>{t.tool_call_count}</td><td>{t.tool_result_count} 条</td><td>{t.eligible ? <label><input type="checkbox" checked={t.consent} disabled={busy} onChange={e => void consent([t.id], e.target.checked)} /> 允许</label> : "—"}</td><td>{t.eligible ? <button className="secondary-button" disabled={busy} onClick={() => void validate([t.id], true)}>预览</button> : "—"}</td></tr>)}
    </tbody></table>{!loading && !traces.length && <p className="adp-empty">暂无已归档的 DeepSeek 或 Claude 会话。</p>}{loading && <p className="adp-empty">正在读取轨迹…</p>}</div>
    <div className="adp-actions"><label className="adp-format-select">预览与校验格式 <select value={format} disabled={busy} onChange={e => { setFormat(e.target.value as ExportFormat); setResult(null); setPreviewId(null); }}><option value="adp">ADP</option><option value="atif">ATIF-v1.7</option></select></label><button className="secondary-button" disabled={!selected.length || busy} onClick={() => void validate(selected)}>Schema 校验</button><button className="primary-button" disabled={!result?.valid || result.format !== "adp" || !!previewId || busy} onClick={() => void download("adp")}>下载 ADP JSONL</button><button className="primary-button" disabled={!result?.valid || result.format !== "atif" || !!previewId || busy} onClick={() => void download("atif")}>下载 ATIF JSONL</button><small>请选择格式并校验后下载。</small></div>
    {result && <div className="adp-results" aria-live="polite"><strong>{previewId ? "脱敏预览" : result.valid ? "校验通过" : "校验未通过"}</strong>{result.traces.map(r => <div key={r.id}><p>{traces.find(t => t.id === r.id)?.title ?? r.id}：{r.valid ? `校验通过，脱敏 ${r.redaction_count} 处` : r.error}</p>{r.valid && <><RedactionDetails redactions={r.redactions ?? []} /><details><summary>查看脱敏后的完整候选内容</summary><pre>{JSON.stringify(r.trajectory, null, 2)}</pre></details></>}</div>)}</div>}
  </section>;
}
