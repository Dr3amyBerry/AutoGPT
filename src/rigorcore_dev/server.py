"""Central HTTPS/MCP control plane and WebSocket agent registry."""
from __future__ import annotations
import asyncio
import base64
import contextlib
import json
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from .agent import Agent
from .config import AgentConfig, require_secret, state_dir
from .hub import Hub
from .storage import TaskStore
from .oauth import MCPBearerAuth, OAuthSettings, OAuthVerifier

AUTH_MODE = os.getenv("RIGORCORE_MCP_AUTH_MODE", "bearer").strip().lower()
if AUTH_MODE not in {"bearer", "oauth"}:
    raise RuntimeError("RIGORCORE_MCP_AUTH_MODE must be bearer or oauth")
MCP_TOKEN = require_secret("RIGORCORE_MCP_TOKEN") if AUTH_MODE == "bearer" else None
OAUTH = OAuthVerifier(OAuthSettings.from_env()) if AUTH_MODE == "oauth" else None
AGENT_TOKEN = require_secret("RIGORCORE_AGENT_TOKEN")
hub = Hub(AGENT_TOKEN, TaskStore(state_dir() / "jobs.sqlite3"))
public_host = os.getenv("RIGORCORE_PUBLIC_HOST", "").strip()
allowed_hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"]
allowed_origins = ["http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*"]
if public_host:
    if "/" in public_host or ":" in public_host:
        raise RuntimeError("RIGORCORE_PUBLIC_HOST must be a DNS hostname, without scheme or port")
    allowed_hosts.extend([public_host, public_host + ":*"])
    allowed_origins.append("https://" + public_host)
mcp = FastMCP(
    "RigorCore Dev", stateless_http=True, json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    ),
)

@mcp.tool()
async def list_nodes() -> list[dict]:
    """List active Linux and Windows development nodes and their approval modes."""
    return hub.snapshot()

@mcp.tool()
async def list_workspaces(node_id: str) -> list[str]:
    """List registered workspace IDs on an online node."""
    node = hub.nodes.get(node_id)
    if not node:
        raise ValueError("node is offline")
    return node.workspaces

@mcp.tool()
async def list_files(node_id: str, workspace: str, path: str = ".", limit: int = 250) -> list[str]:
    """Inspect the file tree under a configured workspace."""
    return await hub.request(node_id, workspace, "list_files", {"path": path, "limit": limit})

@mcp.tool()
async def read_file(node_id: str, workspace: str, path: str) -> dict:
    """Read UTF-8 source file contents from a configured workspace."""
    return await hub.request(node_id, workspace, "read_file", {"path": path})

@mcp.tool()
async def search_text(node_id: str, workspace: str, query: str, path: str = ".", limit: int = 50) -> list[dict]:
    """Search source files in a workspace with line-numbered matches."""
    return await hub.request(node_id, workspace, "search_text", {"query": query, "path": path, "limit": limit})

@mcp.tool()
async def write_files(node_id: str, workspace: str, files: list[dict]) -> dict:
    """Replace or create a batch of UTF-8 source files. Shell approval is not required."""
    return await hub.request(node_id, workspace, "write_files", {"files": files})

@mcp.tool()
async def run_shell(node_id: str, workspace: str, command: str, timeout: int = 3600,
                    wait_seconds: float = 0) -> dict:
    """Launch shell without blocking ChatGPT (wait_seconds=0). Optionally wait 1-20s
    for a quick command; long-running commands return a task_id for later polling.
    Approval is handled locally on the device. Shell cwd is NOT a sandbox.
    timeout is the job execution limit in seconds, capped at 3600.
    """
    if not isinstance(command, str) or not command or len(command) > 10000:
        raise ValueError("command must be between 1 and 10000 characters")
    if not 1 <= timeout <= 3600:
        raise ValueError("timeout must be between 1 and 3600 seconds")
    return await hub.request(node_id, workspace, "shell",
                             {"command": command, "timeout": timeout},
                             wait_seconds=wait_seconds)

@mcp.tool()
async def run_shell_batch(node_id: str, workspace: str, commands: list[str],
                          timeout: int = 3600) -> list[dict]:
    """Queue multiple independent commands and receive all task IDs immediately.
    They run concurrently on the client, subject to the host's own capacity.
    Each shell command still requires individual local approval in Approve mode.
    """
    if not commands or any(not isinstance(c, str) or not c or len(c) > 10000 for c in commands):
        raise ValueError("commands must be a non-empty array of valid shell commands")
    if not 1 <= timeout <= 3600:
        raise ValueError("timeout must be between 1 and 3600 seconds")
    return await asyncio.gather(*(hub.request(node_id, workspace, "shell",
                  {"command": cmd, "timeout": timeout}) for cmd in commands))

