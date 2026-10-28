"""Claude manifest validation without opening short-lived download links."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from .import_contracts import ImportWorkflowError


@dataclass(frozen=True)
class ExportPart:
    category: str
    part: int
    batch_index: int
    filename: str
    export_url: str


def validate_download_url(url: str, *, redirect: bool = False) -> str:
    """Permit HTTPS Claude exports and known storage redirects only."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ImportWorkflowError(
            "CLAUDE_UNSAFE_URL", "Invalid export download address."
        ) from exc
    host = (parsed.hostname or "").lower()
    allowed = host in {"claude.ai", "claude.com"}
    if redirect:
        allowed |= any(
            host.endswith(suffix)
            for suffix in (
                ".anthropic.com",
                ".amazonaws.com",
                ".cloudfront.net",
            )
        )
    if (
        parsed.scheme != "https"
        or not allowed
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise ImportWorkflowError(
            "CLAUDE_UNSAFE_URL", "Unsupported export download address."
        )
    return host


def parse_manifest(payload: bytes) -> tuple[ExportPart, ...]:
    """Validate manifest v1 and its complete, uniquely named part list."""
    try:
        data = json.loads(payload)
        if not isinstance(data, dict) or data.get("version") != "1.0":
            raise ValueError
        rows = data["data_files"]
        if (
            not isinstance(rows, list)
            or not 1 <= len(rows) <= 1000
            or data["total_files"] != len(rows)
        ):
            raise ValueError
        entries = []
        seen = set()
        names = set()
        batches = set()
        for row in rows:
            category, part, batch = row["category"], row["part"], row["batch_index"]
            name, url = row["filename"], row["export_url"]
            if (
                not isinstance(category, str)
                or not category
                or type(part) is not int
                or part < 0
                or type(batch) is not int
                or batch < 0
                or not isinstance(name, str)
                or PurePosixPath(name).name != name
                or "\\" in name
                or not name.endswith(".zip")
                or not isinstance(url, str)
                or (category, part) in seen
                or name in names
                or batch in batches
            ):
                raise ValueError
            validate_download_url(url)
            entries.append(ExportPart(category, part, batch, name, url))
            seen.add((category, part))
            names.add(name)
            batches.add(batch)
        return tuple(sorted(entries, key=lambda e: (e.batch_index, e.category, e.part)))
    except (ValueError, KeyError, TypeError) as exc:
        raise ImportWorkflowError(
            "CLAUDE_INVALID_MANIFEST", "Invalid or unsupported Claude manifest."
        ) from exc
