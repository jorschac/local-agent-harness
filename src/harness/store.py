from __future__ import annotations
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from .errors import HarnessError

TERMINAL = {"completed", "incomplete", "failed", "cancelled", "timed_out", "interrupted"}


def new_id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


class Store:
    def __init__(self, path: Path):
        self.connection = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript('''
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,title TEXT,status TEXT NOT NULL,created_at REAL NOT NULL,closed_at REAL);
        CREATE TABLE IF NOT EXISTS messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT NOT NULL,run_id TEXT NOT NULL,message TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,session_id TEXT NOT NULL,status TEXT NOT NULL,created_at REAL NOT NULL,started_at REAL,finished_at REAL,input TEXT NOT NULL,result TEXT,error TEXT,idempotency_key TEXT UNIQUE,fingerprint TEXT NOT NULL,tool_calls INTEGER NOT NULL DEFAULT 0,cancel_requested INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,type TEXT NOT NULL,data TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS intents(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,tool_call_id TEXT,name TEXT NOT NULL,arguments TEXT NOT NULL,status TEXT NOT NULL,result TEXT);
        CREATE INDEX IF NOT EXISTS events_run ON events(run_id,seq);
        CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id,seq);
        ''')
        if "cost" not in {row[1] for row in self.connection.execute("PRAGMA table_info(runs)")}:
            self.connection.execute("ALTER TABLE runs ADD COLUMN cost REAL")
        self.connection.commit()
        for sibling in (Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            if sibling.exists():
                sibling.chmod(0o600)

    def recover(self):
        rows = self.connection.execute("SELECT id,kind,session_id FROM runs WHERE status NOT IN ('completed','incomplete','failed','cancelled','timed_out','interrupted')").fetchall()
        for row in rows:
            self.update_run(row["id"], status="interrupted", finished_at=time.time(), error={"error": {"message": "Owner process stopped before completion; side effects may have occurred", "type": "server_error", "code": "owner_interrupted"}})
            self.event(row["id"], "run.interrupted", {"execution": "unconfirmed"})
            if row["kind"] == "chat":
                self.block_session(row["session_id"])
        # A pending intent was never executed. A started intent without a result is UNKNOWN,
        # not a replay request. Recovery never invokes plugin code or a model.
        self.connection.execute("UPDATE intents SET status='not_started' WHERE status='pending'")
        self.connection.execute("UPDATE intents SET status='unknown' WHERE status='started' AND result IS NULL")
        self.connection.commit()
        for row in self.connection.execute("SELECT DISTINCT r.session_id FROM runs r JOIN intents i ON i.run_id=r.id WHERE r.kind='chat' AND i.status IN ('unknown','unconfirmed')").fetchall():
            self.block_session(row[0])
        for session in self.sessions():
            self.block_if_unpaired(session["id"])

    def create_session(self, title: str | None = None) -> dict:
        sid = new_id("session")
        with self.connection:
            self.connection.execute("INSERT INTO sessions VALUES(?,?,?,?,NULL)", (sid, title, "open", time.time()))
        return self.session(sid)

    def session(self, sid: str) -> dict:
        row = self.connection.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise HarnessError("Session not found", "session_not_found", 404)
        return dict(row)

    def sessions(self) -> list:
        return [dict(row) for row in self.connection.execute("SELECT * FROM sessions ORDER BY created_at DESC")]

    def close_session(self, sid: str) -> dict:
        self.session(sid)
        with self.connection:
            self.connection.execute("UPDATE sessions SET status='closed',closed_at=? WHERE id=?", (time.time(), sid))
        return self.session(sid)

    def messages(self, sid: str) -> list:
        self.session(sid)
        return [json.loads(row[0]) for row in self.connection.execute("SELECT message FROM messages WHERE session_id=? ORDER BY seq", (sid,))]

    def commit_messages(self, sid: str, rid: str, messages: list):
        with self.connection:
            self.connection.executemany("INSERT INTO messages(session_id,run_id,message) VALUES(?,?,?)", [(sid, rid, json.dumps(message, ensure_ascii=False)) for message in messages])

    def finish_tool(self, sid: str, rid: str, intent_id: int, result: str, call_id: str | None):
        with self.connection:
            self.connection.execute("UPDATE intents SET status='finished',result=? WHERE id=?", (result, intent_id))
            if call_id is not None:
                message = {"role": "tool", "tool_call_id": call_id, "content": result}
                self.connection.execute("INSERT INTO messages(session_id,run_id,message) VALUES(?,?,?)", (sid, rid, json.dumps(message, ensure_ascii=False)))

    def block_session(self, sid: str, status: str = "interrupted"):
        with self.connection:
            self.connection.execute("UPDATE sessions SET status=? WHERE id=? AND status='open'", (status, sid))

    def block_if_unpaired(self, sid: str):
        pending = []
        for message in self.messages(sid):
            if message.get("role") == "tool":
                if not pending or message.get("tool_call_id") != pending.pop(0):
                    self.block_session(sid)
                    return
            else:
                if pending:
                    self.block_session(sid)
                    return
                if message.get("role") == "assistant":
                    pending = [call["id"] for call in message.get("tool_calls", [])]
        if pending:
            self.block_session(sid)

    def complete_chat(self, sid: str, rid: str, messages: list, result: dict, *, persisted=False):
        # Compatibility for callers that still supply all history; the loop now journals
        # incrementally and sets persisted=True to avoid duplicate messages.
        status = "incomplete" if result["choices"][0]["finish_reason"] == "length" else "completed"
        with self.connection:
            if not persisted:
                self.connection.executemany("INSERT INTO messages(session_id,run_id,message) VALUES(?,?,?)", [(sid, rid, json.dumps(message, ensure_ascii=False)) for message in messages])
            self.connection.execute("UPDATE runs SET status=?,result=?,finished_at=? WHERE id=?", (status, json.dumps(result, ensure_ascii=False), time.time(), rid))
            if status == "incomplete":
                self.connection.execute("UPDATE sessions SET status='incomplete' WHERE id=? AND status='open'", (sid,))

    def idempotent(self, key: str, fingerprint: str) -> dict | None:
        row = self.connection.execute("SELECT id,fingerprint FROM runs WHERE idempotency_key=?", (key,)).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise HarnessError("Idempotency key was used for a different request", "idempotency_conflict", 409)
        return self.run(row["id"])

    def create_run(self, rid: str, kind: str, sid: str, body: dict, fingerprint: str, key: str | None):
        with self.connection:
            self.connection.execute("INSERT INTO runs(id,kind,session_id,status,created_at,input,fingerprint,idempotency_key) VALUES(?,?,?,'queued',?,?,?,?)", (rid, kind, sid, time.time(), json.dumps(body), fingerprint, key))
        self.event(rid, "run.queued", {"status": "queued", "session_id": sid, "kind": kind})

    def run(self, rid: str) -> dict:
        row = self.connection.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise HarnessError("Run not found", "run_not_found", 404)
        result = dict(row)
        for key in ("input", "result", "error"):
            if result[key] is not None:
                result[key] = json.loads(result[key])
        result["run_id"] = result["id"]
        result["cancel_requested"] = bool(result["cancel_requested"])
        result["events_url"] = "/api/runs/" + rid + "/events"
        result["status_url"] = "/api/runs/" + rid
        return result

    def runs(self) -> list:
        return [self.run(row[0]) for row in self.connection.execute("SELECT id FROM runs ORDER BY created_at DESC")]

    def update_run(self, rid: str, **fields):
        allowed = {"status", "started_at", "finished_at", "result", "error", "tool_calls", "cancel_requested", "cost"}
        if not fields or set(fields) - allowed:
            raise ValueError("Invalid run update")
        values = [json.dumps(value, ensure_ascii=False) if key in ("result", "error") and value is not None else value for key, value in fields.items()]
        with self.connection:
            self.connection.execute("UPDATE runs SET " + ",".join(key + "=?" for key in fields) + " WHERE id=?", (*values, rid))

    def event(self, rid: str, event_type: str, data: dict) -> int:
        with self.connection:
            cursor = self.connection.execute("INSERT INTO events(run_id,type,data,created_at) VALUES(?,?,?,?)", (rid, event_type, json.dumps(data, ensure_ascii=False, allow_nan=False), time.time()))
        return cursor.lastrowid

    def events(self, rid: str, after: int = 0) -> list:
        return [{"id": row["seq"], "run_id": row["run_id"], "type": row["type"], "data": json.loads(row["data"]), "created_at": row["created_at"]} for row in self.connection.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (rid, after))]

    def intent(self, rid: str, call_id: str, name: str, args: dict) -> int:
        with self.connection:
            cursor = self.connection.execute("INSERT INTO intents(run_id,tool_call_id,name,arguments,status) VALUES(?,?,?,?,'pending')", (rid, call_id, name, json.dumps(args)))
        return cursor.lastrowid

    def update_intent(self, intent_id: int, status: str, result: str | None = None):
        with self.connection:
            self.connection.execute("UPDATE intents SET status=?,result=? WHERE id=?", (status, result, intent_id))

    def close(self):
        self.connection.close()