@mcp.tool()
async def get_task(task_id: str) -> dict:
    """Get a task's status and final result, without waiting for it to finish."""
    task = hub.store.get(task_id)
    if task is None:
        raise ValueError("task not found")
    return task

@mcp.tool()
async def get_tasks(task_ids: list[str], include_results: bool = False) -> list[dict]:
    """Get many statuses in one call, compact by default to save model context.
    Set include_results=True to include complete output (may be very large).
    """
    return hub.store.get_many(task_ids, include_results=include_results)

@mcp.tool()
async def list_tasks(node_id: str = "", workspace: str = "", status: str = "",
                     limit: int = 30, after_created: float = 0) -> list[dict]:
    """List recent shell commands, newest first, including commands sent in past MCP
    requests. Filter by node, workspace, status or creation timestamp.
    """
    return hub.store.list(node=node_id, workspace=workspace, status=status,
                          limit=limit, after_created=after_created)

@mcp.tool()
async def get_task_logs(task_id: str, after_seq: int = 0, limit: int = 100) -> dict:
    """Read incremental stdout/stderr already received from a running job.
    Use next_seq for the next polling request; logs may be truncated on storage quota.
    """
    return hub.store.logs(task_id, after_seq=after_seq, limit=limit)

@mcp.tool()
async def wait_for_task(task_id: str, wait_seconds: float = 5) -> dict:
    """Optionally wait briefly (max 20s) for an existing task, then return its
    current state. Does not tie the MCP request to the command lifetime.
    """
    if not hub.store.get(task_id):
        raise ValueError("task not found")
    return await hub.await_task(task_id, wait_seconds)

@mcp.tool()
async def cancel_task(task_id: str) -> dict:
    """Request cancellation of an active shell task."""
    return await hub.cancel(task_id)

# Explicit, typed MCP tools. The hub continues to enforce node/workspace selection.
async def _remote(node_id: str, workspace: str, operation: str, **args):
    return await hub.request(node_id, workspace, operation, args)

async def _media(node_id: str, workspace: str, operation: str, **args):
    result = await _remote(node_id, workspace, operation, **args)
    if not isinstance(result, dict) or "base64" not in result:
        raise ValueError("agent did not return encoded media")
    raw = base64.b64decode(result["base64"], validate=True)
    if len(raw) > 4_000_000:
        raise ValueError("media exceeds response limit")
    kind = result.get("mime_type", "")
    if kind not in {"image/png", "image/gif"}:
        raise ValueError("unsupported media format")
    return Image(data=raw, format=kind.removeprefix("image/"))

@mcp.tool()
async def take_screenshot(node_id: str, workspace: str, monitor: int = 1) -> Image:
    """Capture the selected Windows monitor as a real PNG image visible to ChatGPT."""
    return await _media(node_id, workspace, "take_screenshot", monitor=monitor)

@mcp.tool()
async def capture_screen(node_id: str, workspace: str, monitor: int = 1) -> Image:
    """Capture a Windows monitor; monitor=0 captures the entire virtual desktop."""
    return await _media(node_id, workspace, "capture_screen", monitor=monitor)

@mcp.tool()
async def list_monitors(node_id: str, workspace: str) -> list[dict]:
    """List available displays and their bounds for screenshots."""
    return await _remote(node_id, workspace, "list_monitors")

@mcp.tool()
async def record_screen(node_id: str, workspace: str, seconds: int = 3, monitor: int = 1) -> Image:
    """Record 1-8 seconds of desktop activity as an animated GIF, not a video file."""
    return await _media(node_id, workspace, "record_screen", seconds=seconds, monitor=monitor)

@mcp.tool()
async def list_windows(node_id: str, workspace: str, limit: int = 100) -> list[dict]:
    """Enumerate visible top-level Windows windows, titles, handles, and rectangles."""
    return await _remote(node_id, workspace, "list_windows", limit=limit)

@mcp.tool()
async def capture_window(node_id: str, workspace: str, hwnd: int) -> Image:
    """Capture a window rectangle; occluding windows may be visible in the capture."""
    return await _media(node_id, workspace, "capture_window", hwnd=hwnd)

@mcp.tool()
async def focus_window(node_id: str, workspace: str, hwnd: int) -> dict:
    """Restore and focus an existing Windows window handle."""
    return await _remote(node_id, workspace, "focus_window", hwnd=hwnd)

