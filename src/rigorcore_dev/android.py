"""Bounded ADB operations for WSA, Android emulators, and USB devices."""
from __future__ import annotations
import base64
import os
import re
import shutil
import subprocess

PACKAGE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z][a-zA-Z0-9_]*)+$")
SERIAL = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


def _adb(args: list[str], serial: str = "", timeout: int = 25, binary: bool = False):
    executable = shutil.which("adb")
    if not executable:
        raise RuntimeError("Android Platform Tools (adb) are not in PATH")
    argv = [executable]
    if serial:
        if not SERIAL.fullmatch(serial):
            raise ValueError("invalid Android device serial")
        argv += ["-s", serial]
    argv += args
    try:
        done = subprocess.run(argv, capture_output=True, timeout=timeout, check=False,
                              creationflags=0x08000000 if os.name == "nt" else 0)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ADB command timed out") from exc
    if done.returncode:
        raise RuntimeError("ADB failed: " + done.stderr.decode("utf-8", errors="replace")[:500])
    if binary:
        return done.stdout
    return done.stdout.decode("utf-8", errors="replace")[:50000]


def android_devices():
    output = _adb(["devices", "-l"], timeout=12)
    result = []
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            result.append({"serial": parts[0], "state": parts[1],
                           "details": " ".join(parts[2:])[:300]})
    return result


def _serial(serial: str):
    if serial:
        if not SERIAL.fullmatch(serial):
            raise ValueError("invalid Android device serial")
        return serial
    connected = [x["serial"] for x in android_devices() if x["state"] == "device"]
    if len(connected) != 1:
        raise ValueError("specify a device serial (connected devices: " + str(len(connected)) + ")")
    return connected[0]


def android_screenshot(serial: str = ""):
    serial = _serial(serial)
    data = _adb(["exec-out", "screencap", "-p"], serial, timeout=20, binary=True)
    if len(data) > 4_000_000 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Android PNG invalid or exceeds capture limit")
    return {"mime_type": "image/png", "base64": base64.b64encode(data).decode(),
            "bytes": len(data), "serial": serial}


def android_tap(x: int, y: int, serial: str = ""):
    if not (0 <= x <= 12000 and 0 <= y <= 12000):
        raise ValueError("tap position out of range")
    serial = _serial(serial)
    _adb(["shell", "input", "tap", str(x), str(y)], serial, timeout=10)
    return {"serial": serial, "tap": [x, y]}


def android_swipe(x1: int, y1: int, x2: int, y2: int, ms: int = 350, serial: str = ""):
    if any(not 0 <= p <= 12000 for p in (x1, y1, x2, y2)) or not 50 <= ms <= 5000:
        raise ValueError("swipe arguments out of range")
    serial = _serial(serial)
    _adb(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms)], serial)
    return {"serial": serial, "swipe": [x1, y1, x2, y2], "ms": ms}


def android_launch(package: str, serial: str = ""):
    if not PACKAGE.fullmatch(package):
        raise ValueError("invalid Android package name")
    serial = _serial(serial)
    output = _adb(["shell", "monkey", "-p", package,
                   "-c", "android.intent.category.LAUNCHER", "1"], serial, timeout=20)
    if "No activities found" in output or "monkey aborted" in output.lower():
        raise RuntimeError("no launchable activity for package")
    return {"serial": serial, "package": package, "output": output[:1200]}


def android_input_text(text: str, serial: str = ""):
    if not text or len(text) > 500 or any(ord(c) < 32 or ord(c) > 126 for c in text):
        raise ValueError("only printable ASCII text up to 500 chars supported")
    # Validate untrusted text before even probing connected devices.
    # ADB's input text parser treats spaces as %s. Avoid shell metacharacters.
    if re.search(r"[^a-zA-Z0-9 _.,@+-]", text):
        raise ValueError("text contains characters unsupported by adb input")
    serial = _serial(serial)
    _adb(["shell", "input", "text", text.replace(" ", "%s")], serial, timeout=15)
    return {"serial": serial, "characters": len(text)}


def android_logcat(serial: str = "", lines: int = 300, errors_only: bool = False):
    if not 1 <= lines <= 1000:
        raise ValueError("lines must be 1..1000")
    serial = _serial(serial)
    argv = ["logcat", "-d", "-t", str(lines)]
    if errors_only:
        argv += ["*:E"]
    output = _adb(argv, serial, timeout=20)
    return {"serial": serial, "logs": output[-40000:], "truncated": len(output) > 40000}


def android_ui_tree(serial: str = ""):
    serial = _serial(serial)
    path = "/sdcard/window-rigorcore-uiautomator.xml"
    _adb(["shell", "uiautomator", "dump", path], serial, timeout=30)
    xml = _adb(["shell", "cat", path], serial, timeout=15)
    if not xml.lstrip().startswith("<?xml"):
        raise RuntimeError("UIAutomator did not return XML")
    return {"serial": serial, "xml": xml[:45000], "truncated": len(xml) > 45000}


def android_profile(package: str, serial: str = ""):
    if not PACKAGE.fullmatch(package):
        raise ValueError("invalid Android package name")
    serial = _serial(serial)
    memory = _adb(["shell", "dumpsys", "meminfo", package], serial, timeout=25)
    gfx = _adb(["shell", "dumpsys", "gfxinfo", package], serial, timeout=25)
    return {"serial": serial, "package": package, "memory": memory[:18000],
            "gfx": gfx[:18000]}
