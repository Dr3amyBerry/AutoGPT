"""Opt-in outbound agent. It makes an explicit, visible connection to one server."""
from __future__ import annotations
import asyncio
import importlib
import json
import os
import signal
import sys
from pathlib import Path
from websockets.asyncio.client import connect
from .config import AgentConfig, require_secret
from .workspace import Workspace
from .capabilities import CAPABILITIES

class Agent:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.spaces = {name: Workspace(path) for name, path in config.workspaces.items()}
        self.mode = config.mode
        self.enabled = True
        self.stopping = False
        self.connected = False
        self.pending: dict[str, tuple[str, asyncio.Future]] = {}
        self.jobs: dict[str, asyncio.Task] = {}
        self.ws = None

    async def approve(self, task_id: str, allowed: bool) -> bool:
        item = self.pending.get(task_id)
        if not item or item[1].done():
            return False
        item[1].set_result(allowed)
        return True

    async def authorize_operation(self, request_id: str, description: str):
        """Enforce local device approval in approve mode for sensitive operations."""
        if self.mode != "approve":
            return
        loop = asyncio.get_running_loop()
        decision = loop.create_future()
        self.pending[request_id] = (description[:4000], decision)
        await self.send({"type": "status", "id": request_id, "status": "approval_required"})
        print("\n[APPROVAL REQUIRED] " + request_id + "\n" + description[:4000] + "\n")
        try:
            if not await decision:
                raise PermissionError("operation denied by device")
        finally:
            self.pending.pop(request_id, None)

    async def change_mode(self, mode: str):
        if mode not in {"approve", "auto"}:
            raise ValueError("invalid mode")
        self.mode = mode
        if self.ws:
            await self.ws.send(json.dumps({"type": "mode", "mode": mode}))

    async def set_enabled(self, enabled: bool):
        self.enabled = enabled
        if not enabled and self.ws:
            await self.ws.close()
        if not enabled:
            for task in list(self.jobs.values()):
                task.cancel()

    async def update_workspaces(self, workspaces: dict[str, str]):
        self.config.workspaces = workspaces
        self.spaces = {name: Workspace(path) for name, path in workspaces.items()}
        if self.ws:
            await self.ws.close()

    async def send(self, message: dict):
        if self.ws is not None:
            await self.ws.send(json.dumps(message))

    async def shell(self, request_id: str, workspace: Workspace, args: dict):
        command = args.get("command")
        if not isinstance(command, str) or not command or len(command) > 10_000:
            raise ValueError("invalid command")
        # Each job may execute for up to an hour, regardless of MCP request duration.
        timeout = min(3600, max(1, int(args.get("timeout", 3600))))
        await self.authorize_operation(request_id, command)
        await self.send({"type": "status", "id": request_id, "status": "running"})
        if os.name == "nt":
            argv = ["cmd", "/d", "/s", "/c", command]
            # CREATE_NEW_PROCESS_GROUP (0x200) | CREATE_NO_WINDOW (0x08000000)
            options = {"creationflags": 0x08000200}
        else:
            argv = ["/bin/sh", "-lc", command]
            options = {"start_new_session": True}
        env = {k: v for k, v in os.environ.items() if not k.startswith("RIGORCORE_")}
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(workspace.root), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **options
        )
        async def bounded_read(stream, stream_name):
            # Stream fresh output to the hub and retain only a bounded final summary.
            output = bytearray()
            truncated = False
            while True:
                block = await stream.read(8192)
                if not block:
                    break
                room = max(0, 100_000 - len(output))
                if len(block) > room:
                    truncated = True
                output.extend(block[:room])
                await self.send({"type": "log", "id": request_id, "stream": stream_name,
                                 "text": block.decode("utf-8", errors="replace")})
            return bytes(output), truncated

        try:
            _, (out, out_cut), (err, err_cut) = await asyncio.wait_for(
                asyncio.gather(
                    process.wait(),
                    bounded_read(process.stdout, "stdout"),
                    bounded_read(process.stderr, "stderr"),
                ), timeout=timeout
            )
        except BaseException as error:
            if process.returncode is None:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                await process.wait()
            if isinstance(error, asyncio.TimeoutError):
                raise ValueError("command timed out") from error
            raise
        return {"exit_code": process.returncode,
                "stdout": out.decode("utf-8", errors="replace"),
                "stderr": err.decode("utf-8", errors="replace"),
                "truncated": out_cut or err_cut}

    async def execute(self, msg: dict):
        rid, op = msg["id"], msg["op"]
        try:
            ws = self.spaces.get(msg.get("workspace"))
            if ws is None:
                raise ValueError("workspace unavailable")
            args = msg.get("args", {})
            if op == "shell":
                result = await self.shell(rid, ws, args)
            elif op == "read_file":
                result = await asyncio.to_thread(ws.read, args["path"])
            elif op == "list_files":
                result = await asyncio.to_thread(ws.files, args.get("path", "."), args.get("limit", 250))
            elif op == "search_text":
                result = await asyncio.to_thread(ws.search, args["query"], args.get("path", "."), args.get("limit", 50))
            elif op == "write_files":
                result = await asyncio.to_thread(ws.write, args["files"])
            elif op in CAPABILITIES:
                module_name, sensitive, needs_root = CAPABILITIES[op]
                if sensitive:
                    await self.authorize_operation(rid, op + ": " + repr(args)[:1500])
                module = importlib.import_module("." + module_name, package=__package__)
                function = getattr(module, "capture_screen" if op == "take_screenshot" else op)
                if needs_root:
                    result = await asyncio.to_thread(function, str(ws.root), **args)
                else:
                    result = await asyncio.to_thread(function, **args)
            else:
                raise ValueError("unknown operation")
            await self.send({"type": "reply", "id": rid, "ok": True, "result": result})
        except asyncio.CancelledError:
            await self.send({"type": "reply", "id": rid, "ok": False, "error": "cancelled"})
        except Exception as error:
            message = ("shell denied by device" if op == "shell" else "operation denied by device") if (
                isinstance(error, PermissionError) and "denied by device" in str(error)
            ) else str(error)[:600]
            await self.send({"type": "reply", "id": rid, "ok": False, "error": message})
        finally:
            self.jobs.pop(rid, None)

    async def run(self):
        token = require_secret("RIGORCORE_AGENT_TOKEN")
        while not self.stopping:
            if not self.enabled:
                await asyncio.sleep(0.5)
                continue
            try:
                async with connect(self.config.server, additional_headers={"Authorization": "Bearer " + token},
                                   max_size=8_000_000, open_timeout=10, ping_interval=25, ping_timeout=30) as ws:
                    self.ws = ws
                    await ws.send(json.dumps({"type": "hello", "node_id": self.config.node_id,
                        "name": self.config.name, "workspaces": list(self.spaces), "mode": self.mode}))
                    hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if hello.get("type") != "ready":
                        raise ConnectionError("server rejected handshake")
                    self.connected = True
                    print("[CONNECTED] " + self.config.node_id)
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") == "request":
                            task = asyncio.create_task(self.execute(msg))
                            self.jobs[msg["id"]] = task
                        elif msg.get("type") == "cancel":
                            task = self.jobs.get(msg.get("id"))
                            if task:
                                task.cancel()
            except (OSError, ConnectionError, TimeoutError, asyncio.TimeoutError) as exc:
                print("[OFFLINE] " + str(exc)[:160], file=sys.stderr)
            except Exception as exc:
                print("[DISCONNECTED] " + str(exc)[:160], file=sys.stderr)
            finally:
                self.connected, self.ws = False, None
                for task in list(self.jobs.values()):
                    task.cancel()
                if self.jobs:
                    await asyncio.gather(*self.jobs.values(), return_exceptions=True)
                self.jobs.clear()
                self.pending.clear()
            await asyncio.sleep(2)

    async def stop(self):
        self.stopping = True
        await self.set_enabled(False)
