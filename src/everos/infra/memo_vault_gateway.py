"""SQLite persistence for Vault grants, admission leases and metadata-only audits."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from everos.component.utils.datetime import from_iso_format, get_utc_now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), paused INTEGER NOT NULL DEFAULT 1,
 cursor_secret TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS export_grants (
 id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
 fingerprint TEXT NOT NULL,
 config_json TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0, consumed INTEGER NOT NULL DEFAULT 0,
 version INTEGER NOT NULL DEFAULT 1, pack_id TEXT, manifest_json TEXT);
CREATE TABLE IF NOT EXISTS gateway_audit (
 id TEXT PRIMARY KEY, grant_id TEXT NOT NULL, operation TEXT NOT NULL,
 transport TEXT NOT NULL,
 created_at TEXT NOT NULL, deadline TEXT NOT NULL,
 outcome TEXT NOT NULL,
 reason TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
 duration_ms INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS gateway_audit_grant ON gateway_audit(grant_id,created_at);
CREATE TABLE IF NOT EXISTS gateway_grant_requests (
 request_key TEXT PRIMARY KEY, grant_id TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gateway_pack_drafts (
 id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL, scope_json TEXT NOT NULL,
 expires_at TEXT NOT NULL);
"""


class RepositoryDeniedError(RuntimeError):
    """An admission could not be authorized under the current database state."""


class DuplicateGrantError(RuntimeError):
    """An owner request already created a credential; secrets are never replayed."""

    def __init__(self, grant_id: str) -> None:
        self.grant_id = grant_id
        super().__init__("Authorization already created; inspect or rotate it.")


class RepositoryLimitedError(RuntimeError):
    """Rate or concurrency ceiling reached."""


