import { useEffect, useState } from "react";
import { claudeRequest, type ClaudeContent } from "./api";

export default function ChatGPTReview({ batchId }: { batchId: string }) {
  const [content, setContent] = useState<ClaudeContent | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void claudeRequest<ClaudeContent>(`imports/${batchId}/claude-content`)
      .then((result) => { if (active) setContent(result); })
      .catch((reason: unknown) => { if (active) setError(String(reason)); });
    return () => { active = false; };
  }, [batchId]);

  async function approve(recordId: string, status: string) {
    setBusyId(recordId);
    setError(null);
    try {
      if (status === "submitting" || status === "delivery_unknown") {
        await claudeRequest(`imports/${batchId}/documents/${recordId}/reconcile`, "POST");
      } else {
        await claudeRequest(`imports/${batchId}/documents/${recordId}/review`, "POST", JSON.stringify({ decision: "knowledge" }));
        await claudeRequest(`imports/${batchId}/documents/${recordId}/deliver`, "POST", JSON.stringify({ confirmed: true }));
      }
      setContent(await claudeRequest<ClaudeContent>(`imports/${batchId}/claude-content`));
      setNotice("Knowledge 投递状态已更新。");
    } catch (reason) {
      setError(String(reason));
      setContent(await claudeRequest<ClaudeContent>(`imports/${batchId}/claude-content`).catch(() => content));
    } finally {
      setBusyId(null);
    }
  }

  return <section>
    <h3>Agent 轨迹</h3>
    {content?.agent_events.length ? <div className="table-wrap"><table><thead><tr><th>事件</th><th>工具 / 调用 ID</th><th>时间</th><th>参数或结果</th></tr></thead><tbody>
      {content.agent_events.map(event => <tr key={event.record_id}><td>{event.record_type}</td><td>{event.tool_name ?? "结果"}<br /><code>{event.call_id ?? "无调用 ID"}</code></td><td>{event.timestamp_ms == null ? "未提供" : new Date(event.timestamp_ms).toLocaleString()}</td><td><pre className="trace-preview">{event.preview}</pre></td></tr>)}
    </tbody></table></div> : <p className="subtle">本次导出没有可识别的工具调用。</p>}
    <h3>项目文档与附件</h3>
    {content?.documents.length ? <><p className="subtle">原文件已保留在 Vault。仅批准的文档会进入项目 Knowledge。</p>
      {content.documents.map(record => <article className="claude-content-card" key={record.record_id}>
        <strong>{record.title ?? "未命名文档"}</strong>
        <p>{record.status === "delivered" ? "已进入 Knowledge" : "保留在 Vault"}</p>
        <details><summary>查看文档</summary><div className="claude-document-text">{record.text || "此文件没有可显示的纯文本。"}</div></details>
        <button className="primary-button" disabled={busyId !== null || record.status === "delivered"} onClick={() => void approve(record.record_id, record.status)}>{record.status === "delivered" ? "已进入 Knowledge" : record.status === "submitting" || record.status === "delivery_unknown" ? "核对投递结果" : busyId === record.record_id ? "处理中…" : "批准进入 Knowledge"}</button>
      </article>)}
    </> : <p className="subtle">本次导出没有可投递的项目附件或文档。</p>}
    {notice ? <p className="notice success" role="status">{notice}</p> : null}
    {error ? <p className="notice error" role="alert">{error}</p> : null}
  </section>;
}
