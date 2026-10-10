"""Workspace file operations; shell commands have different permissions."""
from __future__ import annotations
import os
import tempfile
from pathlib import Path

class WorkspaceError(ValueError):
    pass

class Workspace:
    IGNORED = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build"}

    def __init__(self, root: str):
        path = Path(root).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise WorkspaceError("workspace not a directory")
        self.root = path

    def resolve(self, relative: str) -> Path:
        if not isinstance(relative, str) or not relative or "\x00" in relative:
            raise WorkspaceError("invalid path")
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root):
            raise WorkspaceError("path escapes workspace")
        return target

    def read(self, path: str) -> dict:
        target = self.resolve(path)
        if not target.is_file():
            raise WorkspaceError("file not found")
        if target.stat().st_size > 300_000:
            raise WorkspaceError("file too large")
        return {"path": path, "content": target.read_text(encoding="utf-8")}

    def files(self, path: str = ".", limit: int = 250) -> list[str]:
        base = self.resolve(path)
        if not base.is_dir():
            raise WorkspaceError("directory not found")
        results = []
        for root, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = [d for d in sorted(dirs) if d not in self.IGNORED and not (Path(root) / d).is_symlink()]
            for name in sorted(files):
                target = Path(root) / name
                if target.is_symlink() or not target.is_file():
                    continue
                results.append(target.relative_to(self.root).as_posix())
                if len(results) >= min(max(int(limit), 1), 2000):
                    return results
        return results

    def search(self, query: str, path: str = ".", limit: int = 50) -> list[dict]:
        if not query or len(query) > 200:
            raise WorkspaceError("invalid search")
        results = []
        for relative in self.files(path, limit=2000):
            try:
                target = self.resolve(relative)
                if target.stat().st_size > 300_000:
                    continue
                for i, line in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
                    if query.casefold() in line.casefold():
                        results.append({"path": relative, "line": i, "text": line[:350]})
                        if len(results) >= min(max(int(limit), 1), 200):
                            return results
            except (UnicodeError, OSError):
                continue
        return results

    def write(self, files: list[dict]) -> dict:
        if not isinstance(files, list) or not 1 <= len(files) <= 100:
            raise WorkspaceError("invalid number of files")
        planned, seen, total = [], set(), 0
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                raise WorkspaceError("missing file content")
            target = self.resolve(item.get("path"))
            if target in seen or target.is_dir():
                raise WorkspaceError("duplicate or directory")
            seen.add(target)
            data = item["content"].encode("utf-8")
            total += len(data)
            if len(data) > 1_000_000 or total > 5_000_000:
                raise WorkspaceError("write exceeds size limits")
            planned.append((target, data))
        history, staged = [], []
        try:
            for target, data in planned:
                target.parent.mkdir(parents=True, exist_ok=True)
                if self.resolve(str(target.relative_to(self.root))) != target:
                    raise WorkspaceError("path changed during write")
                history.append((target, target.read_bytes() if target.exists() else None))
                fd, temp = tempfile.mkstemp(prefix=".rigorcore-", dir=target.parent)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                staged.append((target, Path(temp)))
            for target, temp in staged:
                temp.replace(target)
        except Exception:
            for target, old in reversed(history):
                if old is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(old)
            raise
        finally:
            for target, temp in staged:
                temp.unlink(missing_ok=True)
        return {"written": [t.relative_to(self.root).as_posix() for t, _ in planned], "bytes": total}
