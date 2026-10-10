from rigorcore_dev.storage import TaskStore

def test_status_lifecycle(tmp_path):
    store = TaskStore(tmp_path / "task.db")
    store.create("a", "node1", "repo", "shell")
    assert store.get("a")["status"] == "queued"
    store.update("a", "running")
    store.update("a", "completed", result={"exit_code": 0})
    assert store.get("a")["result"]["exit_code"] == 0
    store.update("a", "failed", error="must not override completed")
    assert store.get("a")["status"] == "completed"

def test_restart_marks_in_flight_interrupted(tmp_path):
    db = tmp_path / "task.db"
    store = TaskStore(db)
    store.create("a", "node1", "repo", "shell")
    store.conn.close()
    restored = TaskStore(db)
    assert restored.get("a")["status"] == "interrupted"