@mcp.tool()
async def resize_window(node_id: str, workspace: str, hwnd: int, left: int, top: int,
                        width: int, height: int) -> dict:
    """Move and resize an existing Windows window."""
    return await _remote(node_id, workspace, "resize_window", hwnd=hwnd, left=left,
                         top=top, width=width, height=height)

@mcp.tool()
async def inspect_ui(node_id: str, workspace: str, hwnd: int, limit: int = 100) -> list[dict]:
    """Inspect accessible UI Automation controls from a specific Windows window."""
    return await _remote(node_id, workspace, "inspect_ui", hwnd=hwnd, limit=limit)

@mcp.tool()
async def click_element(node_id: str, workspace: str, hwnd: int, name: str,
                        control_type: str = "") -> dict:
    """Click a uniquely matched accessible control inside a specific window."""
    return await _remote(node_id, workspace, "click_element", hwnd=hwnd,
                         name=name, control_type=control_type)

@mcp.tool()
async def gui_click(node_id: str, workspace: str, x: int, y: int,
                    button: str = "left") -> dict:
    """Click absolute desktop coordinates, respecting pyautogui's failsafe."""
    return await _remote(node_id, workspace, "gui_click", x=x, y=y, button=button)

@mcp.tool()
async def type_text(node_id: str, workspace: str, text: str,
                    interval: float = 0.01) -> dict:
    """Type up to 2000 characters into the currently focused window."""
    return await _remote(node_id, workspace, "type_text", text=text, interval=interval)

@mcp.tool()
async def send_keys(node_id: str, workspace: str, keys: list[str]) -> dict:
    """Send a small, allowlisted keyboard shortcut, e.g. ['ctrl','s']."""
    return await _remote(node_id, workspace, "send_keys", keys=keys)

@mcp.tool()
async def android_devices(node_id: str, workspace: str) -> list[dict]:
    """List devices accessible via ADB, including a connected Windows Subsystem for Android."""
    return await _remote(node_id, workspace, "android_devices")

@mcp.tool()
async def android_screenshot(node_id: str, workspace: str, serial: str = "") -> Image:
    """Capture a connected Android/WSA display as PNG directly in ChatGPT."""
    return await _media(node_id, workspace, "android_screenshot", serial=serial)

@mcp.tool()
async def android_tap(node_id: str, workspace: str, x: int, y: int,
                      serial: str = "") -> dict:
    """Tap screen coordinates on an explicitly selected Android device."""
    return await _remote(node_id, workspace, "android_tap", x=x, y=y, serial=serial)

@mcp.tool()
async def android_swipe(node_id: str, workspace: str, x1: int, y1: int,
                        x2: int, y2: int, ms: int = 350, serial: str = "") -> dict:
    """Swipe between two Android coordinates."""
    return await _remote(node_id, workspace, "android_swipe",
                         x1=x1, y1=y1, x2=x2, y2=y2, ms=ms, serial=serial)

@mcp.tool()
async def android_launch(node_id: str, workspace: str, package: str,
                         serial: str = "") -> dict:
    """Start the main launcher activity of an Android application package."""
    return await _remote(node_id, workspace, "android_launch", package=package, serial=serial)

@mcp.tool()
async def android_input_text(node_id: str, workspace: str, text: str,
                             serial: str = "") -> dict:
    """Enter a short restricted ASCII string into the Android focused field."""
    return await _remote(node_id, workspace, "android_input_text", text=text, serial=serial)

@mcp.tool()
async def android_logcat(node_id: str, workspace: str, serial: str = "",
                         lines: int = 300, errors_only: bool = False) -> dict:
    """Read a bounded segment of Android logcat to diagnose problems."""
    return await _remote(node_id, workspace, "android_logcat", serial=serial,
                         lines=lines, errors_only=errors_only)

@mcp.tool()
async def android_ui_tree(node_id: str, workspace: str, serial: str = "") -> dict:
    """Read Android UIAutomator XML of the active application."""
    return await _remote(node_id, workspace, "android_ui_tree", serial=serial)

@mcp.tool()
async def android_profile(node_id: str, workspace: str, package: str,
                          serial: str = "") -> dict:
    """Collect Android memory and rendering diagnostics for a package."""
    return await _remote(node_id, workspace, "android_profile",
                         package=package, serial=serial)

@mcp.tool()
async def inspect_process(node_id: str, workspace: str, name: str = "",
                          limit: int = 40) -> list[dict]:
    """List process names, IDs, CPU and resident memory without exposing command-line secrets."""
    return await _remote(node_id, workspace, "inspect_process", name=name, limit=limit)

