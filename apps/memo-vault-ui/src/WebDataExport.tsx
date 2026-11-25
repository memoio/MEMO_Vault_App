import { useEffect, useState } from "react";
import { ownerFetch } from "./ownerSession";
import type { ProjectSpace } from "./api";
import { buildWebReaderHtml, type DataVaultRecordMeta } from "./webReaderTemplate";

interface RecordPage {
  records: DataVaultRecordMeta[];
  total: number;
  page: number;
  page_size: number;
}

interface Envelope<T> {
  data: T;
}

async function jsonData<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await ownerFetch(url, init);
  if (!response.ok) {
    let message = `Web 数据请求失败：HTTP ${response.status}`;
    try {
      const body = await response.json();
      const detail = body?.detail;
      if (typeof detail === "string") message = detail;
      else if (typeof detail?.message === "string") message = detail.message;
    } catch {
      /* keep HTTP message */
    }
    throw new Error(message);
  }
  return ((await response.json()) as Envelope<T>).data;
}

async function saveBlobDownload(response: Response, fallback: string) {
  if (!response.ok) throw new Error(`下载失败：HTTP ${response.status}`);
  const blob = await response.blob();
  const match = response.headers
    .get("content-disposition")
    ?.match(/filename\*?=(?:UTF-8''|")?([^";]+)/i);
  const filename = match ? decodeURIComponent(match[1].replace(/"$/, "")) : fallback;
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(link.href), 60_000);
}

