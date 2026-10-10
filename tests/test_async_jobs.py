"""Regression tests for decoupled MCP calls and long-lived shell job state."""
import asyncio
import sqlite3
import time

from rigorcore_dev.hub import Hub, Node
from rigorcore_dev.storage import TaskStore

class MemorySocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)

def test_async_shell_dispatch_returns_before_job_finishes(tmp_path):
    async def scenario():
        store = TaskStore(tmp_path / "jobs.db")
        hub = Hub("agent-token-secret", store)
        socket = MemorySocket()
        hub.nodes["vps"] = Node("vps", "VPS", ["project"], "auto", socket, time.time())
        result = await hub.request("vps", "project", "shell",
                                   {"command": "sleep 5", "timeout": 3600})
        assert result["status"] == "queued"
        assert result["command"] == "sleep 5"
        assert result["task_id"] == result["id"]
        assert socket.sent[0]["id"] == result["id"]
        assert store.get(result["id"])["status"] == "queued"
    asyncio.run(scenario())

def test_short_wait_observes_completion_without_long_hold(tmp_path):
    async def scenario():
        store = TaskStore(tmp_path / "jobs.db")
        hub = Hub("agent-token-secret", store)
        socket = MemorySocket()
        hub.nodes["n"] = Node("n", "Node", ["src"], "auto", socket, time.time())

        async def complete():
            while not socket.sent:
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.03)
            store.update(socket.sent[0]["id"], "completed", {"exit_code": 0, "stdout": "ok"})

        completing = asyncio.create_task(complete())
        result = await hub.request("n", "src", "shell",
                                   {"command": "echo ok", "timeout": 3600}, wait_seconds=1)
        await completing
        assert result["status"] == "completed"
        assert result["result"]["stdout"] == "ok"
    asyncio.run(scenario())

def test_batch_history_and_streaming_log_pagination(tmp_path):
    async def scenario():
        store = TaskStore(tmp_path / "jobs.db")
        hub = Hub("secret", store)
        socket = MemorySocket()
        hub.nodes["n"] = Node("n", "Node", ["repo"], "approve", socket, time.time())
        ids = []
        for cmd in ["pytest", "npm run build", "git status"]:
            reply = await hub.request("n", "repo", "shell", {"command": cmd})
            ids.append(reply["id"])
        assert len(socket.sent) == 3
        assert [t["command"] for t in store.list(node="n", workspace="repo")] == [
            "git status", "npm run build", "pytest"
        ]
        assert len(store.get_many(ids)) == 3
        store.update(ids[0], "running")
        store.append_log(ids[0], "stdout", "first\n")
        store.append_log(ids[0], "stderr", "warning\n")
        page1 = store.logs(ids[0], limit=1)
        page2 = store.logs(ids[0], after_seq=page1["next_seq"], limit=1)
        assert page1["logs"][0]["text"] == "first\n"
        assert page2["logs"][0]["text"] == "warning\n"
        store.update(ids[0], "completed", {"exit_code": 0})
        assert store.list(status="completed")[0]["id"] == ids[0]
        store.append_log(ids[0], "stdout", "should be ignored")
        assert len(store.logs(ids[0])["logs"]) == 2
    asyncio.run(scenario())

def test_disconnect_does_not_fail_another_nodes_waiters(tmp_path):
    async def scenario():
        store = TaskStore(tmp_path / "jobs.db")
        hub = Hub("agent-token", store)
        async def noop():
            await asyncio.sleep(100)
        own = asyncio.get_running_loop().create_future()
        other = asyncio.get_running_loop().create_future()
        hub.waiting["own"] = ("a", own)
        hub.waiting["other"] = ("b", other)
        # A client's disconnection must not fail requests in a different workspace.
        for rid, (owner, future) in hub.waiting.items():
            if owner == "a" and not future.done():
                future.set_exception(ConnectionError("agent disconnected"))
        assert not other.done()
        assert isinstance(own.exception(), ConnectionError)
        other.cancel()
    asyncio.run(scenario())

def test_legacy_database_migration_preserves_history(tmp_path):
    db = tmp_path / "legacy.db"
    connection = sqlite3.connect(db)
    connection.execute("""CREATE TABLE tasks (
        id TEXT PRIMARY KEY, node TEXT NOT NULL, workspace TEXT NOT NULL,
        kind TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT,
        created REAL NOT NULL, updated REAL NOT NULL)""")
    connection.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)",
                       ("old", "n", "repo", "shell", "completed", '{"exit_code":0}', None, 1, 2))
    connection.commit()
    connection.close()
    store = TaskStore(db)
    assert store.get("old")["result"]["exit_code"] == 0
    assert store.get("old")["command"] == ""
    store.create("new", "n", "repo", "shell", "echo hello")
    assert store.get("new")["command"] == "echo hello"

def test_log_quota_is_bounded(tmp_path):
    store = TaskStore(tmp_path / "task.db")
    store.create("job", "n", "repo", "shell", "command")
    store.update("job", "running")
    store.append_log("job", "stdout", "abcdefgh", max_log_bytes=5)
    assert store.get("job")["logs_truncated"] == 1
    assert store.logs("job")["logs"][0]["text"] == "abcde"


def test_fifty_concurrent_queued_commands_have_distinct_ids(tmp_path):
    async def scenario():
        store = TaskStore(tmp_path / "jobs.db")
        hub = Hub("agent-secret", store)
        socket = MemorySocket()
        hub.nodes["heavy"] = Node("heavy", "Heavy VPS", ["project"], "auto", socket, time.time())
        jobs = await asyncio.gather(*(
            hub.request("heavy", "project", "shell",
                        {"command": "echo " + str(i), "timeout": 3600})
            for i in range(50)
        ))
        assert len(jobs) == len({item["task_id"] for item in jobs}) == 50
        assert len(socket.sent) == 50
        assert len(store.list(node="heavy", limit=100)) == 50
        assert len(store.get_many([item["task_id"] for item in jobs])) == 50
    asyncio.run(scenario())


def test_many_task_summaries_avoid_large_mcp_payloads(tmp_path):
    store = TaskStore(tmp_path / "tasks.db")
    store.create("job", "n", "repo", "shell", "echo " + "x" * 1000)
    store.update("job", "completed", {"exit_code": 0, "stdout": "a" * 5000,
                                     "stderr": "b" * 4000, "truncated": False})
    summary = store.list(node="n")[0]
    assert summary["command_truncated"] is True
    assert len(summary["command"]) == 400
    assert len(summary["result"]["stdout_preview"]) == 300
    assert "stdout" not in summary["result"]
    compact_batch = store.get_many(["job"])[0]
    assert len(compact_batch["result"]["stderr_preview"]) == 300
    detailed_batch = store.get_many(["job"], include_results=True)[0]
    assert len(detailed_batch["result"]["stdout"]) == 5000
    assert len(store.get("job")["command"]) > 400