class GatewayRepository:
    def __init__(self, database_path: Path, *, audit_retention_days: int = 30) -> None:
        self.database_path = database_path
        self.audit_retention_days = max(1, min(audit_retention_days, 365))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database_path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(_SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(export_grants)")}
            if "stale" not in columns:
                db.execute(
                    "ALTER TABLE export_grants ADD COLUMN stale "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            db.execute(
                "INSERT OR IGNORE INTO gateway_state VALUES(1,1,?)",
                (secrets.token_hex(32),),
            )

    def status(self) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT paused FROM gateway_state WHERE singleton=1"
            ).fetchone()
        return {"paused": bool(row["paused"])}

    def set_paused(self, paused: bool) -> dict[str, Any]:
        with self.connect() as db:
            db.execute(
                "UPDATE gateway_state SET paused=? WHERE singleton=1", (int(paused),)
            )
        return self.status()

    def cursor_secret(self) -> bytes:
        with self.connect() as db:
            return bytes.fromhex(
                db.execute(
                    "SELECT cursor_secret FROM gateway_state WHERE singleton=1"
                ).fetchone()[0]
            )

    def create_grant(
        self,
        config: dict[str, Any],
        *,
        manifest: dict[str, Any] | None = None,
        draft_id: str | None = None,
        request_key: str | None = None,
    ) -> dict[str, Any]:
        token = "mv_" + secrets.token_urlsafe(32)
        grant_id = uuid.uuid4().hex
        now = get_utc_now()
        pack_id = uuid.uuid4().hex if manifest else None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_key:
                existing = db.execute(
                    "SELECT grant_id FROM gateway_grant_requests WHERE request_key=?",
                    (request_key,),
                ).fetchone()
                if existing:
                    raise DuplicateGrantError(existing[0])

            if draft_id is not None:
                draft = db.execute(
                    "SELECT manifest_json FROM gateway_pack_drafts WHERE id=? AND "
                    "expires_at>?",
                    (draft_id, now.isoformat()),
                ).fetchone()
                if draft is None or json.loads(draft[0]) != manifest:
                    raise RepositoryDeniedError()
                db.execute("DELETE FROM gateway_pack_drafts WHERE id=?", (draft_id,))
            db.execute(
                """INSERT INTO export_grants
                   (id,token_hash,fingerprint,config_json,created_at,expires_at,
                    pack_id,manifest_json) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    grant_id,
                    self._hash(token),
                    token[:6] + "…" + token[-4:],
                    json.dumps(config),
                    now.isoformat(),
                    (now + timedelta(hours=config["expires_in_hours"])).isoformat(),
                    pack_id,
                    json.dumps(manifest) if manifest else None,
                ),
            )
            if request_key:
                db.execute(
                    "INSERT INTO gateway_grant_requests VALUES(?,?,?)",
                    (request_key, grant_id, now.isoformat()),
                )
        return self.get_grant(grant_id) | {"token": token}

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _public(self, row: sqlite3.Row) -> dict[str, Any]:
        result = json.loads(row["config_json"])
        status = (
            "revoked"
            if row["revoked"]
            else "expired"
            if row["expires_at"] <= get_utc_now().isoformat()
            else "consumed"
            if row["consumed"]
            else "stale"
            if row["stale"]
            else "active"
        )
        return result | {
            "id": row["id"],
            "fingerprint": row["fingerprint"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "status": status,
            "pack_id": row["pack_id"],
            "version": row["version"],
            "last_used_at": row["last_used_at"]
            if "last_used_at" in dict(row)
            else None,
        }

    def get_grant(self, grant_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT *, (SELECT MAX(created_at) FROM gateway_audit "
                "WHERE grant_id=export_grants.id "
                "AND outcome='admitted') AS last_used_at "
                "FROM export_grants WHERE id=?",
                (grant_id,),
            ).fetchone()
        if row is None:
            raise RepositoryDeniedError()
        return self._public(row)

    def list_grants(self) -> list[dict[str, Any]]:
        """Return the latest grants for internal callers that do not page."""
        items, _ = self.list_grant_page(limit=500)
        return items

    def list_grant_page(
        self, *, limit: int = 50, offset: int = 0, bucket: str = "all"
    ) -> tuple[list[dict[str, Any]], int]:
        now = get_utc_now().isoformat()
        active = "revoked=0 AND consumed=0 AND stale=0 AND expires_at>?"
        if bucket == "active":
            where, params = f"WHERE {active}", (now,)
        elif bucket == "history":
            where, params = f"WHERE NOT ({active})", (now,)
        else:
            where, params = "", ()
        with self.connect() as db:
            rows = db.execute(
                "SELECT *, (SELECT MAX(created_at) FROM gateway_audit "
                "WHERE grant_id=export_grants.id "
                "AND outcome='admitted') AS last_used_at "
                f"FROM export_grants {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, max(1, min(limit, 500)), max(0, offset)),
            ).fetchall()
            total = db.execute(
                f"SELECT COUNT(*) FROM export_grants {where}", params
            ).fetchone()[0]
        return [self._public(row) for row in rows], int(total)

    def authorize(
        self, token: str, *, db: sqlite3.Connection | None = None
    ) -> dict[str, Any]:
        if db is None:
            with self.connect() as connection:
                return self.authorize(token, db=connection)
        row = db.execute(
            "SELECT * FROM export_grants WHERE token_hash=?", (self._hash(token),)
        ).fetchone()
        paused = db.execute(
            "SELECT paused FROM gateway_state WHERE singleton=1"
        ).fetchone()
        if (
            row is None
            or paused is None
            or paused[0]
            or row["revoked"]
            or row["stale"]
            or row["expires_at"] <= get_utc_now().isoformat()
        ):
            raise RepositoryDeniedError()
        result = self._public(row)
        result["manifest"] = (
            json.loads(row["manifest_json"]) if row["manifest_json"] else None
        )
        result["consumed"] = bool(row["consumed"])
        return result

    def mark_stale(self, grant_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE export_grants SET stale=1 WHERE id=?", (grant_id,))

    def revoke(self, grant_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE export_grants SET revoked=1, version=version+1 WHERE id=? AND "
                "revoked=0",
                (grant_id,),
            )

    def delete_grant(self, grant_id: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT revoked,consumed,stale,expires_at FROM export_grants WHERE id=?",
                (grant_id,),
            ).fetchone()
            if row is None:
                raise RepositoryDeniedError()
            if not (
                row["revoked"]
                or row["consumed"]
                or row["stale"]
                or row["expires_at"] <= get_utc_now().isoformat()
            ):
                raise RepositoryDeniedError(
                    "Active grants must be revoked before deletion."
                )
            db.execute("DELETE FROM gateway_audit WHERE grant_id=?", (grant_id,))
            db.execute(
                "DELETE FROM gateway_grant_requests WHERE grant_id=?", (grant_id,)
            )
            db.execute("DELETE FROM export_grants WHERE id=?", (grant_id,))

    def rotate(self, grant_id: str) -> dict[str, Any]:
        token = "mv_" + secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM export_grants WHERE id=?", (grant_id,)
            ).fetchone()
            if (
                row is None
                or row["revoked"]
                or row["stale"]
                or row["consumed"]
                or row["expires_at"] <= get_utc_now().isoformat()
            ):
                raise RepositoryDeniedError()
            db.execute(
                "UPDATE export_grants SET token_hash=?, fingerprint=?, "
                "version=version+1 WHERE id=?",
                (self._hash(token), token[:6] + "…" + token[-4:], grant_id),
            )
        return self.get_grant(grant_id) | {"token": token}

    def begin(
        self, token: str, operation: str, transport: str
    ) -> tuple[dict[str, Any], str]:
        now = get_utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            grant = self.authorize(token, db=db)
            if (
                operation != "capabilities" and operation not in grant["operations"]
            ) or (grant["consumed"] and operation == "read_memory_pack"):
                raise RepositoryDeniedError()
            minute = (now - timedelta(minutes=1)).isoformat()
            count = db.execute(
                "SELECT count(*) FROM gateway_audit WHERE grant_id=? AND created_at>?",
                (grant["id"], minute),
            ).fetchone()[0]
            pending = db.execute(
                "SELECT count(*) FROM gateway_audit WHERE grant_id=? AND "
                "outcome='pending' AND deadline>?",
                (grant["id"], now.isoformat()),
            ).fetchone()[0]
            if count >= grant["requests_per_minute"] or pending >= 2:
                raise RepositoryLimitedError()
            request_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO gateway_audit(id,grant_id,operation,transport,"
                "created_at,deadline,outcome) VALUES(?,?,?,?,?,?,'pending')",
                (
                    request_id,
                    grant["id"],
                    operation,
                    transport,
                    now.isoformat(),
                    (now + timedelta(seconds=30)).isoformat(),
                ),
            )
            db.execute(
                "DELETE FROM gateway_audit WHERE created_at<?",
                ((now - timedelta(days=self.audit_retention_days)).isoformat(),),
            )
        return grant, request_id

    def admit(
        self,
        token: str,
        grant: dict[str, Any],
        request_id: str,
        sources: list[dict[str, Any]],
        metadata: dict[str, Any],
    ) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.authorize(token, db=db)
            pending = db.execute(
                "SELECT * FROM gateway_audit WHERE id=? AND outcome='pending'",
                (request_id,),
            ).fetchone()
            if (
                current["version"] != grant["version"]
                or pending is None
                or pending["deadline"] <= get_utc_now().isoformat()
            ):
                raise RepositoryDeniedError()
            for source in sources:
                row = db.execute(
                    "SELECT * FROM memories WHERE id=? AND status='confirmed'",
                    (source["memory_id"],),
                ).fetchone()
                fields = (
                    "everos_memory_id",
                    "namespace",
                    "kind",
                    "revision",
                    "external_use",
                    "valid_until",
                    "lifecycle_status",
                    "evidence_kind",
                    "confirmed_at",
                )
                if row is None or any(row[k] != source[k] for k in fields):
                    raise RepositoryDeniedError()
                if (
                    row["valid_until"]
                    and from_iso_format(row["valid_until"]) <= get_utc_now()
                ):
                    raise RepositoryDeniedError()
            if pending["operation"] == "read_memory_pack":
                if current["consumed"]:
                    raise RepositoryDeniedError()
                db.execute(
                    "UPDATE export_grants SET consumed=1 WHERE id=?", (grant["id"],)
                )
            db.execute(
                "UPDATE gateway_audit SET outcome='admitted',metadata_json=? WHERE "
                "id=?",
                (json.dumps(metadata), request_id),
            )

    def deny(self, request_id: str, reason: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE gateway_audit SET outcome='denied',reason=? WHERE id=? AND "
                "outcome='pending'",
                (reason, request_id),
            )

    def audit(self, grant_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT "
                "id,operation,transport,created_at,outcome,reason,metadata_json FROM "
                "gateway_audit WHERE grant_id=? ORDER BY created_at DESC LIMIT ?",
                (grant_id, min(limit, 100)),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_draft(self, manifest: dict[str, Any], scope: dict[str, Any]) -> str:
        draft_id = uuid.uuid4().hex
        now = get_utc_now()
        with self.connect() as db:
            db.execute(
                "DELETE FROM gateway_pack_drafts WHERE expires_at<=?",
                (now.isoformat(),),
            )
            db.execute(
                "INSERT INTO gateway_pack_drafts VALUES(?,?,?,?)",
                (
                    draft_id,
                    json.dumps(manifest),
                    json.dumps(scope),
                    (now + timedelta(minutes=5)).isoformat(),
                ),
            )
        return draft_id

    def draft(self, draft_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM gateway_pack_drafts WHERE id=? AND expires_at>?",
                (draft_id, get_utc_now().isoformat()),
            ).fetchone()
        if row is None:
            raise RepositoryDeniedError()
        return {
            "manifest": json.loads(row["manifest_json"]),
            "scope": json.loads(row["scope_json"]),
        }
