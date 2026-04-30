"""Workspace path resolution for agentloop.

Single source of truth for where state.json / todolist.md / runs/ / stdout.log
live, and which directory is the "project root" (for config.toml and as the
subprocess cwd when we launch cco/ccs).

Every loop has its own workspace directory at
``<cwd>/.agentloop/workspaces/<slug>/`` containing state.json, todolist.md,
runs/, stdout.log, and a design.md symlink. ``<cwd>/.agentloop/config.toml``
stays shared across workspaces.

Construct via :meth:`WorkspacePaths.for_workspace`; the rest of the code only
sees the resolved file/directory paths.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

AGENTLOOP_DIR = ".agentloop"
WORKSPACES_SUBDIR = "workspaces"
STATE_FILE = "state.json"
TODOLIST_FILE = "todolist.md"
RUNS_SUBDIR = "runs"
STDOUT_LOG = "stdout.log"
DESIGN_FILE = "design.md"

# Slug safety — accepted characters must keep the path confined to
# ``<cwd>/.agentloop/workspaces/<slug>/``. The set is intentionally narrow:
# letters/digits/underscore/dot/dash. Anything else (slashes, whitespace,
# leading ``.``/``-``) is rejected so API-sourced slugs can't escape.
_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")


def _validate_slug(slug: str) -> None:
    if not slug:
        raise ValueError("workspace slug must be non-empty")
    if not _SAFE_SLUG_RE.fullmatch(slug):
        raise ValueError(
            "workspace slug must match [A-Za-z0-9_][A-Za-z0-9._-]* — got "
            f"{slug!r}"
        )
    if ".." in slug.split("."):
        # Defensive: block ``..`` segments even if the regex technically lets
        # repeated dots through.
        raise ValueError(f"workspace slug must not contain '..' segments: {slug!r}")


@dataclass(frozen=True)
class WorkspacePaths:
    """Resolved paths for one agentloop run."""

    project_root: Path
    slug: str

    # ------- primary directories ------------------------------------------

    @property
    def workspace_dir(self) -> Path:
        """Directory that holds state.json / todolist.md / runs/ / design.md."""
        return self.project_root / AGENTLOOP_DIR / WORKSPACES_SUBDIR / self.slug

    @property
    def subprocess_cwd(self) -> Path:
        """cwd to pass to cco/ccs subprocesses — always the project root."""
        return self.project_root

    # ------- file paths ---------------------------------------------------

    @property
    def state_file(self) -> Path:
        return self.workspace_dir / STATE_FILE

    @property
    def todolist(self) -> Path:
        return self.workspace_dir / TODOLIST_FILE

    @property
    def runs_dir(self) -> Path:
        return self.workspace_dir / RUNS_SUBDIR

    @property
    def design(self) -> Path:
        return self.workspace_dir / DESIGN_FILE

    @property
    def stdout_log(self) -> Path:
        return self.workspace_dir / STDOUT_LOG

    # ------- constructors -------------------------------------------------

    @classmethod
    def for_workspace(cls, project_root: Path, slug: str) -> "WorkspacePaths":
        _validate_slug(slug)
        return cls(project_root=Path(project_root).resolve(), slug=slug)


# ---- slug generation / discovery --------------------------------------------

_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


def _kebab(s: str, max_len: int = 24) -> str:
    s = _SLUG_RE.sub("-", s).strip("-").lower()
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "loop"


def generate_slug(design_path: Path | None = None, *, now: datetime | None = None) -> str:
    """Produce ``YYYYMMDD-HHMMSS-<design-stem>`` in UTC.

    Callers that get ``mkdir(exist_ok=False)`` collisions should retry with a
    uuid-suffixed slug — see :func:`agentloop_manager.start` for the retry
    shape; this function is intentionally deterministic given inputs.
    """
    ts = (now or datetime.utcnow()).strftime("%Y%m%d-%H%M%S")
    stem = ""
    if design_path is not None:
        stem = _kebab(Path(design_path).stem)
    return f"{ts}-{stem}" if stem else ts


def list_workspaces(project_root: Path) -> list[str]:
    """Return workspace slugs that exist on disk under ``project_root``.

    Sorted lexicographically — since slugs start with a UTC timestamp, this is
    also chronological order.
    """
    root = Path(project_root) / AGENTLOOP_DIR / WORKSPACES_SUBDIR
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())
