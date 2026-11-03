"""Evidence-source checks used by MEMO Vault candidate review.

Imported candidates require a linked message and an unchanged, hash-verified
archive. ``evidence_status`` remains a conservative text heuristic for other
sources; it catches obvious invented English names but does not prove a claim.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROPER_WORD = re.compile(r"(?<![\w])[A-Z][a-z]{2,}(?![\w])")
_GENERIC = {
    "The", "This", "That", "These", "Those", "User", "Agent", "Assistant",
    "Today", "Yesterday", "Tomorrow", "During", "After", "Before", "When",
    "While", "Because", "Some", "Any", "All", "Many", "Other", "First",
    "His", "Her", "Their", "Our", "Your", "My", "For", "From", "With",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December", "I", "We", "They", "He", "She",
}

_HAN = re.compile(r"[\u3400-\u9fff]+")
_LATIN = re.compile(r"[a-z0-9]{2,}")
_LATIN_KEY = re.compile(r"[a-z][a-z0-9_-]{2,}")
_NUMBER_KEY = re.compile(r"\d{2,}")
_SENTENCE = re.compile(r"[^\n。！？.!?;；]+(?:[。！？.!?;；]+|$)")
MAX_EVIDENCE_CHARS = 360


@dataclass(frozen=True)
class EvidenceExcerpt:
    """A literal, bounded span from an imported message."""

    quote: str
    char_start: int | None
    char_end: int | None
    matched: bool


def select_evidence_excerpt(candidate_content: str, source_content: str) -> EvidenceExcerpt:
    """Pick one short source span with lexical overlap; never synthesize text.

    Imported source files remain the provenance authority. This helper only
    locates a compact literal excerpt for the UI; a missing match is surfaced
    explicitly instead of showing an arbitrary part of the file as support.
    """
    if not candidate_content.strip() or not source_content.strip():
        return EvidenceExcerpt("", None, None, False)
    candidate_terms = _terms(candidate_content)
    if not candidate_terms:
        return EvidenceExcerpt("", None, None, False)

    best: tuple[int, int, int] | None = None
    for match in _SENTENCE.finditer(source_content):
        raw = match.group(0)
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        start = match.start() + left
        end = match.start() + right
        if start >= end:
            continue
        text = source_content[start:end]
        overlap = len(candidate_terms & _terms(text))
        if overlap == 0:
            continue
        # Prefer sentences with more shared terms, then modest excerpts.
        score = overlap * 1000 - min(len(text), MAX_EVIDENCE_CHARS)
        if best is None or score > best[0]:
            best = (score, start, end)
    if best is None:
        return EvidenceExcerpt("", None, None, False)

    start, end = best[1], best[2]
    if end - start > MAX_EVIDENCE_CHARS:
        span = source_content[start:end]
        shared = next(
            (term for term in candidate_terms if term in _terms(span)), None
        )
        if shared:
            position = span.casefold().find(shared.casefold())
            if position < 0:
                han_match = _HAN.search(span)
                position = han_match.start() if han_match else 0
        else:
            position = 0
        window_start = max(0, min(position - MAX_EVIDENCE_CHARS // 3, len(span) - MAX_EVIDENCE_CHARS))
        start += window_start
        end = start + MAX_EVIDENCE_CHARS
    quote = source_content[start:end]
    return EvidenceExcerpt(quote, start, end, True)


def select_key_excerpt(candidate_content: str, source_content: str) -> EvidenceExcerpt:
    """Find a useful literal sentence span, rejecting weak matches such as ``v4``."""
    if not candidate_content.strip() or not source_content.strip():
        return EvidenceExcerpt("", None, None, False)
    candidate_terms = _key_terms(candidate_content)
    if not candidate_terms:
        return EvidenceExcerpt("", None, None, False)

    ranked: list[tuple[int, int, int]] = []
    for match in _SENTENCE.finditer(source_content):
        raw = match.group(0)
        start = match.start() + len(raw) - len(raw.lstrip())
        end = match.start() + len(raw.rstrip())
        if end - start < 8:
            continue
        overlap = candidate_terms & _key_terms(source_content[start:end])
        # One short shared token often points at punctuation, version strings, or
        # generic words. Require at least two independent signals for a quote.
        if len(overlap) < 2:
            continue
        ranked.append((len(overlap) * 1000 - min(end - start, MAX_EVIDENCE_CHARS), start, end))
    if not ranked:
        return EvidenceExcerpt("", None, None, False)
    _, start, end = max(ranked)
    if end - start > MAX_EVIDENCE_CHARS:
        span = source_content[start:end]
        overlap = candidate_terms & _key_terms(span)
        position = -1
        for term in sorted(overlap, key=len, reverse=True):
            position = span.casefold().find(term.casefold())
            if position >= 0:
                break
        if position < 0:
            position = 0
        window_start = max(
            0,
            min(position - MAX_EVIDENCE_CHARS // 3, len(span) - MAX_EVIDENCE_CHARS),
        )
        start += window_start
        end = start + MAX_EVIDENCE_CHARS
    return EvidenceExcerpt(source_content[start:end], start, end, True)


def source_segment_digest(texts: Sequence[str]) -> str:
    """Hash the ordered literal input pieces using the candidate API contract."""
    encoded = json.dumps(list(texts), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_source_segment(
    value: object,
    source_rows: Sequence[Mapping[str, Any]],
) -> dict[str, object] | None:
    """Validate model-service offsets against the archived normalized messages."""
    validated, _ = _validated_source_segment(value, source_rows)
    return validated


def _validated_source_segment(
    value: object,
    source_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, object] | None, str]:
    """Return a normalized segment and distinguish tampering from bad shape."""
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or isinstance(value.get("version"), bool)
    ):
        return None, "invalid"
    if value.get("precision") != "exact_input" or not isinstance(value.get("items"), list):
        return None, "invalid"
    rows_by_id = {
        str(row["id"] if "id" in row.keys() else row["message_id"]): row
        for row in source_rows
    }
    items: list[dict[str, object]] = []
    texts: list[str] = []
    conversation_ids: set[str] = set()
    last_end_by_id: dict[str, int] = {}
    if len(value["items"]) > 1000:
        return None, "invalid"
    for item in value["items"]:
        if not isinstance(item, dict):
            return None, "invalid"
        message_id = item.get("message_id")
        start, end = item.get("char_start"), item.get("char_end")
        if (
            not isinstance(message_id, str)
            or message_id not in rows_by_id
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
        ):
            return None, "invalid"
        row = rows_by_id[message_id]
        content = str(row["content"] or "")
        if (
            start < 0
            or end <= start
            or end > len(content)
            or start < last_end_by_id.get(message_id, 0)
        ):
            return None, "invalid"
        last_end_by_id[message_id] = end
        texts.append(content[start:end])
        conversation_ids.add(str(row["conversation_id"]))
        items.append({"message_id": message_id, "char_start": start, "char_end": end})
    if not items or len(conversation_ids) != 1:
        return None, "invalid"
    digest = source_segment_digest(texts)
    if value.get("input_sha256") != digest:
        return None, "hash_mismatch"
    return {
        "version": 1,
        "precision": "exact_input",
        "conversation_id": next(iter(conversation_ids)),
        "input_sha256": digest,
        "items": items,
    }, "valid"


def load_source_segment(
    connection: sqlite3.Connection,
    candidate_id: str,
    memory_content: str,
    *,
    data_dir: Path,
    evidence_dir: Path,
    selected_message_id: str | None = None,
) -> dict[str, object] | None:
    """Read exact or legacy linked-message provenance for candidate and library UIs."""
    candidate = connection.execute(
        """SELECT c.batch_id, c.kind, c.metadata_json, c.source_segment_json, b.original_filename,
                  b.raw_relpath, b.file_sha256
           FROM candidates c JOIN import_batches b ON b.id = c.batch_id
           WHERE c.id = ?""",
        (candidate_id,),
    ).fetchone()
    if candidate is None:
        return None
    if candidate["kind"] in {"preference", "profile"}:
        try:
            metadata = json.loads(candidate["metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        quoted_evidence = metadata.get("evidence") if isinstance(metadata, dict) else None
        if isinstance(quoted_evidence, str) and quoted_evidence.strip():
            memory_content = quoted_evidence
    rows = connection.execute(
        """SELECT e.message_id, m.conversation_id, m.ordinal, m.role,
                  m.timestamp_ms, m.content
           FROM candidate_evidence e
           JOIN normalized_messages m ON m.id = e.message_id
           WHERE e.candidate_id = ? AND m.batch_id = ?
           ORDER BY m.ordinal, e.message_id""",
        (candidate_id, candidate["batch_id"]),
    ).fetchall()
    try:
        stored = json.loads(candidate["source_segment_json"] or "null")
    except (TypeError, ValueError):
        stored = None
    validated, validation_status = _validated_source_segment(stored, rows)
    segment_status = (
        "legacy_fallback"
        if candidate["source_segment_json"] is None
        else validation_status
    )
    archive_status = archived_source_status(
        data_dir, evidence_dir, candidate["raw_relpath"], candidate["file_sha256"]
    )
    if not rows:
        return {
            "version": 1,
            "precision": "linked_messages",
            "conversation_id": None,
            "filename": candidate["original_filename"],
            "archive_available": archive_status == "available",
            "archive_status": archive_status,
            "segment_status": segment_status,
            "key_position_status": "not_found",
            "messages": [],
        }

    rows_by_id = {str(row["message_id"]): row for row in rows}
    selected_row = rows_by_id.get(selected_message_id or "")
    conversation_id = (
        str(selected_row["conversation_id"])
        if selected_row is not None
        else str(validated["conversation_id"])
        if validated is not None
        else str(rows[0]["conversation_id"])
    )
    conversation_rows = [row for row in rows if str(row["conversation_id"]) == conversation_id]
    exact_items = validated["items"] if validated and validated["conversation_id"] == conversation_id else None
    if exact_items is None:
        segment_items = [
            {
                "message_id": str(row["message_id"]),
                "char_start": 0,
                "char_end": len(str(row["content"] or "")),
            }
            for row in conversation_rows
        ]
        precision = "linked_messages"
    else:
        segment_items = exact_items
        precision = "exact_input"

    row_by_id = {str(row["message_id"]): row for row in conversation_rows}
    messages: list[dict[str, object]] = []
    for item in segment_items:
        message_id = str(item["message_id"])
        row = row_by_id.get(message_id)
        if row is None:
            continue
        source_content = str(row["content"] or "")
        source_start = int(item["char_start"])
        source_end = int(item["char_end"])
        text = source_content[source_start:source_end]
        excerpt = select_key_excerpt(memory_content, text)
        key_position = None
        if excerpt.matched and excerpt.char_start is not None and excerpt.char_end is not None:
            key_position = {
                "char_start": excerpt.char_start,
                "char_end": excerpt.char_end,
                "source_char_start": source_start + excerpt.char_start,
                "source_char_end": source_start + excerpt.char_end,
                "text": excerpt.quote,
                "method": "lexical",
            }
        messages.append({
            "message_id": message_id,
            "ordinal": int(row["ordinal"]),
            "role": str(row["role"]),
            "timestamp_ms": row["timestamp_ms"],
            "source_char_start": source_start,
            "source_char_end": source_end,
            "content": text,
            "key_position": key_position,
        })
    return {
        "version": 1,
        "precision": precision,
        "conversation_id": conversation_id,
        "filename": candidate["original_filename"],
        "archive_available": archive_status == "available",
        "archive_status": archive_status,
        "segment_status": segment_status,
        "key_position_status": "located" if any(item["key_position"] for item in messages) else "not_found",
        "input_sha256": validated["input_sha256"] if precision == "exact_input" and validated else None,
        "messages": messages,
    }


def _terms(text: str) -> set[str]:
    lowered = text.casefold()
    terms = set(_LATIN.findall(lowered))
    for block in _HAN.findall(lowered):
        if len(block) <= 10:
            terms.add(block)
        terms.update(block[index : index + 2] for index in range(len(block) - 1))
    return terms


def _key_terms(text: str) -> set[str]:
    lowered = text.casefold()
    terms = set(_LATIN_KEY.findall(lowered)) | set(_NUMBER_KEY.findall(lowered))
    for block in _HAN.findall(lowered):
        if 2 <= len(block) <= 10:
            terms.add(block)
        terms.update(block[index : index + 2] for index in range(len(block) - 1))
    return terms


def evidence_status(candidate_content: str, source_texts: Sequence[str]) -> str:
    """Return valid, missing, or mismatch for a linked candidate.

    Valid means a source is locatable and no obvious unsupported named person/place
    was found. Human review is still necessary for semantic accuracy.
    """
    if not source_texts or not any(text.strip() for text in source_texts):
        return "missing"
    source = "\n".join(source_texts).casefold()
    for word in _PROPER_WORD.findall(candidate_content):
        if word in _GENERIC:
            continue
        if not re.search(rf"(?<![\w]){re.escape(word.casefold())}(?![\w])", source):
            return "mismatch"
    return "valid"


def archived_source_exists(
    data_dir: Path, evidence_dir: Path, raw_relpath: str | None
) -> bool:
    """Return whether an import's original archive exists inside the evidence root."""
    return archived_source_status(data_dir, evidence_dir, raw_relpath, None) != "missing"


def archived_source_status(
    data_dir: Path,
    evidence_dir: Path,
    raw_relpath: str | None,
    expected_sha256: str | None,
) -> str:
    """Check archive confinement, existence, and (when known) byte integrity."""
    if not raw_relpath:
        return "missing"
    try:
        archive_path = (data_dir / raw_relpath).resolve()
        if not archive_path.is_relative_to(evidence_dir.resolve()) or not archive_path.is_file():
            return "missing"
        if not expected_sha256:
            return "hash_unavailable"
        digest = hashlib.sha256()
        with archive_path.open("rb") as archived_file:
            for chunk in iter(lambda: archived_file.read(1024 * 1024), b""):
                digest.update(chunk)
        return "available" if digest.hexdigest() == expected_sha256 else "hash_mismatch"
    except (OSError, RuntimeError, ValueError):
        return "missing"
