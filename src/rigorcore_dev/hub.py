"""Authenticated outbound-agent WebSocket broker."""
from __future__ import annotations
import asyncio
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from fastapi import WebSocket
from .config import SAFE_ID
from .storage import TaskStore

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
        self.waiting: dict[str, asyncio.Future] = {}

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

    async def request(self, node_id: str, workspace: str, op: str, args: dict, wait: bool = True):
        node = self.choose(node_id, workspace)
        request_id = uuid.uuid4().hex
        message = {"type": "request", "id": request_id, "workspace": workspace,
                   "op": op, "args": args}
        if op == "shell":
            self.store.create(request_id, node_id, workspace, op)
            await node.websocket.send_json(message)
            return {"task_id": request_id, "status": "queued",
                    "hint": "Use get_task to poll status; approval may be needed on the device"}
        if not wait:
            raise ValueError("unsupported asynchronous operation")
        future = asyncio.get_running_loop().create_future()
        self.waiting[request_id] = future
        try:
            await node.websocket.send_json(message)
            return await asyncio.wait_for(future, timeout=45)
        finally:
            self.waiting.pop(request_id, None)

    async def cancel(self, task_id: str):
        task = self.store.get(task_id)
        if not task:
            raise ValueError("task not found")
        if task["status"] in {"completed", "failed", "denied", "cancelled", "interrupted"}:
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
                if kind == "reply":
                    future = self.waiting.get(request_id)
                    if future and not future.done():
                        if msg.get("ok"):
                            future.set_result(msg.get("result"))
                        else:
                            future.set_exception(ValueError(str(msg.get("error", "remote error"))[:1000]))
                    task = self.store.get(request_id) if isinstance(request_id, str) else None
                    if task and task["node"] == node_id:
                        result = msg.get("result")
                        status = "completed" if msg.get("ok") else (
                            "denied" if msg.get("error") == "shell denied by device" else "failed"
                        )
                        self.store.update(request_id, status, result=result, error=msg.get("error"))
                elif kind == "status":
                    task = self.store.get(request_id) if isinstance(request_id, str) else None
                    if task and task["node"] == node_id:
                        self.store.update(request_id, msg.get("status", "running"))
                elif kind == "mode":
                    if msg.get("mode") in {"approve", "auto"}:
                        node.mode = msg["mode"]
        except Exception:
            # Disconnect is expected here; never leak agent keys into logs.
            pass
        finally:
            if node is not None and self.nodes.get(node.id) is node:
                self.nodes.pop(node.id, None)
                self.store.interrupt_node(node.id)
                for request_id, future in list(self.waiting.items()):
                    if not future.done():
                        future.set_exception(ConnectionError("agent disconnected"))
