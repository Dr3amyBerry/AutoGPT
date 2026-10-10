"""Command-line interface: server, agent, and initial configuration."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import socket
from pathlib import Path
from .agent import Agent
from .config import AgentConfig

async def console(agent: Agent):
    print("Commands: status, pending, allow ID, deny ID, mode approve|auto, connect, disconnect, quit")
    while not agent.stopping:
        try:
            line = (await asyncio.to_thread(input, "rigorcore> ")).strip()
        except (EOFError, KeyboardInterrupt):
            await agent.stop()
            return
        if not line:
            continue
        pieces = line.split()
        cmd = pieces[0].lower()
        try:
            if cmd == "status":
                print(json.dumps({"connected": agent.connected, "enabled": agent.enabled,
                                  "mode": agent.mode, "pending": list(agent.pending)}, indent=2))
            elif cmd == "pending":
                for task_id, (command, _) in agent.pending.items():
                    print(task_id + " " + command)
            elif cmd in {"allow", "deny"} and len(pieces) == 2:
                print("updated" if await agent.approve(pieces[1], cmd == "allow") else "unknown request")
            elif cmd == "mode" and len(pieces) == 2:
                await agent.change_mode(pieces[1])
                print("mode:", agent.mode)
            elif cmd == "connect":
                await agent.set_enabled(True)
            elif cmd == "disconnect":
                await agent.set_enabled(False)
            elif cmd in {"quit", "exit"}:
                await agent.stop()
            else:
                print("Unknown command. Use status, pending, allow, deny, mode, connect, disconnect, quit.")
        except Exception as error:
            print("Error:", error)

async def run_agent(config_path: str, headless: bool):
    agent = Agent(AgentConfig.load(config_path))
    active = asyncio.create_task(agent.run())
    try:
        if headless:
            await active
        else:
            await console(agent)
    finally:
        await agent.stop()
        active.cancel()
        await asyncio.gather(active, return_exceptions=True)

def init_agent(args):
    spaces = {}
    for raw in args.workspace:
        if "=" not in raw:
            raise SystemExit("Use --workspace name=/absolute/path")
        name, path = raw.split("=", 1)
        spaces[name] = str(Path(path).expanduser().resolve(strict=True))
    config = {"node_id": args.node_id or socket.gethostname(), "name": args.name or socket.gethostname(),
              "server": args.server, "workspaces": spaces, "mode": args.mode}
    target = Path(args.config).expanduser()
    if target.exists():
        raise SystemExit("Config already exists. Edit it manually or use a different --config path.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        target.chmod(0o600)
    print("Saved", target, "(no secret stored)")
    print("Set RIGORCORE_AGENT_TOKEN in your process environment before running.")

def main():
    parser = argparse.ArgumentParser(prog="rigorcore-dev")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create agent config")
    init.add_argument("--config", default="~/.config/rigorcore-dev/agent.json")
    init.add_argument("--server", required=True, help="wss://hostname/agents/ws")
    init.add_argument("--workspace", action="append", required=True, help="name=/absolute/path")
    init.add_argument("--node-id", default=None)
    init.add_argument("--name", default=None)
    init.add_argument("--mode", choices=["approve", "auto"], default="approve")
    agent = commands.add_parser("agent", help="connect outgoing agent")
    agent.add_argument("--config", default="~/.config/rigorcore-dev/agent.json")
    agent.add_argument("--headless", action="store_true")
    tray = commands.add_parser("tray", help="Windows notification-area client")
    tray.add_argument("--config", default="~/.config/rigorcore-dev/agent.json")
    server = commands.add_parser("serve", help="start MCP server and agent gateway")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.command == "init":
        init_agent(args)
    elif args.command == "agent":
        try:
            asyncio.run(run_agent(args.config, args.headless))
        except KeyboardInterrupt:
            pass
    elif args.command == "tray":
        from .tray import start_tray
        start_tray(args.config)
    elif args.command == "serve":
        import uvicorn
        uvicorn.run("rigorcore_dev.server:app", host=args.host, port=args.port)

if __name__ == "__main__":
    main()
