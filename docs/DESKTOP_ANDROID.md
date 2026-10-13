# Desktop, WSA/Android and diagnostic MCP tools

This module adds opt-in remote development operations to **the existing** authenticated
RigorCore Dev MCP server/agent. It does **not** introduce another network listener,
remove OAuth, or expand workspace file access. Registering tools in a Git repository
does **not** deploy them: update/restart both the VPS service and the Windows tray agent.

## Install

On Windows 10/11, in an interactive session (a system service in Session 0 cannot
capture the user's desktop):

```powershell
git pull --ff-only origin main
py -m pip install -e ".[windows,test]"
py -m pytest -q
# Exit the existing tray agent from the notification area, then launch it again:
rigorcore-dev tray
```

On the Linux VPS:

```bash
git pull --ff-only origin main
python3 -m pip install -e ".[test]"
python3 -m pytest -q
# Restart your EXISTING rigorcore-dev server service using its normal service manager.
```

No change to Auth0 issuer, audience, OAuth tokens, agent credentials or the App-backed
plugin `.app.json` connection is required. Do not commit any credentials.
The hosted MCP server must be restarted to publish new tool schemas and the local
Windows agent must be restarted to understand the newly dispatched operations.
Reopen/reconnect the ChatGPT app connection if its tool discovery is cached.
Deployment/restarts are operator activities, not proof of successful execution.

### Required local dependencies

The `windows` extra installs Pillow, pystray, mss, pyautogui, pywinauto and
psutil. The CLI diagnostic tools use Python and OS-provided commands. Android
requires Android SDK Platform Tools `adb` on PATH; the package does not silently
download or install Android, WSA or drivers.

Check WSA/Android with `adb devices -l`. For more than one online device, pass
`serial` explicitly. A connected modified WSA installation is supported via ADB,
regardless of how it was installed. The code does not depend on a particular WSA
installer.

## Tool groups

| Group | MCP functions |
| --- | --- |
| Native screenshots | `take_screenshot`, `capture_screen`, `list_monitors`, `record_screen` |
| Windows window manager | `list_windows`, `capture_window`, `focus_window`, `resize_window` |
| Windows UI automation | `inspect_ui`, `click_element`, `gui_click`, `type_text`, `send_keys` |
| Android & WSA | `android_devices`, `android_screenshot`, `android_tap`, `android_swipe`, `android_launch`, `android_input_text`, `android_logcat`, `android_ui_tree`, `android_profile` |
| Process / performance | `inspect_process`, `debug_application`, `profile_cpu`, `profile_memory`, `inspect_gpu`, `get_crash_logs` |
| Networking | `inspect_ports`, `inspect_requests`, `test_endpoint` |
| Development & evidence | `inspect_git`, `inspect_dependencies`, `build_project`, `run_tests`, `capture_test_failure` |

The original `run_shell`/`run_shell_batch`, file APIs, task polling/logs and
OAuth protections remain available. `build_project` and `run_tests` use the
existing durable task interface and return a `task_id`; use `get_task`,
`wait_for_task` or `get_task_logs` to inspect completion.

### Media behavior

Capture operations transmit bounded Base64 through the authenticated agent
WebSocket; the MCP server validates/decodes bytes and returns an actual MCP
image content block so ChatGPT can inspect it without requesting manual uploads.
Each response is bounded to 4 MB of decoded bytes. `record_screen` returns an
**animated GIF** (1–8 seconds, 2 fps), not MP4. Extremely large captures
fail with an explicit error rather than allocating unlimited memory.

`capture_window` currently captures the window's onscreen bounding rectangle,
not a guaranteed composited application-only surface. If a different window
overlaps it, the overlap may appear. Minimized windows must be restored.
`inspect_ui` uses UI Automation; custom-rendered apps might not expose
control names, in which case use the screenshot + coordinate tools.

`type_text` uses pyautogui keyboard synthesis, which cannot reliably insert
all Unicode characters. `send_keys` only accepts a small allowlist of keys,
not arbitrary keyboard sequences.

### Approval and trust boundaries

Every remote MCP function requires authenticated OAuth/bearer access and a valid
`node_id` and workspace. The workstation chooses its own **Approve** or
**Full Auto** mode. In Approve, screenshots, GUI input, window changes, Android
actions/log access, builds/tests, endpoint probes and crash logs wait for explicit
device-side approval. Full Auto, when locally selected, skips these prompts.
The current tray confirmation text refers to shell commands even when the
pending operation is GUI/ADB; its preview still shows the exact operation and
arguments. A new tray UI should be reviewed separately; do not bypass approval.

Avoid capturing sensitive windows, credentials or personal files. Do not record
screens indefinitely. `test_endpoint` only allows HTTP(S) requests to loopback
addresses and does not follow redirects; `inspect_requests` exposes socket
metadata, **not** decrypted network payloads. Most GUI operations are available
only on an interactive Windows desktop and can be affected by the secure desktop
(UAC), RDP lock state, DPI scaling and operating-system permissions.

Tools run with the workstation user's OS permissions; this is **not** a
sandbox. The existing `run_shell` can access paths outside the workspace;
use least-privilege OS accounts and protect agent credentials.

## Verification

```bash
python -m compileall -q src
python -m pytest -q
```

On a Windows host, after updating and restarting the agent and server:

1. Call `list_nodes` to verify the expected authenticated device.
2. Call `list_monitors`, `list_windows` and `take_screenshot`; confirm image media actually appears.
3. Call `android_devices`, followed by `android_screenshot(serial=...)`; verify correct WSA/device.
4. In Approve mode, verify sensitive operations wait for local confirmation and denial propagates.
5. Run a test project and verify task state transitions to completed/failed with logs.

Unit tests cannot prove live desktop access; CI passing does not imply VPS
deployment, WSA connection or Windows permission correctness.
