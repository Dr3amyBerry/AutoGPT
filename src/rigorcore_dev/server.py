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
from .agent import Agent
from .config import AgentConfig, require_secret, state_dir
from .hub import Hub
from .storage import TaskStore

MCP_TOKEN = require_secret("RIGORCORE_MCP_TOKEN")
AGENT_TOKEN = require_secret("RIGORCORE_AGENT_TOKEN")
hub = Hub(AGENT_TOKEN, TaskStore(state_dir() / "jobs.sqlite3"))
mcp = FastMCP("RigorCore Dev", stateless_http=True, json_response=True,
              streamable_http_path="/")

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
async def run_shell(node_id: str, workspace: str, command: str, timeout: int = 120) -> dict:
    """Queue a shell job in a workspace. Device approval is required in approve mode.
    The shell uses OS user privileges; cwd is NOT a sandbox. Returns a task_id.
    """
    return await hub.request(node_id, workspace, "shell", {"command": command, "timeout": timeout})

@mcp.tool()
async def get_task(task_id: str) -> dict:
    """Poll a shell task for queued, approval_required, running, or terminal status."""
    task = hub.store.get(task_id)
    if task is None:
        raise ValueError("task not found")
    return task

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
