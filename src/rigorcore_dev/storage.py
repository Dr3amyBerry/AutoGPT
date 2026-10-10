"""Persistent command history, task states and incremental output for MCP polling."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from threading import RLock

TERMINAL = {"completed", "failed", "denied", "cancelled", "interrupted"}
STATUSES = TERMINAL | {"queued", "running", "approval_required", "cancel_requested"}

class TaskStore:
    def __init__(self, path: Path):
        self.lock = RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, node TEXT NOT NULL, workspace TEXT NOT NULL,
            kind TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT,
            created REAL NOT NULL, updated REAL NOT NULL,
            command TEXT NOT NULL DEFAULT '', log_bytes INTEGER NOT NULL DEFAULT 0,
            logs_truncated INTEGER NOT NULL DEFAULT 0
        )""")
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(tasks)")}
        for name, sql in [
            ("command", "TEXT NOT NULL DEFAULT ''"),
            ("log_bytes", "INTEGER NOT NULL DEFAULT 0"),
            ("logs_truncated", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            if name not in columns:
                self.conn.execute("ALTER TABLE tasks ADD COLUMN " + name + " " + sql)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS task_logs (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL, stream TEXT NOT NULL,
            text TEXT NOT NULL, created REAL NOT NULL
        )""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_recent ON tasks(created DESC)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_task_logs ON task_logs(task_id,seq)")
        self.conn.execute(
            "UPDATE tasks SET status='interrupted', updated=? WHERE status IN "
            "('running','queued','approval_required','cancel_requested')", (time.time(),)
        )
        self.conn.commit()

    def create(self, task_id: str, node: str, workspace: str, kind: str, command: str = "") -> None:
        with self.lock:
            now = time.time()
            self.conn.execute(
                "INSERT INTO tasks (id,node,workspace,kind,status,created,updated,command) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (task_id, node, workspace, kind, "queued", now, now, command),
            )
            self.conn.commit()

    def update(self, task_id: str, status: str, result=None, error=None) -> None:
        if status not in STATUSES:
            raise ValueError("invalid task status")
        with self.lock:
            old = self.get(task_id)
            if not old or old["status"] in TERMINAL:
                return
            self.conn.execute(
                "UPDATE tasks SET status=?,result=?,error=?,updated=? WHERE id=?",
                (status, json.dumps(result) if result is not None else None,
                 str(error)[:1500] if error else None, time.time(), task_id),
            )
            self.conn.commit()

    def get(self, task_id: str):
        with self.lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            data = dict(row)
            data["result"] = json.loads(data["result"]) if data["result"] else None
            return data

    @staticmethod
    def compact(entry: dict) -> dict:
        """Keep list/batch MCP responses small; get_task remains the detailed endpoint."""
        command = entry.get("command") or ""
        entry["command_truncated"] = len(command) > 400
        entry["command"] = command[:400]
        result = entry.get("result")
        if isinstance(result, dict):
            entry["result"] = {
                "exit_code": result.get("exit_code"),
                "stdout_preview": str(result.get("stdout") or "")[:300],
                "stderr_preview": str(result.get("stderr") or "")[:300],
                "truncated": bool(result.get("truncated")),
            }
        return entry

    def get_many(self, task_ids: list[str], include_results: bool = False) -> list[dict]:
        if not isinstance(task_ids, list) or not 1 <= len(task_ids) <= 100:
            raise ValueError("provide 1-100 task ids")
        return [entry if include_results else self.compact(entry)
                for task_id in task_ids if (entry := self.get(task_id)) is not None]

    def list(self, node: str = "", workspace: str = "", status: str = "",
             limit: int = 30, after_created: float = 0.0) -> list[dict]:
        if status and status not in STATUSES:
            raise ValueError("unknown status")
        limit = min(max(int(limit), 1), 100)
        query = "SELECT * FROM tasks WHERE created > ?"
        params: list = [after_created]
        for column, value in [("node", node), ("workspace", workspace), ("status", status)]:
            if value:
                query += " AND " + column + " = ?"
                params.append(value)
        query += " ORDER BY created DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self.lock:
            rows = self.conn.execute(query, params).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["result"] = json.loads(item["result"]) if item["result"] else None
                result.append(self.compact(item))
            return result

    def append_log(self, task_id: str, stream: str, chunk: str, max_log_bytes: int = 2_000_000) -> None:
        if stream not in {"stdout", "stderr"} or not isinstance(chunk, str):
            raise ValueError("invalid log event")
        with self.lock:
            row = self.conn.execute("SELECT log_bytes,status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["status"] in TERMINAL:
                return
            data = chunk.encode("utf-8")
            available = max(0, max_log_bytes - row["log_bytes"])
            fragment = data[:available].decode("utf-8", errors="ignore")
            count = len(fragment.encode("utf-8"))
            if fragment:
                self.conn.execute(
                    "INSERT INTO task_logs(task_id,stream,text,created) VALUES(?,?,?,?)",
                    (task_id, stream, fragment, time.time()),
                )
            self.conn.execute(
                "UPDATE tasks SET log_bytes=log_bytes+?,logs_truncated=CASE WHEN ? THEN 1 ELSE logs_truncated END,"
                "updated=? WHERE id=?",
                (count, int(len(data) > available), time.time(), task_id),
            )
            self.conn.commit()

    def logs(self, task_id: str, after_seq: int = 0, limit: int = 100) -> dict:
        if not self.get(task_id):
            raise ValueError("task not found")
        with self.lock:
            rows = self.conn.execute(
                "SELECT seq,stream,text,created FROM task_logs WHERE task_id=? AND seq>? "
                "ORDER BY seq LIMIT ?",
                (task_id, max(int(after_seq), 0), min(max(1, int(limit)), 200)),
            ).fetchall()
            return {"task_id": task_id, "logs": [dict(row) for row in rows],
                    "next_seq": rows[-1]["seq"] if rows else max(int(after_seq), 0)}

    def interrupt_node(self, node: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE tasks SET status='interrupted',updated=? WHERE node=? "
                "AND status NOT IN ('completed','failed','denied','cancelled','interrupted')",
                (time.time(), node),
            )
            self.conn.commit()
