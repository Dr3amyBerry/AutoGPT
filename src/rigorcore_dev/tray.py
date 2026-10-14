"""Windows notification-area UI with an explicit disconnect and manual approvals."""
from __future__ import annotations
import asyncio
import threading
import time
from .agent import Agent
from .config import AgentConfig

def start_tray(path: str):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise SystemExit("Install Windows extras: pip install '.[windows]'") from exc
    config = AgentConfig.load(path)
    agent = Agent(config)
    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=_loop_worker, args=(loop, agent), daemon=True)
    worker.start()

    def submit(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def first_pending():
        return next(iter(agent.pending), None)

    def choose(allow: bool):
        task_id = first_pending()
        if task_id is None:
            return
        if allow:
            # Always show the COMPLETE command before approving on Windows.
            # MessageBox buttons: MB_YESNO | MB_ICONWARNING; IDYES = 6.
            import ctypes
            command = agent.pending[task_id][0]
            decision = ctypes.windll.user32.MessageBoxW(
                None, "Run this shell command?\\n\\n" + command,
                "RigorCore Dev - Shell approval", 0x00000004 | 0x00000030)
            allow = (decision == 6)
        submit(agent.approve(task_id, allow))

    def change_workspace():
        def _pick():
            import ctypes
            import ctypes.wintypes
            import json
            from pathlib import Path

            # The modern shell picker needs STA COM on this worker thread.
            ole32 = ctypes.windll.ole32
            shell32 = ctypes.windll.shell32
            ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            ole32.CoInitializeEx.restype = ctypes.c_long
            ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ole32.CoTaskMemFree.restype = None
            ole32.CoUninitialize.argtypes = []
            ole32.CoUninitialize.restype = None

            class BROWSEINFO(ctypes.Structure):
                _fields_ = [
                    ("hwndOwner", ctypes.wintypes.HWND),
                    ("pidlRoot", ctypes.c_void_p),
                    ("pszDisplayName", ctypes.c_wchar_p),
                    ("lpszTitle", ctypes.c_wchar_p),
                    ("ulFlags", ctypes.c_uint),
                    ("lpfn", ctypes.c_void_p),
                    ("lParam", ctypes.c_ssize_t),
                    ("iImage", ctypes.c_int),
                ]

            browse = shell32.SHBrowseForFolderW
            browse.argtypes = [ctypes.POINTER(BROWSEINFO)]
            browse.restype = ctypes.c_void_p
            get_path = shell32.SHGetPathFromIDListW
            get_path.argtypes = [ctypes.c_void_p, ctypes.wintypes.LPWSTR]
            get_path.restype = ctypes.wintypes.BOOL
            result = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
            if result < 0:
                raise OSError(f"Cannot initialize folder picker COM: {result:#x}")
            path_buf = ctypes.create_unicode_buffer(260)
            pidl = None
            try:
                display = ctypes.create_unicode_buffer(260)
                bi = BROWSEINFO()
                bi.pszDisplayName = ctypes.cast(display, ctypes.c_wchar_p)
                bi.lpszTitle = "Seleccionar nueva carpeta de trabajo para RigorCore Dev"
                bi.ulFlags = 0x0001 | 0x0010 | 0x0040
                pidl = browse(ctypes.byref(bi))
                if not pidl or not get_path(pidl, path_buf):
                    return
            finally:
                if pidl:
                    ole32.CoTaskMemFree(pidl)
                ole32.CoUninitialize()

            selected = path_buf.value
            if not selected:
                return

            selected_path = str(Path(selected).resolve())

            # Preserve the existing workspace key name; fall back to "demo".
            existing_key = next(iter(agent.config.workspaces), "demo")
            new_spaces = {existing_key: selected_path}

            submit(agent.update_workspaces(new_spaces))

            # Persist to agent.json
            try:
                cfg_file = Path(path).expanduser()
                data = json.loads(cfg_file.read_text(encoding="utf-8-sig"))
                data["workspaces"] = new_spaces
                cfg_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except Exception:
                pass

        threading.Thread(target=_pick, daemon=True).start()

    def set_mode(mode: str):
        submit(agent.change_mode(mode))

    def toggle_connection():
        submit(agent.set_enabled(not agent.enabled))

    def stop(icon):
        submit(agent.stop())
        icon.stop()

    def current_ws_info():
        ws = agent.config.workspaces.get("demo") or next(iter(agent.config.workspaces.values()), "")
        if len(ws) > 35:
            ws = "..." + ws[-32:]
        return "Workspace: " + ws

    image = Image.new("RGBA", (64, 64), (22, 95, 161, 255))
    pen = ImageDraw.Draw(image)
    pen.rounded_rectangle((8, 10, 56, 54), radius=9, outline="white", width=4)
    pen.line((19, 25, 28, 32, 19, 40), fill="white", width=4)
    pen.line((33, 41, 46, 41), fill="white", width=4)

    menu = pystray.Menu(
        pystray.MenuItem(lambda _: "RigorCore Dev — " + ("Connected" if agent.connected else "Disconnected"),
                         None, enabled=False),
        pystray.MenuItem(lambda _: current_ws_info(), None, enabled=False),
        pystray.MenuItem("📁 Cambiar carpeta de trabajo...", lambda icon, item: change_workspace()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda _: "Pending shell approvals: " + str(len(agent.pending)),
                         None, enabled=False),
        pystray.MenuItem("Review and approve next shell command", lambda icon, item: choose(True),
                         enabled=lambda _: bool(agent.pending)),
        pystray.MenuItem("Deny next shell command", lambda icon, item: choose(False),
                         enabled=lambda _: bool(agent.pending)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Approve each shell command", lambda icon, item: set_mode("approve"),
                         checked=lambda _: agent.mode == "approve", radio=True),
        pystray.MenuItem("Full Auto", lambda icon, item: set_mode("auto"),
                         checked=lambda _: agent.mode == "auto", radio=True),
        pystray.MenuItem(lambda _: "Disconnect" if agent.enabled else "Reconnect",
                         lambda icon, item: toggle_connection()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit and disconnect", lambda icon, item: stop(icon)),
    )
    icon = pystray.Icon("rigorcore-dev", image, "RigorCore Dev", menu)
    done = threading.Event()

    def refresh():
        while not done.wait(1.5):
            try:
                icon.update_menu()
            except Exception:
                break

    updater = threading.Thread(target=refresh, daemon=True)
    updater.start()
    try:
        icon.run()
    finally:
        done.set()
        submit(agent.stop())
        loop.call_soon_threadsafe(loop.stop)

def _loop_worker(loop: asyncio.AbstractEventLoop, agent: Agent):
    asyncio.set_event_loop(loop)
    loop.create_task(agent.run())
    loop.run_forever()
    tasks = asyncio.all_tasks(loop)
    for task in tasks:
        task.cancel()
    loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
    loop.close()
