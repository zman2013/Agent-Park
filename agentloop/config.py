"""Config loading for agentloop.

Resolution order (later overrides earlier):
    1. Built-in defaults (planner = cco, dev/qa = ccs, pm = code-version)
    2. ``~/.agentloop/config.toml`` (global fallback)
    3. ``<workspace_dir>/config.toml`` (per-workspace, the primary knob)
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
from .workspace import CONFIG_FILE, AGENTLOOP_DIR


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
    def load(cls, workspace_dir: Path) -> "AgentConfig":
        """Load config, layering user global → workspace-local.

        ``workspace_dir`` is the absolute path of the workspace (``<project>/
        .agentloop/workspaces/<slug>/``). Its ``config.toml`` overrides any
        global values from ``~/.agentloop/config.toml``.
        """
        config = cls()
        config._merge_from(_user_config_path())
        config._merge_from(Path(workspace_dir) / CONFIG_FILE)
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
    return Path.home() / AGENTLOOP_DIR / CONFIG_FILE


def seed_workspace_config(workspace_dir: Path, *, template: Path | None = None) -> Path | None:
    """Ensure ``workspace_dir/config.toml`` exists, seeding from a fallback chain.

    Fallback order for the template:
    1. Explicit ``template`` argument (CLI flag)
    2. ``~/.agentloop/config.toml`` (user global)
    3. No file created — loader falls back to built-in defaults

    Idempotent: if the destination already exists it's left untouched so hand
    edits survive. Returns the path that was written, or ``None`` if nothing
    was written (either no template available or destination already existed).
    """
    dest = Path(workspace_dir) / CONFIG_FILE
    if dest.exists():
        return None

    source: Path | None = None
    if template is not None:
        t = Path(template)
        if t.is_file():
            source = t
    if source is None:
        user = _user_config_path()
        if user.is_file():
            source = user

    if source is None:
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(source.read_bytes())
    return dest
