export interface DataVaultRecordMeta {
  record_id: string;
  project_id: string;
  project_name: string;
  project_path: string;
  title: string;
  source_url: string;
  domain: string;
  capture_mode: string;
  capture_scope: string;
  unit_count: number;
  html_bytes: number;
  html_sha256: string;
  html_available: boolean;
  captured_at: string;
  created_at: string;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function extractBodyInner(rawHtml: string): string {
  const mainMatch = rawHtml.match(/<main\b[^>]*>[\s\S]*<\/main>/i);
  if (mainMatch) return mainMatch[0];
  const bodyMatch = rawHtml.match(/<body\b[^>]*>([\s\S]*)<\/body>/i);
  if (bodyMatch) return `<main data-memo-record-type="web_page">${bodyMatch[1]}</main>`;
  return `<main data-memo-record-type="web_page">${rawHtml}</main>`;
}

function formatDate(value: string): string {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

const READER_STYLES = `
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 24px 28px 40px;
    background: #ffffff;
    color: #1f2d3d;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    font-size: 14px;
    line-height: 1.72;
    word-break: break-word;
  }
  .memo-reader-header {
    margin-bottom: 24px;
    padding: 18px 20px;
    border: 1px solid #dce5f0;
    border-radius: 12px;
    background: linear-gradient(180deg, #f7faff 0%, #f2f6fc 100%);
  }
  .memo-reader-title {
    margin: 0 0 10px;
    font-size: 20px;
    line-height: 1.35;
    color: #132238;
  }
  .memo-reader-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 10px;
  }
  .memo-badge {
    display: inline-flex;
    align-items: center;
    padding: 2px 9px;
    border-radius: 999px;
    background: #e6f0fc;
    color: #1f5ca8;
    font-size: 11px;
    font-weight: 600;
  }
  .memo-badge.muted {
    background: #edf1f7;
    color: #4d5d73;
    font-weight: 500;
  }
  .memo-reader-meta {
    margin: 0;
    font-size: 12px;
    color: #53647a;
    line-height: 1.7;
  }
  .memo-reader-meta a {
    color: #1d69c9;
    text-decoration: none;
  }
  .memo-reader-meta a:hover { text-decoration: underline; }
  main[data-memo-record-type="web_page"] { max-width: 860px; }
  h1, h2, h3, h4, h5, h6 {
    margin: 1.35em 0 0.55em;
    color: #16263b;
    line-height: 1.35;
  }
  h1 { font-size: 1.55em; padding-bottom: 6px; border-bottom: 1px solid #e6ecf3; }
  h2 { font-size: 1.3em; padding-bottom: 4px; border-bottom: 1px solid #edf1f6; }
  h3 { font-size: 1.12em; }
  p { margin: 0.65em 0; color: #27374b; }
  a { color: #1f6ed4; text-decoration: none; }
  a:hover { text-decoration: underline; }
  ul.memo-links {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    list-style: none;
    margin: -2px 0 12px;
    padding: 0;
  }
  ul.memo-links li a {
    display: inline-block;
    padding: 2px 9px;
    border: 1px solid #cfe0f5;
    border-radius: 999px;
    background: #f4f9ff;
    color: #1f5ca8;
    font-size: 11px;
  }
  ul, ol { margin: 0.6em 0 0.9em; padding-left: 22px; }
  li { margin: 0.25em 0; }
  blockquote {
    margin: 12px 0;
    padding: 10px 14px;
    border-left: 3px solid #4c8ee3;
    border-radius: 0 8px 8px 0;
    background: #f5f9ff;
    color: #3a4c63;
  }
  pre {
    margin: 12px 0;
    padding: 12px 14px;
    overflow-x: auto;
    border: 1px solid #e1e8f0;
    border-radius: 8px;
    background: #f7f9fc;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12px;
    line-height: 1.6;
  }
  code {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  table {
    width: 100%;
    margin: 14px 0;
    border-collapse: collapse;
    border: 1px solid #dce4ee;
    border-radius: 8px;
    font-size: 12.5px;
    display: block;
    overflow-x: auto;
  }
  caption {
    padding: 6px 8px;
    text-align: left;
    font-weight: 600;
    color: #46576d;
  }
  th, td {
    padding: 8px 11px;
    border-bottom: 1px solid #e7edf4;
    border-right: 1px solid #edf1f6;
    text-align: left;
    vertical-align: top;
  }
  th { background: #f4f7fb; color: #36475e; font-weight: 600; }
  tbody tr:nth-child(even) { background: #fafcff; }
  article.memo-card {
    margin: 12px 0;
    padding: 14px 16px;
    border: 1px solid #dfe7f1;
    border-radius: 10px;
    background: #fbfdff;
  }
  article.memo-card h3 { margin: 0 0 8px; font-size: 15px; }
  article.memo-card dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 4px 12px;
    margin: 6px 0;
    font-size: 12.5px;
  }
  article.memo-card dt { color: #64748b; font-weight: 600; }
  article.memo-card dd { margin: 0; color: #243447; }
  img.memo-card-img {
    display: block;
    max-width: 100%;
    max-height: 240px;
    object-fit: cover;
    border-radius: 8px;
    margin-bottom: 10px;
    border: 1px solid #e3eaf2;
  }
  figure.memo-image {
    margin: 16px 0;
    padding: 12px;
    border: 1px solid #e3eaf2;
    border-radius: 10px;
    background: #fafcff;
    text-align: center;
  }
  figure.memo-image img {
    display: inline-block;
    max-width: 100%;
    max-height: 520px;
    height: auto;
    border-radius: 6px;
  }
  figure.memo-image figcaption {
    margin-top: 8px;
    font-size: 12px;
    color: #637388;
  }
`;

export function buildWebReaderHtml(rawHtml: string, record: DataVaultRecordMeta): string {
  const modeLabel = record.capture_mode === "selection" ? "选区采集" : "整页采集";
  const bodyHtml = extractBodyInner(rawHtml);
  const safeBase = escapeHtml(record.source_url);
  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>${escapeHtml(record.title)}</title>
  <base href="${safeBase}" target="_blank">
  <style>${READER_STYLES}</style>
</head>
<body>
  <header class="memo-reader-header">
    <h1 class="memo-reader-title">${escapeHtml(record.title)}</h1>
    <div class="memo-reader-badges">
      <span class="memo-badge">${escapeHtml(record.record_id)}</span>
      <span class="memo-badge muted">项目：${escapeHtml(record.project_path || record.project_name)}</span>
      <span class="memo-badge muted">${modeLabel}</span>
      <span class="memo-badge muted">${record.unit_count} 条内容块</span>
      ${record.domain ? `<span class="memo-badge muted">${escapeHtml(record.domain)}</span>` : ""}
    </div>
    <p class="memo-reader-meta">
      来源链接：<a href="${safeBase}" target="_blank" rel="noopener noreferrer">${safeBase}</a><br>
      采集时间：${escapeHtml(formatDate(record.captured_at))}
      ${record.capture_scope ? `<br>采集范围：${escapeHtml(record.capture_scope)}` : ""}
    </p>
  </header>
  ${bodyHtml}
</body>
</html>`;
}
