"""Tests for the summary stage: config plumbing, _finalize fallback, injector."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentloop import loop as scheduler
from agentloop.config import AgentConfig, FeishuConfig, SummaryConfig
from agentloop.loop import ExitCode, LoopResult, _finalize
from agentloop.state import LoopState
from agentloop.workspace import WorkspacePaths


def _ws(tmp_path: Path, slug: str = "test-ws") -> WorkspacePaths:
    ws = WorkspacePaths.for_workspace(tmp_path, slug)
    ws.workspace_dir.mkdir(parents=True, exist_ok=True)
    return ws


# ---------- config parsing -------------------------------------------------


def test_config_parses_summary_section(tmp_path: Path):
    (tmp_path / "config.toml").write_text(
        """
[summary]
enabled = true
feishu_enabled = false

[summary.feishu]
cli_path = "/opt/feishu/cli.py"
chat_id = "oc_abc"
env_file = "/opt/feishu/env"

[agents.summary]
cmd = "custom-cli"
""".strip(),
        encoding="utf-8",
    )
    cfg = AgentConfig.load(tmp_path)
    assert cfg.summary_config.enabled is True
    assert cfg.summary_config.feishu_enabled is False
    assert cfg.summary_config.feishu.cli_path == "/opt/feishu/cli.py"
    assert cfg.summary_config.feishu.chat_id == "oc_abc"
    assert cfg.summary.cmd == "custom-cli"


def test_config_defaults_are_feishu_enabled():
    cfg = AgentConfig()
    assert cfg.summary_config.enabled is True
    assert cfg.summary_config.feishu_enabled is True
    assert cfg.summary.cmd == "cco"


# ---------- _finalize ------------------------------------------------------


def _make_config(
    *,
    enabled: bool = True,
    feishu_enabled: bool = False,
) -> AgentConfig:
    cfg = AgentConfig()
    cfg.summary_config = SummaryConfig(
        enabled=enabled,
        feishu_enabled=feishu_enabled,
        feishu=FeishuConfig(),
    )
    return cfg


def test_finalize_disabled_returns_result_unchanged(tmp_path: Path, monkeypatch):
    ws = _ws(tmp_path)
    cfg = _make_config(enabled=False)
    state = LoopState()
    # If disabled, summary agent must not be called at all.
    called = {"summary": False, "notify": False}

    def _should_not_run(*a, **kw):  # pragma: no cover — negative assert
        called["summary"] = True
        raise AssertionError("summary agent called despite enabled=False")

    monkeypatch.setattr(scheduler.summary_agent, "run", _should_not_run)
    monkeypatch.setattr(
        scheduler.notify, "send_feishu_card",
        lambda *a, **k: called.__setitem__("notify", True),
    )
    result = _finalize(ws, cfg, state, LoopResult(ExitCode.SUCCESS, "ok"))
    assert result.code == ExitCode.SUCCESS
    assert result.reason == "ok"
    assert called == {"summary": False, "notify": False}
    assert not (ws.workspace_dir / "summary.md").exists()


def test_finalize_writes_fallback_on_agent_crash(tmp_path: Path, monkeypatch):
    ws = _ws(tmp_path)
    cfg = _make_config(enabled=True, feishu_enabled=False)
    state = LoopState(cycle=3, total_cost_cny=2.5)

    def _boom(*a, **kw):
        raise RuntimeError("summarizer died")

    monkeypatch.setattr(scheduler.summary_agent, "run", _boom)
    result = _finalize(ws, cfg, state, LoopResult(ExitCode.EXHAUSTED, "budget"))
    assert result.code == ExitCode.EXHAUSTED
    assert result.reason == "budget"
    summary_md = ws.workspace_dir / "summary.md"
    assert summary_md.exists()
    body = summary_md.read_text(encoding="utf-8")
    assert "EXHAUSTED" in body
    assert "budget" in body
    assert "fallback" in body.lower()


def test_finalize_writes_fallback_when_agent_silent(tmp_path: Path, monkeypatch):
    ws = _ws(tmp_path)
    cfg = _make_config(enabled=True, feishu_enabled=False)
    state = LoopState()

    from agentloop.agents.base import RunResult

    # Agent "succeeded" but failed to write summary.md.
    def _no_write(*a, **kw):
        return RunResult(
            stream_json_path=Path("/dev/null"),
            duration_sec=1.0,
            cost_cny=0.1,
            success=True,
        )

    monkeypatch.setattr(scheduler.summary_agent, "run", _no_write)
    _finalize(ws, cfg, state, LoopResult(ExitCode.SUCCESS, ""))
    assert (ws.workspace_dir / "summary.md").exists()
    assert "fallback" in (ws.workspace_dir / "summary.md").read_text(
        encoding="utf-8"
    ).lower()


def test_finalize_skips_feishu_when_disabled(tmp_path: Path, monkeypatch):
    ws = _ws(tmp_path)
    cfg = _make_config(enabled=True, feishu_enabled=False)
    state = LoopState()

    from agentloop.agents.base import RunResult

    def _fake_run(*a, **kw):
        (ws.workspace_dir / "summary.md").write_text("# real summary\n", encoding="utf-8")
        return RunResult(
            stream_json_path=Path("/dev/null"),
            duration_sec=1.0,
            cost_cny=0.2,
            success=True,
        )

    notify_calls = []
    monkeypatch.setattr(scheduler.summary_agent, "run", _fake_run)
    monkeypatch.setattr(
        scheduler.notify, "send_feishu_card",
        lambda *a, **k: notify_calls.append((a, k)),
    )
    _finalize(ws, cfg, state, LoopResult(ExitCode.SUCCESS, "done"))
    assert notify_calls == []


def test_finalize_calls_feishu_when_enabled(tmp_path: Path, monkeypatch):
    ws = _ws(tmp_path)
    cfg = _make_config(enabled=True, feishu_enabled=True)
    cfg.summary_config.feishu = FeishuConfig(cli_path="/bin/true", chat_id="oc_x")
    state = LoopState(cycle=4, total_cost_cny=1.1)

    from agentloop.agents.base import RunResult

    def _fake_run(*a, **kw):
        (ws.workspace_dir / "summary.md").write_text(
            "# real\n\n本次 loop 完成目标。\n", encoding="utf-8"
        )
        return RunResult(
            stream_json_path=Path("/dev/null"),
            duration_sec=1.0,
            cost_cny=0.3,
            success=True,
        )

    captured = {}

    def _fake_send(fcfg, message):
        captured["cfg"] = fcfg
        captured["msg"] = message
        return True

    monkeypatch.setattr(scheduler.summary_agent, "run", _fake_run)
    monkeypatch.setattr(scheduler.notify, "send_feishu_card", _fake_send)

    _finalize(ws, cfg, state, LoopResult(ExitCode.PARTIAL_SUCCESS, "1 abandoned"))
    assert "msg" in captured
    assert "PARTIAL_SUCCESS" in captured["msg"]
    assert "1 abandoned" in captured["msg"]
    assert "本次 loop 完成目标" in captured["msg"]
    # summary cost should have been recorded on state
    assert state.total_cost_cny == pytest.approx(1.4)


# ---------- injector -------------------------------------------------------


def test_inject_feishu_writes_when_project_configured(tmp_path: Path, monkeypatch):
    # Simulate a project-level wiki_ingest.feishu_notify with valid values.
    from server import agentloop_manager as mgr

    monkeypatch.setattr(
        "server.config.wiki_ingest_config",
        lambda: {
            "feishu_notify": {
                "enabled": True,
                "cli_path": "/opt/feishu/cli.py",
                "chat_id": "oc_test",
                "env_file": "/opt/feishu/env",
            }
        },
    )
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[limits]\nmax_cycles = 10\n", encoding="utf-8")
    mgr._inject_feishu_into_workspace_config(cfg_file)
    out = cfg_file.read_text(encoding="utf-8")
    assert "[summary.feishu]" in out
    assert "/opt/feishu/cli.py" in out
    assert "oc_test" in out
    # original content preserved
    assert "max_cycles = 10" in out


def test_inject_feishu_skips_when_already_present(tmp_path: Path, monkeypatch):
    from server import agentloop_manager as mgr

    monkeypatch.setattr(
        "server.config.wiki_ingest_config",
        lambda: {
            "feishu_notify": {
                "enabled": True,
                "cli_path": "/opt/feishu/cli.py",
                "chat_id": "oc_test",
                "env_file": "",
            }
        },
    )
    cfg_file = tmp_path / "config.toml"
    existing = "[summary.feishu]\ncli_path = \"/user/edit.py\"\n"
    cfg_file.write_text(existing, encoding="utf-8")
    mgr._inject_feishu_into_workspace_config(cfg_file)
    assert cfg_file.read_text(encoding="utf-8") == existing


def test_inject_feishu_skips_when_project_disabled(tmp_path: Path, monkeypatch):
    from server import agentloop_manager as mgr

    monkeypatch.setattr(
        "server.config.wiki_ingest_config",
        lambda: {
            "feishu_notify": {
                "enabled": False,
                "cli_path": "/opt/feishu/cli.py",
                "chat_id": "oc_test",
                "env_file": "",
            }
        },
    )
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("", encoding="utf-8")
    mgr._inject_feishu_into_workspace_config(cfg_file)
    assert "[summary.feishu]" not in cfg_file.read_text(encoding="utf-8")
