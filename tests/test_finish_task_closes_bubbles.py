"""_finish_task must close bubbles the subprocess left open.

A run that dies between item.started and item.completed (crash, EOF, nonzero
exit) used to leave streaming=True forever: _finish_task saved the task but
never closed messages, and only the explicit WebSocket stop path swept them.
The transcript is persisted right after, so the unclosed bubble survived a
reload too.

Not specific to sub-agents — command_execution's single-slot bubble had the
same exposure — so the sweep lives in the runner, not in an adapter.
"""

import asyncio

from server.agent_runner import AgentRunner
from server.models import Message, Task, TaskStatus
from server.state import app_state


def _task_with_open_bubbles(task_id):
    """A task holding one open tool bubble, one closed message."""
    task = Task(id=task_id, agent_id="agent-does-not-exist", name="n")
    task.status = TaskStatus.running
    task.messages = [
        Message(role="agent", type="tool_use", content="spawn", streaming=True),
        Message(role="agent", type="text", content="done", streaming=False),
    ]
    app_state.tasks[task_id] = task
    return task


def _runner(monkeypatch, sent):
    """A runner with outward-facing side effects stubbed out.

    _finish_task also schedules a Feishu notification and a memory-history
    append (which can trigger an LLM consolidation). Neither belongs in a
    unit test — the first sends a real card to a real chat.
    """
    async def fake_broadcast(payload):
        sent.append(payload)

    monkeypatch.setattr("server.routes_ws.broadcast", fake_broadcast)
    runner = AgentRunner()
    monkeypatch.setattr(runner, "_schedule_notify", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_record_history", lambda *a, **k: None)
    return runner


def test_sweep_ignores_bubbles_appended_during_the_broadcast(monkeypatch):
    """A replacement run's bubble must survive the dying run's sweep.

    user_message is allowed while a task is running, so send_input() can append
    a new streaming message while _finish_task is suspended on a broadcast.
    Iterating the live list would close that bubble even though its subprocess
    is still producing output.
    """
    task = _task_with_open_bubbles("t-race")
    newcomer = Message(role="agent", type="tool_use", content="new run",
                       streaming=True)
    sent = []

    async def racing_broadcast(payload):
        # The concurrent append lands while we are suspended here.
        if payload.get("type") == "message_done" and newcomer not in task.messages:
            task.messages.append(newcomer)
        sent.append(payload)

    monkeypatch.setattr("server.routes_ws.broadcast", racing_broadcast)
    runner = AgentRunner()
    monkeypatch.setattr(runner, "_schedule_notify", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_record_history", lambda *a, **k: None)
    asyncio.run(runner._finish_task("t-race", TaskStatus.failed))

    assert newcomer.streaming is True, "closed the replacement run's bubble"
    done_ids = [p["message_id"] for p in sent if p.get("type") == "message_done"]
    assert newcomer.id not in done_ids, done_ids


def test_premature_exit_closes_open_bubbles(monkeypatch):
    task = _task_with_open_bubbles("t-premature")
    sent = []
    runner = _runner(monkeypatch, sent)
    asyncio.run(runner._finish_task("t-premature", TaskStatus.failed))

    assert all(not m.streaming for m in task.messages)
    done = [p for p in sent if p.get("type") == "message_done"]
    assert [p["message_id"] for p in done] == [task.messages[0].id], done


def test_already_closed_messages_are_not_rebroadcast(monkeypatch):
    task = _task_with_open_bubbles("t-noop")
    for m in task.messages:
        m.streaming = False
    sent = []
    runner = _runner(monkeypatch, sent)
    asyncio.run(runner._finish_task("t-noop", TaskStatus.success))

    assert [p for p in sent if p.get("type") == "message_done"] == []


def test_terminal_reentry_does_not_sweep(monkeypatch):
    """A second _finish_task for an already-finished task stays a no-op."""
    task = _task_with_open_bubbles("t-terminal")
    task.status = TaskStatus.success
    sent = []
    runner = _runner(monkeypatch, sent)
    asyncio.run(runner._finish_task("t-terminal", TaskStatus.failed))

    # Left as-is: the run that owned this bubble already reached a terminal
    # state, and re-closing here would fire message_done for a foreign run.
    assert task.messages[0].streaming is True
    assert [p for p in sent if p.get("type") == "message_done"] == []


def test_resume_kill_does_not_sweep(monkeypatch):
    """send_input kills the old process; that failure must not touch bubbles."""
    task = _task_with_open_bubbles("t-resume")
    sent = []
    runner = _runner(monkeypatch, sent)
    runner._resuming.add("t-resume")
    asyncio.run(runner._finish_task("t-resume", TaskStatus.failed))

    assert task.messages[0].streaming is True
    assert sent == []
