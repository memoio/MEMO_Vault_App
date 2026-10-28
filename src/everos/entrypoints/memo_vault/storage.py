"""Local SQLite metadata store for Vault workflow state."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self

_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO schema_version(version) VALUES (1);

CREATE TABLE IF NOT EXISTS vault_identity (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    app_id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL UNIQUE,
    agent_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS project_spaces (
    app_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    parent_project_id TEXT,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(app_id, project_id)
);

CREATE TABLE IF NOT EXISTS import_batches (
    id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    namespace TEXT,
    status TEXT NOT NULL,
    extraction_status TEXT NOT NULL DEFAULT 'not_started',
    source_type TEXT NOT NULL DEFAULT 'generic',
    original_filename TEXT NOT NULL DEFAULT '',
    file_size INTEGER NOT NULL DEFAULT 0,
    parser_version TEXT NOT NULL DEFAULT '',
    conversation_count INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    duplicate_message_count INTEGER NOT NULL DEFAULT 0,
    raw_relpath TEXT NOT NULL DEFAULT '',
    archived_at TEXT,
    extraction_method TEXT,
    app_id TEXT NOT NULL DEFAULT '',
    project_id TEXT NOT NULL DEFAULT 'personal',
    user_id TEXT NOT NULL DEFAULT '',
    agent_id TEXT NOT NULL DEFAULT '',
    error_code TEXT,
    error_message TEXT,
    error_stage TEXT,
    umif_package_relpath TEXT,
    umif_package_id TEXT,
    umif_record_count INTEGER NOT NULL DEFAULT 0,
    umif_validation_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_import_batches_hash
    ON import_batches(file_sha256);

CREATE TABLE IF NOT EXISTS evidence_files (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES import_batches(id),
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    media_type TEXT NOT NULL,
    original_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS umif_record_map (
    batch_id TEXT NOT NULL REFERENCES import_batches(id),
    source_record_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT,
    status TEXT NOT NULL DEFAULT 'archived',
    error_stage TEXT,
    error_code TEXT,
    last_error TEXT,
    PRIMARY KEY(batch_id, source_record_id)
);

CREATE TABLE IF NOT EXISTS import_content_reviews (
    batch_id TEXT NOT NULL REFERENCES import_batches(id),
    source_record_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('knowledge', 'vault_only', 'exclude')),
    reviewer_id TEXT NOT NULL,
    app_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(batch_id, source_record_id)
);

CREATE TABLE IF NOT EXISTS normalized_messages (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES import_batches(id),
    conversation_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp_ms INTEGER,
    content_sha256 TEXT NOT NULL,
    source_message_id TEXT,
    is_duplicate INTEGER NOT NULL DEFAULT 0,
    memory_input INTEGER NOT NULL DEFAULT 1,
    UNIQUE(batch_id, conversation_id, ordinal)
);

CREATE TABLE IF NOT EXISTS normalized_tool_events (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES import_batches(id),
    conversation_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    source_event_id TEXT NOT NULL,
    source_event_seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    turn INTEGER,
    step INTEGER,
    call_id TEXT,
    tool_name TEXT,
    arguments_json TEXT,
    result_content_json TEXT,
    result_text TEXT,
    is_error INTEGER,
    source_event_seqs_json TEXT NOT NULL DEFAULT '[]',
    content_sha256 TEXT NOT NULL,
    is_duplicate INTEGER NOT NULL DEFAULT 0,
    source_content_sha256 TEXT,
    UNIQUE(batch_id, conversation_id, ordinal),
    UNIQUE(batch_id, conversation_id, source_event_id)
);
CREATE INDEX IF NOT EXISTS ix_tool_events_call_id
    ON normalized_tool_events(batch_id, call_id, source_event_seq);
CREATE INDEX IF NOT EXISTS ix_tool_events_source_seq
    ON normalized_tool_events(batch_id, conversation_id, source_event_seq);

CREATE TABLE IF NOT EXISTS import_events (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES import_batches(id),
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES import_batches(id),
    everos_scope TEXT NOT NULL,
    everos_memory_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'atomic_fact',
    metadata_json TEXT,
    source_segment_json TEXT,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    evidence_kind TEXT,
    external_use TEXT NOT NULL DEFAULT 'ask',
    origin TEXT NOT NULL DEFAULT 'import',
    target_project_id TEXT,
    validity_days INTEGER,
    valid_until TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS candidate_evidence (
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    message_id TEXT NOT NULL REFERENCES normalized_messages(id),
    char_start INTEGER,
    char_end INTEGER,
    quote TEXT NOT NULL,
    quote_sha256 TEXT NOT NULL,
    PRIMARY KEY(candidate_id, message_id, char_start, char_end)
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    everos_memory_id TEXT NOT NULL UNIQUE,
    namespace TEXT NOT NULL,
    kind TEXT NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'atomic_fact',
    status TEXT NOT NULL DEFAULT 'confirmed',
    revision INTEGER NOT NULL DEFAULT 1,
    evidence_kind TEXT NOT NULL,
    external_use TEXT NOT NULL DEFAULT 'ask',
    origin TEXT NOT NULL DEFAULT 'import',
    valid_until TEXT,
    lifecycle_status TEXT NOT NULL DEFAULT 'active',
    withdrawn_at TEXT,
    deleted_at TEXT,
    confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS memory_lifecycle_events (
    id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES memories(id),
    action TEXT NOT NULL,
    from_revision INTEGER NOT NULL,
    to_revision INTEGER NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_lifecycle_revision
    ON memory_lifecycle_events(memory_id, to_revision);

CREATE TABLE IF NOT EXISTS adp_traces (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES import_batches(id),
    payload_json TEXT NOT NULL,
    redacted_json TEXT,
    consent INTEGER NOT NULL DEFAULT 0,
    validation_status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    export_type TEXT NOT NULL,
    format TEXT NOT NULL,
    item_ids_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class VaultStore:
    """Creates the runtime layout and initializes the first metadata schema."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.database_path = data_dir / "vault.sqlite3"
        self.evidence_dir = data_dir / "evidence"

    def initialize(self) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(_SCHEMA)
            self._migrate_import_schema(connection)
            self._migrate_memory_revision(connection)
            self._migrate_export_metadata(connection)
            self._ensure_identity_and_default_project(connection)

    @staticmethod
    def _migrate_import_schema(connection: sqlite3.Connection) -> None:
        """Add import fields to Vault databases created by the initial skeleton."""
        additions = {
            "import_batches": {
                "source_type": "TEXT NOT NULL DEFAULT 'generic'",
                "original_filename": "TEXT NOT NULL DEFAULT ''",
                "file_size": "INTEGER NOT NULL DEFAULT 0",
                "parser_version": "TEXT NOT NULL DEFAULT ''",
                "conversation_count": "INTEGER NOT NULL DEFAULT 0",
                "message_count": "INTEGER NOT NULL DEFAULT 0",
                "duplicate_message_count": "INTEGER NOT NULL DEFAULT 0",
                "raw_relpath": "TEXT NOT NULL DEFAULT ''",
                "archived_at": "TEXT",
                "extraction_method": "TEXT",
                "app_id": "TEXT NOT NULL DEFAULT ''",
                "project_id": "TEXT NOT NULL DEFAULT 'personal'",
                "user_id": "TEXT NOT NULL DEFAULT ''",
                "agent_id": "TEXT NOT NULL DEFAULT ''",
                "error_code": "TEXT",
                "error_message": "TEXT",
                "error_stage": "TEXT",
                "umif_package_relpath": "TEXT",
                "umif_package_id": "TEXT",
                "umif_record_count": "INTEGER NOT NULL DEFAULT 0",
                "umif_validation_json": "TEXT",
            },
            "normalized_messages": {
                "source_message_id": "TEXT",
                "is_duplicate": "INTEGER NOT NULL DEFAULT 0",
                "memory_input": "INTEGER NOT NULL DEFAULT 1",
            },
            "normalized_tool_events": {
                "source_content_sha256": "TEXT",
            },
            "candidates": {
                "memory_type": "TEXT NOT NULL DEFAULT 'atomic_fact'",
                "metadata_json": "TEXT",
                "source_segment_json": "TEXT",
                "origin": "TEXT NOT NULL DEFAULT 'import'",
                "target_project_id": "TEXT",
                "validity_days": "INTEGER",
                "valid_until": "TEXT",
            },
            "memories": {
                "memory_type": "TEXT NOT NULL DEFAULT 'atomic_fact'",
                "origin": "TEXT NOT NULL DEFAULT 'import'",
                "valid_until": "TEXT",
                "lifecycle_status": "TEXT NOT NULL DEFAULT 'active'",
                "withdrawn_at": "TEXT",
                "deleted_at": "TEXT",
            },
            "project_spaces": {
                "parent_project_id": "TEXT",
                "description": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, columns in additions.items():
            present = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for name, declaration in columns.items():
                if name not in present:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                    )

    @staticmethod
    def _migrate_memory_revision(connection: sqlite3.Connection) -> None:
        """Separate lifecycle revisions from candidate-review revisions."""
        version = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if version is None or version < 2:
            connection.execute(
                "UPDATE memories SET revision = 1 WHERE lifecycle_status = 'active'"
            )
            connection.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (2)")

    @staticmethod
    def _migrate_export_metadata(connection: sqlite3.Connection) -> None:
        """Add metadata-only Pack audit fields to existing Vault databases."""
        present = {row[1] for row in connection.execute("PRAGMA table_info(exports)")}
        if "metadata_json" not in present:
            connection.execute(
                "ALTER TABLE exports ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
            )

    def record_export(
        self,
        *,
        export_type: str,
        format: str,
        item_revisions: dict[str, int],
        content_sha256: str,
        created_at: str,
        namespace_scope: list[str],
        delivery_action: str,
        confirmed_ask_ids: list[str],
        preview_sha256: str,
    ) -> None:
        """Record Pack audit metadata only; this API accepts no content body."""
        metadata = {
            "item_revisions": item_revisions,
            "namespace_scope": sorted(set(namespace_scope)),
            "delivery_action": delivery_action,
            "confirmed_ask_ids": sorted(set(confirmed_ask_ids)),
            "preview_sha256": preview_sha256,
        }
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO exports
                   (id, export_type, format, item_ids_json, content_sha256, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    uuid.uuid4().hex,
                    export_type,
                    format,
                    json.dumps(sorted(item_revisions), separators=(",", ":")),
                    content_sha256,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    created_at,
                ),
            )

    @staticmethod
    def _ensure_identity_and_default_project(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            """SELECT app_id, user_id, agent_id FROM vault_identity
               WHERE singleton_id = 1"""
        ).fetchone()
        if row is None:
            identity_suffix = uuid.uuid4().hex[:20]
            app_id = f"memo_vault_{identity_suffix}"
            user_id = f"user_{identity_suffix}"
            agent_id = f"agent_{identity_suffix}"
            connection.execute(
                """INSERT INTO vault_identity
                   (singleton_id, app_id, user_id, agent_id)
                   VALUES (1, ?, ?, ?)""",
                (app_id, user_id, agent_id),
            )
        else:
            app_id, user_id, agent_id = row

        connection.execute(
            """INSERT OR IGNORE INTO project_spaces (app_id, project_id, name)
               VALUES (?, 'personal', '个人空间')""",
            (app_id,),
        )
        legacy_batches = connection.execute(
            """SELECT id, namespace FROM import_batches WHERE app_id = ''
               AND namespace IS NOT NULL AND namespace != ''"""
        ).fetchall()
        for batch_id, old_namespace in legacy_batches:
            project_id = re.sub(
                r"[^a-z0-9_-]+", "-", old_namespace.lower()
            ).strip("-_")
            project_id = project_id[:64].strip("-_") or "personal"
            if not project_id[0].isalnum():
                project_id = f"project-{project_id}"[:64]
            connection.execute(
                """INSERT OR IGNORE INTO project_spaces (app_id, project_id, name)
                   VALUES (?, ?, ?)""",
                (app_id, project_id, old_namespace),
            )
            connection.execute(
                "UPDATE import_batches SET project_id = ? WHERE id = ?",
                (project_id, batch_id),
            )
        connection.execute(
            """UPDATE import_batches
               SET app_id = ?, user_id = ?, agent_id = ?,
                   project_id = CASE
                       WHEN project_id IS NULL OR project_id = '' THEN 'personal'
                       ELSE project_id
                   END
               WHERE app_id = '' OR user_id = '' OR agent_id = ''""",
            (app_id, user_id, agent_id),
        )

    def get_identity(self) -> dict[str, str]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT app_id, user_id, agent_id FROM vault_identity
                   WHERE singleton_id = 1"""
            ).fetchone()
            if row is None:
                raise RuntimeError("Vault identity has not been initialized.")
            return dict(row)

    def list_projects(self) -> list[dict[str, object]]:
        identity = self.get_identity()
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT project_id, name, parent_project_id, description FROM project_spaces
                   WHERE app_id = ? ORDER BY created_at, project_id""",
                (identity["app_id"],),
            ).fetchall()
        by_id = {str(row["project_id"]): dict(row) for row in rows}
        result: list[dict[str, object]] = []
        for row in rows:
            project = by_id[str(row["project_id"])]
            parts = [str(project["name"])]
            parent_id = project["parent_project_id"]
            seen = {str(project["project_id"])}
            depth = 0
            while parent_id and str(parent_id) in by_id:
                parent_key = str(parent_id)
                if parent_key in seen:
                    break
                seen.add(parent_key)
                parent = by_id[parent_key]
                parts.insert(0, str(parent["name"]))
                parent_id = parent["parent_project_id"]
                depth += 1
            project["full_path"] = "/" + "/".join(parts)
            project["depth"] = depth
            result.append(project)
        result.sort(key=lambda project: (str(project["full_path"]).casefold(), str(project["project_id"])))
        return result

    def create_project(
        self,
        *,
        name: str,
        parent_project_id: str | None,
        description: str = "",
    ) -> dict[str, object]:
        identity = self.get_identity()
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30].strip("-")
        project_id = f"space_{slug or 'project'}_{uuid.uuid4().hex[:10]}"
        with self.connect() as connection:
            if parent_project_id is not None:
                parent = connection.execute(
                    "SELECT 1 FROM project_spaces WHERE app_id = ? AND project_id = ?",
                    (identity["app_id"], parent_project_id),
                ).fetchone()
                if parent is None:
                    raise KeyError(parent_project_id)
            duplicate = connection.execute(
                """SELECT 1 FROM project_spaces
                   WHERE app_id = ? AND ifnull(parent_project_id, '') = ifnull(?, '')
                     AND name = ? COLLATE NOCASE""",
                (identity["app_id"], parent_project_id, name),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("A project with this name already exists under the selected parent.")
            connection.execute(
                """INSERT INTO project_spaces
                   (app_id, project_id, name, parent_project_id, description)
                   VALUES (?, ?, ?, ?, ?)""",
                (identity["app_id"], project_id, name, parent_project_id, description),
            )
        return next(project for project in self.list_projects() if project["project_id"] == project_id)

    def has_project(self, project_id: str) -> bool:
        identity = self.get_identity()
        with self.connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM project_spaces WHERE app_id = ? AND project_id = ?",
                    (identity["app_id"], project_id),
                ).fetchone()
                is not None
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def __enter__(self) -> Self:
        self.initialize()
        return self

    def __exit__(self, *_: object) -> None:
        return None
