import asyncio
import pytest
from rigorcore_dev.agent import Agent
from rigorcore_dev.config import AgentConfig

def build(tmp_path, mode="approve"):
    (tmp_path / "source").mkdir()
    return Agent(AgentConfig(node_id="test", name="Test client", server="ws://localhost:8765/agents/ws",
                             workspaces={"repo": str(tmp_path / "source")}, mode=mode))

def test_approval_gate(tmp_path):
    async def scenario():
        agent = build(tmp_path)
        status = []
        async def capture(payload):
            status.append(payload)
        agent.send = capture
        task = asyncio.create_task(agent.shell("job", agent.spaces["repo"], {"command": "echo RIGORCORE_OK"}))
        for _ in range(100):
            if "job" in agent.pending:
                break
            await asyncio.sleep(.01)
        assert "job" in agent.pending
        assert not task.done()
        assert await agent.approve("job", True)
        result = await task
        assert result["exit_code"] == 0
        assert "RIGORCORE_OK" in result["stdout"]
    asyncio.run(scenario())

def test_denial(tmp_path):
    async def scenario():
        agent = build(tmp_path)
        async def capture(payload):
            pass
        agent.send = capture
        task = asyncio.create_task(agent.shell("denied", agent.spaces["repo"], {"command": "echo bad"}))
        for _ in range(100):
            if "denied" in agent.pending:
                break
            await asyncio.sleep(.01)
        assert await agent.approve("denied", False)
        with pytest.raises(PermissionError):
            await task
    asyncio.run(scenario())


def test_shell_streams_logs_without_waiting_for_full_output(tmp_path):
    async def scenario():
        agent = build(tmp_path, mode="auto")
        events = []
        async def capture(payload):
            events.append(payload)
        agent.send = capture
        result = await agent.shell("logging", agent.spaces["repo"], {
            "command": "echo RIGORCORE_STREAM_V2", "timeout": 3600,
        })
        assert result["exit_code"] == 0
        assert "RIGORCORE_STREAM_V2" in result["stdout"]
        assert any(entry.get("type") == "status" and entry.get("status") == "running"
                   for entry in events)
        assert any(entry.get("type") == "log" and "RIGORCORE_STREAM_V2" in entry.get("text", "")
                   for entry in events)
    asyncio.run(scenario())
