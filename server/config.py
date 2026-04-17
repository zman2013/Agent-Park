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


def memory_config() -> dict:
    """Return the global memory configuration with defaults."""
    cfg = get_config().get("memory", {})
    return {
        "command": cfg.get("command", "cco"),
        "max_lines": int(cfg.get("max_lines", 200)),
    }


def knowledge_config() -> dict:
    """Return the knowledge summary configuration with defaults."""
    cfg = get_config().get("knowledge", {})
    return {
        "enabled": cfg.get("enabled", True),
        "command": cfg.get("command", "minimax"),
        "errors_max_items": int(cfg.get("errors_max_items", 10)),
        "errors_max_chars": int(cfg.get("errors_max_chars", 2000)),
        "project_max_items": int(cfg.get("project_max_items", 15)),
        "project_max_chars": int(cfg.get("project_max_chars", 2000)),
        "hotfiles_max_items": int(cfg.get("hotfiles_max_items", 20)),
        "hotfiles_recent_days": int(cfg.get("hotfiles_recent_days", 7)),
        "default_task_count": int(cfg.get("default_task_count", 5)),
    }


def wiki_search_config() -> dict:
    """Return wiki search configuration with defaults."""
    cfg = get_config().get("wiki_search", {})
    wiki_cfg = wiki_ingest_config()
    return {
        "command": cfg.get("command", wiki_cfg["command"]),
        "wiki_base": cfg.get("wiki_base", wiki_cfg["wiki_base"]),
        "timeout": int(cfg.get("timeout", 30)),
        "max_pages": int(cfg.get("max_pages", 5)),
    }


def wiki_ingest_config() -> dict:
    """Return the wiki ingest configuration with defaults."""
    cfg = get_config().get("wiki_ingest", {})
    feishu_cfg = cfg.get("feishu_notify", {})
    schedule_cfg = cfg.get("schedule", {})
    return {
        "command": cfg.get("command", "qwen"),
        "wiki_base": cfg.get("wiki_base", "/data1/common/wiki"),
        "timeout": int(cfg.get("timeout", 300)),
        "max_message_chars": int(cfg.get("max_message_chars", 50000)),
        "retry_commands": cfg.get("retry_commands", ["glm", "ccs"]),
        "feishu_notify": {
            "enabled": feishu_cfg.get("enabled", False),
            "cli_path": feishu_cfg.get("cli_path", ""),
            "chat_id": feishu_cfg.get("chat_id", ""),
            "env_file": feishu_cfg.get("env_file", ""),
        },
        "schedule": {
            "enabled": schedule_cfg.get("enabled", True),
            "hour": int(schedule_cfg.get("hour", 0)),
            "minute": int(schedule_cfg.get("minute", 0)),
        },
    }
