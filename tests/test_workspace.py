import pytest
from rigorcore_dev.workspace import Workspace, WorkspaceError

def test_write_read_list_search(tmp_path):
    workspace = Workspace(str(tmp_path))
    result = workspace.write([
        {"path": "src/a.py", "content": "hello = 1\n"},
        {"path": "README.md", "content": "hello world\n"},
    ])
    assert result["bytes"] > 0
    assert workspace.read("src/a.py")["content"] == "hello = 1\n"
    assert "src/a.py" in workspace.files()
    assert workspace.search("hello")[0]["line"] == 1

def test_escape_rejected(tmp_path):
    workspace = Workspace(str(tmp_path))
    with pytest.raises(WorkspaceError):
        workspace.read("../outside")
    with pytest.raises(WorkspaceError):
        workspace.write([{"path": "/etc/passwd", "content": "bad"}])

def test_symlink_escape_rejected(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (tmp_path / "shortcut").symlink_to(outside, target_is_directory=True)
    workspace = Workspace(str(tmp_path))
    with pytest.raises(WorkspaceError):
        workspace.write([{"path": "shortcut/forbidden", "content": "test"}])
    assert not (outside / "forbidden").exists()

def test_duplicate_write_is_atomic(tmp_path):
    workspace = Workspace(str(tmp_path))
    with pytest.raises(WorkspaceError):
        workspace.write([{"path": "x", "content": "a"}, {"path": "x", "content": "b"}])
    assert not (tmp_path / "x").exists()

def test_too_many_files_rejected(tmp_path):
    workspace = Workspace(str(tmp_path))
    with pytest.raises(WorkspaceError):
        workspace.write([{"path": str(i), "content": "x"} for i in range(101)])
