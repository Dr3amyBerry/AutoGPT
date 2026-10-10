"""Server auth and agent-registration integration."""
import os
from rigorcore_dev.server import hub

def test_health_auth_and_agent_registration(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/mcp/").status_code == 401
    with client.websocket_connect(
        "/agents/ws",
        headers={"Authorization": "Bearer " + os.environ["RIGORCORE_AGENT_TOKEN"]}
    ) as ws:
        ws.send_json({"type": "hello", "node_id": "linux-1", "name": "Linux laptop",
                      "workspaces": ["project"], "mode": "approve"})
        assert ws.receive_json()["type"] == "ready"
        assert hub.snapshot()[0]["node_id"] == "linux-1"
        assert hub.snapshot()[0]["workspaces"] == ["project"]
    assert not hub.snapshot()
