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


def compact_config() -> dict:
    """Return the compact (auto-trigger / warning) configuration with defaults.

    Thresholds are absolute token counts on the per-turn input
    (input_tokens + cache_read + cache_creation). Ratio-based config was
    removed because:
      - cco's assistant chunk usually lacks ``context_window``, so ratio
        decisions had to guess the window and mis-fired on 1M models.
      - Users generally care about "compact before the next turn costs too
        much" rather than a fraction of an opaque window that varies by
        model.

    Defaults target the common 200k-window case (Claude Sonnet/Opus). They
    roughly correspond to the previous ratio behaviour (warn ~78%, compact
    ~85% of 200k). 1M-window users should override via ``config.json``
    (e.g. ``warn_tokens: 820000`` / ``auto_compact_tokens: 880000``).
    """
    cfg = get_config().get("compact", {})
    return {
        "warn_tokens": int(cfg.get("warn_tokens", 156_000)),
        "auto_compact_tokens": int(cfg.get("auto_compact_tokens", 170_000)),
        "auto_compact_enabled": bool(cfg.get("auto_compact_enabled", True)),
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
    """Return wiki search configuration with defaults.

    Path fields (``memforge_script``, ``wiki_base``) have no hardcoded defaults;
    callers must supply them via config.json. Empty string means "not
    configured" — the relevant code path is expected to degrade gracefully.
    """
    cfg = get_config().get("wiki_search", {})
    wiki_cfg = wiki_ingest_config()
    return {
        "backend": cfg.get("backend", "local"),
        "memforge_script": cfg.get("memforge_script", ""),
        "command": cfg.get("command", wiki_cfg["command"]),
        "wiki_base": cfg.get("wiki_base", wiki_cfg["wiki_base"]),
        "timeout": int(cfg.get("timeout", 30)),
        "max_pages": int(cfg.get("max_pages", 5)),
        "top_k": int(cfg.get("top_k", 5)),
    }


def wiki_ingest_config() -> dict:
    """Return the wiki ingest configuration with defaults.

    Path fields (``wiki_base``, ``memforge_reindex_script``) have no hardcoded
    defaults; callers must supply them via config.json. Empty string means
    "not configured" — callers should handle that explicitly.
    """
    cfg = get_config().get("wiki_ingest", {})
    feishu_cfg = cfg.get("feishu_notify", {})
    schedule_cfg = cfg.get("schedule", {})
    return {
        "command": cfg.get("command", "qwen"),
        "wiki_base": cfg.get("wiki_base", ""),
        "timeout": int(cfg.get("timeout", 300)),
        "max_message_chars": int(cfg.get("max_message_chars", 50000)),
        "retry_commands": cfg.get("retry_commands", ["glm", "ccs"]),
        "memforge_reindex_enabled": bool(cfg.get("memforge_reindex_enabled", False)),
        "memforge_reindex_script": cfg.get("memforge_reindex_script", ""),
        "memforge_reindex_timeout": int(cfg.get("memforge_reindex_timeout", 600)),
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
