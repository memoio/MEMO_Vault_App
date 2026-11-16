"""Local chat persistence and fixed model routing."""

from __future__ import annotations

import uuid

MODELS = {
    "qwen": ("default", "Qwen3.5-0.8B", "Default Agent"),
    "qwen-1": ("openhands", "Qwen3.5-0.8B-1", "OpenHands"),
    "qwen-2": ("swe", "Qwen3.5-0.8B-2", "SWE"),
    "qwen-3": ("agentlab", "Qwen3.5-0.8B-3", "AgentLab"),
}


def resolve_model(model: str, agent: str) -> str:
    if model not in MODELS or agent not in ("default", "openhands", "swe", "agentlab"):
        raise ValueError("未知模型或 Agent")
    return MODELS[model][0]


class AgentChatStore:
    def __init__(self, vault):
        self.vault = vault

    def initialize(self):
        with self.vault.connect() as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS agent_chat_sessions(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS agent_chat_messages(id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES agent_chat_sessions(id), role TEXT NOT NULL, content TEXT NOT NULL, model_id TEXT, agent_id TEXT, status TEXT NOT NULL, request_id TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS agent_chat_events(id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL, type TEXT NOT NULL, content TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS agent_chat_session_idx ON agent_chat_messages(session_id,created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS agent_chat_request_idx ON agent_chat_messages(session_id,request_id,role) WHERE request_id IS NOT NULL;""")
            columns = {row[1] for row in c.execute("PRAGMA table_info(agent_chat_messages)")}
            if "framework_version" not in columns:
                c.execute("ALTER TABLE agent_chat_messages ADD COLUMN framework_version TEXT")
            c.execute(
                "UPDATE agent_chat_messages SET status='failed',content='服务重启导致请求中断，请重试。' WHERE status='pending'"
            )

    def create_session(self, project_id):
        if not any(p["project_id"] == project_id for p in self.vault.list_projects()):
            raise KeyError(project_id)
        sid = uuid.uuid4().hex
        with self.vault.connect() as c:
            c.execute(
                "INSERT INTO agent_chat_sessions(id,project_id,title) VALUES(?,?,?)",
                (sid, project_id, "新对话"),
            )
        return self.get_session(sid)

    def list_sessions(self, project_id=None, q=""):
        with self.vault.connect() as c:
            return [
                dict(x)
                for x in c.execute(
                    "SELECT s.*, EXISTS(SELECT 1 FROM agent_chat_messages m WHERE m.session_id=s.id AND m.status='pending') AS generating FROM agent_chat_sessions s WHERE (? IS NULL OR s.project_id=?) AND instr(s.title,?)>0 ORDER BY s.updated_at DESC,s.rowid DESC",
                    (project_id, project_id, q),
                )
            ]

    def get_session(self, sid):
        with self.vault.connect() as c:
            row = c.execute(
                "SELECT * FROM agent_chat_sessions WHERE id=?", (sid,)
            ).fetchone()
            if row is None:
                raise KeyError(sid)
            result = dict(row)
            result["messages"] = [
                dict(x)
                for x in c.execute(
                    "SELECT * FROM agent_chat_messages WHERE session_id=? ORDER BY rowid",
                    (sid,),
                )
            ]
            for message in result["messages"]:
                message["events"] = [dict(event) for event in c.execute(
                    "SELECT type,content FROM agent_chat_events WHERE message_id=? ORDER BY id", (message["id"],)
                )]
            return result

    def add_event(self, message_id, kind, content):
        with self.vault.connect() as c:
            c.execute("INSERT INTO agent_chat_events(message_id,type,content) VALUES(?,?,?)", (message_id, kind, content[:20000]))

    def delete_session(self, sid):
        with self.vault.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute(
                "SELECT 1 FROM agent_chat_sessions WHERE id=?", (sid,)
            ).fetchone():
                raise KeyError(sid)
            if c.execute(
                "SELECT 1 FROM agent_chat_messages WHERE session_id=? AND status='pending'",
                (sid,),
            ).fetchone():
                raise ValueError("当前会话正在生成，请等待完成后再删除")
            c.execute("DELETE FROM agent_chat_events WHERE message_id IN (SELECT id FROM agent_chat_messages WHERE session_id=?)", (sid,))
            c.execute("DELETE FROM agent_chat_messages WHERE session_id=?", (sid,))
            c.execute("DELETE FROM agent_chat_sessions WHERE id=?", (sid,))

    def rename_session(self, sid, title):
        title = title.strip()
        if not title or len(title) > 100 or "\n" in title or "\r" in title:
            raise ValueError("会话名称须为 1 到 100 个字符且不能换行")
        with self.vault.connect() as c:
            updated = c.execute(
                "UPDATE agent_chat_sessions SET title=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (title, sid),
            )
            if updated.rowcount == 0:
                raise KeyError(sid)
        return self.get_session(sid)

    def begin_message(self, sid, content, model, agent, request_id):
        resolve_model(model, agent)
        if not content.strip() or len(content) > 20000:
            raise ValueError("消息为空或超过长度限制")
        with self.vault.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM agent_chat_sessions WHERE id=?", (sid,)
            ).fetchone()
            if not row:
                raise KeyError(sid)
            existing = c.execute(
                "SELECT id, model_id, agent_id FROM agent_chat_messages WHERE session_id=? AND request_id=? AND role='assistant'",
                (sid, request_id),
            ).fetchone()
            if existing:
                original = c.execute(
                    "SELECT content FROM agent_chat_messages WHERE session_id=? AND request_id=? AND role='user'",
                    (sid, request_id),
                ).fetchone()
                if (
                    existing["model_id"] != model
                    or existing["agent_id"] != agent
                    or not original
                    or original["content"] != content
                ):
                    raise ValueError("请求 ID 已用于不同内容")
                return {"assistant_id": existing["id"], "duplicate": True}
            if c.execute(
                "SELECT 1 FROM agent_chat_messages WHERE session_id=? AND status='pending'",
                (sid,),
            ).fetchone():
                raise ValueError("当前会话正在生成")
            uid, aid = uuid.uuid4().hex, uuid.uuid4().hex
            c.execute(
                "INSERT INTO agent_chat_messages(id,session_id,role,content,model_id,agent_id,status,request_id) VALUES(?,?,?,?,?,?,?,?)",
                (uid, sid, "user", content, model, agent, "completed", request_id),
            )
            c.execute(
                "INSERT INTO agent_chat_messages(id,session_id,role,content,model_id,agent_id,status,request_id) VALUES(?,?,?,?,?,?,?,?)",
                (aid, sid, "assistant", "", model, agent, "pending", request_id),
            )
            title = (
                content.strip().replace("\n", " ")[:40]
                if row["title"] == "新对话"
                else row["title"]
            )
            c.execute(
                "UPDATE agent_chat_sessions SET title=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (title, sid),
            )
        return {"assistant_id": aid, "duplicate": False}

    def finish_message(self, aid, content, failed=False, framework_version=None):
        with self.vault.connect() as c:
            c.execute(
                "UPDATE agent_chat_messages SET content=?,status=?,framework_version=? WHERE id=? AND status=?",
                (content, "failed" if failed else "completed", framework_version, aid, "pending"),
            )
