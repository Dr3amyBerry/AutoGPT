# RigorCore Dev architecture

## Components

1. **Central server:** FastAPI + MCP Python SDK. Authenticated Streamable HTTP MCP endpoint at /mcp/, agent WebSocket hub at /agents/ws, health check at /health.
2. **Outbound client:** Connects via WSS with a dedicated agent key, advertises workspace names, handles files and shell. No inbound ports are required on clients.
3. **Linux UI:** Interactive terminal commands for local per-command approval, mode changes, connect/disconnect.
4. **Windows UI:** Visible system-tray app, with equivalent controls.
5. **SQLite registry:** Stores shell job states and outputs. In-flight work becomes interrupted on server restart; commands never auto-retry.

The central server can itself be a node with RIGORCORE_LOCAL_WORKSPACES, using an embedded loopback client. For interactive approvals on the server, use a separate Linux CLI agent instead.

## Protocol v1

Agent -> Server: hello(node_id,name,mode,workspaces), reply(id,ok,result/error), status(id,status), mode(mode).

Server -> Agent: ready(node_id), request(id,workspace,op,args), cancel(id).

File operations: list_files, read_file, search_text, write_files. Shell operations return a task ID; get_task polls persisted status.

## Trust and security

- Use two different, randomly generated bearer tokens: one MCP token and one agent token.
- Set RIGORCORE_PUBLIC_HOST to the exact HTTPS DNS hostname when deploying behind a reverse proxy; MCP rejects hosts outside the explicit allowlist.
- Never commit tokens. Reverse proxy must enforce HTTPS/WSS and host/origin limits.
- Clients initiate outbound connections only; they can disconnect at any time.
- File API workspace paths are restricted, including resolved symlink escape checks.
- Shell CWD is **not** a sandbox. Shell can reach anywhere permitted to the operating-system user, including parent directories. Run as a dedicated unprivileged OS user.
- Full Auto is a local-only opt-in; it disables shell approvals on that client.
- Windows app is visible in system tray; no hidden/background persistence is installed.
- The agent token is currently shared by all nodes. Per-node revocation and enrollment are future production-hardening work.
- SQLite task status survives chat calls, but in-flight jobs do not resume after server restart.
- No RBAC, tenant isolation, container sandbox, or strong network isolation is shipped in v0.1.

## Future priorities

Per-device enrollment and token revocation; improved audit logging; Git diff/patch and rollback; streamed output and durable task leases; OS sandbox; Windows installer; signed releases; multi-user roles.

## Flow

ChatGPT -> /mcp/ (MCP bearer) -> Hub -> /agents/ws (agent bearer) -> Agent -> Filesystem or OS shell.

Agent -> Reply/status -> Hub -> SQLite -> MCP get_task.

## Validation

See CI for actual test results. Passing tests are not equivalent to deployment or a security audit.
