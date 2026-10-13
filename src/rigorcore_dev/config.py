"""Validate agent configuration and secrets."""
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

@dataclass(frozen=True)
class AgentConfig:
    node_id: str
    name: str
    server: str
    workspaces: dict[str, str]
    mode: str = "approve"

    @classmethod
    def load(cls, filename: str) -> "AgentConfig":
        data = json.loads(Path(filename).expanduser().read_text(encoding="utf-8-sig"))
        config = cls(**data)
        if not SAFE_ID.fullmatch(config.node_id) or not config.name:
            raise ValueError("invalid node identity")
        if config.mode not in ("approve", "auto"):
            raise ValueError("mode must be approve or auto")
        if not (config.server.startswith("wss://") or
                config.server.startswith("ws://127.0.0.1:") or
                config.server.startswith("ws://localhost:")):
            raise ValueError("remote connections require wss://")
        if not config.workspaces or any(not SAFE_ID.fullmatch(n) for n in config.workspaces):
            raise ValueError("invalid workspaces")
        return config

def require_secret(name: str) -> str:
    value = os.environ.get(name, "")
    if len(value) < 24:
        raise RuntimeError(name + " must contain at least 24 characters")
    return value

def state_dir() -> Path:
    folder = Path(os.getenv("RIGORCORE_STATE_DIR", "~/.local/state/rigorcore-dev")).expanduser()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder
