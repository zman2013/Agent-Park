"""Config loading for agentloop.

Resolution order (later overrides earlier):
    1. Built-in defaults (planner = cco, dev/qa = ccs, pm = code-version)
    2. ``~/.agentloop/config.toml`` (global fallback)
    3. ``<cwd>/.agentloop/config.toml`` (project level)
    4. CLI flags (applied by caller)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib  # type: ignore

from .state import Limits

CONFIG_FILE = "config.toml"
STATE_DIR = ".agentloop"


@dataclass
class AgentBackend:
    cmd: str | None = None          # None => code-version (only legal for PM)
    model: str | None = None
    timeout_sec: int = 1800
    # Max seconds between two consecutive stdout lines before we decide the
    # CLI has stalled (e.g. claude-cli boundary bug where the session stays
    # alive but never issues another LLM request). 0 disables stall detection.
    stall_timeout_sec: int = 180
    # whether this role is implemented in code (no LLM call)
    is_code: bool = False


@dataclass
class AgentConfig:
    limits: Limits = field(default_factory=Limits)
    planner: AgentBackend = field(default_factory=lambda: AgentBackend(cmd="cco"))
    dev: AgentBackend = field(default_factory=lambda: AgentBackend(cmd="ccs"))
    qa: AgentBackend = field(default_factory=lambda: AgentBackend(cmd="ccs"))
    pm: AgentBackend = field(default_factory=lambda: AgentBackend(cmd=None, is_code=True))
    review_plan: bool = False

    def backend_for(self, role: str) -> AgentBackend:
        try:
            return getattr(self, role)
        except AttributeError as e:
            raise KeyError(f"unknown role: {role}") from e

    @classmethod
    def load(cls, cwd: Path) -> "AgentConfig":
        config = cls()
        config._merge_from(_user_config_path())
        config._merge_from(cwd / STATE_DIR / CONFIG_FILE)
        return config

    def _merge_from(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return

        lim = data.get("limits", {})
        if "max_cycles" in lim:
            self.limits.max_cycles = int(lim["max_cycles"])
        if "max_item_attempts" in lim:
            self.limits.max_item_attempts = int(lim["max_item_attempts"])
        if "max_cost_cny" in lim:
            self.limits.max_cost_cny = float(lim["max_cost_cny"])
        if "max_planner_attempts" in lim:
            self.limits.max_planner_attempts = int(lim["max_planner_attempts"])
        if "max_fingerprint_stuck" in lim:
            self.limits.max_fingerprint_stuck = int(lim["max_fingerprint_stuck"])

        if "review_plan" in data:
            self.review_plan = bool(data["review_plan"])

        agents = data.get("agents", {})
        for role in ("planner", "dev", "qa", "pm"):
            cfg = agents.get(role)
            if not cfg:
                continue
            backend = self.backend_for(role)
            if "cmd" in cfg:
                backend.cmd = cfg["cmd"] or None
                backend.is_code = backend.cmd is None
            if "model" in cfg:
                backend.model = cfg["model"] or None
            if "timeout_sec" in cfg:
                backend.timeout_sec = int(cfg["timeout_sec"])
            if "stall_timeout_sec" in cfg:
                backend.stall_timeout_sec = int(cfg["stall_timeout_sec"])


def _user_config_path() -> Path:
    return Path.home() / STATE_DIR / CONFIG_FILE
