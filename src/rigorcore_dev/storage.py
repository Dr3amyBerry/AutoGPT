"""SQLite-backed task states so clients can poll after chat requests finish."""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path

TERMINAL = {"completed", "failed", "denied", "cancelled", "interrupted"}

class TaskStore:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, node TEXT NOT NULL, workspace TEXT NOT NULL,
            kind TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT,
            created REAL NOT NULL, updated REAL NOT NULL
        )""")
        self.conn.execute("UPDATE tasks SET status='interrupted', updated=? WHERE status IN ('running','queued','approval_required','cancel_requested')", (time.time(),))
        self.conn.commit()

    def create(self, task_id: str, node: str, workspace: str, kind: str) -> None:
        now = time.time()
        self.conn.execute(
            "INSERT INTO tasks (id,node,workspace,kind,status,created,updated) VALUES (?,?,?,?,?,?,?)",
            (task_id, node, workspace, kind, "queued", now, now),
        )
        self.conn.commit()

    def update(self, task_id: str, status: str, result=None, error=None) -> None:
        if status not in TERMINAL | {"queued", "running", "approval_required", "cancel_requested"}:
            raise ValueError("invalid task status")
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
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["result"] = json.loads(data["result"]) if data["result"] else None
        return data

    def interrupt_node(self, node: str) -> None:
        self.conn.execute(
            "UPDATE tasks SET status='interrupted',updated=? WHERE node=? AND status NOT IN ('completed','failed','denied','cancelled','interrupted')",
            (time.time(), node),
        )
        self.conn.commit()
