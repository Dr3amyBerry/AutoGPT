"""MCP request/response integration against the actual ASGI app."""
import os
os.environ.setdefault("RIGORCORE_MCP_TOKEN", "test-mcp-token-at-least-24-chars")
os.environ.setdefault("RIGORCORE_AGENT_TOKEN", "test-agent-token-at-least-24-chars")

from fastapi.testclient import TestClient
from rigorcore_dev.server import app

HEADERS = {
    "Authorization": "Bearer " + os.environ["RIGORCORE_MCP_TOKEN"],
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

def test_mcp_initialize_and_tools_list():
    with TestClient(app) as client:
        init = client.post("/mcp/", headers=HEADERS, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "rigorcore-pytest", "version": "1.0"},
            }
        })
        assert init.status_code == 200, init.text
        result = init.json()
        assert result["result"]["serverInfo"]["name"]
        listing = client.post("/mcp/", headers={**HEADERS, "MCP-Protocol-Version": "2025-06-18"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert listing.status_code == 200, listing.text
        names = {tool["name"] for tool in listing.json()["result"]["tools"]}
        assert {"list_nodes", "list_workspaces", "read_file", "write_files", "run_shell", "get_task"} <= names