function formatSize(bytes: number): string {
  return bytes < 1024 * 1024
    ? `${(bytes / 1024).toFixed(1)} KB`
    : `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function formatDateTime(value: string): string {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

export default function WebDataExport({ projects }: { projects: ProjectSpace[] }) {
  const [projectId, setProjectId] = useState(() => {
    const fromUrl = new URLSearchParams(window.location.search).get("project_id")?.trim();
    return fromUrl || "all";
  });
  const [domain, setDomain] = useState("");
  const [captureMode, setCaptureMode] = useState("");
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [reloadKey, setReloadKey] = useState(0);
  const [result, setResult] = useState<RecordPage | null>(null);
  const [listError, setListError] = useState<string | null>(null);

  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [includePayload, setIncludePayload] = useState(false);
  const [activeRecordId, setActiveRecordId] = useState<string | null>(() => {
    return new URLSearchParams(window.location.search).get("record_id")?.trim() || null;
  });

  const [viewMode, setViewMode] = useState<"reader" | "source">("reader");
  const [rawHtml, setRawHtml] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 3500);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({
      project_id: projectId,
      page: String(page),
      page_size: "20",
    });
    if (domain.trim()) params.set("domain", domain.trim());
    if (captureMode) params.set("capture_mode", captureMode);
    if (query.trim()) params.set("query", query.trim());

    setListError(null);
    void jsonData<RecordPage>(`/api/v1/data-vault/records?${params.toString()}`, {
      signal: controller.signal,
    })
      .then((data) => {
        if (controller.signal.aborted) return;
        setResult(data);
        setSelectedIds((prev) => prev.filter((id) => data.records.some((r) => r.record_id === id)));
        setActiveRecordId((prev) =>
          prev && data.records.some((r) => r.record_id === prev)
            ? prev
            : data.records[0]?.record_id ?? null,
        );
      })
      .catch((reason) => {
        if (controller.signal.aborted) return;
        setListError(reason instanceof Error ? reason.message : String(reason));
      });
    return () => controller.abort();
  }, [projectId, domain, captureMode, query, page, reloadKey]);

  const activeRecord =
    result?.records.find((r) => r.record_id === activeRecordId) ?? null;

  useEffect(() => {
    if (!activeRecord) {
      setRawHtml(null);
      setPreviewError(null);
      return;
    }
    const controller = new AbortController();
    setPreviewLoading(true);
    setPreviewError(null);
    setRawHtml(null);
    void ownerFetch(
      `/api/v1/data-vault/records/${encodeURIComponent(activeRecord.record_id)}/html`,
      { signal: controller.signal },
    )
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTML 快照读取失败：HTTP ${res.status}`);
        return res.text();
      })
      .then((htmlText) => {
        if (!controller.signal.aborted) setRawHtml(htmlText);
      })
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setPreviewError(reason instanceof Error ? reason.message : String(reason));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setPreviewLoading(false);
      });
    return () => controller.abort();
  }, [activeRecord?.record_id, reloadKey]);

  const records = result?.records ?? [];
  const availableRecords = records.filter((r) => r.html_available);
  const chosenRecords = records.filter((r) => selectedIds.includes(r.record_id));
  const totalSelectedBytes = chosenRecords.reduce((sum, r) => sum + r.html_bytes, 0);
  const allChecked =
    availableRecords.length > 0 &&
    availableRecords.every((r) => selectedIds.includes(r.record_id));

  async function handleDownloadHtml(record: DataVaultRecordMeta) {
    if (busy) return;
    setBusy(true);
    setNotice(null);
    setListError(null);
    try {
      const response = await ownerFetch(
        `/api/v1/data-vault/records/${encodeURIComponent(record.record_id)}/html?download=1`,
      );
      await saveBlobDownload(response, `${record.record_id}.html`);
      setNotice(`已开始下载 ${record.record_id}.html`);
    } catch (reason) {
      setListError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function handleBatchZip() {
    if (busy || !chosenRecords.length) return;
    setBusy(true);
    setNotice(null);
    setListError(null);
    try {
      const response = await ownerFetch("/api/v1/data-vault/records/export-zip", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          record_ids: chosenRecords.map((r) => r.record_id),
          include_payload: includePayload,
        }),
      });
      await saveBlobDownload(response, "data-vault-web-export.zip");
      setNotice(`已打包下载 ${chosenRecords.length} 条 Web 数据（ZIP）。`);
    } catch (reason) {
      setListError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteRecord(record: DataVaultRecordMeta) {
    if (busy) return;
    const ok = window.confirm(
      `确定永久删除 Web 归档「${record.title}」（${record.record_id}）？\n此操作会同时删除磁盘中的 HTML 快照且无法恢复。`,
    );
    if (!ok) return;
    setBusy(true);
    setNotice(null);
    setListError(null);
    try {
      await jsonData<{ deleted: boolean; record_id: string }>(
        `/api/v1/data-vault/records/${encodeURIComponent(record.record_id)}`,
        { method: "DELETE" },
      );
      setSelectedIds((prev) => prev.filter((id) => id !== record.record_id));
      setNotice(`已删除归档 ${record.record_id}。`);
      setReloadKey((k) => k + 1);
    } catch (reason) {
      setListError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function handleCopySource() {
    if (!rawHtml) return;
    try {
      await navigator.clipboard.writeText(rawHtml);
      setNotice("已复制原始 HTML5 源码到剪贴板。");
    } catch {
      setListError("复制失败，请手动选中复制。");
    }
  }

  const readerHtml =
    activeRecord && rawHtml ? buildWebReaderHtml(rawHtml, activeRecord) : null;

  return (
    <section className="web-export">
      <div className="card-heading">
        <div>
          <h2>Web 数据（Data Vault）</h2>
          <p className="subtle">
            浏览并导出由 MEMO Plugins 抓取归档的独立 HTML5 网页快照（不进入正式记忆库或 Knowledge）。
          </p>
        </div>
        <button
          className="secondary-button"
          disabled={busy}
          onClick={() => setReloadKey((k) => k + 1)}
        >
          刷新
        </button>
      </div>

      <div className="web-export-filters">
        <label>
          项目空间
          <select
            value={projectId}
            onChange={(e) => {
              setProjectId(e.target.value);
              setPage(1);
              setSelectedIds([]);
            }}
          >
            <option value="all">全部项目空间</option>
            {projects.map((item) => (
              <option key={item.project_id} value={item.project_id}>
                {item.full_path}
              </option>
            ))}
          </select>
        </label>
        <label>
          来源域名
          <input
            value={domain}
            onChange={(e) => {
              setDomain(e.target.value);
              setPage(1);
            }}
            placeholder="例如 huggingface.co"
          />
        </label>
        <label>
          采集模式
          <select
            value={captureMode}
            onChange={(e) => {
              setCaptureMode(e.target.value);
              setPage(1);
            }}
          >
            <option value="">全部模式</option>
            <option value="page">整页采集</option>
            <option value="selection">选区采集</option>
          </select>
        </label>
        <label>
          关键词搜索
          <div className="knowledge-search">
            <input
              value={queryInput}
              onChange={(e) => setQueryInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  setQuery(queryInput.trim());
                  setPage(1);
                }
              }}
              placeholder="标题 / URL / 编号"
            />
            <button
              className="secondary-button"
              onClick={() => {
                setQuery(queryInput.trim());
                setPage(1);
              }}
            >
              搜索
            </button>
          </div>
        </label>
      </div>

      {listError ? (
        <p className="notice error" role="alert">
          {listError}
        </p>
      ) : null}
      {notice ? <p className="notice success">{notice}</p> : null}

      <div className="web-export-batchbar">
        <label className="web-batch-check">
          <input
            type="checkbox"
            checked={allChecked}
            disabled={availableRecords.length === 0 || busy}
            onChange={(e) =>
              setSelectedIds(
                e.target.checked ? availableRecords.map((r) => r.record_id) : [],
              )
            }
          />
          <span>
            全选本页（共 {result?.total ?? 0} 条 · 已选 {chosenRecords.length} 条 /{" "}
            {formatSize(totalSelectedBytes)}）
          </span>
        </label>
        <div className="web-batch-actions">
          <label className="web-batch-check">
            <input
              type="checkbox"
              checked={includePayload}
              disabled={busy}
              onChange={(e) => setIncludePayload(e.target.checked)}
            />
            <span>ZIP 附带 payload.json</span>
          </label>
          <button
            className="primary-button"
            disabled={busy || chosenRecords.length === 0}
            onClick={() => void handleBatchZip()}
          >
            {busy ? "处理中…" : `下载所选 ZIP (${chosenRecords.length})`}
          </button>
        </div>
      </div>

      <div className="web-export-layout">
        <div className="web-record-column">
          {!result && !listError ? (
            <div className="web-empty-card">正在读取 Data Vault 网页归档…</div>
          ) : records.length === 0 ? (
            <div className="web-empty-card">
              <strong>暂无 Web 数据归档</strong>
              <span>使用 MEMO Plugins 采集普通网页并授权存入 Data Vault 后，会展示在这里。</span>
            </div>
          ) : (
            <div className="web-record-list">
              {records.map((item) => {
                const isActive = item.record_id === activeRecord?.record_id;
                const isChecked = selectedIds.includes(item.record_id);
                return (
                  <article
                    key={item.record_id}
                    className={`web-record-card ${isActive ? "selected" : ""}`}
                    onClick={() => setActiveRecordId(item.record_id)}
                  >
                    <div className="web-record-top">
                      <input
                        type="checkbox"
                        aria-label={`选择 ${item.title}`}
                        checked={isChecked}
                        disabled={!item.html_available || busy}
                        onClick={(e) => e.stopPropagation()}
                        onChange={(e) =>
                          setSelectedIds((prev) =>
                            e.target.checked
                              ? [...prev, item.record_id]
                              : prev.filter((id) => id !== item.record_id),
                          )
                        }
                      />
                      <div className="web-record-info">
                        <strong className="web-record-title">{item.title}</strong>
                        <small className="web-record-sub">
                          {item.record_id} · {item.project_name} ·{" "}
                          {item.capture_mode === "selection" ? "选区" : "整页"} ·{" "}
                          {item.unit_count} 项 · {formatSize(item.html_bytes)}
                        </small>
                        <small className="web-record-url">
                          {item.domain || item.source_url} · {formatDateTime(item.created_at)}
                        </small>
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          )}

          {result && result.total > result.page_size ? (
            <div className="actions">
              <button
                className="secondary-button"
                disabled={page <= 1}
                onClick={() => {
                  setSelectedIds([]);
                  setPage(page - 1);
                }}
              >
                上一页
              </button>
              <span className="subtle">
                第 {page} / {Math.max(1, Math.ceil(result.total / result.page_size))} 页
              </span>
              <button
                className="secondary-button"
                disabled={page * result.page_size >= result.total}
                onClick={() => {
                  setSelectedIds([]);
                  setPage(page + 1);
                }}
              >
                下一页
              </button>
            </div>
          ) : null}
        </div>

        <div className="web-preview-panel">
          {!activeRecord ? (
            <div className="web-empty-card">选择左侧一条 Web 归档以查看 HTML5 模板预览。</div>
          ) : (
            <>
              <div className="web-preview-toolbar">
                <div className="web-preview-tabs">
                  <button
                    className={viewMode === "reader" ? "selected" : ""}
                    onClick={() => setViewMode("reader")}
                  >
                    阅读模板预览
                  </button>
                  <button
                    className={viewMode === "source" ? "selected" : ""}
                    onClick={() => setViewMode("source")}
                  >
                    HTML5 源码
                  </button>
                </div>
                <div className="web-preview-actions">
                  <a
                    className="secondary-button web-link-btn"
                    href={activeRecord.source_url}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    打开原网页 ↗
                  </a>
                  {viewMode === "source" && rawHtml ? (
                    <button
                      className="secondary-button"
                      onClick={() => void handleCopySource()}
                    >
                      复制源码
                    </button>
                  ) : null}
                  <button
                    className="secondary-button"
                    disabled={!activeRecord.html_available || busy}
                    onClick={() => void handleDownloadHtml(activeRecord)}
                  >
                    下载 HTML
                  </button>
                  <button
                    className="secondary-button danger-button"
                    disabled={busy}
                    onClick={() => void handleDeleteRecord(activeRecord)}
                  >
                    删除归档
                  </button>
                </div>
              </div>

              {previewLoading ? (
                <div className="web-empty-card">正在加载 HTML5 快照…</div>
              ) : previewError ? (
                <div className="notice error" role="alert">
                  {previewError}
                </div>
              ) : viewMode === "reader" && readerHtml ? (
                <iframe
                  title={`Web 预览：${activeRecord.title}`}
                  className="web-reader-frame"
                  sandbox="allow-popups allow-popups-to-escape-sandbox"
                  srcDoc={readerHtml}
                />
              ) : rawHtml ? (
                <pre className="web-source-pre">{rawHtml}</pre>
              ) : (
                <div className="web-empty-card">暂无可预览的 HTML 内容。</div>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}
