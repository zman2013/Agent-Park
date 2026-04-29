"""Prompt template loader.

Templates live in ``agentloop/prompts/<role>.md`` and use simple
``{{var}}`` substitution (Jinja-compatible syntax without the engine).
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_VAR_RE = re.compile(r"\{\{\s*(?P<name>[a-zA-Z_][a-zA-Z0-9_+\-\s]*)\s*\}\}")


@lru_cache(maxsize=None)
def load_prompt(role: str) -> str:
    path = PROMPTS_DIR / f"{role}.md"
    return path.read_text(encoding="utf-8")


def render(template: str, **vars: Any) -> str:
    def replace(m: re.Match[str]) -> str:
        name = m.group("name").strip()
        # support simple {{cycle+N}} arithmetic used by the dev prompt example
        if "+" in name:
            base, rest = name.split("+", 1)
            base = base.strip()
            rest = rest.strip()
            if base in vars and rest.isdigit():
                return str(vars[base] + int(rest))
        return str(vars.get(name, m.group(0)))

    return _VAR_RE.sub(replace, template)
