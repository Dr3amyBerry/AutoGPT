"""Explicit, bounded Windows desktop capture and GUI automation primitives."""
from __future__ import annotations

import base64
import ctypes
import io
import os
import time
from typing import Any

MAX_MEDIA = 4_000_000


def _windows():
    if os.name != "nt":
        raise RuntimeError("Windows GUI functions require a Windows interactive session")


def _image_result(image, fmt="PNG", *, max_edge=1800):
    from PIL import Image
    image = image.convert("RGB")
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format=fmt, optimize=True)
    data = output.getvalue()
    if len(data) > MAX_MEDIA:
        # A predictable wire-size ceiling keeps the WebSocket and MCP response bounded.
        image.thumbnail((1100, 1100), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format=fmt, optimize=True)
        data = output.getvalue()
    if len(data) > MAX_MEDIA:
        raise ValueError("capture too large; select a smaller window or display")
    return {"mime_type": "image/" + fmt.lower(), "base64": base64.b64encode(data).decode(),
            "width": image.width, "height": image.height, "bytes": len(data)}


def list_monitors():
    _windows()
    try:
        import mss
    except ImportError as exc:
        raise RuntimeError("Install desktop dependencies: pip install '.[desktop]'") from exc
    with mss.mss() as sct:
        return [{"index": idx, "left": m["left"], "top": m["top"],
                 "width": m["width"], "height": m["height"]}
                for idx, m in enumerate(sct.monitors) if idx > 0]


def capture_screen(monitor: int = 1):
    _windows()
    import mss
    from PIL import Image
    with mss.mss() as sct:
        if not 0 <= monitor < len(sct.monitors):
            raise ValueError("monitor out of range (0 is the entire virtual desktop)")
        shot = sct.grab(sct.monitors[monitor])
        img = Image.frombytes("RGB", shot.size, shot.rgb)
        result = _image_result(img)
        result["monitor"] = monitor
        return result


def _u32():
    _windows()
    return ctypes.windll.user32


def list_windows(limit: int = 100):
    _windows()
    if not 1 <= limit <= 200:
        raise ValueError("limit must be 1..200")
    from ctypes import wintypes
    user32 = _u32()
    windows = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or not user32.GetWindowTextLengthW(hwnd):
            return True
        length = min(512, user32.GetWindowTextLengthW(hwnd) + 1)
        title = ctypes.create_unicode_buffer(length)
        user32.GetWindowTextW(hwnd, title, length)
        rect = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width > 0 and height > 0:
                windows.append({"hwnd": int(hwnd), "title": title.value,
                                "left": rect.left, "top": rect.top, "width": width,
                                "height": height, "minimized": bool(user32.IsIconic(hwnd))})
        return len(windows) < limit

    callback = callback_type(visit)
    user32.EnumWindows(callback, 0)
    return windows


def _window_rect(hwnd: int):
    _windows()
    from ctypes import wintypes
    if not isinstance(hwnd, int) or hwnd <= 0 or not _u32().IsWindow(hwnd):
        raise ValueError("invalid or closed window handle")
    rect = wintypes.RECT()
    if not _u32().GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("GetWindowRect failed")
    return rect


def capture_window(hwnd: int):
    _windows()
    from PIL import ImageGrab
    rect = _window_rect(hwnd)
    if _u32().IsIconic(hwnd):
        raise ValueError("window is minimized; restore it before capturing")
    if rect.right <= rect.left or rect.bottom <= rect.top:
        raise ValueError("window has invalid bounds")
    img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True)
    result = _image_result(img)
    result.update({"hwnd": hwnd, "warning": "Screen-region capture; overlapping windows may appear."})
    return result


def focus_window(hwnd: int):
    _window_rect(hwnd)
    user32 = _u32()
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if not user32.SetForegroundWindow(hwnd):
        raise RuntimeError("Windows denied foreground activation")
    return {"focused": hwnd}


def resize_window(hwnd: int, left: int, top: int, width: int, height: int):
    _window_rect(hwnd)
    if not 100 <= width <= 10000 or not 100 <= height <= 10000:
        raise ValueError("window dimensions out of range")
    if not -30000 <= left <= 30000 or not -30000 <= top <= 30000:
        raise ValueError("window position out of range")
    if not _u32().MoveWindow(hwnd, left, top, width, height, True):
        raise RuntimeError("MoveWindow failed")
    return {"hwnd": hwnd, "left": left, "top": top, "width": width, "height": height}


