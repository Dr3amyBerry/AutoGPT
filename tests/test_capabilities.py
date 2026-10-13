"""Remote capability registration, authorization and input-boundary regression tests."""
import asyncio
import pytest
from rigorcore_dev.agent import Agent
from rigorcore_dev.config import AgentConfig
from rigorcore_dev.capabilities import CAPABILITIES
from rigorcore_dev import android, diagnostics


def make_agent(tmp_path, mode="approve"):
    return Agent(AgentConfig(
        node_id="test", name="test", server="ws://localhost:8765/agents/ws",
        workspaces={"demo": str(tmp_path)}, mode=mode))


def test_registry_exposes_requested_tools():
    requested = {
        "take_screenshot", "capture_screen", "list_monitors", "record_screen",
        "list_windows", "capture_window", "focus_window", "resize_window",
        "click_element", "type_text", "send_keys", "inspect_ui", "gui_click",
        "android_devices", "android_screenshot", "android_tap", "android_launch",
        "inspect_process", "get_crash_logs", "debug_application",
        "profile_cpu", "profile_memory", "inspect_gpu", "inspect_ports",
        "test_endpoint", "inspect_requests", "run_tests", "inspect_dependencies",
        "build_project", "inspect_git", "capture_test_failure",
    }
    assert requested <= CAPABILITIES.keys()
    assert CAPABILITIES["capture_screen"][1] is True
    assert CAPABILITIES["type_text"][1] is True


def test_sensitive_operation_waits_for_local_approval(tmp_path, monkeypatch):
    async def scenario():
        agent = make_agent(tmp_path)
        messages = []
        async def receive(msg):
            messages.append(msg)
        agent.send = receive
        task = asyncio.create_task(agent.execute({
            "id": "desktop-req", "op": "capture_screen", "workspace": "demo",
            "args": {"monitor": 1}
        }))
        for _ in range(100):
            if "desktop-req" in agent.pending:
                break
            await asyncio.sleep(0.01)
        assert "desktop-req" in agent.pending
        assert not task.done()
        assert await agent.approve("desktop-req", False)
        await task
        assert any(m.get("ok") is False and m.get("error") == "operation denied by device"
                   for m in messages)
    asyncio.run(scenario())


def test_auto_mode_dispatches_capability(tmp_path, monkeypatch):
    from rigorcore_dev import desktop
    monkeypatch.setattr(desktop, "list_monitors", lambda: [{"index": 1, "width": 100}])
    async def scenario():
        agent = make_agent(tmp_path, mode="auto")
        messages = []
        async def capture(msg):
            messages.append(msg)
        agent.send = capture
        await agent.execute({"id": "mon", "op": "list_monitors",
                             "workspace": "demo", "args": {}})
        assert messages[-1]["ok"] is True
        assert messages[-1]["result"][0]["width"] == 100
    asyncio.run(scenario())


def test_android_injection_rejected():
    with pytest.raises(ValueError):
        android.android_launch("com.test; rm -rf /")
    with pytest.raises(ValueError):
        android.android_tap(-1, 10)
    with pytest.raises(ValueError):
        android.android_swipe(1, 2, 3, 4, ms=99999)
    with pytest.raises(ValueError):
        android.android_input_text("; rm /")


def test_android_serial_must_be_unambiguous(monkeypatch):
    monkeypatch.setattr(android, "android_devices", lambda: [
        {"serial": "one", "state": "device"}, {"serial": "two", "state": "device"}
    ])
    with pytest.raises(ValueError, match="specify a device"):
        android._serial("")
    with pytest.raises(ValueError):
        android._serial("danger;echo")


def test_http_endpoint_blocks_non_loopback():
    for url in ["https://example.com", "http://192.168.1.1", "file:///etc/passwd",
                "http://user:password@localhost:8080"]:
        with pytest.raises((ValueError, PermissionError)):
            diagnostics.test_endpoint(url)


def test_build_requires_a_known_manifest(tmp_path):
    with pytest.raises(ValueError):
        diagnostics.build_project(str(tmp_path))


def test_desktop_boundaries_do_not_need_gui(monkeypatch):
    from rigorcore_dev import desktop
    monkeypatch.setattr(desktop, "_windows", lambda: None)
    with pytest.raises(ValueError):
        desktop.gui_click(99999, 0)
    with pytest.raises(ValueError):
        desktop.send_keys(["cmd", "powershell"])
    with pytest.raises(ValueError):
        desktop.type_text("x" * 2001)
