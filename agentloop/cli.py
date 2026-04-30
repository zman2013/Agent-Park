"""Command-line entry point for agentloop."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import loop as scheduler
from .config import AgentConfig
from .state import LoopState
from .todolist import parse as parse_todolist
from .workspace import (
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

    p_run = sub.add_parser("run", help="Run or resume a project.")
    p_run.add_argument("design", type=Path, help="Path to design.md")
    p_run.add_argument("--fresh", action="store_true", help="Delete this workspace's state, start over")
    p_run.add_argument("--review-plan", action="store_true", help="Pause after planner for human review")
    p_run.add_argument("--max-cycles", type=int, default=None)
    p_run.add_argument("--max-cost", type=float, default=None, dest="max_cost_cny")
    p_run.add_argument(
        "--workspace",
        default=None,
        help="Workspace slug under <cwd>/.agentloop/workspaces/. "
        "Auto-generated from timestamp + design stem if omitted.",
    )
    p_run.add_argument("-v", "--verbose", action="store_true")

    p_resume = sub.add_parser("resume", help="Continue an exhausted run with extra budget.")
    p_resume.add_argument("design", type=Path, help="Path to design.md")
    p_resume.add_argument("--more-cycles", type=int, default=20)
    p_resume.add_argument("--more-cost", type=float, default=None)
    p_resume.add_argument(
        "--workspace",
        default=None,
        help="Workspace slug to resume. Required when >1 workspace exists.",
    )
    p_resume.add_argument("-v", "--verbose", action="store_true")

    p_status = sub.add_parser("status", help="Show current project progress.")
    p_status.add_argument("design", type=Path, help="Path to design.md (or its parent directory)")
    p_status.add_argument(
        "--workspace",
        default=None,
        help="Workspace slug to inspect. Required when >1 workspace exists.",
    )

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


def _resolve_project_root(design: Path) -> Path:
    return (design.parent if design.is_file() else design).resolve()


def _resolve_workspace_for_run(design: Path, slug: str | None) -> WorkspacePaths:
    project_root = _resolve_project_root(design)
    if slug is None:
        slug = generate_slug(design)
    return WorkspacePaths.for_workspace(project_root, slug)


def _resolve_workspace_for_existing(
    design: Path, slug: str | None
) -> WorkspacePaths | None:
    """Pick the workspace for resume/status. Returns None on ambiguous choice."""
    project_root = _resolve_project_root(design)
    if slug is not None:
        return WorkspacePaths.for_workspace(project_root, slug)

    existing = list_workspaces(project_root)

    if len(existing) == 1:
        return WorkspacePaths.for_workspace(project_root, existing[0])
    if not existing:
        print(f"[agentloop] no workspace found under {project_root}", file=sys.stderr)
        return None
    # >1 workspace — force explicit pick.
    print(
        "[agentloop] multiple workspaces found — pass --workspace SLUG to pick:",
        file=sys.stderr,
    )
    for s in existing:
        print(f"  {s}", file=sys.stderr)
    return None


def _cmd_run(args: argparse.Namespace) -> int:
    ws = _resolve_workspace_for_run(args.design, args.workspace)
    result = scheduler.run(
        args.design,
        fresh=args.fresh,
        review_plan=args.review_plan,
        max_cycles=args.max_cycles,
        max_cost_cny=args.max_cost_cny,
        ws=ws,
    )
    return _report_result(result)


def _cmd_resume(args: argparse.Namespace) -> int:
    ws = _resolve_workspace_for_existing(args.design, args.workspace)
    if ws is None:
        return 2
    state = LoopState.load_or_init(ws)
    if not state.exhausted_reason:
        print("[agentloop] project is not exhausted — use `agentloop run` instead.")
        return 2

    config = AgentConfig.load(ws.project_root)
    # Clear exhaustion, raise limits.
    state.exhausted_reason = None
    state.save(ws)
    new_max = state.cycle + args.more_cycles
    new_cost = (config.limits.max_cost_cny + args.more_cost) if args.more_cost else None

    result = scheduler.run(
        args.design,
        max_cycles=new_max,
        max_cost_cny=new_cost,
        ws=ws,
    )
    return _report_result(result)


def _cmd_status(args: argparse.Namespace) -> int:
    ws = _resolve_workspace_for_existing(args.design, args.workspace)
    if ws is None:
        return 2
    state = LoopState.load_or_init(ws)
    todolist = parse_todolist(ws)

    print(f"project:   {ws.project_root}")
    print(f"workspace: {ws.slug}")
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
