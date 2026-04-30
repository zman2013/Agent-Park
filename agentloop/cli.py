"""Command-line entry point for agentloop."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

from . import loop as scheduler
from .config import AgentConfig, seed_workspace_config
from .state import LoopState
from .todolist import parse as parse_todolist
from .workspace import (
    AGENTLOOP_DIR,
    WORKSPACES_SUBDIR,
    WorkspacePaths,
    generate_slug,
    list_workspaces,
)

logger = logging.getLogger("agentloop")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentloop",
        description="Planner → PM → Dev → QA scheduler for design.md-driven projects.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("run", "Run or resume a project."),
        ("resume", "Continue an exhausted run with extra budget."),
        ("status", "Show current project progress."),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("design", type=Path, help="Path to design.md")
        p.add_argument(
            "--workspace-dir",
            type=Path,
            default=None,
            dest="workspace_dir",
            help="Absolute path to the workspace directory "
            "(<project>/.agentloop/workspaces/<slug>/). Takes precedence over "
            "--project-root/--workspace.",
        )
        p.add_argument(
            "--project-root",
            type=Path,
            default=None,
            dest="project_root",
            help="Project root under which .agentloop/workspaces/<slug>/ lives. "
            "Required (together with --workspace for resume/status, or for run "
            "without --workspace-dir).",
        )
        p.add_argument(
            "--workspace",
            default=None,
            help="Workspace slug. For `run`, auto-generated from timestamp + "
            "design stem if both --workspace and --workspace-dir are omitted.",
        )
        p.add_argument("-v", "--verbose", action="store_true")

    p_run = sub.choices["run"]
    p_run.add_argument("--fresh", action="store_true", help="Delete this workspace's state, start over")
    p_run.add_argument("--review-plan", action="store_true", help="Pause after planner for human review")
    p_run.add_argument("--max-cycles", type=int, default=None)
    p_run.add_argument("--max-cost", type=float, default=None, dest="max_cost_cny")
    p_run.add_argument(
        "--config",
        type=Path,
        default=None,
        dest="config_template",
        help="Template config.toml seeded into the workspace on first creation.",
    )

    p_resume = sub.choices["resume"]
    p_resume.add_argument("--more-cycles", type=int, default=20)
    p_resume.add_argument("--more-cost", type=float, default=None)

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if getattr(args, "verbose", False) else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "run":
        return _cmd_run(args)
    if args.command == "resume":
        return _cmd_resume(args)
    if args.command == "status":
        return _cmd_status(args)

    parser.print_help()
    return 2


def _workspace_from_dir_arg(wd: Path) -> WorkspacePaths | None:
    """Build a WorkspacePaths from ``--workspace-dir``, rejecting paths that
    aren't shaped like ``<project>/.agentloop/workspaces/<slug>``.

    ``--fresh`` routes the resolved workspace dir into ``_wipe_agentloop_state``
    which deletes nearly everything inside it; without this gate a mistyped
    ``agentloop run --fresh --workspace-dir /home/user`` would blow away
    arbitrary directories. We mirror the confinement that the slug-based
    ``for_workspace`` composer gives us for free: the parent must be
    ``.agentloop/workspaces`` and the basename must pass slug validation.
    """
    resolved = Path(wd).resolve()
    parent = resolved.parent
    grandparent = parent.parent
    if parent.name != WORKSPACES_SUBDIR or grandparent.name != AGENTLOOP_DIR:
        print(
            "[agentloop] --workspace-dir must point inside "
            f"<project>/{AGENTLOOP_DIR}/{WORKSPACES_SUBDIR}/<slug>; got {resolved}",
            file=sys.stderr,
        )
        return None
    try:
        return WorkspacePaths.for_workspace(grandparent.parent, resolved.name)
    except ValueError as e:
        print(f"[agentloop] invalid workspace slug in --workspace-dir: {e}", file=sys.stderr)
        return None


def _default_project_root(design: Path | None) -> Path:
    """Pick the default project root when neither ``--project-root`` nor
    ``--workspace-dir`` was given.

    Prefer the design path's directory so ``agentloop run /other/repo/design.md``
    from an unrelated cwd still anchors the workspace under ``/other/repo``
    (agents need ancestor-based git/project context). If ``design`` is itself
    a directory (``agentloop status .``), treat it as the candidate root
    directly — the old resolver accepted this shape and callers rely on it.
    If the candidate lives inside ``<root>/.agentloop/workspaces/<slug>/`` —
    the nested-bootstrap trap that originally motivated dropping design-based
    defaults — walk up to the real project root that contains ``.agentloop``.
    Fall back to ``cwd`` only when no design path is available.
    """
    if design is None:
        return Path.cwd()
    resolved = Path(design).resolve()
    candidate = resolved if resolved.is_dir() else resolved.parent
    # Look for an ancestor segment named AGENTLOOP_DIR. If found, the real
    # project root is that segment's parent — prevents a workspace from being
    # mistaken for a new project root when the design.md was passed via a
    # symlink inside the workspace.
    for p in (candidate, *candidate.parents):
        if p.name == AGENTLOOP_DIR:
            return p.parent
    return candidate


def _resolve_workspace_for_run(args: argparse.Namespace) -> WorkspacePaths | None:
    """Pick the workspace for `run`.

    Precedence:
        1. ``--workspace-dir <abs>`` — direct; slug-shaped path required.
        2. ``--project-root <p>`` + optional ``--workspace <slug>`` — compose;
           auto-generate the slug from design stem when omitted.
        3. Neither → default project root derived from the design path (see
           :func:`_default_project_root`), with an explicit guard against the
           nested-bootstrap scenario where the design lives inside an existing
           workspace.
    """
    wd = getattr(args, "workspace_dir", None)
    if wd is not None:
        return _workspace_from_dir_arg(wd)

    project_root = getattr(args, "project_root", None)
    if project_root is None:
        project_root = _default_project_root(args.design)
    slug = args.workspace or generate_slug(args.design)
    return WorkspacePaths.for_workspace(Path(project_root), slug)


def _resolve_workspace_for_existing(args: argparse.Namespace) -> WorkspacePaths | None:
    """Pick the workspace for resume/status. Returns None on ambiguous choice."""
    wd = getattr(args, "workspace_dir", None)
    if wd is not None:
        return _workspace_from_dir_arg(wd)

    project_root = getattr(args, "project_root", None)
    if project_root is None:
        project_root = _default_project_root(getattr(args, "design", None))

    root = Path(project_root).resolve()
    slug = args.workspace
    if slug is not None:
        return WorkspacePaths.for_workspace(root, slug)

    existing = list_workspaces(root)
    if len(existing) == 1:
        return WorkspacePaths.for_workspace(root, existing[0])
    if not existing:
        print(f"[agentloop] no workspace found under {root}", file=sys.stderr)
        return None
    print(
        "[agentloop] multiple workspaces found — pass --workspace SLUG to pick:",
        file=sys.stderr,
    )
    for s in existing:
        print(f"  {s}", file=sys.stderr)
    return None


def _link_or_copy_design(link: Path, target: Path) -> None:
    """Expose ``target`` as ``link`` inside the workspace.

    The subprocess cwd is the workspace dir, so every prompt that says
    "design.md is in cwd" assumes this file exists. Mirrors
    ``server/agentloop_manager.start``'s pattern: try symlink first, fall
    back to copy2 on FS that rejects symlinks (Windows without privilege,
    restricted chroots).

    Two self-link guards, both necessary:
      * If ``link`` already resolves to ``target``, nothing to do — avoids
        disturbing an open fd on reused workspaces.
      * If ``link`` path *equals* ``target`` path (user passed the workspace-
        local design path, possibly before the file exists), we cannot
        symlink or copy-from-self; just leave whatever's there. Without this
        guard, a missing ``link`` would fall through to ``symlink_to(target)``
        and create a self-referential loop — the scheduler then errors out
        with "design not found" and the workspace is wedged until manually
        cleaned up.

    If both symlink and copy fail AND there is no existing ``link``, we
    surface the error but don't abort — the scheduler will immediately raise
    "design not found" when it tries to read the missing file, which is a
    clearer signal than failing here. If the link already existed and the
    replacement fails, we leave the old content in place rather than
    deleting it and leaving the workspace without a design.md.
    """
    if link == target:
        return
    try:
        current = link.resolve(strict=True)
    except OSError:
        current = None
    if current == target:
        return

    tmp = link.with_name(link.name + ".tmp")
    if tmp.is_symlink() or tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass

    try:
        tmp.symlink_to(target)
    except OSError:
        try:
            shutil.copy2(target, tmp)
        except OSError as e:
            print(
                f"[agentloop] warning: could not stage design.md in workspace "
                f"(symlink/copy failed: {e}); leaving existing link untouched",
                file=sys.stderr,
            )
            return

    try:
        tmp.replace(link)
    except OSError as e:
        print(
            f"[agentloop] warning: could not move staged design.md into place: {e}",
            file=sys.stderr,
        )
        try:
            tmp.unlink()
        except OSError:
            pass


def _cmd_run(args: argparse.Namespace) -> int:
    ws = _resolve_workspace_for_run(args)
    if ws is None:
        return 2
    ws.workspace_dir.mkdir(parents=True, exist_ok=True)
    try:
        seed_workspace_config(ws.workspace_dir, template=getattr(args, "config_template", None))
    except FileNotFoundError as e:
        print(f"[agentloop] {e}", file=sys.stderr)
        return 2
    # Resolve the design path to absolute — subprocess cwd is ws.workspace_dir,
    # so any relative path the user typed (``agentloop run design.md``) would
    # otherwise be unreachable by the planner / dev / qa agents.
    design_abs = Path(args.design).resolve()
    _link_or_copy_design(ws.design, design_abs)
    result = scheduler.run(
        design_abs,
        fresh=args.fresh,
        review_plan=args.review_plan,
        max_cycles=args.max_cycles,
        max_cost_cny=args.max_cost_cny,
        ws=ws,
    )
    return _report_result(result)


def _cmd_resume(args: argparse.Namespace) -> int:
    ws = _resolve_workspace_for_existing(args)
    if ws is None:
        return 2
    state = LoopState.load_or_init(ws)
    if not state.exhausted_reason:
        print("[agentloop] project is not exhausted — use `agentloop run` instead.")
        return 2

    config = AgentConfig.load(ws.workspace_dir)
    # Clear exhaustion, raise limits.
    state.exhausted_reason = None
    state.save(ws)
    new_max = state.cycle + args.more_cycles
    new_cost = (config.limits.max_cost_cny + args.more_cost) if args.more_cost else None

    result = scheduler.run(
        Path(args.design).resolve(),
        max_cycles=new_max,
        max_cost_cny=new_cost,
        ws=ws,
    )
    return _report_result(result)


def _cmd_status(args: argparse.Namespace) -> int:
    ws = _resolve_workspace_for_existing(args)
    if ws is None:
        return 2
    state = LoopState.load_or_init(ws)
    todolist = parse_todolist(ws)

    print(f"workspace: {ws.workspace_dir}")
    print(f"cycles:    {state.cycle}")
    print(f"cost:      ¥{state.total_cost_cny:.2f}")
    print(f"rollbacks: {len(state.rollbacks)}")
    if state.exhausted_reason:
        print(f"exhausted: {state.exhausted_reason}")
    if state.last_decision:
        d = state.last_decision
        print(f"last decision: {d.next} {d.item_id or ''} — {d.reason}")

    if todolist.items:
        print()
        print("items:")
        for it in todolist.items:
            print(f"  {it.id:<7} {it.type:<13} {it.status:<14} {it.title}")
    else:
        print()
        print("items: (none — run `agentloop run design.md` to plan)")

    return 0


def _report_result(result: scheduler.LoopResult) -> int:
    tag = {
        scheduler.ExitCode.SUCCESS: "SUCCESS",
        scheduler.ExitCode.PARTIAL_SUCCESS: "PARTIAL_SUCCESS",
        scheduler.ExitCode.EXHAUSTED: "EXHAUSTED",
        scheduler.ExitCode.ERROR: "ERROR",
    }[result.code]
    print(f"[agentloop] {tag}: {result.reason}")
    return result.code.value


if __name__ == "__main__":
    sys.exit(main())
