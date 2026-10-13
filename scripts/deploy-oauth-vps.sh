#!/usr/bin/env bash
# Deploy RigorCore Dev's tested OAuth discovery release on the existing VPS.
# Run AFTER a fast-forward git pull, with the previous commit as argument.
# Never print credentials or upload the private environment file.
set -Eeuo pipefail

ROOT=/opt/rigorcore-dev
PY="$ROOT/.venv/bin/python"
SERVICE=rigorcore-dev
BASE=https://mcp.rigorcore.com
if (( $# != 1 )); then
  echo "Usage: sudo bash scripts/deploy-oauth-vps.sh PREVIOUS_COMMIT_SHA" >&2
  exit 2
fi
PREVIOUS_SHA="$1"
INSTALLED=0

if (( EUID != 0 )); then
  echo "Run as root with sudo on the RigorCore VPS." >&2
  exit 2
fi
if [[ ! "$PREVIOUS_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Invalid previous commit SHA." >&2
  exit 2
fi
cd "$ROOT"
git cat-file -e "$PREVIOUS_SHA^{commit}"
if ! git merge-base --is-ancestor "$PREVIOUS_SHA" HEAD; then
  echo "Previous SHA is not an ancestor; refusing deployment." >&2
  exit 2
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree is not clean. Preserve changes before deployment." >&2
  exit 2
fi
if [[ ! -x "$PY" ]]; then
  echo "Python virtual environment not found." >&2
  exit 2
fi
if ! grep -qx 'RIGORCORE_MCP_AUTH_MODE=oauth' /etc/rigorcore-dev/server.env; then
  echo "OAuth mode not enabled. Refusing deployment." >&2
  exit 2
fi

restore_previous() {
  local code=$?
  trap - ERR
  if (( INSTALLED != 0 )); then
    echo "Deployment check failed; restoring previous commit..." >&2
    git reset --hard "$PREVIOUS_SHA" || true
    "$PY" -m pip install -e "$ROOT[test]" || true
    systemctl restart "$SERVICE" || true
  fi
  exit "$code"
}
trap restore_previous ERR

mkdir -p /var/backups/rigorcore-dev
printf '%s\n' "$PREVIOUS_SHA" >/var/backups/rigorcore-dev/previous-commit-sha
chmod 600 /var/backups/rigorcore-dev/previous-commit-sha

echo "[1/6] Install and verify Python"
INSTALLED=1
"$PY" -m pip install -e "$ROOT[test]"
"$PY" -m pytest -q

echo "[2/6] Restart the MCP service"
systemctl restart "$SERVICE"
sleep 2
systemctl is-active --quiet "$SERVICE"

echo "[3/6] Confirm Auth0 protected-resource metadata"
metadata="$(curl --fail --silent --show-error --max-time 20 "$BASE/.well-known/oauth-protected-resource")"
printf '%s' "$metadata" | "$PY" -c 'import sys,json; d=json.load(sys.stdin); assert d["resource"]=="https://mcp.rigorcore.com/mcp/"; assert "rigorcore:access" in d["scopes_supported"]; print("Resource metadata OK")'

echo "[4/6] Check missing-token GET returns 401"
status="$(curl --silent --show-error --max-time 20 -o /dev/null -w '%{http_code}' "$BASE/mcp/")"
test "$status" = 401

echo "[5/6] Verify unauthenticated initialize and tool descriptions"
initialized="$(curl --fail --silent --show-error --max-time 20 -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"oauth-deploy-probe","version":"1.0"}}}' "$BASE/mcp/")"
printf '%s' "$initialized" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); assert d["result"]["serverInfo"]["name"]; print("Initialize OK")'
discovery="$(curl --fail --silent --show-error --max-time 20 -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -H 'MCP-Protocol-Version: 2025-06-18' --data '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' "$BASE/mcp/")"
printf '%s' "$discovery" | "$PY" -c 'import json,sys; tools={t["name"]:t for t in json.load(sys.stdin)["result"]["tools"]}; assert {"get_profile","list_nodes","write_files","run_shell"}.issubset(tools); assert all(t["securitySchemes"]==[{"type":"oauth2","scopes":["rigorcore:access"]}] for t in tools.values()); print("Tool discovery OK")'

echo "[6/6] Confirm tool execution is denied without auth"
denied="$(curl --fail --silent --show-error --max-time 20 -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -H 'MCP-Protocol-Version: 2025-06-18' --data '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_nodes","arguments":{}}}' "$BASE/mcp/")"
printf '%s' "$denied" | "$PY" -c 'import json,sys; d=json.load(sys.stdin)["result"]; assert d["isError"] is True; assert "mcp/www_authenticate" in d["_meta"]; print("Unauthenticated tool call denied")'

trap - ERR
echo "OAuth MCP deployment healthy. No credentials printed."
