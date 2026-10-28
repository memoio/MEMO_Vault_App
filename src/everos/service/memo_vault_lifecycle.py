"""Native EverOS mutations for Vault-confirmed user memories."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request

from everos.core.persistence import (
    EntryId,
    MarkdownReader,
    MarkdownWriter,
    MemoryRoot,
    render_structured_entry,
)
from everos.entrypoints.api.utils import cascade_orchestrator
from everos.infra.persistence.index import (
    UserProfile,
    all_of,
    atomic_fact_repo,
    eq,
    episode_repo,
    user_profile_repo,
)
from everos.infra.persistence.markdown import (
    AtomicFactReader,
    AtomicFactWriter,
    EpisodeReader,
    EpisodeWriter,
    ProfileReader,
    ProfileWriter,
    UserProfileFrontmatter,
)
from everos.memory.cascade.handlers._common import content_sha256

LifecycleAction = Literal["correct", "withdraw", "delete"]
MemoryType = Literal["atomic_fact", "episode", "profile"]


class LifecycleConflict(RuntimeError):
    """The published memory changed or no longer allows this transition."""


class LifecycleNotFound(RuntimeError):
    """The exact published memory is absent from the canonical store."""


async def mutate_published_memory(
    *,
    root: MemoryRoot,
    ledger_path: Path,
    candidate_id: str,
    memory_id: str,
    app_id: str,
    project_id: str,
    user_id: str,
    memory_type: MemoryType,
    action: LifecycleAction,
    expected_revision: int,
    content: str | None,
    request: Request,
) -> dict[str, object]:
    """Apply one revisioned lifecycle transition and return its receipt.

    The markdown store remains authoritative. Index rows are synchronously
    hidden before a body is changed or removed; corrected daily entries are
    immediately re-cascaded so their vectors and text index match the file.
    """
    scope = f"{app_id}/{project_id}/users/{user_id}"
    final_status = {"correct": "active", "withdraw": "withdrawn", "delete": "deleted"}[action]
    if action == "correct" and (not content or not content.strip()):
        raise HTTPException(status_code=422, detail={"code": "EMPTY_MEMORY_CONTENT", "message": "Memory content cannot be empty."})

    connection = sqlite3.connect(ledger_path, timeout=600)
    try:
        _ensure_ledger(connection)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT memory_id, scope, content, memory_type, lifecycle_status,
                      revision, operation_action
               FROM published_candidates WHERE candidate_id = ?""",
            (candidate_id,),
        ).fetchone()
        if row is None or row[1] != scope or row[0] != memory_id or row[3] != memory_type:
            raise LifecycleNotFound("The exact published memory does not exist in this scope.")
        current_content, current_status, revision, operation = row[2], row[4], int(row[5]), row[6]
        if current_status == final_status and operation is None:
            if action != "correct" or (current_content == content and revision > expected_revision):
                connection.commit()
                return _receipt(memory_id, current_status, revision, current_content)
        if current_status != "active":
            raise LifecycleConflict("This memory is no longer active.")
        if revision != expected_revision:
            raise LifecycleConflict("This memory changed since it was loaded. Refresh before editing.")
        connection.execute(
            "UPDATE published_candidates SET lifecycle_status = 'updating', operation_action = ? WHERE candidate_id = ?",
            (action, candidate_id),
        )
        marker = f"memo_vault_{final_status}:{candidate_id}"
        if memory_type == "profile":
            await _mutate_profile(
                root=root,
                app_id=app_id,
                project_id=project_id,
                user_id=user_id,
                candidate_id=candidate_id,
                action=action,
                marker=marker,
                content=content,
            )
        else:
            await _mutate_daily_entry(
                root=root,
                app_id=app_id,
                project_id=project_id,
                user_id=user_id,
                memory_id=memory_id,
                memory_type=memory_type,
                candidate_id=candidate_id,
                action=action,
                marker=marker,
                content=content,
                request=request,
            )

        next_revision = revision + 1
        result_content = (content or "") if action == "correct" else ("" if action == "delete" else current_content)
        connection.execute(
            """UPDATE published_candidates SET content = ?, lifecycle_status = ?,
               revision = ?, operation_action = NULL WHERE candidate_id = ?""",
            (result_content, final_status, next_revision, candidate_id),
        )
        connection.commit()
        return _receipt(memory_id, final_status, next_revision, result_content)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ensure_ledger(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS published_candidates (
           candidate_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL,
           scope TEXT NOT NULL, content TEXT NOT NULL,
           memory_type TEXT NOT NULL DEFAULT 'atomic_fact',
           lifecycle_status TEXT NOT NULL DEFAULT 'active',
           revision INTEGER NOT NULL DEFAULT 1, operation_action TEXT)"""
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(published_candidates)")}
    additions = {
        "memory_type": "TEXT NOT NULL DEFAULT 'atomic_fact'",
        "lifecycle_status": "TEXT NOT NULL DEFAULT 'active'",
        "revision": "INTEGER NOT NULL DEFAULT 1",
        "operation_action": "TEXT",
    }
    for name, declaration in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE published_candidates ADD COLUMN {name} {declaration}")


def _receipt(memory_id: str, status: str, revision: int, content: str) -> dict[str, object]:
    return {
        "memory_id": memory_id,
        "lifecycle_status": status,
        "revision": revision,
        "content": content if status != "deleted" else "",
    }


async def _mutate_daily_entry(
    *,
    root: MemoryRoot,
    app_id: str,
    project_id: str,
    user_id: str,
    memory_id: str,
    memory_type: Literal["episode", "atomic_fact"],
    candidate_id: str,
    action: LifecycleAction,
    marker: str,
    content: str | None,
    request: Request,
) -> None:
    prefix = f"{user_id}_"
    if not memory_id.startswith(prefix):
        raise LifecycleNotFound("The memory ID does not belong to this user.")
    try:
        entry_id = EntryId.parse(memory_id[len(prefix) :])
    except ValueError as exc:
        raise LifecycleNotFound("The memory ID is invalid.") from exc

    repo = episode_repo if memory_type == "episode" else atomic_fact_repo
    reader = EpisodeReader(root) if memory_type == "episode" else AtomicFactReader(root)
    writer = EpisodeWriter(root) if memory_type == "episode" else AtomicFactWriter(root)
    path = reader.path_for(user_id, entry_id.date, app_id=app_id, project_id=project_id)
    path = path.resolve()
    if not path.is_relative_to(root.root):
        raise LifecycleNotFound("The memory path is outside the configured root.")
    section_name = "Content" if memory_type == "episode" else "Fact"

    if action != "correct":
        await _mark_daily_index(repo, memory_id, user_id, app_id, project_id, marker)

    markdown_writer = MarkdownWriter(root)
    changed = False
    needs_reindex = False
    async with markdown_writer.lock_for(path):
        if not path.is_file():
            if action == "correct":
                raise LifecycleNotFound("The canonical memory entry no longer exists.")
            return
        parsed = await MarkdownReader.read(path)
        entry = next((value for value in parsed.entries if value.id == entry_id.format()), None)
        frontmatter = dict(parsed.frontmatter)
        body = parsed.body
        if action == "correct":
            if entry is None:
                raise LifecycleNotFound("The canonical memory entry no longer exists.")
            structured = entry.as_structured()
            if section_name not in structured.sections:
                raise LifecycleConflict("The canonical memory entry has an unexpected structure.")
            if structured.sections[section_name] != (content or "").strip():
                sections = dict(structured.sections)
                sections[section_name] = (content or "").strip()
                replacement = (
                    f"<!-- entry:{entry.id} -->\n"
                    f"{render_structured_entry(header=structured.header, inline=structured.inline, sections=sections)}\n"
                    f"<!-- /entry:{entry.id} -->\n"
                )
                body = body[: entry.start] + replacement + body[entry.end :]
                # Hide the stale vector/text row until the changed entry is re-cascaded.
                await _mark_daily_index(repo, memory_id, user_id, app_id, project_id, f"memo_vault_reindexing:{candidate_id}")
                changed = True
                needs_reindex = True
        else:
            deprecated = frontmatter.get("deprecated_entries")
            deprecated_map = dict(deprecated) if isinstance(deprecated, dict) else {}
            deprecated_map[entry_id.format()] = marker
            frontmatter["deprecated_entries"] = deprecated_map
            if action == "delete" and entry is not None:
                body = body[: entry.start] + body[entry.end :]
                changed = True
            else:
                changed = deprecated_map.get(entry_id.format()) != (deprecated or {}).get(entry_id.format()) if isinstance(deprecated, dict) else True
        if changed:
            await markdown_writer.write_markdown(path, frontmatter=frontmatter, body=body)

    if action == "correct":
        indexed = await repo.find_by_owner_entry(user_id, entry_id.format(), app_id=app_id, project_id=project_id)
        expected_field = "episode" if memory_type == "episode" else "fact"
        if not needs_reindex and indexed is not None and getattr(indexed, expected_field, None) == (content or "").strip() and indexed.deprecated_by is None:
            return
        orchestrator = cascade_orchestrator(request)
        if orchestrator is None:
            raise HTTPException(status_code=503, detail={"code": "CASCADE_UNAVAILABLE", "message": "EverOS indexing is unavailable; the corrected record is hidden until it can be re-indexed."})
        kind = "episode" if memory_type == "episode" else "atomic_fact"
        await orchestrator.sync_once(kinds={kind})
        indexed = await repo.find_by_owner_entry(user_id, entry_id.format(), app_id=app_id, project_id=project_id)
        if indexed is None or getattr(indexed, expected_field, None) != (content or "").strip() or indexed.deprecated_by is not None:
            raise HTTPException(status_code=503, detail={"code": "MEMORY_REINDEX_PENDING", "message": "The corrected memory was saved but its EverOS search index has not caught up. Retry after the cascade worker is available."})


async def _mark_daily_index(repo: object, memory_id: str, user_id: str, app_id: str, project_id: str, marker: str) -> None:
    await repo.update(
        {"deprecated_by": marker},
        where=all_of(
            eq("id", memory_id), eq("owner_id", user_id),
            eq("app_id", app_id), eq("project_id", project_id),
        ),
    )


async def _mutate_profile(
    *,
    root: MemoryRoot,
    app_id: str,
    project_id: str,
    user_id: str,
    candidate_id: str,
    action: LifecycleAction,
    marker: str,
    content: str | None,
) -> None:
    reader = ProfileReader(root)
    writer = ProfileWriter(root)
    existing = await reader.read(user_id, schema=UserProfileFrontmatter, app_id=app_id, project_id=project_id)
    if existing is None:
        raise LifecycleNotFound("The canonical user profile does not exist.")
    frontmatter, _ = existing
    # The profile row is an aggregate. Hide its old projection during the rewrite,
    # then synchronously restore the aggregate with this candidate removed/updated.
    found = False
    updates: dict[str, list[object]] = {}
    for section in ("explicit_info", "implicit_traits"):
        values = list(getattr(frontmatter, section))
        next_values: list[object] = []
        for value in values:
            if not isinstance(value, dict) or value.get("memo_vault_candidate_id") != candidate_id:
                next_values.append(value)
                continue
            found = True
            if action == "correct":
                next_values.append({**value, "description": (content or "").strip()})
            elif action == "withdraw":
                continue
            # Delete also drops the full profile item from canonical markdown.
        updates[section] = next_values
    if not found and action == "correct":
        raise LifecycleNotFound("The profile item is no longer present.")
    profile_path = writer.path_for(user_id, schema=UserProfileFrontmatter, app_id=app_id, project_id=project_id).resolve()
    if not profile_path.is_relative_to(root.root):
        raise LifecycleNotFound("The profile path is outside the configured root.")
    await user_profile_repo.delete(
        all_of(eq("owner_id", user_id), eq("app_id", app_id), eq("project_id", project_id))
    )
    updated = frontmatter.model_copy(update=updates)
    summary = "\n".join(
        str(item.get("description", ""))
        for section in ("explicit_info", "implicit_traits")
        for item in getattr(updated, section)
        if isinstance(item, dict) and item.get("description")
    )
    updated = updated.model_copy(update={"summary": summary})
    await writer.write(user_id, frontmatter=updated, body=summary, app_id=app_id, project_id=project_id)
    await _upsert_profile_index(updated, profile_path, root, app_id, project_id, user_id)


async def _upsert_profile_index(
    frontmatter: UserProfileFrontmatter,
    path: Path,
    root: MemoryRoot,
    app_id: str,
    project_id: str,
    user_id: str,
) -> None:
    explicit = json.dumps(frontmatter.explicit_info, sort_keys=True, ensure_ascii=False)
    implicit = json.dumps(frontmatter.implicit_traits, sort_keys=True, ensure_ascii=False)
    summary = frontmatter.summary
    digest = content_sha256({
        "frontmatter:summary": summary,
        "frontmatter:explicit_info_json": explicit,
        "frontmatter:implicit_traits_json": implicit,
    })
    await user_profile_repo.upsert([
        UserProfile(
            id=user_id,
            owner_id=user_id,
            owner_type="user",
            app_id=app_id,
            project_id=project_id,
            summary=summary,
            explicit_info_json=explicit,
            implicit_traits_json=implicit,
            profile_timestamp_ms=frontmatter.profile_timestamp_ms,
            md_path=path.relative_to(root.root).as_posix(),
            content_sha256=digest,
        )
    ])
