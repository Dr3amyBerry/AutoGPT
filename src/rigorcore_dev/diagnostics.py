"""Bounded developer diagnostics. Subprocesses use argument arrays, never shells."""
from __future__ import annotations

import http.client
import ipaddress
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


def _psutil():
    try:
        import psutil
        return psutil
    except ImportError as exc:
        raise RuntimeError("Install diagnostics dependencies: pip install '.[diagnostics]'") from exc


def inspect_process(name: str = "", limit: int = 40):
    psutil = _psutil()
    if not 1 <= limit <= 100 or len(name) > 120:
        raise ValueError("invalid process filter")
    items = []
    for proc in psutil.process_iter(["pid", "name", "status", "memory_info", "cpu_percent"]):
        try:
            data = proc.info
            if name and name.casefold() not in str(data["name"]).casefold():
                continue
            memory = data.get("memory_info")
            items.append({"pid": data["pid"], "name": data["name"],
                          "status": data.get("status"), "rss_bytes": memory.rss if memory else None,
                          "cpu_percent": data.get("cpu_percent")})
            if len(items) >= limit:
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return items


def debug_application(pid: int):
    psutil = _psutil()
    if not 1 <= pid <= 2**31 - 1:
        raise ValueError("invalid pid")
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            return {"pid": pid, "name": proc.name(), "status": proc.status(),
                    "threads": proc.num_threads(), "memory": proc.memory_info()._asdict(),
                    "cpu_percent": proc.cpu_percent(interval=0.15),
                    "created": proc.create_time()}
    except psutil.NoSuchProcess as exc:
        raise ValueError("process not found") from exc
    except psutil.AccessDenied as exc:
        raise PermissionError("process access denied") from exc


def profile_cpu(pid: int = 0, seconds: float = 0.2):
    psutil = _psutil()
    if not 0.1 <= seconds <= 2:
        raise ValueError("sampling must take 0.1..2 seconds")
    if pid:
        proc = psutil.Process(pid)
        return {"pid": pid, "cpu_percent": proc.cpu_percent(interval=seconds)}
    return {"system_cpu_percent": psutil.cpu_percent(interval=seconds),
            "per_cpu_percent": psutil.cpu_percent(interval=None, percpu=True)}


def profile_memory(pid: int = 0):
    psutil = _psutil()
    if pid:
        proc = psutil.Process(pid)
        return {"pid": pid, "rss_bytes": proc.memory_info().rss,
                "memory_percent": proc.memory_percent()}
    return psutil.virtual_memory()._asdict()


def _run(argv: list[str], cwd: str, timeout: int = 40, limit: int = 25000):
    if not argv or not 1 <= timeout <= 180:
        raise ValueError("invalid command")
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=timeout,
                              creationflags=0x08000000 if os.name == "nt" else 0,
                              check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError("command could not finish: " + type(exc).__name__) from exc
    stdout = done.stdout.decode("utf-8", errors="replace")
    stderr = done.stderr.decode("utf-8", errors="replace")
    return {"exit_code": done.returncode, "stdout": stdout[:limit],
            "stderr": stderr[:limit], "truncated": len(stdout) > limit or len(stderr) > limit}


def inspect_gpu():
    if shutil.which("nvidia-smi"):
        return _run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                     "--format=csv,noheader,nounits"], os.getcwd(), timeout=12, limit=4000)
    if os.name == "nt":
        return _run(["powershell", "-NoProfile", "-Command",
                     "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,AdapterRAM | ConvertTo-Json"],
                    os.getcwd(), timeout=20, limit=4000)
    return {"available": False, "reason": "no supported GPU diagnostics provider"}


def inspect_ports(limit: int = 80):
    psutil = _psutil()
    if not 1 <= limit <= 200:
        raise ValueError("limit out of range")
    rows = []
    for conn in psutil.net_connections(kind="inet"):
        rows.append({"local": str(conn.laddr), "remote": str(conn.raddr) if conn.raddr else "",
                     "status": conn.status, "pid": conn.pid})
        if len(rows) >= limit:
            break
    return rows


def inspect_requests(limit: int = 80):
    return {"connections": inspect_ports(limit),
            "note": "Socket metadata only; does not intercept or decrypt HTTP request bodies."}


def test_endpoint(url: str):
    if len(url) > 512:
        raise ValueError("URL too long")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        raise ValueError("only http(s) endpoints without credentials supported")
    host = (parsed.hostname or "").lower()
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    if not local:
        raise PermissionError("endpoint probes are restricted to loopback addresses")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("port out of range")
    conn_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = conn_type(host, port, timeout=6)
    try:
        conn.request("GET", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""))
        response = conn.getresponse()
        body = response.read(1200)
        return {"status": response.status, "reason": response.reason,
                "body_preview": body.decode("utf-8", errors="replace")[:1000],
                "content_type": response.getheader("Content-Type", "")}
    finally:
        conn.close()


def inspect_git(root: str):
    branch = _run(["git", "branch", "--show-current"], root, timeout=10, limit=1000)
    status = _run(["git", "status", "--short"], root, timeout=10)
    latest = _run(["git", "log", "-5", "--format=%h %s"], root, timeout=10, limit=3000)
    return {"branch": branch, "status": status, "recent_commits": latest}


def inspect_dependencies(root: str):
    p = Path(root)
    manifests = [x for x in ["pyproject.toml", "requirements.txt", "package.json",
                             "gradle/libs.versions.toml", "build.gradle.kts", "Cargo.toml"]
                 if (p / x).is_file()]
    return {"manifests": manifests, "hint": "Dependency manifests only; inspect files with read_file."}


def build_project(root: str, timeout: int = 180):
    p = Path(root)
    if (p / "gradlew.bat").is_file() and os.name == "nt":
        argv = [str(p / "gradlew.bat"), "assembleDebug", "--no-daemon"]
    elif (p / "gradlew").is_file() and os.name != "nt":
        argv = ["sh", str(p / "gradlew"), "assembleDebug", "--no-daemon"]
    elif (p / "package.json").is_file() and (p / "node_modules").exists():
        argv = ["npm.cmd" if os.name == "nt" else "npm", "run", "build"]
    elif (p / "pyproject.toml").is_file():
        argv = [sys.executable, "-m", "compileall", "-q", "src"]
    else:
        raise ValueError("no supported build detected; use run_shell explicitly")
    return _run(argv, root, timeout=min(timeout, 180))


def run_tests(root: str, timeout: int = 180):
    p = Path(root)
    if (p / "tests").is_dir() and ((p / "pyproject.toml").is_file() or (p / "pytest.ini").is_file()):
        argv = [sys.executable, "-m", "pytest", "-q"]
    elif (p / "package.json").is_file() and (p / "node_modules").exists():
        argv = ["npm.cmd" if os.name == "nt" else "npm", "test", "--", "--runInBand"]
    else:
        raise ValueError("no supported test runner detected; use run_shell explicitly")
    return _run(argv, root, timeout=min(timeout, 180))


def get_crash_logs():
    if os.name != "nt":
        return {"note": "For Linux use journalctl or application log files via run_shell."}
    return _run(["powershell", "-NoProfile", "-Command",
                 "Get-WinEvent -FilterHashtable @{LogName='Application'; Level=2} -MaxEvents 12 -ErrorAction SilentlyContinue | Select-Object TimeCreated,ProviderName,Id,Message | ConvertTo-Json -Depth 3"],
                os.getcwd(), timeout=20, limit=14000)


def capture_test_failure():
    from .desktop import capture_screen
    result = capture_screen(0)
    result["note"] = "Visual snapshot only; combine with run_tests and get_crash_logs."
    return result
