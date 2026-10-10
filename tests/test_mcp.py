"""MCP Streamable HTTP initialization and tool discovery."""
import os

HEADERS = {
    "Authorization": "Bearer " + os.environ["RIGORCORE_MCP_TOKEN"],
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

def test_mcp_initialize_and_tools_list(client):
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
    assert {"list_nodes", "list_workspaces", "read_file", "write_files", "run_shell",
            "run_shell_batch", "get_task", "get_tasks", "list_tasks",
            "get_task_logs", "wait_for_task", "cancel_task"} <= names
