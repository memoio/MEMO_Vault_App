"""Lightweight local document parser for Agent Chat file attachments."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # 5 MiB
MAX_EXTRACTED_CHARS = 1800

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".csv",
    ".tsv",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".log",
    ".ini",
    ".cfg",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".sql",
    ".sh",
    ".css",
}
HTML_EXTENSIONS = {".html", ".htm"}
DOCX_EXTENSIONS = {".docx"}
PDF_EXTENSIONS = {".pdf"}

SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | HTML_EXTENSIONS | DOCX_EXTENSIONS | PDF_EXTENSIONS


class AttachmentParseError(ValueError):
    """Raised when an uploaded attachment is unsupported, too large, or unreadable."""

    def __init__(self, message: str, *, code: str = "INVALID_ATTACHMENT", status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _extract_docx_text(raw: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            if "word/document.xml" not in zf.namelist():
                raise AttachmentParseError("DOCX 文件缺少正文内容（word/document.xml）。")
            xml_bytes = zf.read("word/document.xml")
    except zipfile.BadZipFile as exc:
        raise AttachmentParseError("无法解析 DOCX 文件，文件可能已损坏。") from exc

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise AttachmentParseError("DOCX 正文 XML 解析失败。") from exc

    paragraphs: list[str] = []
    for elem in root.iter():
        if elem.tag.endswith("}p") or elem.tag == "p":
            texts = [t.text for t in elem.iter() if (t.tag.endswith("}t") or t.tag == "t") and t.text]
            line = "".join(texts).strip()
            if line:
                paragraphs.append(line)
    return "\n".join(paragraphs)


def _extract_html_text(raw: bytes) -> str:
    decoded = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(decoded, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    lines = [line.strip() for line in soup.get_text("\n").splitlines() if line.strip()]
    return "\n".join(lines)


def _extract_pdf_text(raw: bytes) -> str:
    pdftotext = shutil.which("pdftotext")
    if pdftotext:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(raw)
            tmp.flush()
            proc = subprocess.run(
                [pdftotext, tmp.name, "-"],
                capture_output=True,
                timeout=20,
                check=False,
            )
            if proc.returncode == 0:
                text = proc.stdout.decode("utf-8", errors="replace").strip()
                if text:
                    return text
    raise AttachmentParseError(
        "当前环境暂不支持解析此 PDF（或 PDF 为扫描图片），建议转为 .txt / .md / .docx 后上传。",
        code="PDF_UNSUPPORTED",
        status_code=422,
    )


def parse_chat_attachment(
    *,
    filename: str,
    media_type: str,
    raw_bytes: bytes,
) -> dict[str, Any]:
    """Validate and extract readable text from a common document attachment."""
    safe_name = Path(filename or "attachment.txt").name.strip() or "attachment.txt"
    size_bytes = len(raw_bytes)
    if size_bytes == 0:
        raise AttachmentParseError("附件内容为空。", code="EMPTY_ATTACHMENT", status_code=422)
    if size_bytes > MAX_ATTACHMENT_BYTES:
        raise AttachmentParseError(
            "附件大小超过 5 MB 上限。", code="ATTACHMENT_TOO_LARGE", status_code=413
        )

    ext = Path(safe_name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        if (media_type or "").startswith("text/"):
            ext = ".txt"
        else:
            raise AttachmentParseError(
                f"暂不支持 {ext or media_type or '该格式'} 附件；支持 .txt / .md / .json / .csv / .html / .docx / .pdf 及常见代码文件。",
                code="UNSUPPORTED_ATTACHMENT_TYPE",
                status_code=415,
            )

    if ext in DOCX_EXTENSIONS:
        extracted = _extract_docx_text(raw_bytes)
    elif ext in HTML_EXTENSIONS:
        extracted = _extract_html_text(raw_bytes)
    elif ext in PDF_EXTENSIONS:
        extracted = _extract_pdf_text(raw_bytes)
    else:
        try:
            extracted = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            extracted = raw_bytes.decode("gb18030", errors="replace")

    cleaned = re.sub(r"\r\n?", "\n", extracted).strip()
    if not cleaned:
        raise AttachmentParseError("未能从附件中提取到可读文本。", code="EMPTY_ATTACHMENT", status_code=422)

    truncated = len(cleaned) > MAX_EXTRACTED_CHARS
    content = cleaned[:MAX_EXTRACTED_CHARS]
    snippet = re.sub(r"\s+", " ", content).strip()
    if len(snippet) > 200:
        snippet = snippet[:199].rstrip() + "…"

    return {
        "attachment_id": f"att_{uuid.uuid4().hex[:12]}",
        "filename": safe_name,
        "media_type": media_type or "application/octet-stream",
        "size_bytes": size_bytes,
        "char_count": len(content),
        "original_char_count": len(cleaned),
        "truncated": truncated,
        "content": content,
        "snippet": snippet,
    }
