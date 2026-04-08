"""Adapter factory — select the right protocol adapter based on command."""

from __future__ import annotations

from server.adapters.base import BaseAdapter
from server.adapters.cco import CcoAdapter
from server.adapters.codex import CodexAdapter


def get_adapter(command: str) -> BaseAdapter:
    """Return the appropriate adapter for the given agent command."""
    if "codex" in command:
        return CodexAdapter()
    return CcoAdapter()
