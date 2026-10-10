# RigorCore Dev (AutoGPT)

Private, self-hosted MCP control plane for programming on a Linux VPS or a Windows workstation from ChatGPT. The server manages a list of currently connected workspaces; opt-in clients make outbound connections and execute file requests and (when authorized locally) shell jobs.

> **Status: early developer preview (v0.1).** This repository contains executable server and agent code and automated tests. It has not been deployed to your VPS, security audited, or connected to the ChatGPT RigorCore Dev plugin. Do not expose it publicly without HTTPS and limited system accounts.

## What works

- Central FastAPI server with authenticated MCP tools and authenticated WebSocket node hub.
- List online nodes and their workspaces; read, search, list, and batch-write UTF-8 code files.
- Queue shell commands without blocking the ChatGPT conversation, poll task output/status, cancel active jobs.
- Store shell job results in SQLite; mark unfinished jobs interrupted after restart.
- Linux terminal client with **Approve each shell command** / **Full Auto** and instant disconnect.
- Windows system-tray client with the same modes, approval buttons, and disconnect.
- Optional loopback client hosted by the central server itself.

## Installation

Requires Python 3.11+, Linux or Windows. For a clean install, use a virtual environment:

    git clone https://github.com/Dr3amyBerry/AutoGPT.git
    cd AutoGPT
    python3 -m venv .venv
    . .venv/bin/activate
    pip install -e ".[test]"

On Windows, in PowerShell:

    py -3 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -e ".[windows,test]"

### 1. Start the central server (Linux VPS)

Generate **different** cryptographically random tokens and keep them out of Git. For example, run this twice and store each result in a secure password manager:

    python3 -c "import secrets; print(secrets.token_urlsafe(48))"

Set the environment variables in the service manager or your shell:

    export RIGORCORE_MCP_TOKEN='YOUR_MCP_SECRET'
    export RIGORCORE_AGENT_TOKEN='YOUR_DIFFERENT_AGENT_SECRET'
    export RIGORCORE_PUBLIC_HOST='dev.example.com'
    rigorcore-dev serve --host 127.0.0.1 --port 8765

The server listens on loopback by default. Add a real domain, firewall and HTTPS reverse proxy. For example, with Caddy:

    dev.example.com {
        reverse_proxy 127.0.0.1:8765
    }

This exposes the MCP endpoint at https://dev.example.com/mcp/ and the agent gateway at wss://dev.example.com/agents/ws. Replace the example host with your own domain; do not copy it as a working endpoint. Both require their own bearer tokens.

The FastMCP endpoint authenticates by Authorization: Bearer YOUR_MCP_SECRET. Set that in a private plugin connection that supports bearer credentials; do not place it in public plugin manifests. Connection setup and publication require supported ChatGPT configuration and user approval.

### 2. Add a Linux client

On each client, install the package as above and make a workspace:

    mkdir -p ~/projects/demo
    rigorcore-dev init --server wss://dev.example.com/agents/ws --node-id vps-01 --workspace demo=$HOME/projects/demo

Set the agent token, then run:

    export RIGORCORE_AGENT_TOKEN='YOUR_AGENT_SECRET'
    rigorcore-dev agent

Use the interactive console:
- status / pending: view connection and approval requests.
- allow TASK_ID / deny TASK_ID: approve or deny one shell command.
- mode approve / mode auto: switch approval mode locally.
- disconnect / connect: disable or restore the network connection.
- quit: exit.

For a second client, choose a different node ID and add multiple --workspace arguments. The server only lists online clients.

### 3. Add a Windows client

Install with the windows extras above, create agent config with a Windows absolute path, then set RIGORCORE_AGENT_TOKEN in that process environment. Run:

    rigorcore-dev init --server wss://dev.example.com/agents/ws --node-id windows-pc --workspace app=C:\Users\YourUser\source\app
    rigorcore-dev tray

A small, visible icon appears in the Windows notification area. Right-click it to approve/deny queued shell jobs, switch between approval modes, disconnect/reconnect, or exit. You can launch it with pythonw.exe to avoid keeping a console window open. It does **not** install auto-start or run as a hidden service.

### 4. Optionally connect the central VPS as a client

The server can also register its own workspaces by starting an outbound loopback agent:

    export RIGORCORE_LOCAL_WORKSPACES='{"backend":"/srv/projects/backend"}'
    export RIGORCORE_LOCAL_AGENT_URL='ws://127.0.0.1:8765/agents/ws'
    rigorcore-dev serve

For shell approvals on the server, prefer starting an interactive CLI agent as a separate process instead of the embedded agent. An embedded agent in approve mode has no dedicated approval console.

## MCP tools available

| MCP tool | Purpose |
| --- | --- |
| list_nodes | Find connected VPS or Windows hosts |
| list_workspaces | Show workspaces exposed by one host |
| list_files | Enumerate files within the workspace |
| read_file | Read one UTF-8 source file |
| search_text | Locate code with line numbers |
| write_files | Write up to 100 UTF-8 files as one batch, no shell approval |
| run_shell | Queue a command immediately, or wait 1–20 seconds with `wait_seconds` |
| run_shell_batch | Queue multiple shell commands in parallel in a single call |
| get_task | Read status/result of one command without blocking |
| get_tasks | Fetch up to 100 known task IDs in one request |
| list_tasks | List latest commands, optionally filtered by node/workspace/status |
| get_task_logs | Read incremental stdout/stderr since a previous sequence number |
| wait_for_task | Briefly wait 0–20 seconds for a known command |
| cancel_task | Request cancellation of an active command |

Choose a node and workspace explicitly for each operation. A run_shell result is a task ID, not necessarily command completion.

## Async shell jobs and long-lived connections

**Default: do not hold an MCP call open while a command is executing.**
`run_shell(node_id="vps-01", workspace="demo", command="pytest -q", timeout=3600, wait_seconds=0)`
responds after dispatch with a durable task ID. The job keeps running on the client independently of that MCP request. You may read or edit other files and query its status later with `get_task(task_id)`.

For quick commands, set `wait_seconds=2` (or up to 20 seconds). If the command finishes in that short window, the result is included immediately; otherwise the response contains the task ID and current state. `wait_for_task(task_id,wait_seconds=5)` offers the same choice later without launching another command.

Queue independent commands with `run_shell_batch(node_id,workspace,commands=["pytest -q","git status","npm run build"])`. This returns their task IDs without serial waits. Each command still requires separate permission on the device when in **Approve** mode; **Full Auto** is a local selection and requires no per-command approval.

Retrieve the latest commands with `list_tasks(node_id="vps-01", workspace="demo", limit=30)`. For previously known IDs, `get_tasks(task_ids=[...])` provides their statuses/results in a single MCP call. During execution, call `get_task_logs(task_id, after_seq=0)` and reuse its `next_seq` to retrieve only new output. Incremental logs are retained in SQLite up to 2 MB per job, after which `logs_truncated` is set. The final command summary independently retains up to 100 KB each of stdout/stderr.

The client WebSocket is designed to remain connected continuously, and uses periodic ping/pong frames; it is not a one-hour per-job TCP connection. **Shell commands can run for up to 3600 seconds (1 hour)** regardless of whether ChatGPT's individual tool request remains active. The server itself does not impose a hard global concurrency cap on shell jobs, but the operating system, network, SQLite storage and ChatGPT provider may impose their own limits.

**Important:** if the client disconnects, currently executing subprocesses are cancelled; on control-plane restart, unfinished jobs are marked `interrupted`, not automatically replayed. This version does not yet resume running jobs across process restarts or offer guaranteed 24/7 uptime. MCP sessions can expire according to platform rules; no fixed one-hour ChatGPT connector guarantee is claimed.

## Safety boundaries

**Shell CWD is not a sandbox.** Even when a command starts under ~/projects/demo, the shell can navigate elsewhere and access anything the agent operating-system account can access. Running an agent as root, administrator, or with unrestricted Docker credentials is unsafe. Use a dedicated low-privilege account and container isolation if strict boundaries are needed.

Use HTTPS/WSS, high-entropy independent tokens, private file permissions, restrictive firewall rules, controlled reverse-proxy origins, and a non-root service account. The current shared agent token means it is not suitable for untrusted third-party machines. Full Auto removes command-by-command approval and must be explicitly selected on the local machine.

The source-file writer validates workspace paths against escaping and symlinks, with file count/size limits. It does not provide OS-level isolation.

## Developer workflow

    python -m compileall -q src
    python -m pytest -q

GitHub Actions runs these checks on pushes to main. See [architecture and limitations](docs/ARCHITECTURE.md).

## Project status

This is a functional starting implementation, not a completed remote development platform. Deployment, end-to-end WSS/MCP integration testing, hardened authentication, full shell command visibility in the Windows tray, and a production readiness/security review are still required. The ChatGPT plugin remains separate until its actual deployed MCP endpoint is configured.
