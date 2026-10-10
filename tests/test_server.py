"""Verify control-plane routing, auth and online-agent registration."""
import os

os.environ.setdefault("RIGORCORE_MCP_TOKEN", "test-mcp-token-at-least-24-chars")
os.environ.setdefault("RIGORCORE_AGENT_TOKEN", "test-agent-token-at-least-24-chars")
from fastapi.testclient import TestClient
from rigorcore_dev.server import app

def test_health_auth_and_agent_registration():
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/mcp/").status_code == 401
        with client.websocket_connect(
            "/agents/ws",
            headers={"Authorization": "Bearer " + os.environ["RIGORCORE_AGENT_TOKEN"]}
        ) as ws:
            ws.send_json({"type": "hello", "node_id": "linux-1", "name": "Linux laptop",
                          "workspaces": ["project"], "mode": "approve"})
            assert ws.receive_json()["type"] == "ready"
            from rigorcore_dev.server import hub
            assert hub.snapshot()[0]["node_id"] == "linux-1"
            assert hub.snapshot()[0]["workspaces"] == ["project"]
        assert not hub.snapshot()
