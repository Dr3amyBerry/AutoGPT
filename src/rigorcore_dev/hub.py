"""Authenticated outbound-agent WebSocket broker."""
from __future__ import annotations
import asyncio
import hmac
import time
import uuid
from dataclasses import dataclass
from fastapi import WebSocket
from .config import SAFE_ID
from .storage import TaskStore, TERMINAL

@dataclass
class Node:
    id: str
    name: str
    workspaces: list[str]
    mode: str
    websocket: WebSocket
    joined: float

class Hub:
    def __init__(self, agent_token: str, store: TaskStore):
        self.agent_token = agent_token
        self.store = store
        self.nodes: dict[str, Node] = {}
        self.waiting: dict[str, tuple[str, asyncio.Future]] = {}

    def snapshot(self) -> list[dict]:
        return [{"node_id": n.id, "name": n.name, "workspaces": n.workspaces,
                 "mode": n.mode, "connected_since": n.joined}
                for n in self.nodes.values()]

    def choose(self, node_id: str, workspace: str) -> Node:
        node = self.nodes.get(node_id)
        if node is None:
            raise ValueError("node is offline")
        if workspace not in node.workspaces:
            raise ValueError("workspace unavailable on this node")
        return node

    async def await_task(self, task_id: str, seconds: float = 0):
        # A short wait, never the entire shell lifetime. ChatGPT polls later.
        seconds = min(max(float(seconds), 0.0), 20.0)
        deadline = asyncio.get_running_loop().time() + seconds
        task = self.store.get(task_id)
        while task and task["status"] not in TERMINAL and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(min(0.1, max(0.0, deadline - asyncio.get_running_loop().time())))
            task = self.store.get(task_id)
        return task

    async def request(self, node_id: str, workspace: str, op: str, args: dict,
                      wait_seconds: float = 0):
        node = self.choose(node_id, workspace)
        request_id = uuid.uuid4().hex
        message = {"type": "request", "id": request_id, "workspace": workspace,
                   "op": op, "args": args}
        if op in {"shell", "build_project", "run_tests"}:
            self.store.create(request_id, node_id, workspace, op, args.get("command", op))
            try:
                await node.websocket.send_json(message)
            except Exception as exc:
                self.store.update(request_id, "failed", error="could not dispatch command")
                raise ConnectionError("could not send job to agent") from exc
            result = await self.await_task(request_id, wait_seconds)
            return {**result, "task_id": request_id, "hint": ("Command finished" if result["status"] in TERMINAL else
                "Command is running or awaiting local approval. Continue other work; "
                "use get_task, list_tasks or get_task_logs later.")}
        future = asyncio.get_running_loop().create_future()
        self.waiting[request_id] = (node_id, future)
        try:
            await node.websocket.send_json(message)
            return await asyncio.wait_for(future, timeout=45)
        finally:
            self.waiting.pop(request_id, None)

    async def cancel(self, task_id: str):
        task = self.store.get(task_id)
        if not task:
            raise ValueError("task not found")
        if task["status"] in TERMINAL:
            return task
        node = self.nodes.get(task["node"])
        if node:
            await node.websocket.send_json({"type": "cancel", "id": task_id})
            self.store.update(task_id, "cancel_requested")
        else:
            self.store.update(task_id, "interrupted")
        return self.store.get(task_id)

    async def handle(self, ws: WebSocket):
        token = ws.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token, self.agent_token):
            await ws.close(code=4401)
            return
        await ws.accept()
        node = None
        try:
            hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
            if hello.get("type") != "hello":
                raise ValueError("expected hello message")
            node_id = hello.get("node_id")
            spaces = hello.get("workspaces")
            if not isinstance(node_id, str) or not SAFE_ID.fullmatch(node_id):
                raise ValueError("invalid node_id")
            if not isinstance(spaces, list) or not spaces or len(spaces) > 100 or any(
                not isinstance(s, str) or not SAFE_ID.fullmatch(s) for s in spaces
            ):
                raise ValueError("invalid workspace identifiers")
            mode = hello.get("mode")
            if mode not in {"approve", "auto"}:
                raise ValueError("invalid approval mode")
            name = str(hello.get("name", node_id))[:100]
            previous = self.nodes.get(node_id)
            if previous:
                await previous.websocket.close(code=4001)
            node = Node(node_id, name, spaces, mode, ws, time.time())
            self.nodes[node_id] = node
            await ws.send_json({"type": "ready", "node_id": node_id})
            while True:
                msg = await ws.receive_json()
                kind = msg.get("type")
                request_id = msg.get("id")
                if not isinstance(request_id, str):
                    if kind == "mode" and msg.get("mode") in {"approve", "auto"}:
                        node.mode = msg["mode"]
                    continue
                if kind == "reply":
                    pending = self.waiting.get(request_id)
                    if pending and pending[0] == node_id and not pending[1].done():
                        if msg.get("ok"):
                            pending[1].set_result(msg.get("result"))
                        else:
                            pending[1].set_exception(ValueError(str(msg.get("error", "remote error"))[:1000]))
                    task = self.store.get(request_id)
                    if task and task["node"] == node_id:
                        result = msg.get("result")
                        if msg.get("ok"):
                            status = "completed"
                        elif msg.get("error") in {"shell denied by device", "operation denied by device"}:
                            status = "denied"
                        elif msg.get("error") == "cancelled":
                            status = "cancelled"
                        else:
                            status = "failed"
                        self.store.update(request_id, status, result=result, error=msg.get("error"))
                elif kind == "status":
                    task = self.store.get(request_id)
                    if task and task["node"] == node_id:
                        self.store.update(request_id, msg.get("status", "running"))
                elif kind == "log":
                    task = self.store.get(request_id)
                    chunk = msg.get("text")
                    if task and task["node"] == node_id and isinstance(chunk, str) and len(chunk) <= 16384:
                        self.store.append_log(request_id, msg.get("stream"), chunk)
        except Exception:
            # Disconnect or invalid client data: don't leak credentials in exceptions.
            pass
        finally:
            if node is not None and self.nodes.get(node.id) is node:
                self.nodes.pop(node.id, None)
                self.store.interrupt_node(node.id)
                for request_id, (owner, future) in list(self.waiting.items()):
                    if owner == node.id and not future.done():
                        future.set_exception(ConnectionError("agent disconnected"))
