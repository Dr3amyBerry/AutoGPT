"""One MCP session per process, with localhost as an approved MCP host."""
import os
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("RIGORCORE_MCP_TOKEN", "test-mcp-token-at-least-24-chars")
os.environ.setdefault("RIGORCORE_AGENT_TOKEN", "test-agent-token-at-least-24-chars")

from rigorcore_dev.server import app

@pytest.fixture(scope="session")
def client():
    with TestClient(app, base_url="http://localhost") as session:
        yield session
