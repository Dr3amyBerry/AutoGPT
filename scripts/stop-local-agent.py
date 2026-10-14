"""Stop this installation's tray, including the venv launcher child."""
from pathlib import Path
import psutil

root = Path(__file__).resolve().parents[1]
matched = []
for process in psutil.process_iter():
    try:
        if process.name().lower() != "pythonw.exe":
            continue
        args = process.cmdline()
        if "rigorcore_dev.cli" not in args or "tray" not in args:
            continue
        if Path(process.cwd()).resolve() != root:
            continue
        matched.append(process)
    except psutil.NoSuchProcess:
        continue
    except psutil.AccessDenied:
        continue
for process in matched:
    try:
        process.terminate()
    except psutil.NoSuchProcess:
        pass
_, alive = psutil.wait_procs(matched, timeout=10)
if alive:
    raise RuntimeError("A previous agent is still running")
print(f"Stopped {len(matched)} local agent processes")
