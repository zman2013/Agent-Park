"""Tests for agentloop.workspace.WorkspacePaths path resolution + slug helpers."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from agentloop.workspace import (
    AGENTLOOP_DIR,
    WORKSPACES_SUBDIR,
    WorkspacePaths,
    generate_slug,
    list_workspaces,
)
from agentloop.state import LoopState
from agentloop.todolist import Item, Todolist, parse as parse_todolist, write as write_todolist
from agentloop.loop import _wipe_agentloop_state


# ---------- path resolution ----------


def test_workspace_paths_point_under_slug_dir(tmp_path: Path):
    ws = WorkspacePaths.for_workspace(tmp_path, "20260430-120000-feat-a")
    base = tmp_path / AGENTLOOP_DIR / WORKSPACES_SUBDIR / "20260430-120000-feat-a"
    assert ws.workspace_dir == base
    assert ws.state_file == base / "state.json"
    assert ws.todolist == base / "todolist.md"
    assert ws.runs_dir == base / "runs"
    assert ws.design == base / "design.md"
    # subprocess cwd stays at project root — that's where config.toml lives and
    # agents need project-level git access
    assert ws.subprocess_cwd == tmp_path.resolve()


def test_two_workspaces_same_cwd_are_isolated(tmp_path: Path):
    a = WorkspacePaths.for_workspace(tmp_path, "ws-a")
    b = WorkspacePaths.for_workspace(tmp_path, "ws-b")
    assert a.state_file != b.state_file
    assert a.todolist != b.todolist
    assert a.runs_dir != b.runs_dir
    assert a.subprocess_cwd == b.subprocess_cwd  # same project root


def test_for_workspace_rejects_empty_slug(tmp_path: Path):
    with pytest.raises(ValueError):
        WorkspacePaths.for_workspace(tmp_path, "")


@pytest.mark.parametrize(
    "bad_slug",
    [
        "../escape",
        "foo/bar",
        "/abs/path",
        "..",
        ".hidden",
        "-leading-dash",
        "has space",
        "nul\x00byte",
    ],
)
def test_for_workspace_rejects_unsafe_slug(tmp_path: Path, bad_slug: str):
    with pytest.raises(ValueError):
        WorkspacePaths.for_workspace(tmp_path, bad_slug)


# ---------- slug generation ----------


def test_generate_slug_format():
    slug = generate_slug(Path("/tmp/My Feature Doc.md"), now=datetime(2026, 4, 30, 12, 0, 0))
    assert slug == "20260430-120000-my-feature-doc"


def test_generate_slug_truncates_long_stem():
    long_name = "this-is-a-very-long-design-filename-that-should-be-cut"
    slug = generate_slug(Path(f"/tmp/{long_name}.md"), now=datetime(2026, 4, 30, 12, 0, 0))
    # stem portion should not exceed 24 chars
    after_ts = slug[len("20260430-120000-"):]
    assert len(after_ts) <= 24


def test_generate_slug_without_design():
    slug = generate_slug(None, now=datetime(2026, 4, 30, 12, 0, 0))
    assert slug == "20260430-120000"


# ---------- discovery ----------


def test_list_workspaces_sorted(tmp_path: Path):
    ws_root = tmp_path / AGENTLOOP_DIR / WORKSPACES_SUBDIR
    for name in ("20260430-120000-z", "20260430-120000-a", "20260501-080000"):
        (ws_root / name).mkdir(parents=True)
    assert list_workspaces(tmp_path) == [
        "20260430-120000-a",
        "20260430-120000-z",
        "20260501-080000",
    ]


def test_list_workspaces_empty(tmp_path: Path):
    assert list_workspaces(tmp_path) == []


# ---------- round-trip through state + todolist ----------


def test_state_persist_round_trip_in_workspace(tmp_path: Path):
    ws = WorkspacePaths.for_workspace(tmp_path, "20260430-120000-feat")
    s = LoopState(cycle=3)
    s.save(ws)
    assert ws.state_file.exists()
    assert ws.state_file.parent == tmp_path / AGENTLOOP_DIR / WORKSPACES_SUBDIR / "20260430-120000-feat"
    loaded = LoopState.load_or_init(ws)
    assert loaded.cycle == 3


def test_todolist_write_isolated_per_workspace(tmp_path: Path):
    a = WorkspacePaths.for_workspace(tmp_path, "ws-a")
    b = WorkspacePaths.for_workspace(tmp_path, "ws-b")
    tl_a = Todolist(metadata={"project": "A"},
                    items=[Item(id="T-001", type="dev", status="pending", title="A")])
    tl_b = Todolist(metadata={"project": "B"},
                    items=[Item(id="T-001", type="dev", status="pending", title="B")])
    write_todolist(a, tl_a)
    write_todolist(b, tl_b)
    assert parse_todolist(a).items[0].title == "A"
    assert parse_todolist(b).items[0].title == "B"


# ---------- wipe ----------


def test_wipe_workspace_preserves_siblings_and_config(tmp_path: Path):
    # set up: two workspaces + shared config.toml
    config = tmp_path / AGENTLOOP_DIR / "config.toml"
    config.parent.mkdir()
    config.write_text("[limits]\nmax_cycles=9\n", encoding="utf-8")

    ws_a = WorkspacePaths.for_workspace(tmp_path, "ws-a")
    ws_b = WorkspacePaths.for_workspace(tmp_path, "ws-b")
    ws_a.workspace_dir.mkdir(parents=True)
    ws_b.workspace_dir.mkdir(parents=True)
    (ws_a.state_file).write_text("{}", encoding="utf-8")
    (ws_b.state_file).write_text("{}", encoding="utf-8")

    _wipe_agentloop_state(ws_a)

    # ws_a gone (then recreated empty); ws_b + config intact
    assert ws_a.workspace_dir.exists()
    assert not ws_a.state_file.exists()
    assert ws_b.state_file.exists()
    assert config.exists()
    assert "max_cycles=9" in config.read_text(encoding="utf-8")


# ---------- loop_id stability (via server manager) ----------


def test_server_loop_id_differs_per_slug():
    from server.agentloop_manager import _loop_id
    a = _loop_id("/tmp/proj", "ws-a")
    b = _loop_id("/tmp/proj", "ws-b")
    assert a != b


def test_server_loop_id_stable_for_same_input():
    """Same (cwd, slug) must produce the same 8-char id across calls."""
    from server.agentloop_manager import _loop_id
    assert _loop_id("/tmp/proj", "ws-a") == _loop_id("/tmp/proj", "ws-a")
