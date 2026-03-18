"""Load config.json from project root."""

from __future__ import annotations

import json
import os
from pathlib import Path

_CONFIG: dict | None = None

def _find_config() -> Path:
    """Find config.json relative to this file (project root)."""
    here = Path(__file__).resolve().parent  # server/
    return here.parent / "config.json"

def get_config() -> dict:
    global _CONFIG
    if _CONFIG is None:
        path = _find_config()
        if path.exists():
            with open(path) as f:
                _CONFIG = json.load(f)
        else:
            _CONFIG = {}
    return _CONFIG

def server_host() -> str:
    return get_config().get("server", {}).get("host", "0.0.0.0")

def server_port() -> int:
    return get_config().get("server", {}).get("port", 8001)

def agent_defaults() -> list[dict]:
    return get_config().get("agents", [
        {"name": "Scheduler", "command": "cco", "cwd": ""},
        {"name": "Codegen", "command": "cco", "cwd": ""},
        {"name": "Reviewer", "command": "cco", "cwd": ""},
    ])
