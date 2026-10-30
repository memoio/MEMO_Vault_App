import AuthorizationMcpPage from "./AuthorizationMcpPage";
import { createPackDraft, type FixedDraft } from "./gatewayApi";
import { text as authText } from "./authorization.zh";
import ClaudeImport from "./ClaudeImport";
import ChatGPTReview from "./ChatGPTReview";
import KnowledgeExport from "./KnowledgeExport";
import TrackInventory from "./TrackInventory";
import AdpExport from "./AdpExport";
import { useEffect, useRef, useState, type DragEvent } from "react";
import {
  cancelPreview,
  commitImport,
  confirmCandidate,
  createManualCandidate,
  createProject,
  correctLibraryMemory,
  deleteLibraryMemory,
  extractImport,
  getLibraryMemories,
  getLibraryMemory,
  getMemoryLibrarySummary,
  getHealth,
  getCandidates,
  getCandidateSource,
  getProjects,
  previewMemoryPack,
  exportMemoryPack,
  previewImport,
  rejectCandidate,
  withdrawLibraryMemory,
  type CandidateKind,
  type CandidateChanges,
  type ManualCandidateInput,
  type ReviewCandidate,
  type SourceConversation,
  type ExtractionResult,
  type ImportBatch,
  type ImportPreview,
  type LibraryMemory,
  type LibraryMemoryDetail,
  type LibraryProjectCount,
  type LibraryStatus,
  type ProjectSpace,
  type VaultHealth,
  type PackKind,
  type PackFormat,
  type PackEvidenceMode,
  type PackPreview,
  type PackPreviewItem,
  type PackExport,
  type PackExportRequest,
} from "./api";

const pages = [
  { id: "imports", title: "导入", description: "安全检查、预览并归档原始记录。" },
  { id: "review", title: "候选审核", description: "查看来源证据，逐条确认进入 EverOS 正式记忆。" },
  { id: "library", title: "记忆库", description: "浏览已确认并写入 EverOS 的正式记忆。" },
  { id: "pack", title: "导出中心", description: "导出已确认的正式记忆或项目 Knowledge 原始文档。" },
  { id: "authorization", title: authText.title, description: authText.description },
  { id: "adp", title: "ADP / ATIF 导出 Beta", description: "真实 Agent 轨迹 → ADP / ATIF 候选数据" },
] as const;
const wizardSteps = ["选择来源", "上传与检查", "解析预览", "抽取与审核", "完成"];
const sources = [
  { id: "generic", title: "通用文件", detail: "Markdown / JSON / TXT / ZIP", available: true },
  { id: "deepseek_harness", title: "DeepSeek", detail: "网页导出 ZIP 或 Harness v4 会话", available: true },
  { id: "chatgpt", title: "ChatGPT 导出", detail: "账户导出 ZIP · 对话与来源文件", available: true },
  { id: "claude", title: "Claude 导出", detail: "多个本地 ZIP 一次导入 / Manifest JSON", available: true },
  { id: "agent", title: "Cursor / Agent 记录", detail: "后续支持", available: false },
  { id: "auto", title: "自动抓取", detail: "MVP 不支持", available: false },
] as const;

type PageId = (typeof pages)[number]["id"];
type ExtractState = "idle" | "running" | "done" | "failed" | "deferred";
type DeliveredPackSnapshot = PackExport & { delivery_action: "copy" | "download" };

function formatSize(bytes: number): string {
  return bytes < 1024 * 1024
    ? `${(bytes / 1024).toFixed(1)} KB`
    : `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function formatTime(timestamp: number | null): string {
  if (timestamp === null) return "未提供";
  return new Date(timestamp).toLocaleString();
}

function formatDateTime(value: string): string {
  const normalized = value.includes("T") ? value : `${value.replace(" ", "T")}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function canConfirmCandidate(candidate: ReviewCandidate): boolean {
  return candidate.origin === "manual" || candidate.evidence_status === "valid";
}

function archiveStatusLabel(status: string | undefined): string {
  switch (status) {
    case "available": return "原文件哈希一致";
    case "hash_mismatch": return "原文件内容已变化（哈希不一致）";
    case "hash_unavailable": return "未记录文件哈希，无法校验";
    case "missing": return "原文件缺失";
    default: return "原文件状态未知";
  }
}

function segmentStatusLabel(status: string | undefined): string | null {
  switch (status) {
    case "hash_mismatch": return "输入片段哈希不匹配，已回退到关联消息";
    case "invalid": return "输入片段格式无效，已回退到关联消息";
    case "legacy_fallback": return "旧数据：使用关联消息作为来源片段";
    default: return null;
  }
}

function highlightSourceSpan(
  content: string,
  position: { char_start: number; char_end: number } | null | undefined,
) {
  if (!position || position.char_start < 0 || position.char_end <= position.char_start) {
    return content;
  }
  // Backend offsets count Unicode code points; Array.from keeps emoji and other
  // supplementary characters aligned with Python's string indexing.
  const characters = Array.from(content);
  const start = Math.min(position.char_start, characters.length);
  const end = Math.min(position.char_end, characters.length);
  if (start >= end) return content;
  return <>{characters.slice(0, start).join("")}<mark>{characters.slice(start, end).join("")}</mark>{characters.slice(end).join("")}</>;
}

function memoryKindLabel(kind: string): string {
  return ({ preference: "画像", fact: "事实", decision: "决策", todo: "待办", episode: "情节", profile: "画像" } as Record<string, string>)[kind] ?? kind;
}

function externalUseLabel(value: LibraryMemory["external_use"]): string {
  return value === "allow" ? "允许外发" : value === "deny" ? "不可外发" : "导出时确认";
}

const packKindOptions: { value: PackKind; label: string }[] = [
  { value: "fact", label: "事实" },
  { value: "episode", label: "情节" },
  { value: "profile", label: "画像" },
];

const exclusionLabels: Record<string, string> = {
  expired: "已过期",
  withdrawn: "已撤销",
  deleted: "已删除",
  index_unavailable: "EverOS 索引暂不可用",
  empty_canonical_body: "正式正文为空",
  not_selected: "手动排除",
  ask_requires_selection: "待逐条选择的 ask 项",
  over_budget: "超出预算，整条排除",
};

function packErrorLabel(message: string): string {
  if (message.includes("EVEROS")) return `EverOS 当前离线或暂不可用。${message}`;
  if (message.includes("PROJECT_NOT_FOUND")) return "所选项目空间已不存在，请重新选择。";
  if (message.includes("PACK_PREVIEW_STALE")) return "预览已过期，正在重新读取当前正式记忆。请核对后再操作。";
  if (message.includes("PACK_SOURCE_CHANGED")) return "读取期间记忆状态发生变化，请重试预览。";
  return message;
}

function MemoryPackPage({
  projects,
  projectLoadError,
  onRetryProjects,
  onAuthorize,
}: {
  projects: ProjectSpace[];
  projectLoadError: string | null;
  onRetryProjects: () => void;
  onAuthorize: (draft: FixedDraft) => void;
}) {
  const [selectedProjects, setSelectedProjects] = useState<string[] | null>(null);
  const [selectedKinds, setSelectedKinds] = useState<PackKind[]>(packKindOptions.map((item) => item.value));
  const [format, setFormat] = useState<PackFormat>("markdown");
  const [evidenceMode, setEvidenceMode] = useState<PackEvidenceMode>("id");
  const [tokenBudget, setTokenBudget] = useState(4000);
  const [selectedMemoryIds, setSelectedMemoryIds] = useState<string[] | null>(null);
  const [packPreview, setPackPreview] = useState<PackPreview | null>(null);
  const [deliveredSnapshot, setDeliveredSnapshot] = useState<DeliveredPackSnapshot | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [confirmationOpen, setConfirmationOpen] = useState(false);
  const [confirmedAskIds, setConfirmedAskIds] = useState<string[]>([]);
  const [confirmEvidence, setConfirmEvidence] = useState(false);
  const [confirmTransfer, setConfirmTransfer] = useState(false);
  const [deliveryAction, setDeliveryAction] = useState<"copy" | "download" | "authorize">("copy");
  const [exportBusy, setExportBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const sequence = useRef(0);
  const scopeInitialized = useRef(false);

  useEffect(() => {
    if (scopeInitialized.current || projects.length === 0) return;
    scopeInitialized.current = true;
    const personal = projects.find((project) => project.project_id === "personal");
    setSelectedProjects(personal ? [personal.project_id] : []);
  }, [projects]);

  useEffect(() => {
    const currentSequence = ++sequence.current;
    if (selectedProjects === null) return;
    if (selectedProjects.length === 0 || selectedKinds.length === 0) {
      setPackPreview(null);
      setPreviewLoading(false);
      setPreviewError(null);
      return;
    }
    const controller = new AbortController();
    setPreviewLoading(true);
    setPreviewError(null);
    setNotice(null);
    previewMemoryPack({
      namespaces: selectedProjects,
      kinds: selectedKinds,
      format,
      token_budget: tokenBudget,
      evidence_mode: evidenceMode,
      memory_ids: selectedMemoryIds,
    }, controller.signal)
      .then((result) => {
        if (controller.signal.aborted || currentSequence !== sequence.current) return;
        setPackPreview(result);
        if (selectedMemoryIds === null) {
          setSelectedMemoryIds(result.items.filter((item) => item.selected).map((item) => item.memory_id));
        }
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted || currentSequence !== sequence.current) return;
        setPackPreview(null);
        const message = reason instanceof Error ? reason.message : "Memory Pack 预览失败。";
        if (message.includes("PACK_SELECTION_INVALID") && selectedMemoryIds !== null) {
          setPreviewError("PACK_PREVIEW_STALE: 所选记忆已离开当前可导出范围，正在清除旧选择并重新读取。");
          setSelectedMemoryIds(null);
          return;
        }
        setPreviewError(message);
      })
      .finally(() => {
        if (currentSequence === sequence.current) setPreviewLoading(false);
      });
    return () => controller.abort();
  }, [selectedProjects, selectedKinds, format, evidenceMode, tokenBudget, selectedMemoryIds, reloadKey]);

  const selectedIds = selectedMemoryIds ?? packPreview?.items.filter((item) => item.selected).map((item) => item.memory_id) ?? [];
  const allowItems = packPreview?.items.filter((item) => item.external_use === "allow") ?? [];
  const askItems = packPreview?.items.filter((item) => item.external_use === "ask") ?? [];
  const includedAskItems = packPreview
    ? packPreview.selected_ask_ids
      .map((id) => packPreview.items.find((item) => item.memory_id === id))
      .filter((item): item is PackPreviewItem => Boolean(item))
    : [];

  function toggleMemory(memoryId: string, checked: boolean) {
    setDeliveredSnapshot(null);
    setSelectedMemoryIds((current) => {
      const ids = new Set(current ?? packPreview?.items.filter((item) => item.selected).map((item) => item.memory_id) ?? []);
      if (checked) ids.add(memoryId);
      else ids.delete(memoryId);
      return [...ids].sort();
    });
    setConfirmedAskIds([]);
    setConfirmationOpen(false);
  }

  function toggleKind(kind: PackKind, checked: boolean) {
    setDeliveredSnapshot(null);
    setSelectedKinds((current) => checked ? [...current, kind] : current.filter((item) => item !== kind));
    setSelectedMemoryIds(null);
    setConfirmationOpen(false);
  }

  function toggleProject(projectId: string, checked: boolean) {
    setDeliveredSnapshot(null);
    setSelectedProjects((current) => {
      const ids = new Set(current ?? []);
      if (checked) ids.add(projectId);
      else ids.delete(projectId);
      return [...ids];
    });
    setSelectedMemoryIds(null);
    setConfirmationOpen(false);
  }

  async function performDelivery(action: "copy" | "download" | "authorize") {
    if (!packPreview || exportBusy || !selectedProjects?.length) return;
    setExportBusy(true);
    setPreviewError(null);
    try {
      const request: PackExportRequest = {
        namespaces: selectedProjects,
        kinds: selectedKinds,
        format,
        token_budget: tokenBudget,
        evidence_mode: evidenceMode,
        memory_ids: selectedIds,
        preview_sha256: packPreview.preview_sha256,
        preview_generated_at: packPreview.generated_at,
        confirmed_ask_ids: confirmedAskIds,
        confirm_evidence: confirmEvidence,
        confirm_unrecoverable_transfer: confirmTransfer,
        delivery_action: action === "authorize" ? "copy" : action,
      };
      if (action === "authorize") {
        const draft = await createPackDraft(request);
        setConfirmationOpen(false); setConfirmedAskIds([]); setConfirmEvidence(false); setConfirmTransfer(false);
        onAuthorize(draft);
        return;
      }
      const result = await exportMemoryPack(request);
      // The server has consumed this delivery's confirmations. Clear them
      // before the browser-side clipboard or file operation can fail.
      setConfirmationOpen(false);
      setConfirmedAskIds([]);
      setConfirmEvidence(false);
      setConfirmTransfer(false);
      const selectedAskIds = packPreview.items
        .filter((item) => item.external_use === "ask" && item.selected)
        .map((item) => item.memory_id);
      if (selectedAskIds.length) {
        setSelectedMemoryIds((current) => (current ?? []).filter((id) => !selectedAskIds.includes(id)));
      }
      if (action === "copy") {
        if (!navigator.clipboard?.writeText) throw new Error("当前浏览器不支持剪贴板写入；可以选择下载文件。 ");
        await navigator.clipboard.writeText(result.rendered_content);
        setDeliveredSnapshot({ ...result, delivery_action: action });
        setNotice(`已复制 ${result.included_count} 条记忆（SHA-256 ${result.content_sha256.slice(0, 12)}…）。`);
      } else {
        const extension = result.format === "markdown" ? "md" : "json";
        const blob = new Blob([result.rendered_content], { type: result.format === "markdown" ? "text/markdown;charset=utf-8" : "application/json;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = `memory-pack.${extension}`;
        anchor.click();
        URL.revokeObjectURL(url);
        setDeliveredSnapshot({ ...result, delivery_action: action });
        setNotice(`已下载 ${result.included_count} 条记忆（SHA-256 ${result.content_sha256.slice(0, 12)}…）。`);
      }
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "Memory Pack 导出失败。";
      setPreviewError(message);
      if (message.includes("PACK_PREVIEW_STALE")) {
        setConfirmationOpen(false);
        setConfirmedAskIds([]);
        setConfirmEvidence(false);
        setConfirmTransfer(false);
        setSelectedMemoryIds(null);
        setPackPreview(null);
        setReloadKey((current) => current + 1);
      }
    } finally {
      setExportBusy(false);
    }
  }

  function openConfirmation(action: "copy" | "download" | "authorize") {
    if (!packPreview || packPreview.included_count === 0) return;
    setDeliveredSnapshot(null);
    setDeliveryAction(action);
    setConfirmedAskIds([]);
    setConfirmEvidence(false);
    setConfirmTransfer(false);
    setConfirmationOpen(true);
  }

  const isStale = Boolean(previewError?.includes("PACK_PREVIEW_STALE"));
  const hasScope = Boolean(selectedProjects?.length);

  return (
    <section className="workspace pack-workspace">
      <aside className="pack-config-panel">
        <section className="pack-control-section">
          <div className="pack-section-heading"><h2>项目空间</h2><span>仅选择的项目，不包含其子项目</span></div>
          {projects.length === 0 ? projectLoadError ? <div className="notice error" role="alert">{projectLoadError}<button className="secondary-button pack-retry" onClick={onRetryProjects}>重试读取项目空间</button></div> : <p className="subtle">正在读取可用项目空间…</p> : projects.map((project) => (
            <label className="pack-project-option" key={project.project_id}>
              <input type="checkbox" checked={selectedProjects?.includes(project.project_id) ?? false} onChange={(event) => toggleProject(project.project_id, event.target.checked)} />
              <span><strong>{project.name}</strong><small>{project.full_path} · {project.project_id}</small></span>
            </label>
          ))}
        </section>

        <section className="pack-control-section">
          <div className="pack-section-heading"><h2>记忆类型</h2><span>{selectedKinds.length}/3</span></div>
          <div className="pack-kind-grid">
            {packKindOptions.map((item) => <label key={item.value}>
              <input type="checkbox" checked={selectedKinds.includes(item.value)} onChange={(event) => toggleKind(item.value, event.target.checked)} />
              <span>{item.label}</span>
            </label>)}
          </div>
        </section>

        <section className="pack-control-section pack-settings-grid">
          <label>格式<select value={format} onChange={(event) => { setDeliveredSnapshot(null); setFormat(event.target.value as PackFormat); }}><option value="markdown">Markdown</option><option value="vault-memory/1">vault-memory/1 JSON</option></select></label>
          <label>证据<select disabled={format === "markdown"} value={evidenceMode} onChange={(event) => { setDeliveredSnapshot(null); setEvidenceMode(event.target.value as PackEvidenceMode); }}><option value="id">仅证据 ID</option><option value="none">不包含证据</option><option value="snippet">短摘录</option><option value="full">匹配摘录</option></select></label>
          <label className="pack-budget-field">近似 token 预算<input type="number" min={100} max={32000} step={100} value={tokenBudget} onChange={(event) => { setDeliveredSnapshot(null); setTokenBudget(Math.max(100, Math.min(32000, Number(event.target.value) || 100))); }} /><small>估算值会包含正文与所选证据；超出时整条排除。</small></label>
          <p className="pack-evidence-note">{format === "markdown" ? "Markdown 仅输出增强后的正文，不附带记忆 ID、策略、时间或来源证据字段。证据选项仅适用于 JSON。" : evidenceMode === "none" ? "不在 Pack 中输出来源引用。" : evidenceMode === "id" ? "默认仅输出来源证据 ID，不包含引用文本。" : "Pack 会包含与记忆匹配的来源摘录；即使选择“匹配摘录”，也不会包含完整原始会话，交付前需要再次确认。"}</p>
        </section>

        <section className="pack-control-section">
          <div className="pack-section-heading"><h2>可选记忆</h2><span>{packPreview ? `${packPreview.included_count} 条将包含` : "等待预览"}</span></div>
          {!hasScope ? <div className="pack-empty">选择至少一个项目空间以读取 Pack。</div> : null}
          {hasScope && selectedKinds.length === 0 ? <div className="pack-empty">至少选择一种记忆类型。</div> : null}
          {previewLoading ? <div className="pack-empty">正在读取已确认的 EverOS 正式记忆…</div> : null}
          {previewError ? <div className="notice error" role="alert">{packErrorLabel(previewError)}<button className="secondary-button pack-retry" onClick={() => setReloadKey((current) => current + 1)}>重试预览</button></div> : null}
          {!previewLoading && !previewError && packPreview && packPreview.items.length === 0 ? <div className="pack-empty">所选项目和类型中没有可供选择的 allow/ask 记忆。已拒绝、未确认和不在索引中的记录不会进入 Pack。</div> : null}
          {allowItems.length ? <div className="pack-policy-group"><h3>允许外发 · 默认选中</h3>{allowItems.map((item) => <PackItemRow key={item.memory_id} item={item} checked={item.selected} onToggle={toggleMemory} />)}</div> : null}
          {askItems.length ? <div className="pack-policy-group"><h3>导出时确认 · 逐条选择</h3>{askItems.map((item) => <PackItemRow key={item.memory_id} item={item} checked={item.selected} onToggle={toggleMemory} />)}</div> : null}
          {packPreview?.exclusion_counts.policy_deny ? <div className="pack-locked-note">不可外发：{packPreview.exclusion_counts.policy_deny} 条已锁定并排除；正文不会返回到页面。</div> : null}
          {packPreview && Object.entries(packPreview.exclusion_counts).some(([reason]) => reason !== "policy_deny") ? <div className="pack-exclusion-summary"><strong>排除项</strong>{Object.entries(packPreview.exclusion_counts).filter(([reason]) => reason !== "policy_deny").map(([reason, count]) => <span key={reason}>{exclusionLabels[reason] ?? reason}：{count}</span>)}</div> : null}
        </section>
      </aside>

      <div className="pack-preview-panel">
        <div className="pack-preview-heading">
          <div><h2>Pack 预览</h2><p>{deliveredSnapshot ? `最近${deliveredSnapshot.delivery_action === "copy" ? "复制" : "下载"}的 ${deliveredSnapshot.included_count} 条内容快照 · 约 ${deliveredSnapshot.estimated_tokens} tokens` : packPreview ? `${packPreview.included_count} 条 · 约 ${packPreview.estimated_tokens} tokens · ${packPreview.estimate_is_approximate ? "估算值" : ""}` : "预览只使用服务端返回的 EverOS 正式记忆。"}</p></div>
          <button className="secondary-button" disabled={!hasScope || selectedKinds.length === 0 || previewLoading} onClick={() => { setDeliveredSnapshot(null); setReloadKey((current) => current + 1); }}>刷新预览</button>
        </div>
        {notice ? <div className="notice success" role="status">{notice}</div> : null}
        {deliveredSnapshot ? <div className="notice success" role="status">以下保留本次已交付的 Pack 内容；修改项目、类型、记忆选择或生成设置后会显示当前预览。</div> : null}
        {isStale ? <div className="notice warning" role="alert">{packErrorLabel(previewError ?? "PACK_PREVIEW_STALE")}</div> : null}
        {packPreview || deliveredSnapshot ? <>
          <div className="pack-stats"><div><strong>{deliveredSnapshot?.included_count ?? packPreview?.included_count ?? 0}</strong><span>{deliveredSnapshot ? "上次交付条数" : "包含记忆"}</span></div><div><strong>≈ {deliveredSnapshot?.estimated_tokens ?? packPreview?.estimated_tokens ?? 0}</strong><span>近似 tokens</span></div><div><strong>{deliveredSnapshot ? "已交付" : packPreview?.exclusion_counts.over_budget ?? 0}</strong><span>{deliveredSnapshot ? "快照状态" : "预算排除"}</span></div></div>
          <pre className="pack-rendered-preview" aria-label={deliveredSnapshot ? "最近交付的 Memory Pack 内容快照" : "服务端生成的 Memory Pack 预览"}>{deliveredSnapshot?.rendered_content ?? packPreview?.rendered_content ?? ""}</pre>
          <div className="pack-hash-line"><span>内容 SHA-256</span><code>{deliveredSnapshot?.content_sha256 ?? packPreview?.content_sha256 ?? ""}</code></div>
          <div className="actions pack-delivery-actions">
            <button className="primary-button" disabled={!packPreview || packPreview.included_count === 0 || previewLoading || exportBusy} onClick={() => openConfirmation("copy")}>复制</button>
            <button className="secondary-button" disabled={!packPreview || packPreview.included_count === 0 || previewLoading || exportBusy} onClick={() => openConfirmation("download")}>下载</button>
            <button className="secondary-button" disabled={!packPreview || !packPreview.included_count || previewLoading || exportBusy || !["none", "id"].includes(evidenceMode)} title={authText.noEvidenceExport} onClick={() => openConfirmation("authorize")}>{authText.authorizePack}</button>
          </div>
        </> : <div className="pack-preview-empty">{previewLoading ? "正在生成预览…" : previewError ? "预览读取失败；可重试。" : hasScope ? "当前没有可展示的 Pack 预览。" : "先选择项目空间和类型。"}</div>}
      </div>

      {confirmationOpen && packPreview ? <div className="pack-confirm-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) setConfirmationOpen(false); }}>
        <section className="pack-confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="pack-confirm-title">
          <div className="pack-section-heading"><div><h2 id="pack-confirm-title">确认{deliveryAction === "authorize" ? authText.authorizePack : deliveryAction === "copy" ? "复制" : "下载"}</h2><span>即将交付 {packPreview.included_count} 条记忆 · SHA-256 {packPreview.content_sha256.slice(0, 16)}…</span></div><button className="secondary-button" onClick={() => setConfirmationOpen(false)}>关闭</button></div>
          <div className="pack-confirm-list"><strong>本次实际包含的条目</strong>{packPreview.items.filter((item) => item.included).map((item) => <p key={item.memory_id}>{memoryKindLabel(item.kind)} · 修订 {item.revision} · <code>{item.memory_id}</code></p>)}</div>
          {includedAskItems.length ? <fieldset className="pack-confirm-list"><legend>本次包含的 ask 记忆，请逐条确认</legend>{includedAskItems.map((item) => <label key={item.memory_id}><input type="checkbox" checked={confirmedAskIds.includes(item.memory_id)} onChange={(event) => setConfirmedAskIds((current) => event.target.checked ? [...current, item.memory_id] : current.filter((id) => id !== item.memory_id))} /><span>{memoryKindLabel(item.kind)} · <code>{item.memory_id}</code></span></label>)}</fieldset> : null}
          {format === "vault-memory/1" && (evidenceMode === "snippet" || evidenceMode === "full") ? <label className="pack-confirm-check"><input type="checkbox" checked={confirmEvidence} onChange={(event) => setConfirmEvidence(event.target.checked)} /><span>我确认本次 Pack 会包含与记忆匹配的来源摘录。</span></label> : null}
          <label className="pack-confirm-check"><input type="checkbox" checked={confirmTransfer} onChange={(event) => setConfirmTransfer(event.target.checked)} /><span>我了解交付后的内容无法从接收方处撤回。</span></label>
          <div className="actions right"><button className="secondary-button" disabled={exportBusy} onClick={() => setConfirmationOpen(false)}>取消</button><button className="primary-button" disabled={exportBusy || !confirmTransfer || format === "vault-memory/1" && (evidenceMode === "snippet" || evidenceMode === "full") && !confirmEvidence || includedAskItems.some((item) => !confirmedAskIds.includes(item.memory_id))} onClick={() => void performDelivery(deliveryAction)}>{exportBusy ? "正在校验并生成…" : deliveryAction === "authorize" ? authText.authorizePack : deliveryAction === "copy" ? "确认并复制" : "确认并下载"}</button></div>
        </section>
      </div> : null}
    </section>
  );
}

function PackItemRow({ item, checked, onToggle }: { item: PackPreviewItem; checked: boolean; onToggle: (memoryId: string, checked: boolean) => void }) {
  return <article className={`pack-item-row ${item.eligible ? "" : "excluded"}`}>
    <label className="pack-item-check"><input type="checkbox" checked={checked} disabled={!item.eligible} onChange={(event) => onToggle(item.memory_id, event.target.checked)} /><span><strong>{memoryKindLabel(item.kind)}</strong><small>{item.memory_id} · 修订 {item.revision}</small></span></label>
    <span className={`pack-policy-chip ${item.external_use}`}>{item.external_use === "allow" ? "允许" : "需确认"}</span>
    {item.display_content ? <p>{item.display_content}</p> : <small className="pack-item-exclusion">{exclusionLabels[item.exclusion_reason ?? ""] ?? "当前不可纳入"}</small>}
    {item.included ? <small className="pack-item-included">已纳入预览</small> : item.exclusion_reason === "over_budget" ? <small className="pack-item-exclusion">预算不足，整条排除</small> : null}
  </article>;
}

export default function App() {
  const [pageId, setPageId] = useState<PageId>("imports");
  const [exportTab, setExportTab] = useState<'memory' | 'knowledge'>('memory');
  const [authorizationDraft, setAuthorizationDraft] = useState<FixedDraft | null>(null);
  const [health, setHealth] = useState<VaultHealth | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<ReviewCandidate[]>([]);
  const [candidateStatusFilter, setCandidateStatusFilter] = useState("pending");
  const [candidateKindFilter, setCandidateKindFilter] = useState("");
  const [candidateOriginFilter, setCandidateOriginFilter] = useState("");
  const [candidateEvidenceFilter, setCandidateEvidenceFilter] = useState("");
  const [checkedCandidates, setCheckedCandidates] = useState<string[]>([]);
  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(null);
  const [sourceConversation, setSourceConversation] = useState<SourceConversation | null>(null);
  const [sourceOpen, setSourceOpen] = useState(false);
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const [showFullConversation, setShowFullConversation] = useState(false);
  const [candidateDrafts, setCandidateDrafts] = useState<Record<string, CandidateChanges>>({});
  const [candidateBusy, setCandidateBusy] = useState<string | null>(null);
  const [candidateError, setCandidateError] = useState<string | null>(null);
  const [candidateNotice, setCandidateNotice] = useState<string | null>(null);
  const [showManualCandidateForm, setShowManualCandidateForm] = useState(false);
  const [manualCandidateDraft, setManualCandidateDraft] = useState<ManualCandidateInput>({
    kind: "episode", namespace: "personal", validity_days: null,
    external_use: "ask", content: "",
  });
  const [manualCandidateBusy, setManualCandidateBusy] = useState(false);
  const [manualCandidateError, setManualCandidateError] = useState<string | null>(null);
  const [step, setStep] = useState(0);
  const [source, setSource] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [batch, setBatch] = useState<ImportBatch | null>(null);
  const [extractState, setExtractState] = useState<ExtractState>("idle");
  const [extractResult, setExtractResult] = useState<ExtractionResult | null>(null);
  const [projects, setProjects] = useState<ProjectSpace[]>([]);
  const [projectLoadError, setProjectLoadError] = useState<string | null>(null);
  const [projectId, setProjectId] = useState("");
  const [libraryProjectId, setLibraryProjectId] = useState("all");
  const [librarySearchInput, setLibrarySearchInput] = useState("");
  const [libraryQuery, setLibraryQuery] = useState("");
  const [libraryStatus, setLibraryStatus] = useState<LibraryStatus>("valid");
  const [libraryMemories, setLibraryMemories] = useState<LibraryMemory[]>([]);
  const [libraryMissingIds, setLibraryMissingIds] = useState<string[]>([]);
  const [libraryCounts, setLibraryCounts] = useState<LibraryProjectCount[]>([]);
  const [libraryLoading, setLibraryLoading] = useState(false);
  const [librarySummaryLoading, setLibrarySummaryLoading] = useState(false);
  const [libraryError, setLibraryError] = useState<string | null>(null);
  const [librarySummaryError, setLibrarySummaryError] = useState<string | null>(null);
  const [librarySelected, setLibrarySelected] = useState<LibraryMemory | null>(null);
  const [libraryDetail, setLibraryDetail] = useState<LibraryMemoryDetail | null>(null);
  const [libraryDetailLoading, setLibraryDetailLoading] = useState(false);
  const [libraryDetailError, setLibraryDetailError] = useState<string | null>(null);
  const [libraryReload, setLibraryReload] = useState(0);
  const [showCreateProject, setShowCreateProject] = useState(false);
  const [newProjectName, setNewProjectName] = useState("");
  const [newProjectParentId, setNewProjectParentId] = useState("personal");
  const [newProjectDescription, setNewProjectDescription] = useState("");
  const [projectCreateBusy, setProjectCreateBusy] = useState(false);
  const [projectCreateError, setProjectCreateError] = useState<string | null>(null);
  const [memoryActionBusy, setMemoryActionBusy] = useState(false);
  const [memoryActionError, setMemoryActionError] = useState<string | null>(null);
  const [showMemoryEditor, setShowMemoryEditor] = useState(false);
  const [memoryDraftContent, setMemoryDraftContent] = useState("");
  const [memoryDraftExternalUse, setMemoryDraftExternalUse] = useState<LibraryMemory["external_use"]>("ask");
  const [memoryDraftNote, setMemoryDraftNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const abortController = useRef<AbortController | null>(null);
  const libraryDetailSequence = useRef(0);
  const selectedSourceMessage = useRef<HTMLElement | null>(null);
  const page = pages.find((item) => item.id === pageId) ?? pages[0];

  useEffect(() => {
    if (!sourceOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSourceOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [sourceOpen]);

  useEffect(() => {
    if (!showManualCandidateForm) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !manualCandidateBusy) setShowManualCandidateForm(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [showManualCandidateForm, manualCandidateBusy]);

  useEffect(() => {
    if (!sourceOpen || !showFullConversation || !sourceConversation) return;
    const frame = window.requestAnimationFrame(() => {
      selectedSourceMessage.current?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [sourceOpen, showFullConversation, sourceConversation]);

  useEffect(() => {
    let active = true;
    const refreshHealth = () => getHealth()
      .then((result) => { if (active) { setHealth(result); setHealthError(null); } })
      .catch((reason: unknown) => {
        if (active) setHealthError(reason instanceof Error ? reason.message : "无法连接 Vault API");
      });
    void refreshHealth();
    const timer = window.setInterval(() => void refreshHealth(), 15000);
    getProjects()
      .then((items) => {
        setProjects(items);
        setProjectLoadError(null);
        setProjectId((current) => current || items.find((item) => item.project_id === "personal")?.project_id || items[0]?.project_id || "");
      })
      .catch((reason: unknown) => {
        const message = reason instanceof Error ? reason.message : "无法读取项目空间";
        setProjectLoadError(message);
        setHealthError(message);
      });
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  useEffect(() => {
    if (pageId !== "review") return;
    let active = true;
    setCandidateError(null);
    getCandidates({ status: candidateStatusFilter, kind: candidateKindFilter || undefined, origin: candidateOriginFilter || undefined, evidence: candidateEvidenceFilter || undefined })
      .then((items) => {
        if (!active) return;
        setCandidates(items);
        setCheckedCandidates((current) => current.filter((id) => items.some((item) => item.id === id && item.status === "pending" && canConfirmCandidate(item))));
        setSelectedCandidateId((current) => items.some((item) => item.id === current) ? current : items[0]?.id ?? null);
      })
      .catch((reason: unknown) => {
        if (active) setCandidateError(reason instanceof Error ? reason.message : "无法读取候选内容。");
      });
    return () => { active = false; };
  }, [pageId, candidateStatusFilter, candidateKindFilter, candidateOriginFilter, candidateEvidenceFilter]);

  useEffect(() => {
    const timer = window.setTimeout(() => setLibraryQuery(librarySearchInput), 250);
    return () => window.clearTimeout(timer);
  }, [librarySearchInput]);

  useEffect(() => {
    if (pageId !== "library") return;
    let active = true;
    setLibrarySummaryLoading(true);
    setLibrarySummaryError(null);
    getMemoryLibrarySummary()
      .then((items) => { if (active) setLibraryCounts(items); })
      .catch((reason: unknown) => {
        if (active) setLibrarySummaryError(reason instanceof Error ? reason.message : "无法读取项目记忆数量。");
      })
      .finally(() => { if (active) setLibrarySummaryLoading(false); });
    return () => { active = false; };
  }, [pageId, libraryReload]);

  useEffect(() => {
    if (pageId !== "library") return;
    let active = true;
    const selectedProjects = libraryProjectId === "all"
      ? projects
      : projects.filter((item) => item.project_id === libraryProjectId);
    setLibraryLoading(true);
    setLibraryError(null);
    if (!selectedProjects.length) {
      setLibraryMemories([]);
      setLibraryMissingIds([]);
      setLibraryLoading(false);
      return () => { active = false; };
    }
    Promise.all(selectedProjects.map((item) =>
      getLibraryMemories(item.project_id, libraryQuery, libraryStatus),
    ))
      .then((results) => {
        if (!active) return;
        const memories = results.flatMap((result) => result.memories)
          .sort((left, right) => right.timestamp.localeCompare(left.timestamp));
        setLibraryMemories(memories);
        setLibraryMissingIds(results.flatMap((result) => result.missing_index_ids));
      })
      .catch((reason: unknown) => {
        if (active) setLibraryError(reason instanceof Error ? reason.message : "无法读取记忆库。");
      })
      .finally(() => { if (active) setLibraryLoading(false); });
    return () => { active = false; };
  }, [pageId, libraryProjectId, libraryQuery, libraryStatus, projects, libraryReload]);

  useEffect(() => {
    if (!librarySelected) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeLibraryDetail();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [librarySelected]);

  async function refreshCandidates() {
    const items = await getCandidates({ status: candidateStatusFilter, kind: candidateKindFilter || undefined, origin: candidateOriginFilter || undefined, evidence: candidateEvidenceFilter || undefined });
    setCandidates(items);
    setCheckedCandidates((current) => current.filter((id) => items.some((item) => item.id === id && item.status === "pending" && canConfirmCandidate(item))));
    setSelectedCandidateId((current) => items.some((item) => item.id === current) ? current : items[0]?.id ?? null);
  }

  function openManualCandidateForm() {
    setManualCandidateDraft({
      kind: "episode",
      namespace: selectedCandidate?.namespace ?? projectId ?? "personal",
      validity_days: null,
      external_use: "ask",
      content: "",
    });
    setManualCandidateError(null);
    setShowManualCandidateForm(true);
  }

  async function saveManualCandidate() {
    if (manualCandidateBusy || !manualCandidateDraft.content.trim()) return;
    setManualCandidateBusy(true);
    setManualCandidateError(null);
    setCandidateError(null);
    try {
      const candidate = await createManualCandidate({
        ...manualCandidateDraft,
        content: manualCandidateDraft.content.trim(),
      });
      setCandidateStatusFilter("pending");
      setCandidateKindFilter("");
      setCandidateOriginFilter("");
      setCandidateEvidenceFilter("");
      setCandidates((current) => [candidate, ...current.filter((item) => item.id !== candidate.id)]);
      setSelectedCandidateId(candidate.id);
      setCandidateDrafts((current) => ({
        ...current,
        [candidate.id]: {
          content: candidate.content,
          kind: candidate.kind,
          namespace: candidate.namespace,
          validity_days: candidate.validity_days,
          external_use: candidate.external_use as CandidateChanges["external_use"],
        },
      }));
      setCandidateNotice("人工新增候选已加入待审核列表；确认后才会写入正式记忆。");
      setShowManualCandidateForm(false);
    } catch (reason) {
      setManualCandidateError(reason instanceof Error ? reason.message : "新增候选失败，请稍后重试。");
    } finally {
      setManualCandidateBusy(false);
    }
  }

  function candidateChanges(candidate: ReviewCandidate): CandidateChanges {
    return candidateDrafts[candidate.id] ?? {
      content: candidate.content, kind: candidate.kind, namespace: candidate.namespace,
      validity_days: candidate.validity_days, external_use: candidate.external_use as CandidateChanges["external_use"],
    };
  }

  const eligibleCandidates = candidates.filter((candidate) => {
    const kind = candidateChanges(candidate).kind;
    return candidate.status === "pending"
      && canConfirmCandidate(candidate)
      && (kind === "episode" || kind === "preference");
  });
  const selectedCandidate = candidates.find((candidate) => candidate.id === selectedCandidateId) ?? null;
  const projectName = (id: string) => projects.find((item) => item.project_id === id)?.name ?? id;
  const allVisibleValid = libraryCounts.reduce((sum, item) => sum + item.visible_valid_count, 0);
  const allSyncing = libraryCounts.reduce((sum, item) => sum + item.syncing_count, 0);

  function retryProjects() {
    setProjectLoadError(null);
    void getProjects()
      .then((items) => {
        setProjects(items);
        setProjectId((current) => current || items.find((item) => item.project_id === "personal")?.project_id || items[0]?.project_id || "");
      })
      .catch((reason: unknown) => setProjectLoadError(reason instanceof Error ? reason.message : "无法读取项目空间"));
  }

  async function openSource(candidateId: string, messageId: string, candidateContent: string) {
    if (!sourceOpen) setShowFullConversation(false);
    setSourceOpen(true);
    setSourceConversation(null);
    setSourceError(null);
    setSourceLoading(true);
    try {
      setSourceConversation(await getCandidateSource(candidateId, messageId, candidateContent));
    } catch (reason) {
      setSourceError(reason instanceof Error ? reason.message : "无法读取原对话。");
    } finally {
      setSourceLoading(false);
    }
  }

  async function openLibraryDetail(memory: LibraryMemory) {
    const sequence = ++libraryDetailSequence.current;
    setLibrarySelected(memory);
    setLibraryDetail(null);
    setLibraryDetailError(null);
    setLibraryDetailLoading(true);
    try {
      const detail = await getLibraryMemory(memory.project_id, memory.id);
      if (sequence === libraryDetailSequence.current) setLibraryDetail(detail);
    } catch (reason) {
      if (sequence === libraryDetailSequence.current) {
        setLibraryDetailError(reason instanceof Error ? reason.message : "无法读取记忆详情。");
      }
    } finally {
      if (sequence === libraryDetailSequence.current) setLibraryDetailLoading(false);
    }
  }

  function closeLibraryDetail() {
    libraryDetailSequence.current += 1;
    setLibrarySelected(null);
    setLibraryDetail(null);
    setLibraryDetailError(null);
    setLibraryDetailLoading(false);
  }

  function refreshLibrary() {
    setLibraryReload((current) => current + 1);
  }

  async function confirmSelected() {
    if (candidateBusy || checkedCandidates.length === 0) return;
    setCandidateBusy("bulk");
    setCandidateError(null);
    setCandidateNotice(null);
    let confirmed = 0;
    const completedIds: string[] = [];
    try {
      for (const candidate of eligibleCandidates.filter((item) => checkedCandidates.includes(item.id))) {
        await confirmCandidate(candidate, candidateChanges(candidate));
        confirmed += 1;
        completedIds.push(candidate.id);
      }
      setCheckedCandidates([]);
      setCandidateNotice(`已确认 ${confirmed} 条候选并写入 EverOS 正式记忆。`);
    } catch (reason) {
      setCandidateError(`已确认 ${confirmed} 条；剩余候选未完成：${reason instanceof Error ? reason.message : "审核失败"}`);
      setCheckedCandidates((current) => current.filter((id) => !completedIds.includes(id)));
    } finally {
      setCandidateBusy(null);
      await refreshCandidates();
    }
  }

  async function reviewCandidate(candidate: ReviewCandidate, action: "confirm" | "reject") {
    if (candidateBusy) return;
    setCandidateBusy(candidate.id);
    setCandidateError(null);
    setCandidateNotice(null);
    try {
      if (action === "confirm") {
        const draft = candidateChanges(candidate);
        await confirmCandidate(candidate, draft);
        setCandidateNotice("候选已确认并写入 EverOS 正式记忆。");
      } else {
        await rejectCandidate(candidate);
        setCandidateNotice("候选已拒绝，未写入正式记忆。");
      }
      await refreshCandidates();
    } catch (reason) {
      setCandidateError(reason instanceof Error ? reason.message : "审核操作失败。");
    } finally {
      setCandidateBusy(null);
    }
  }

  function clearWorkflow() {
    if (preview) void cancelPreview(preview.preview_id).catch(() => undefined);
    abortController.current?.abort();
    abortController.current = null;
    setStep(0);
    setSource(null);
    setFile(null);
    setPreview(null);
    setBatch(null);
    setExtractState("idle");
    setExtractResult(null);
    setShowCreateProject(false);
    setNewProjectName("");
    setNewProjectDescription("");
    setError(null);
    setBusy(false);
  }

  async function inspectFile(selected: File | undefined) {
    if (!selected || busy) return;
    setFile(selected);
    setPreview(null);
    setError(null);
    setBusy(true);
    const controller = new AbortController();
    abortController.current = controller;
    try {
      const result = await previewImport(selected, source ?? "generic", projectId, controller.signal);
      setPreview(result);
      setStep(2);
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") {
        setError("已取消处理，未归档任何数据。");
      } else {
        const message = reason instanceof Error ? reason.message : "文件检查或解析失败。";
        setError(`${message} 未产生任何写入。`);
      }
    } finally {
      setBusy(false);
      abortController.current = null;
    }
  }

  async function saveArchive() {
    if (!preview || preview.duplicate_batch_id || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await commitImport(preview.preview_id, projectId);
      setBatch(result);
      setStep(3);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "归档失败。");
    } finally {
      setBusy(false);
    }
  }

  async function addProject() {
    const cleanName = newProjectName.trim();
    if (!cleanName || projectCreateBusy) return;
    setProjectCreateBusy(true);
    setProjectCreateError(null);
    try {
      const created = await createProject(
        cleanName,
        newProjectParentId || null,
        newProjectDescription.trim(),
      );
      setProjects(await getProjects());
      setProjectId(created.project_id);
      setLibraryProjectId(created.project_id);
      setNewProjectName("");
      setNewProjectDescription("");
      setNewProjectParentId("personal");
      setShowCreateProject(false);
      refreshLibrary();
    } catch (reason) {
      setProjectCreateError(reason instanceof Error ? reason.message : "创建项目空间失败。");
    } finally {
      setProjectCreateBusy(false);
    }
  }

  function openProjectCreate() {
    const parent = libraryProjectId !== "all" ? libraryProjectId : pageId === "imports" ? projectId : "personal";
    setNewProjectParentId(parent || "personal");
    setProjectCreateError(null);
    setShowCreateProject(true);
  }

  function projectPathPreview() {
    const parent = projects.find((item) => item.project_id === newProjectParentId);
    const name = newProjectName.trim() || "新项目";
    return parent ? `${parent.full_path}/${name}` : `/${name}`;
  }

  async function saveMemoryCorrection() {
    if (!libraryDetail || memoryActionBusy) return;
    setMemoryActionBusy(true);
    setMemoryActionError(null);
    try {
      const detail = await correctLibraryMemory(
        libraryDetail.project_id,
        libraryDetail,
        memoryDraftContent,
        memoryDraftExternalUse,
        memoryDraftNote,
      );
      setLibraryDetail(detail);
      setLibrarySelected(detail);
      setLibraryMemories((current) => current.map((memory) => (
        memory.id === detail.id && memory.project_id === detail.project_id
          ? { ...memory, ...detail }
          : memory
      )));
      setShowMemoryEditor(false);
      setMemoryDraftNote("");
      refreshLibrary();
    } catch (reason) {
      setMemoryActionError(reason instanceof Error ? reason.message : "修正记忆失败。");
    } finally {
      setMemoryActionBusy(false);
    }
  }

  async function withdrawSelectedMemory() {
    if (!libraryDetail || memoryActionBusy) return;
    if (!window.confirm("撤销后会立即从 EverOS 检索中排除，并在记忆库中保留只读记录。继续吗？")) return;
    setMemoryActionBusy(true);
    setMemoryActionError(null);
    try {
      const detail = await withdrawLibraryMemory(libraryDetail.project_id, libraryDetail);
      setLibraryDetail(detail);
      setLibrarySelected(detail);
      refreshLibrary();
    } catch (reason) {
      setMemoryActionError(reason instanceof Error ? reason.message : "撤销记忆失败。");
    } finally {
      setMemoryActionBusy(false);
    }
  }

  async function deleteSelectedMemory() {
    if (!libraryDetail || memoryActionBusy) return;
    if (!window.confirm("删除后正文和本条来源链接不可恢复；共享导入原文件仍会保留。确定删除吗？")) return;
    setMemoryActionBusy(true);
    setMemoryActionError(null);
    try {
      await deleteLibraryMemory(libraryDetail.project_id, libraryDetail);
      closeLibraryDetail();
      refreshLibrary();
    } catch (reason) {
      setMemoryActionError(reason instanceof Error ? reason.message : "删除记忆失败。");
    } finally {
      setMemoryActionBusy(false);
    }
  }

  async function runExtraction() {
    if (!batch || busy) return;
    setBusy(true);
    setError(null);
    setExtractState("running");
    try {
      const result = await extractImport(batch.batch_id);
      setExtractResult(result);
      setExtractState("done");
      setStep(4);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "本地抽取失败，原文已保留。可重试。 ");
      setExtractState("failed");
    } finally {
      setBusy(false);
    }
  }

  function deferExtraction() {
    if (extractState === "idle") setExtractState("deferred");
    setStep(4);
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    void inspectFile(event.dataTransfer.files[0]);
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          MEMO Vault
          <span>本地记忆保险箱 · MVP</span>
        </div>
        <nav aria-label="主要功能">
          {pages.map((item) => (
            <button
              aria-current={pageId === item.id ? "page" : undefined}
              className={pageId === item.id ? "nav-item active" : "nav-item"}
              key={item.id}
              onClick={() => setPageId(item.id)}
            >
              {item.title}
            </button>
          ))}
        </nav>
        <div className="local-note">仅本机运行 · 文件不会发送到云端</div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div>
            <h1>{page.title}</h1>
            <p>{page.description}</p>
          </div>
          <div className="status-group">
            <span className="status-chip muted-chip">Vault：{health?.app_id ?? "读取中"}</span>
            <span className="status-chip">Vault API：{health ? "在线" : healthError ? "未连接" : "连接中"}</span>
            <span className={`status-chip ${health?.everos_status === "online" ? "" : "offline-chip"}`}>EverOS：{health?.everos_status === "online" ? "在线" : health ? "离线" : "检测中"}</span>
          </div>
        </header>

        {pageId === "adp" ? (
          <AdpExport />
        ) : pageId === "authorization" ? (
          <AuthorizationMcpPage projects={projects} draft={authorizationDraft} onOpenPack={() => setPageId("pack")} onDraftUsed={() => setAuthorizationDraft(null)} />
        ) : pageId === "pack" ? (
          <div className="export-center"><nav className="export-tabs" aria-label="导出类型"><button className={exportTab === 'memory' ? 'selected' : ''} onClick={() => setExportTab('memory')}>Memory Pack</button><button className={exportTab === 'knowledge' ? 'selected' : ''} onClick={() => setExportTab('knowledge')}>Knowledge 文档</button></nav>
            {exportTab === 'memory' ? <MemoryPackPage projects={projects} projectLoadError={projectLoadError} onRetryProjects={retryProjects} onAuthorize={draft => { setAuthorizationDraft(draft); setPageId("authorization"); }} /> : <KnowledgeExport projects={projects} />}
          </div>
        ) : pageId === "library" ? (
          <section className="workspace library-workspace">
            <aside className="library-project-sidebar" aria-label="记忆库项目空间">
              <div className="library-sidebar-heading"><span>项目空间</span><button className="project-tree-create" onClick={openProjectCreate}>新建</button></div>
              <button className={`library-project-button ${libraryProjectId === "all" ? "selected" : ""}`} onClick={() => { setLibraryProjectId("all"); closeLibraryDetail(); }}>
                <span>全部记忆</span><strong>{librarySummaryLoading ? "…" : allVisibleValid}</strong>
                {allSyncing > 0 ? <small>{allSyncing} 条同步中</small> : null}
              </button>
              {projects.map((project) => {
                const count = libraryCounts.find((item) => item.project_id === project.project_id);
                return <button className={`library-project-button ${libraryProjectId === project.project_id ? "selected" : ""}`} key={project.project_id} onClick={() => { setLibraryProjectId(project.project_id); closeLibraryDetail(); }}>
                  <span title={`${project.full_path}${project.description ? `\n${project.description}` : ""}`} style={{ paddingLeft: `${Math.min(project.depth, 6) * 14}px` }}>{project.name}</span><strong>{librarySummaryLoading ? "…" : count?.visible_valid_count ?? 0}</strong>
                  {count?.syncing_count ? <small>{count.syncing_count} 条同步中</small> : null}
                </button>;
              })}
            </aside>
            <div className="library-content">
              <div className="library-toolbar">
                <label className="library-search"><span className="visually-hidden">搜索记忆内容</span><input value={librarySearchInput} onChange={(event) => setLibrarySearchInput(event.target.value)} placeholder="搜索记忆内容…" /></label>
                <label className="library-filter"><span>有效期</span><select value={libraryStatus} onChange={(event) => setLibraryStatus(event.target.value as LibraryStatus)}><option value="valid">未过期</option><option value="expired">已过期</option><option value="all">全部</option></select></label>
                <button className="secondary-button" onClick={refreshLibrary} disabled={libraryLoading || librarySummaryLoading}>刷新</button>
              </div>
              <div className="library-results-heading"><strong>{libraryProjectId === "all" ? "全部记忆" : projectName(libraryProjectId)}</strong><span>{libraryLoading ? "正在从 EverOS 同步读取…" : `${libraryMemories.length} 条正式记忆`}</span></div>
              {health?.everos_status === "offline" ? <div className="notice warning">EverOS 当前离线，无法读取正式记忆正文。重新连接后可刷新。</div> : null}
              {librarySummaryError ? <div className="notice error" role="alert">项目统计读取失败：{librarySummaryError}</div> : null}
              {libraryError ? <div className="notice error" role="alert">记忆库读取失败：{libraryError}<button className="secondary-button library-retry" onClick={refreshLibrary}>重试</button></div> : null}
              {libraryMissingIds.length > 0 ? <div className="notice warning library-sync-notice">有 {libraryMissingIds.length} 条已确认记忆尚未出现在 EverOS 索引中。不会显示候选文本；可以稍后刷新。</div> : null}
              {libraryLoading ? <div className="library-empty">正在读取当前项目中已确认的 EverOS 记忆…</div> : null}
              {!libraryLoading && !libraryError && libraryMemories.length === 0 ? (
                <div className="library-empty">
                  {libraryMissingIds.length > 0 ? <><strong>正式记忆正在同步</strong><span>EverOS 已收到确认写入，但索引尚未可读。请稍后重试。</span><button className="secondary-button" onClick={refreshLibrary}>重新读取</button></> : libraryQuery.trim() ? <><strong>没有匹配的记忆</strong><span>当前项目中没有包含“{libraryQuery.trim()}”的正式记忆。</span></> : <><strong>{libraryStatus === "expired" ? "没有已过期的记忆" : "这里还没有正式记忆"}</strong><span>通过“候选审核”确认记忆后，它会出现在对应项目空间。</span></>}
                </div>
              ) : null}
              {!libraryLoading && libraryMemories.length > 0 ? <div className="library-list" aria-label="正式记忆列表">
                {libraryMemories.map((memory) => <button className="library-row" key={`${memory.project_id}:${memory.id}`} onClick={() => void openLibraryDetail(memory)}>
                  <div className="library-row-heading"><span className={`library-kind kind-${memory.kind}`}>{memoryKindLabel(memory.kind)}</span><span className={`library-validity ${memory.lifecycle_status}`}>{memory.lifecycle_status === "withdrawn" ? "已撤销" : memory.lifecycle_status === "deleted" ? "已删除" : memory.status === "valid" ? "有效" : "已过期"}</span></div>
                  <p className="library-preview">{memory.content_preview}</p>
                  <div className="library-row-meta"><span>{projectName(memory.project_id)}</span><span>{externalUseLabel(memory.external_use)}</span><span>修订 {memory.revision}</span><time>{new Date(memory.timestamp).toLocaleDateString()}</time></div>
                </button>)}
              </div> : null}
            </div>
            {librarySelected ? <div className="library-detail-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) closeLibraryDetail(); }}>
              <aside className="library-drawer" role="dialog" aria-modal="true" aria-label="记忆详情">
                <div className="library-drawer-header"><div><strong>记忆详情</strong><span>{projectName(librarySelected.project_id)}</span></div><button className="secondary-button" onClick={closeLibraryDetail}>关闭</button></div>
                {libraryDetailLoading ? <div className="library-drawer-state">正在读取 EverOS 正式记忆…</div> : null}
                {libraryDetailError ? <div className="notice error" role="alert">{libraryDetailError}<button className="secondary-button library-retry" onClick={() => void openLibraryDetail(librarySelected)}>重试</button></div> : null}
                {libraryDetail ? <div className="library-detail-body">
                  <div className="library-detail-tags"><span className={`library-kind kind-${libraryDetail.kind}`}>{memoryKindLabel(libraryDetail.kind)}</span><span className={`library-validity ${libraryDetail.lifecycle_status}`}>{libraryDetail.lifecycle_status === "withdrawn" ? "已撤销" : libraryDetail.lifecycle_status === "deleted" ? "已删除" : libraryDetail.status === "valid" ? "有效" : "已过期"}</span></div>
                  {libraryDetail.lifecycle_note ? <p className="notice warning">{libraryDetail.lifecycle_note}</p> : null}
                  {libraryDetail.lifecycle_status !== "deleted" ? <><h2>记忆内容</h2><pre className="library-full-content">{libraryDetail.content}</pre></> : null}
                  <h2>属性</h2><dl className="library-metadata"><dt>项目空间</dt><dd>{projects.find((item) => item.project_id === libraryDetail.project_id)?.full_path ?? projectName(libraryDetail.project_id)} · <code>{libraryDetail.namespace}</code></dd><dt>EverOS 类型</dt><dd>{libraryDetail.memory_type}</dd><dt>对外使用</dt><dd>{externalUseLabel(libraryDetail.external_use)}</dd><dt>生命周期</dt><dd>{libraryDetail.lifecycle_status === "active" ? "正式记忆" : libraryDetail.lifecycle_status === "withdrawn" ? "撤销后只读" : "删除标记"}</dd><dt>修订版本</dt><dd>{libraryDetail.revision}</dd><dt>证据标记</dt><dd>{libraryDetail.evidence_kind}</dd><dt>有效期</dt><dd>{libraryDetail.valid_until ? new Date(libraryDetail.valid_until).toLocaleString() : "长期有效"}</dd><dt>EverOS ID</dt><dd><code>{libraryDetail.everos_memory_id}</code></dd></dl>
                  {libraryDetail.lifecycle_status !== "deleted" ? <div className="library-evidence"><h2>来源证据</h2>{libraryDetail.source_segment ? <><div className="segment-status"><strong>{libraryDetail.source_segment.filename}</strong><span>{libraryDetail.source_segment.precision === "exact_input" ? "精确输入片段" : "关联消息（无精确字符范围）"} · {libraryDetail.source_segment.messages.length} 条消息</span><span className={libraryDetail.source_segment.archive_available ? "evidence-valid" : "evidence-invalid"}>{archiveStatusLabel(libraryDetail.source_segment.archive_status)}</span>{segmentStatusLabel(libraryDetail.source_segment.segment_status) ? <span className="evidence-invalid">{segmentStatusLabel(libraryDetail.source_segment.segment_status)}</span> : null}<span>{libraryDetail.source_segment.key_position_status === "located" ? "已定位关键原文" : "未定位关键句，可阅读完整片段"}</span></div>{libraryDetail.source_segment.key_position_status === "located" ? libraryDetail.source_segment.messages.filter((item) => item.key_position).map((item, index) => <article className="library-evidence-item" key={`${item.message_id}:${item.source_char_start}:${index}`}><div><strong>{item.role} · {formatTime(item.timestamp_ms)} · #{item.ordinal + 1}</strong></div><blockquote>{highlightSourceSpan(item.content, item.key_position)}</blockquote></article>) : null}<details className="library-segment-details"><summary>查看完整 MemCell 片段（{libraryDetail.source_segment.messages.length} 条消息）</summary>{libraryDetail.source_segment.messages.map((item, index) => <article className="library-evidence-item" key={`${item.message_id}:${item.source_char_start}:${index}`}><div><strong>{item.role} · {formatTime(item.timestamp_ms)} · #{item.ordinal + 1}</strong><code>{item.message_id}</code></div><pre>{highlightSourceSpan(item.content, item.key_position)}</pre></article>)}</details></> : libraryDetail.evidence.length ? libraryDetail.evidence.map((item) => <article className="library-evidence-item" key={`${item.message_id}:${item.filename}`}><div><strong>{item.filename}</strong><span>{item.role} · {formatTime(item.timestamp_ms)}</span></div>{item.quote && item.excerpt_matched ? <blockquote>{item.quote}</blockquote> : <small>原文件已归档，未定位到与此记忆匹配的短摘录。</small>}<small className={item.available ? "evidence-valid" : "evidence-invalid"}>{archiveStatusLabel(item.archive_status)}</small></article>) : <p className="subtle">没有可读取的来源证据。</p>}{!libraryDetail.evidence_available && libraryDetail.evidence.length ? <p className="notice warning">当前没有可用的原文件证据。</p> : null}</div> : null}
                  {memoryActionError ? <div className="notice error" role="alert">{memoryActionError}</div> : null}
                  {libraryDetail.lifecycle_status === "active" ? <div className="memory-lifecycle-actions"><div className="actions"><button className="secondary-button" disabled={memoryActionBusy} onClick={() => { setMemoryDraftContent(libraryDetail.content); setMemoryDraftExternalUse(libraryDetail.external_use); setMemoryDraftNote(""); setMemoryActionError(null); setShowMemoryEditor(true); }}>修正</button><button className="secondary-button" disabled={memoryActionBusy} onClick={() => void withdrawSelectedMemory()}>撤销</button><button className="secondary-button danger-button" disabled={memoryActionBusy} onClick={() => void deleteSelectedMemory()}>删除</button></div><p>撤销：保留记录，可只读查看。删除：仅保留删除标记，正文不可再查看。两者都会立即从检索、Memory Pack、MCP 和训练导出中排除；已交出的内容无法收回。</p></div> : null}
                  {showMemoryEditor ? <div className="memory-editor-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setShowMemoryEditor(false); }}><section className="memory-editor-modal" role="dialog" aria-modal="true" aria-label="修正记忆"><h2>修正记忆</h2><label>记忆内容<textarea rows={8} value={memoryDraftContent} onChange={(event) => setMemoryDraftContent(event.target.value)} maxLength={4000} /></label><label>对外使用<select value={memoryDraftExternalUse} onChange={(event) => setMemoryDraftExternalUse(event.target.value as LibraryMemory["external_use"])}><option value="allow">允许外发</option><option value="ask">导出时确认</option><option value="deny">不可外发</option></select></label><label>修改说明（可选）<textarea rows={3} value={memoryDraftNote} onChange={(event) => setMemoryDraftNote(event.target.value)} maxLength={500} placeholder="记录本次修改的原因" /></label><div className="actions right"><button className="secondary-button" disabled={memoryActionBusy} onClick={() => setShowMemoryEditor(false)}>取消</button><button className="primary-button" disabled={memoryActionBusy || !memoryDraftContent.trim()} onClick={() => void saveMemoryCorrection()}>{memoryActionBusy ? "保存中…" : "保存修正"}</button></div></section></div> : null}
                </div> : null}
              </aside>
            </div> : null}
          </section>
        ) : pageId === "review" ? (
          <section className="workspace review-workspace">
            <div className="review-toolbar">
              <div><strong>候选记忆</strong><span>核查候选内容、原始来源或人工标记，再决定是否写入正式记忆。</span></div>
              <button className="secondary-button" onClick={openManualCandidateForm}>＋ 人工新增</button>
            </div>
            <div className="review-toolbar review-filter-toolbar">
              <div className="review-filters">
                <label>状态<select value={candidateStatusFilter} onChange={(event) => setCandidateStatusFilter(event.target.value)}><option value="pending">待审核</option><option value="all">全部</option><option value="confirmed">已确认</option><option value="rejected">已拒绝</option></select></label>
                <label>类型<select value={candidateKindFilter} onChange={(event) => setCandidateKindFilter(event.target.value)}><option value="">全部类型</option><option value="episode">情节</option><option value="preference">画像</option></select></label>
                <label>来源<select value={candidateOriginFilter} onChange={(event) => setCandidateOriginFilter(event.target.value)}><option value="">全部来源</option><option value="import">导入抽取</option><option value="agent">Agent 提交</option><option value="manual">人工新增</option></select></label>
                <label>证据状态<select value={candidateEvidenceFilter} onChange={(event) => setCandidateEvidenceFilter(event.target.value)}><option value="">全部证据状态</option><option value="valid">有来源</option><option value="invalid">缺少有效来源</option><option value="not_required">无需原文件来源</option></select></label>
              </div>
            </div>
            {candidateError ? <div className="notice error" role="alert">{candidateError}</div> : null}
            {candidateNotice ? <div className="notice success">{candidateNotice}</div> : null}
            <div className="review-bulkbar">
              <label><input type="checkbox" checked={eligibleCandidates.length > 0 && eligibleCandidates.every((item) => checkedCandidates.includes(item.id))} disabled={candidateBusy !== null || eligibleCandidates.length === 0} onChange={(event) => setCheckedCandidates(event.target.checked ? eligibleCandidates.map((item) => item.id) : [])} /> 全选可确认的待审核候选</label>
              <button className="primary-button" disabled={candidateBusy !== null || checkedCandidates.length === 0} onClick={() => void confirmSelected()}>{candidateBusy === "bulk" ? "批量审核中…" : `批量审核通过 (${checkedCandidates.length})`}</button>
            </div>
            <div className="review-layout">
              <div className="candidate-list">
                {candidates.length === 0 ? <div className="review-empty">{candidateError ? "候选读取失败" : "当前筛选条件下没有候选内容。完成导入并抽取，或使用“人工新增”创建一条候选。"}</div> : null}
                {candidates.map((candidate) => {
                  const validEvidence = candidate.evidence_status === "valid";
                  const confirmable = canConfirmCandidate(candidate);
                  const draft = candidateChanges(candidate);
                  const supportedKind = draft.kind === "episode" || draft.kind === "preference";
                  const originLabel = candidate.metadata?.origin_kind === "platform_summary" ? "Claude platform summary" : candidate.origin === "import" ? "导入抽取" : candidate.origin === "agent" ? "Agent 提交" : "人工新增";
                  return <article className={`candidate-card ${candidate.id === selectedCandidateId ? "selected" : ""}`} key={candidate.id} onClick={() => setSelectedCandidateId(candidate.id)}>
                    <div className="candidate-heading">
                      <label className="candidate-check"><input type="checkbox" aria-label={`选择候选 ${candidate.id}`} disabled={candidateBusy !== null || candidate.status !== "pending" || !confirmable || !supportedKind} checked={checkedCandidates.includes(candidate.id)} onChange={(event) => setCheckedCandidates((current) => event.target.checked ? [...current, candidate.id] : current.filter((id) => id !== candidate.id))} /></label>
                      <button className="candidate-select" aria-pressed={candidate.id === selectedCandidateId} onClick={() => setSelectedCandidateId(candidate.id)}><strong className="candidate-preview">{candidate.content}</strong><span>{candidate.filename} · {candidate.namespace}</span></button>
                      <span className={`candidate-status ${candidate.status}`}>{candidate.status === "pending" ? "待审核" : candidate.status === "confirmed" ? "已确认" : candidate.status === "rejected" ? "已拒绝" : "发布中"}</span>
                    </div>
                    <div className="candidate-meta">{candidate.kind === "episode" || candidate.kind === "preference" ? <span className={`kind-chip kind-${candidate.kind}`}>{candidate.kind === "preference" ? "画像" : "情节"}</span> : null}<span>{originLabel}</span>{candidate.origin !== "manual" ? <span className={validEvidence ? "evidence-valid" : "evidence-invalid"}>{validEvidence ? (candidate.origin === "import" ? "有原文件来源" : "有消息来源") : "缺少有效来源"}</span> : null}</div>
                    {candidate.status === "pending" ? <div className="candidate-actions candidate-card-actions"><button className="primary-button" disabled={candidateBusy !== null || !draft.content.trim() || !confirmable || !supportedKind} onClick={() => void reviewCandidate(candidate, "confirm")}>确认</button><button className="secondary-button danger-button" disabled={candidateBusy !== null} onClick={() => void reviewCandidate(candidate, "reject")}>拒绝</button></div> : null}
                  </article>;
                })}
              </div>
              <aside className="evidence-panel">
                {selectedCandidate ? (() => {
                  const candidate = selectedCandidate;
                  const draft = candidateChanges(candidate);
                  const validEvidence = candidate.evidence_status === "valid";
                  const confirmable = canConfirmCandidate(candidate);
                  const supportedKind = draft.kind === "episode" || draft.kind === "preference";
                  const originLabel = candidate.metadata?.origin_kind === "platform_summary" ? "Claude platform summary" : candidate.origin === "import" ? "导入抽取" : candidate.origin === "agent" ? "Agent 提交" : "人工新增";
                  return <>
                    <h2>候选详情</h2>
                    <p className="evidence-source">{originLabel} · {candidate.namespace}{candidate.origin === "manual" ? ` · 创建于 ${formatDateTime(candidate.created_at)}` : ` · ${candidate.filename}`}<br /><code>{candidate.id}</code></p>
                    <div className="candidate-editor">
                      <label>候选记忆内容<textarea className="candidate-full-content" disabled={candidate.status !== "pending"} rows={6} value={draft.content} onChange={(event) => setCandidateDrafts((current) => ({ ...current, [candidate.id]: { ...draft, content: event.target.value } }))} /></label>
                      <p className={validEvidence || candidate.origin === "manual" ? "subtle" : "notice error"}>{candidate.origin === "manual" ? "由人工填写，不关联原文件或对话；确认后将以“人工新增”标记写入记忆库。" : validEvidence ? (candidate.origin === "import" ? "导入原文件已归档；系统未自动核验候选内容与原文是否一致，请人工核对。" : "已关联归档来源；请核实候选内容。") : (candidate.origin === "import" ? "找不到导入原文件，暂不能确认来源或入库。" : "候选内容未能由关联来源验证。可改写后逐条提交，服务端会重新检查；不可批量通过。")}</p>
                      <div className="candidate-setting-grid">
                        <label>记忆类型{candidate.memory_type !== "atomic_fact" ? <span className="read-only-value">{candidate.kind === "episode" ? "情节" : "画像"}</span> : <select disabled={candidate.status !== "pending"} value={draft.kind === "preference" ? "preference" : ""} onChange={(event) => setCandidateDrafts((current) => ({ ...current, [candidate.id]: { ...draft, kind: event.target.value as CandidateKind } }))}><option value="" disabled>请选择类型</option><option value="preference">画像</option></select>}</label>
                        <label>命名空间<select disabled={candidate.status !== "pending"} value={draft.namespace} onChange={(event) => setCandidateDrafts((current) => ({ ...current, [candidate.id]: { ...draft, namespace: event.target.value } }))}>{projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name} ({project.project_id})</option>)}</select></label>
                        <label>有效期<select disabled={candidate.status !== "pending"} value={draft.validity_days ?? "long"} onChange={(event) => setCandidateDrafts((current) => ({ ...current, [candidate.id]: { ...draft, validity_days: event.target.value === "long" ? null : Number(event.target.value) as 30 | 90 } }))}><option value="long">长期</option><option value={30}>30 天</option><option value={90}>90 天</option></select></label>
                        <label>对外使用<select disabled={candidate.status !== "pending"} value={draft.external_use} onChange={(event) => setCandidateDrafts((current) => ({ ...current, [candidate.id]: { ...draft, external_use: event.target.value as CandidateChanges["external_use"] } }))}><option value="ask">导出时确认</option><option value="allow">允许外发</option><option value="deny">不可外发</option></select></label>
                      </div>
                      {candidate.status === "pending" ? <div className="candidate-actions"><button className="primary-button" disabled={candidateBusy !== null || !draft.content.trim() || !supportedKind || (!confirmable && draft.content.trim() === candidate.content.trim())} onClick={() => void reviewCandidate(candidate, "confirm")}>{candidateBusy === candidate.id ? "处理中…" : "确认入库"}</button><button className="secondary-button danger-button" disabled={candidateBusy !== null} onClick={() => void reviewCandidate(candidate, "reject")}>拒绝</button></div> : null}
                    </div>
                    <div className="source-links"><h3>来源证据</h3>{candidate.evidence.length ? candidate.evidence.map((item) => <div className="source-link" key={item.message_id}><span>{item.role} · {formatTime(item.timestamp_ms)} · {item.message_id.slice(0, 12)}</span><button className="secondary-button" onClick={() => void openSource(candidate.id, item.message_id, draft.content)}>打开原对话</button></div>) : <p className="subtle">{candidate.origin === "manual" ? "人工新增，没有原文件来源。" : "没有可定位的归档消息。"}</p>}</div>
                  </>;
                })() : <p className="subtle">选择左侧候选查看详情与来源。</p>}
              </aside>
            </div>
            {showManualCandidateForm ? <div className="memory-editor-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !manualCandidateBusy) setShowManualCandidateForm(false); }}><section className="memory-editor-modal manual-candidate-modal" role="dialog" aria-modal="true" aria-label="人工新增记忆"><h2>人工新增记忆</h2><p className="subtle">保存后会先进入待审核列表，不会立即写入正式记忆。</p><label>记忆类型<select value={manualCandidateDraft.kind} onChange={(event) => setManualCandidateDraft((current) => ({ ...current, kind: event.target.value as ManualCandidateInput["kind"] }))}><option value="episode">情节</option><option value="preference">画像</option></select></label><label>命名空间（项目空间）<select value={manualCandidateDraft.namespace} onChange={(event) => setManualCandidateDraft((current) => ({ ...current, namespace: event.target.value }))}>{projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}（{project.project_id}）</option>)}</select></label><div className="manual-candidate-setting-grid"><label>有效期<select value={manualCandidateDraft.validity_days ?? "long"} onChange={(event) => setManualCandidateDraft((current) => ({ ...current, validity_days: event.target.value === "long" ? null : Number(event.target.value) as 30 | 90 }))}><option value="long">长期</option><option value={30}>30 天</option><option value={90}>90 天</option></select></label><label>对外使用<select value={manualCandidateDraft.external_use} onChange={(event) => setManualCandidateDraft((current) => ({ ...current, external_use: event.target.value as ManualCandidateInput["external_use"] }))}><option value="ask">导出时确认</option><option value="allow">允许外发</option><option value="deny">不可外发</option></select></label></div><label>记忆内容<textarea rows={7} value={manualCandidateDraft.content} onChange={(event) => setManualCandidateDraft((current) => ({ ...current, content: event.target.value }))} maxLength={4000} placeholder="写下希望保存为长期记忆的内容" /></label>{manualCandidateError ? <p className="notice error" role="alert">{manualCandidateError}</p> : null}{projectLoadError ? <p className="notice error">项目空间读取失败：{projectLoadError}</p> : null}<div className="actions right"><button className="secondary-button" disabled={manualCandidateBusy} onClick={() => setShowManualCandidateForm(false)}>取消</button><button className="primary-button" disabled={manualCandidateBusy || !projects.length || !manualCandidateDraft.content.trim()} onClick={() => void saveManualCandidate()}>{manualCandidateBusy ? "保存中…" : "加入候选审核"}</button></div></section></div> : null}
            {sourceOpen ? <div className="source-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) setSourceOpen(false); }}><section className="source-dialog" role="dialog" aria-modal="true" aria-label="原始会话"><div className="source-dialog-header"><div><h2>来源证据</h2><span>{sourceConversation?.filename ?? "读取中"}</span></div><button className="secondary-button" onClick={() => setSourceOpen(false)}>关闭</button></div>{sourceLoading ? <p>正在读取归档会话…</p> : null}{sourceError ? <p className="notice error" role="alert">{sourceError}</p> : null}{sourceConversation ? <><div className="source-segment"><div className="segment-status"><strong>MemCell 来源片段</strong><span>{sourceConversation.source_segment?.precision === "exact_input" ? "精确输入片段" : "关联消息（无精确字符范围）"} · {sourceConversation.source_segment?.messages.length ?? 0} 条消息</span><span className={sourceConversation.source_segment?.archive_available ? "evidence-valid" : "evidence-invalid"}>{archiveStatusLabel(sourceConversation.source_segment?.archive_status)}</span>{segmentStatusLabel(sourceConversation.source_segment?.segment_status) ? <span className="evidence-invalid">{segmentStatusLabel(sourceConversation.source_segment?.segment_status)}</span> : null}<span>{sourceConversation.source_segment?.key_position_status === "located" ? "已定位关键原文" : "未定位关键句，可阅读本片段"}</span></div>{sourceConversation.source_segment?.messages.map((item, index) => <article className={`source-message ${item.message_id === sourceConversation.selected_message_id ? "selected" : ""}`} key={`${item.message_id}:${item.source_char_start}:${index}`}><div><strong>{item.role}</strong><span>{formatTime(item.timestamp_ms)} · #{item.ordinal + 1} · 消息内字符 {item.source_char_start + 1}–{item.source_char_end}</span>{item.message_id === sourceConversation.selected_message_id ? <em>所选来源</em> : null}</div><pre>{highlightSourceSpan(item.content, item.key_position)}</pre>{!item.key_position ? <small>此消息未定位到可信的关键句。</small> : null}</article>)}{!sourceConversation.source_segment?.messages.length ? <p className="subtle">没有可恢复的来源消息。</p> : null}<button className="secondary-button source-full-toggle" onClick={() => setShowFullConversation(true)}>查看完整归档聊天并定位所选消息</button></div>{showFullConversation ? <div className="source-full-conversation"><h3>完整归档聊天</h3><div className="source-messages">{sourceConversation.messages.map((message) => { const sourceItem = sourceConversation.source_segment?.messages.find((item) => item.message_id === message.id && item.key_position); return <article className={`source-message ${message.id === sourceConversation.selected_message_id ? "selected" : ""}`} key={message.id} ref={(node) => { if (message.id === sourceConversation.selected_message_id) selectedSourceMessage.current = node; }}><div><strong>{message.role}</strong><span>{formatTime(message.timestamp_ms)} · #{message.ordinal + 1}</span>{message.id === sourceConversation.selected_message_id ? <em>所选消息</em> : null}</div><pre>{highlightSourceSpan(message.content, sourceItem?.key_position ? { char_start: sourceItem.key_position.source_char_start, char_end: sourceItem.key_position.source_char_end } : null)}</pre></article>; })}</div></div> : null}</> : null}</section></div> : null}
          </section>
        ) : pageId !== "imports" ? (
          <section className="workspace">
            <div className="empty-state">
              <div className="empty-icon">{page.title.slice(0, 1)}</div>
              <h2>{page.title}尚未实现</h2>
              <p>当前 MVP 正在实现文件导入和原文归档流程。</p>
            </div>
          </section>
        ) : (
          <section className="workspace import-workspace">
            <div className="wizard-progress" aria-label="导入步骤">
              {wizardSteps.map((label, index) => (
                <div className={`wizard-step ${index === step ? "current" : index < step ? "complete" : ""}`} key={label}>
                  <span>{index < step ? "✓" : index + 1}</span>{label}
                </div>
              ))}
            </div>

            {healthError ? <div className="notice error">{healthError}。请先启动本地 MEMO Vault API。</div> : null}
            {error ? <div className="notice error" role="alert">{error}</div> : null}

            {step === 0 ? (
              <div className="import-card">
                <h2>选择来源</h2>
                <p className="subtle">本轮开放通用文件导入，其他来源会在后续阶段接入。</p>
                <div className="source-grid">
                  {sources.map((item) => (
                    <button
                      className={`source-card ${source === item.id ? "selected" : ""} ${!item.available ? "unavailable" : ""}`}
                      disabled={!item.available}
                      key={item.id}
                      onClick={() => setSource(item.id)}
                    >
                      <strong>{item.title}</strong>
                      <span>{item.detail}</span>
                      {item.id === "generic" ? <small>如何导出？支持本地文件</small> : <small>{item.detail}</small>}
                    </button>
                  ))}
                </div>
                {source === "generic" ? (
                  <details className="guide">
                    <summary>通用文件格式说明</summary>
                    <p>支持 UTF-8 编码的 .json、.md、.txt，以及只包含这些格式的安全 ZIP。JSON 需为消息数组或包含 messages 数组的对象，消息字段为 role 和 content。</p>
                    <p>MD 可使用“## User / ## Assistant”标题分隔消息；没有明确角色标记的文本会作为一条消息导入。文件内容只作为文本显示，不会执行脚本。</p>
                  </details>
                ) : null}
                {source === "chatgpt" ? (
                  <details className="guide">
                    <summary>ChatGPT 导入说明</summary>
                    <p>上传 ChatGPT 账户导出的原始 ZIP。仅当前分支的可见用户与助手正文参与候选抽取；其他分支仍保留在 UMIF 和 Vault 中。</p>
                    <p>隐藏推理、账号设置和未随 ZIP 提供的附件不会进入记忆或 Knowledge。所有候选仍需人工审核。</p>
                  </details>
                ) : null}
                {source === "deepseek_harness" ? (
                  <details className="guide">
                    <summary>DeepSeek 导入说明</summary>
                    <p>支持网页端账户导出的 ZIP（包含 user.json 和 conversations.json）。有明确文本的请求和回复用于候选抽取；无法确定当前分支的分叉消息保留归档，暂不参与抽取。</p>
                    <p>支持 Harness v4 的 <code>session.v4.jsonl</code> 或仅含该文件的导出 ZIP。可见的 user/assistant 文本进入标准记忆抽取；工具调用和结果作为独立轨迹归档，当前不会进入记忆抽取。</p>
                    <p>reasoning、工具调用块和 Harness 注入上下文不会作为对话导入。工具参数及结果仅在解析预览中显示脱敏、截断后的内容。</p>
                  </details>
                ) : null}
                <div className="actions right"><button className="primary-button" disabled={!source} onClick={() => { setError(null); setStep(1); }}>下一步</button></div>
              </div>
            ) : null}

            {source === "claude" && step > 0 ? <ClaudeImport projects={projects} initialProjectId={projectId} onBack={clearWorkflow} step={step} onStepChange={setStep} /> : null}
            {step === 1 && source !== "claude" ? (
              <div className="import-card">
                <h2>上传与检查</h2>
                <p className="subtle">
                  {source === "chatgpt" ? "支持 ChatGPT 账户导出的原始 ZIP" : source === "deepseek_harness" ? "支持 DeepSeek 网页导出 ZIP、Harness v4 .jsonl 或 ZIP" : "支持 .json、.md、.txt 和 ZIP"}；单文件最大 {formatSize(health?.max_import_bytes ?? 100 * 1024 * 1024)}。
                  ZIP 解压后最多 {formatSize(health?.max_archive_unpacked_bytes ?? 50 * 1024 * 1024)}、
                  {health?.max_archive_files ?? 100} 个条目，不支持嵌套压缩包。
                </p>
                <input
                  accept={source === "chatgpt" ? ".zip,application/zip" : source === "deepseek_harness" ? ".jsonl,.zip,application/jsonl,application/x-ndjson,application/zip" : ".json,.md,.txt,.zip,application/json,text/plain,text/markdown,application/zip"}
                  className="visually-hidden"
                  onChange={(event) => {
                    const selected = event.target.files?.[0];
                    event.target.value = "";
                    void inspectFile(selected);
                  }}
                  ref={fileInput}
                  type="file"
                />
                <div
                  className={`drop-zone ${dragging ? "dragging" : ""}`}
                  onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
                  onDragLeave={() => setDragging(false)}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={handleDrop}
                >
                  <div className="upload-glyph">↑</div>
                  <strong>{busy ? "正在安全检查并解析…" : file ? file.name : "拖拽文件到这里"}</strong>
                  <span>{file ? `${formatSize(file.size)} · ${file.type || "未知 MIME，将由服务端校验"}` : "或从电脑中选择一个文件"}</span>
                  <button className="secondary-button" disabled={busy} onClick={() => fileInput.current?.click()}>选择文件</button>
                </div>
                <div className="process-stages">
                  {["上传文件", "安全与重复检查", "解析消息"].map((stage, index) => (
                    <div className={`process-stage ${busy && index === 1 ? "active" : preview ? "done" : ""}`} key={stage}>
                      <span>{preview ? "✓" : busy && index === 1 ? "…" : index + 1}</span>{stage}
                    </div>
                  ))}
                </div>
                <div className="actions">
                  <button className="secondary-button" disabled={busy} onClick={() => { setStep(0); setFile(null); setError(null); }}>上一步</button>
                  {busy ? <button className="secondary-button" onClick={() => abortController.current?.abort()}>取消处理</button> : null}
                  <span className="spacer" />
                  {file && !busy ? <button className="secondary-button" onClick={() => { setFile(null); setError(null); }}>重新选择文件</button> : null}
                </div>
              </div>
            ) : null}

            {source !== "claude" && step === 2 && preview ? (
              <div className="import-card">
                <div className="card-heading">
                  <div><h2>解析预览</h2><p className="subtle">{preview.filename} · {formatSize(preview.file_size)}</p></div>
                  <span className={`state-tag ${preview.duplicate_batch_id || preview.source_event_conflict_count ? "warning" : "success"}`}>{preview.duplicate_batch_id ? "原文件已导入" : preview.source_event_conflict_count ? "来源事件冲突，不能归档" : "安全检查与解析通过"}</span>
                </div>
                {preview.duplicate_batch_id ? (
                  <div className="notice warning">该文件已归档到批次 <code>{preview.duplicate_batch_id}</code>。本次没有新增归档。</div>
                ) : null}
                <div className="stats-grid">
                  <div><strong>{preview.conversation_count}</strong><span>会话</span></div>
                  <div><strong>{preview.message_count}</strong><span>消息</span></div>
                  <div><strong>{preview.tool_call_count}</strong><span>工具调用</span></div>
                  <div><strong>{preview.tool_result_count}</strong><span>工具结果</span></div>
                  <div><strong>{preview.umif_record_count}</strong><span>UMIF 记录</span></div>
                  <div><strong>{preview.duplicate_message_count}</strong><span>重复消息（保留来源）</span></div>
                  <div><strong>{preview.duplicate_tool_event_count}</strong><span>重复轨迹事件</span></div>
                </div>
                {source === "chatgpt" ? <p className="subtle">当前分支可参与记忆抽取：{preview.import_report?.counts?.memory_input_messages ?? 0} 条。其他可见分支保留在 Vault，不参与抽取。</p> : null}
                <p className="subtle">UMIF 包 ID：<code>{preview.umif_package_id}</code></p>
                {preview.umif_warnings.length ? <div className="notice warning">{preview.umif_warnings.join("；")}</div> : null}
                {preview.warnings.length || preview.source_event_conflict_count ? (
                  <div className="notice warning">
                    {preview.warnings.map((warning) => <p key={warning}>{warning}</p>)}
                    {preview.source_event_conflict_count ? <p>有 {preview.source_event_conflict_count} 个事件 ID 已存在但内容不同；请先检查来源文件。</p> : null}
                  </div>
                ) : null}
                {Object.keys(preview.skipped_event_counts).length ? (
                  <p className="subtle">保留在原文件、未参与对话抽取的内容：{Object.entries(preview.skipped_event_counts).map(([type, count]) => `${type} ${count}`).join(" · ")}</p>
                ) : null}
                <div className="hash-line">SHA-256 <code>{preview.raw_file_hash}</code></div>
                <h3>消息预览 <span className="subtle">最多展示前 20 条</span></h3>
                <div className="table-wrap">
                  <table>
                    <thead><tr><th>角色</th><th>时间</th><th>内容</th></tr></thead>
                    <tbody>
                      {preview.messages.map((message) => (
                        <tr key={`${message.conversation_id}-${message.ordinal}`}>
                          <td><span className="role-tag">{message.role}</span>{message.duplicate ? <small className="duplicate-label">重复内容</small> : null}</td>
                          <td>{formatTime(message.timestamp_ms)}</td>
                          <td>
                            <textarea
                              className="message-content"
                              aria-label={`${message.role} 消息内容`}
                              value={message.content}
                              rows={4}
                              readOnly
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {preview.tool_events.length ? (
                  <>
                    <h3>工具轨迹 <span className="subtle">最多展示前 20 条；参数和结果已脱敏并截断</span></h3>
                    <div className="table-wrap">
                      <table>
                        <thead><tr><th>事件</th><th>工具 / 调用 ID</th><th>时间</th><th>参数或结果</th></tr></thead>
                        <tbody>
                          {preview.tool_events.map((event) => (
                            <tr key={event.source_event_id}>
                              <td>{event.event_type} · #{event.source_event_seq}{event.duplicate ? <small className="duplicate-label">重复来源</small> : null}</td>
                              <td>{event.tool_name ?? "结果"}<br /><code>{event.call_id ?? "无调用 ID"}</code>{event.is_error ? <small className="duplicate-label">调用失败</small> : null}</td>
                              <td>{formatTime(event.timestamp_ms)}</td>
                              <td><pre className="trace-preview">{event.event_type === "tool/call" ? JSON.stringify(event.arguments, null, 2) : event.result_text ?? JSON.stringify(event.result_content, null, 2)}</pre>{event.source_event_seqs.length ? <small>关联事件：{event.source_event_seqs.join(", ")}</small> : null}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                ) : null}
                <div className="namespace-field">
                  <label htmlFor="project-space">目标项目空间</label>
                  <div className="project-picker">
                    <select id="project-space" value={projectId} onChange={(event) => {
                      if (preview && event.target.value !== projectId) {
                        void cancelPreview(preview.preview_id).catch(() => undefined);
                        setPreview(null);
                        setStep(1);
                      }
                      setProjectId(event.target.value);
                    }}>
                      {projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.name} · {item.project_id}</option>)}
                    </select>
                    <button className="secondary-button" type="button" onClick={openProjectCreate}>新建项目</button>
                  </div>
                  <small>此用户保险箱的 app_id：<code>{health?.app_id ?? "读取中"}</code>。项目之间的记忆互相隔离。</small>
                  <small>会话中的 user 和 assistant 内容共同属于用户聊天记录，进入 <code>users/{health?.user_id ?? "…"}</code>；<code>agents/{health?.agent_id ?? "…"}</code> 仅用于后续接入的结构化工具调用经验。</small>
                </div>
                <div className="actions">
                  <button className="secondary-button" onClick={() => { void cancelPreview(preview.preview_id).catch(() => undefined); setPreview(null); setStep(1); setError(null); }}>上一步</button>
                  <span className="spacer" />
                  <button className="primary-button" disabled={busy || Boolean(preview.duplicate_batch_id) || preview.source_event_conflict_count > 0 || !projectId} onClick={() => void saveArchive()}>{busy ? "正在归档…" : "确认入库"}</button>
                </div>
              </div>
            ) : null}

            {source !== "claude" && step === 3 && batch ? (
              <div className="import-card">
                <div className="notice success">✓ 原文已归档：{batch.conversation_count} 个会话 / {batch.message_count} 条消息。关闭此页也不会丢失数据。</div>
                <h2>抽取与审核</h2>
                <p className="subtle">原文归档与 EverOS 抽取是分开的。抽取失败不会影响已归档文件。</p>
                <TrackInventory batchId={batch.batch_id} />
                {source === "chatgpt" ? <ChatGPTReview batchId={batch.batch_id} /> : null}
                <div className="extract-options">
                  <button className="extract-option" disabled={busy} onClick={() => void runExtraction()}>
                    <strong>{busy ? "正在请求本地 EverOS…" : "立即使用本地 EverOS 抽取"}</strong>
                    <span>只发送到本机配置的 EverOS 服务，不使用云端回退。</span>
                  </button>
                  <button className="extract-option quiet" disabled={busy} onClick={deferExtraction}>
                    <strong>暂不抽取</strong>
                    <span>完成本次导入。原文已保存。</span>
                  </button>
                </div>
                {extractState === "failed" ? <div className="notice error">抽取失败，原文仍已保存。{error}</div> : null}
                <div className="actions right">
                  {extractState === "failed" ? <button className="secondary-button" disabled={busy} onClick={() => void runExtraction()}>重试抽取</button> : null}
                  <button className="secondary-button" disabled={busy} onClick={deferExtraction}>稍后处理</button>
                </div>
              </div>
            ) : null}

            {source !== "claude" && step === 4 && batch ? (
              <div className="import-card completion-card">
                <div className={`completion-mark ${extractState === "done" ? "success-mark" : ""}`}>{extractState === "done" ? "✓" : "⌂"}</div>
                <h2>{extractResult?.status === "deferred_tool_messages" ? "工具轨迹暂未抽取" : extractResult?.status === "unsupported_role_messages" ? "没有可抽取的聊天消息" : extractResult?.status === "no_new_messages" ? "没有新的消息需要抽取" : extractState === "done" ? "本地抽取已完成" : "原文已安全归档"}</h2>
                <p>{batch.original_filename} 已保存到 Vault，包含 {batch.conversation_count} 个会话和 {batch.message_count} 条消息。</p>
                <dl className="receipt-grid">
                  <dt>批次 ID</dt><dd><code>{batch.batch_id}</code></dd>
                  <dt>项目空间</dt><dd><code>{batch.app_id}/{batch.project_id}</code></dd>
                  <dt>原文件 SHA-256</dt><dd><code>{batch.raw_file_hash}</code></dd>
                  <dt>抽取状态</dt><dd>{extractResult?.status === "deferred_tool_messages" ? "待接入结构化工具轨迹" : extractResult?.status === "unsupported_role_messages" ? "已跳过不支持的消息角色" : extractResult?.status === "no_new_messages" ? "已跳过（消息均已存在）" : extractState === "done" ? "已生成待审核候选" : extractState === "failed" ? "失败，原文保留" : "未抽取"}</dd>
                </dl>
                {extractResult?.status === "extracted" ? <div className="notice success">EverOS 根据会话内容生成 <strong>{extractResult.candidate_count}</strong> 条待审核候选。候选保存在 Vault 审核区，尚未写入正式记忆。</div> : null}
                {extractResult?.status === "deferred_tool_messages" ? <div className="notice warning">本批次只包含工具消息。原文和标准化消息已保留；结构化工具轨迹接入后可再抽取。</div> : null}
                {extractResult?.status === "unsupported_role_messages" ? <div className="notice warning">本批次没有 user 或 assistant 聊天消息。其他角色内容仍保留在归档中，没有转换角色或提交抽取。</div> : null}
                {extractResult?.status === "no_new_messages" ? <div className="notice success">本批次消息均与已有归档重复，原始文件仍已保留；没有重复发送到 EverOS。</div> : null}
                {extractResult && extractResult.deferred_tool_message_count > 0 && extractResult.status === "extracted" ? <div className="notice warning">有 {extractResult.deferred_tool_message_count} 条工具结果暂未发送到 EverOS；原文与标准化记录仍保留在 Vault，结构化工具轨迹后续接入。</div> : null}
                {extractResult && extractResult.unsupported_role_message_count > 0 && extractResult.status === "extracted" ? <div className="notice warning">有 {extractResult.unsupported_role_message_count} 条其他角色消息未参与聊天抽取；它们仍保留在 Vault 中。</div> : null}
                {extractResult?.candidate_scope ? <div className="notice success">候选身份：<code>{extractResult.candidate_scope.app_id}/{extractResult.candidate_scope.project_id}/users/{extractResult.candidate_scope.user_id}</code>；<code>agent_id</code> 已保留，供后续工具轨迹抽取使用。</div> : null}
                {extractState === "failed" ? <div className="notice error">{error}</div> : null}
                <div className="actions right"><button className="primary-button" onClick={clearWorkflow}>继续导入</button></div>
              </div>
            ) : null}
          </section>
        )}
        {showCreateProject ? <div className="project-dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setShowCreateProject(false); }}><section className="project-dialog" role="dialog" aria-modal="true" aria-label="新建项目空间"><h2>新建项目空间</h2><label>上级记忆空间<select value={newProjectParentId} onChange={(event) => setNewProjectParentId(event.target.value)}><option value="">根空间</option>{projects.map((item) => <option key={item.project_id} value={item.project_id}>{item.full_path}</option>)}</select></label><label>项目名称<input value={newProjectName} onChange={(event) => setNewProjectName(event.target.value)} placeholder="例如：工作项目" maxLength={80} autoFocus /></label><div className="project-path-preview"><small>完整路径</small><code>{projectPathPreview()}</code></div><label>项目描述<textarea rows={3} value={newProjectDescription} onChange={(event) => setNewProjectDescription(event.target.value)} placeholder="可选，简要说明此空间的用途" maxLength={500} /></label>{projectCreateError ? <div className="notice error" role="alert">{projectCreateError}</div> : null}<div className="actions right"><button className="secondary-button" disabled={projectCreateBusy} onClick={() => setShowCreateProject(false)}>取消</button><button className="primary-button" disabled={projectCreateBusy || !newProjectName.trim()} onClick={() => void addProject()}>{projectCreateBusy ? "创建中…" : "创建并选择"}</button></div></section></div> : null}
      </main>
    </div>
  );
}
