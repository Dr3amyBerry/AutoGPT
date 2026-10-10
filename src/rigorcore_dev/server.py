"""Central HTTPS/MCP control plane and WebSocket agent registry."""
from __future__ import annotations
import asyncio
import contextlib
import hmac
import json
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from .agent import Agent
from .config import AgentConfig, require_secret, state_dir
from .hub import Hub
from .storage import TaskStore

MCP_TOKEN = require_secret("RIGORCORE_MCP_TOKEN")
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

class BearerAuth:
    """Authenticate the MCP surface independently of the agent connection."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
            if not hmac.compare_digest(auth, "Bearer " + MCP_TOKEN):
                response = JSONResponse({"detail": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

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

@app.websocket("/agents/ws")
async def agents(ws: WebSocket):
    await hub.handle(ws)

app.mount("/mcp", BearerAuth(mcp.streamable_http_app()))