@mcp.tool()
async def debug_application(node_id: str, workspace: str, pid: int) -> dict:
    """Inspect one process's state, threads and resource consumption (no debugger attach)."""
    return await _remote(node_id, workspace, "debug_application", pid=pid)

@mcp.tool()
async def profile_cpu(node_id: str, workspace: str, pid: int = 0,
                      seconds: float = 0.2) -> dict:
    """Sample system CPU, or the CPU percentage for a specific process."""
    return await _remote(node_id, workspace, "profile_cpu", pid=pid, seconds=seconds)

@mcp.tool()
async def profile_memory(node_id: str, workspace: str, pid: int = 0) -> dict:
    """Inspect system RAM or a specific process's memory."""
    return await _remote(node_id, workspace, "profile_memory", pid=pid)

@mcp.tool()
async def inspect_gpu(node_id: str, workspace: str) -> dict:
    """Inspect NVIDIA metrics or enumerate Windows display adapters."""
    return await _remote(node_id, workspace, "inspect_gpu")

@mcp.tool()
async def inspect_ports(node_id: str, workspace: str, limit: int = 80) -> list[dict]:
    """List TCP/UDP socket metadata with process IDs, without packet interception."""
    return await _remote(node_id, workspace, "inspect_ports", limit=limit)

@mcp.tool()
async def inspect_requests(node_id: str, workspace: str, limit: int = 80) -> dict:
    """Inspect active socket connections; does not capture HTTP payloads."""
    return await _remote(node_id, workspace, "inspect_requests", limit=limit)

@mcp.tool()
async def test_endpoint(node_id: str, workspace: str, url: str) -> dict:
    """GET a loopback-only HTTP(S) development endpoint with no redirects."""
    return await _remote(node_id, workspace, "test_endpoint", url=url)

@mcp.tool()
async def inspect_git(node_id: str, workspace: str) -> dict:
    """Inspect branch, working-tree status and the five latest commits."""
    return await _remote(node_id, workspace, "inspect_git")

@mcp.tool()
async def inspect_dependencies(node_id: str, workspace: str) -> dict:
    """Identify package manifests in the selected workspace."""
    return await _remote(node_id, workspace, "inspect_dependencies")

@mcp.tool()
async def build_project(node_id: str, workspace: str, timeout: int = 180) -> dict:
    """Run bounded Python/Android/Node build in workspace; requires local approval."""
    return await _remote(node_id, workspace, "build_project", timeout=timeout)

@mcp.tool()
async def run_tests(node_id: str, workspace: str, timeout: int = 180) -> dict:
    """Run bounded Python or Node tests in workspace; requires local approval."""
    return await _remote(node_id, workspace, "run_tests", timeout=timeout)

@mcp.tool()
async def get_crash_logs(node_id: str, workspace: str) -> dict:
    """Read recent Windows Application error events for crash triage."""
    return await _remote(node_id, workspace, "get_crash_logs")

@mcp.tool()
async def capture_test_failure(node_id: str, workspace: str) -> Image:
    """Capture an entire Windows desktop snapshot as visual test failure evidence."""
    return await _media(node_id, workspace, "capture_test_failure")

@asynccontextmanager
async def lifespan(app: FastAPI):
    local_task = None
    async with mcp.session_manager.run():
        config_json = os.getenv("RIGORCORE_LOCAL_WORKSPACES")
        if config_json:
            spaces = json.loads(config_json)
            config = AgentConfig(
                node_id=os.getenv("RIGORCORE_LOCAL_NODE_ID", "central-server"),
                name="Central server", server=os.getenv("RIGORCORE_LOCAL_AGENT_URL", "ws://127.0.0.1:8765/agents/ws"),
                workspaces=spaces, mode=os.getenv("RIGORCORE_LOCAL_MODE", "approve"))
            local = Agent(config)
            local_task = asyncio.create_task(local.run())
        try:
            yield
        finally:
            if local_task:
                await local.stop()
                local_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await local_task

app = FastAPI(title="RigorCore Dev", lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
@app.get("/.well-known/oauth-protected-resource/mcp/")
async def oauth_resource_metadata():
    if OAUTH is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return OAUTH.settings.protected_resource_metadata()

@app.websocket("/agents/ws")
async def agents(ws: WebSocket):
    await hub.handle(ws)

app.mount("/mcp", MCPBearerAuth(mcp.streamable_http_app(), legacy_token=MCP_TOKEN, oauth=OAUTH))