def inspect_ui(hwnd: int, limit: int = 100):
    _window_rect(hwnd)
    if not 1 <= limit <= 250:
        raise ValueError("limit out of range")
    try:
        from pywinauto import Application
        app = Application(backend="uia").connect(handle=hwnd)
        top = app.window(handle=hwnd).wrapper_object()
        items = []
        for element in [top] + top.descendants():
            if len(items) >= limit:
                break
            try:
                info = element.element_info
                rect = element.rectangle()
                items.append({"name": str(info.name)[:180], "type": str(info.control_type),
                              "automation_id": str(info.automation_id)[:100],
                              "rect": [rect.left, rect.top, rect.right, rect.bottom],
                              "enabled": bool(element.is_enabled())})
            except Exception:
                continue
        return items
    except ImportError as exc:
        raise RuntimeError("Install desktop dependencies: pip install '.[desktop]'") from exc


def click_element(hwnd: int, name: str, control_type: str = ""):
    _window_rect(hwnd)
    if not name or len(name) > 180:
        raise ValueError("provide an accessible element name")
    from pywinauto import Application
    top = Application(backend="uia").connect(handle=hwnd).window(handle=hwnd)
    matches = [el for el in top.descendants()
               if el.element_info.name == name and
               (not control_type or el.element_info.control_type == control_type)]
    if len(matches) != 1:
        raise ValueError(f"expected one matching accessible control, found {len(matches)}")
    if not matches[0].is_enabled():
        raise ValueError("control disabled")
    matches[0].click_input()
    return {"clicked": name, "hwnd": hwnd}


def gui_click(x: int, y: int, button: str = "left"):
    _windows()
    if button not in {"left", "right", "middle"}:
        raise ValueError("invalid mouse button")
    if not -30000 <= x <= 30000 or not -30000 <= y <= 30000:
        raise ValueError("coordinates out of range")
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.click(x=x, y=y, button=button)
    return {"clicked": [x, y], "button": button}


def type_text(text: str, interval: float = 0.01):
    _windows()
    if not isinstance(text, str) or len(text) > 2000 or not 0 <= interval <= 0.2:
        raise ValueError("text or interval out of range")
    import pyautogui
    pyautogui.FAILSAFE = True
    # typewrite handles keyboard characters. For Unicode prefer UIA value patterns.
    pyautogui.write(text, interval=interval)
    return {"typed_characters": len(text), "unicode_note": "Use inspect_ui for complex Unicode fields."}


def send_keys(keys: list[str]):
    _windows()
    if not isinstance(keys, list) or not 1 <= len(keys) <= 5:
        raise ValueError("provide 1..5 keys")
    allowed = {"ctrl", "alt", "shift", "win", "enter", "tab", "esc", "up", "down",
               "left", "right", "home", "end", "backspace", "delete", "space",
               "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
               "a", "c", "v", "x", "z", "s", "r"}
    if any(not isinstance(k, str) or k.lower() not in allowed for k in keys):
        raise ValueError("unsupported key combination")
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.hotkey(*(k.lower() for k in keys), interval=0.1)
    return {"keys": keys}


def record_screen(seconds: int = 3, monitor: int = 1):
    _windows()
    if not 1 <= seconds <= 8:
        raise ValueError("duration must be 1..8 seconds")
    import mss
    from PIL import Image
    frames = []
    with mss.mss() as sct:
        if not 0 <= monitor < len(sct.monitors):
            raise ValueError("monitor out of range")
        bounds = sct.monitors[monitor]
        for _ in range(seconds * 2):
            started = time.monotonic()
            shot = sct.grab(bounds)
            frame = Image.frombytes("RGB", shot.size, shot.rgb)
            frame.thumbnail((900, 900), Image.Resampling.LANCZOS)
            frames.append(frame.convert("P", palette=Image.Palette.ADAPTIVE, colors=128))
            time.sleep(max(0, .5 - (time.monotonic() - started)))
    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:],
                   duration=500, loop=0, optimize=True)
    data = output.getvalue()
    if len(data) > MAX_MEDIA:
        raise ValueError("recording too large; reduce duration or display size")
    return {"mime_type": "image/gif", "base64": base64.b64encode(data).decode(),
            "frames": len(frames), "seconds": seconds, "bytes": len(data)}
