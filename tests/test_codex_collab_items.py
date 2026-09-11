"""CodexAdapter's handling of collab_tool_call (sub-agent) items.

The shapes asserted here were captured from a live `codex exec --json` run,
not inferred from the binary's internal event enum — that enum also contains
names like `sub_agent_activity` which never reach this stream.

Before this handling existed, only command_execution and agent_message were
processed, so a task that delegated its work to sub-agents showed the
orchestration shell and none of the work: on one real task 107 of 222 tool
calls were dropped.
"""

import asyncio

from server.adapters.codex import CodexAdapter
from server.models import Message, Task
from server.state import app_state


class FakeCtx:
    """Records what the adapter emitted, in order."""

    def __init__(self, task_id="t-collab"):
        self.task_id = task_id
        self.created: list[tuple[str, str, str]] = []
        self.closed: list[str] = []
        # (message_id, delta) — what a live client would actually receive.
        self.deltas: list[tuple[str, str]] = []
        # message_id -> the Message object handed out, to check final state.
        self.messages: dict[str, Message] = {}

    async def create_message(self, role, type_, content, tool_name="", streaming=False):
        msg = Message(
            role=role, type=type_, content=content,
            tool_name=tool_name, streaming=streaming,
        )
        self.created.append((type_, tool_name, content))
        self.messages[msg.id] = msg
        return msg

    async def append_delta(self, message_id, text):
        self.deltas.append((message_id, text))

    def client_view(self, message_id, opening):
        """Replay what a live client holds: opening content plus its deltas.

        The frontend applies message_chunk by appending the delta and
        message_done by clearing `streaming` only — it never re-reads content
        from the server. So this, not msg.content, is what the user sees
        without reloading the task.
        """
        return opening + "".join(d for mid, d in self.deltas if mid == message_id)

    async def close_message(self, message_id):
        self.closed.append(message_id)

    async def save_session(self, session_id):
        pass

    async def update_tokens(self, *a, **k):
        pass

    async def attach_usage(self, *a, **k):
        pass


def _collab(item_id, tool, *, tids=(), prompt=None, states=None):
    return {
        "id": item_id,
        "type": "collab_tool_call",
        "tool": tool,
        "receiver_thread_ids": list(tids),
        "prompt": prompt,
        "agents_states": states or {},
    }


def _run(coro):
    return asyncio.run(coro)


def test_spawn_wait_close_emits_call_and_reply_once():
    """The sub-agent's reply is surfaced, and only once.

    agents_states keeps echoing the last message on every later call, so a
    naive implementation prints the same reply on spawn, wait and close.
    """
    adapter = CodexAdapter()
    ctx = FakeCtx()
    tid = "01a08e79-eb9b-7362-9ff7-8cbfaa5e145f"
    done = {tid: {"status": "completed", "message": "ping"}}

    async def drive():
        spawn = _collab("item_0", "spawn_agent", prompt="只回答一个词：ping")
        await adapter.handle_chunk({"type": "item.started", "item": spawn}, ctx)
        await adapter.handle_chunk(
            {"type": "item.completed",
             "item": _collab("item_0", "spawn_agent", tids=[tid],
                             prompt="只回答一个词：ping",
                             states={tid: {"status": "pending_init", "message": None}})},
            ctx,
        )
        for i, tool in ((1, "wait"), (2, "close_agent")):
            item = _collab(f"item_{i}", tool, tids=[tid], states=done)
            await adapter.handle_chunk({"type": "item.started", "item": item}, ctx)
            await adapter.handle_chunk({"type": "item.completed", "item": item}, ctx)

    _run(drive())

    labels = [(t, name) for t, name, _ in ctx.created]
    assert labels == [
        ("tool_use", "agent:spawn_agent"),
        ("tool_use", "agent:wait"),
        ("tool_result", ""),
        ("tool_use", "agent:close_agent"),
    ], labels
    replies = [c for t, _, c in ctx.created if t == "tool_result"]
    assert replies == [f"[{CodexAdapter._short_tid(tid)} completed]\nping"], replies


def test_spawn_prompt_has_no_leading_blank_line():
    adapter = CodexAdapter()
    ctx = FakeCtx()

    async def drive():
        item = _collab("item_0", "spawn_agent", prompt="do the thing")
        await adapter.handle_chunk({"type": "item.started", "item": item}, ctx)

    _run(drive())
    _, _, content = ctx.created[0]
    assert content == "do the thing", repr(content)


def test_interleaved_sub_agents_close_their_own_bubbles():
    """Two agents in flight at once must not close each other's bubble."""
    adapter = CodexAdapter()
    ctx = FakeCtx()
    a, b = "01a08e82-5115-7512-966d-6bcdf6e975b7", "01a08e82-5133-7520-8a88-4dd599ecc862"
    la, lb = CodexAdapter._short_tid(a), CodexAdapter._short_tid(b)

    async def drive():
        await adapter.handle_chunk(
            {"type": "item.started", "item": _collab("item_a", "send_input", tids=[a])}, ctx)
        await adapter.handle_chunk(
            {"type": "item.started", "item": _collab("item_b", "wait", tids=[b])}, ctx)
        # b completes first
        await adapter.handle_chunk(
            {"type": "item.completed",
             "item": _collab("item_b", "wait", tids=[b],
                             states={b: {"status": "completed", "message": "from-b"}})}, ctx)
        await adapter.handle_chunk(
            {"type": "item.completed",
             "item": _collab("item_a", "send_input", tids=[a],
                             states={a: {"status": "completed", "message": "from-a"}})}, ctx)

    _run(drive())
    assert adapter._collab_msgs == {}
    replies = [c for t, _, c in ctx.created if t == "tool_result"]
    assert replies == [f"[{lb} completed]\nfrom-b",
                       f"[{la} completed]\nfrom-a"], replies


def test_completion_without_started_still_emits_the_call():
    """An interrupted run must not silently swallow the call."""
    adapter = CodexAdapter()
    ctx = FakeCtx()

    async def drive():
        await adapter.handle_chunk(
            {"type": "item.completed", "item": _collab("orphan", "wait", tids=["cccccccc-3"])}, ctx)

    _run(drive())
    assert [(t, n) for t, n, _ in ctx.created] == [("tool_use", "agent:wait")]


def test_turn_completed_counts_turns_even_without_usage():
    """codex has no result chunk carrying num_turns, so turns are counted here.

    While this stayed at 0, auto_memory's `high_turns > 20` lesson signal was
    permanently dead for every codex task.
    """
    task = Task(id="t-turns", agent_id="a1", name="n")
    app_state.tasks["t-turns"] = task
    adapter = CodexAdapter()
    ctx = FakeCtx("t-turns")

    async def drive():
        for _ in range(3):
            await adapter.handle_chunk({"type": "turn.started"}, ctx)
            await adapter.handle_chunk(
                {"type": "turn.completed",
                 "usage": {"input_tokens": 10, "output_tokens": 2}}, ctx)
        await adapter.handle_chunk({"type": "turn.started"}, ctx)
        await adapter.handle_chunk({"type": "turn.completed", "usage": {}}, ctx)

    _run(drive())
    assert task.num_turns == 4, task.num_turns


def test_repeated_identical_replies_are_both_shown():
    """Two confirmations both answered "OK" are two replies, not one.

    Deduplicating on (thread, text) swallowed the second, leaving its tool
    call with no visible result — the user could not tell the second request
    had completed. The real distinction is the verb: spawn/send_input/wait
    solicit a reply, close_agent only echoes prior state.
    """
    adapter = CodexAdapter()
    ctx = FakeCtx()
    tid = "01a08e82-5115-7512-966d-6bcdf6e975b7"
    states = {tid: {"status": "completed", "message": "OK"}}

    async def drive():
        for i in range(2):
            item = _collab(f"item_{i}", "send_input", tids=[tid],
                           prompt="确认一次", states=states)
            await adapter.handle_chunk({"type": "item.started", "item": item}, ctx)
            await adapter.handle_chunk({"type": "item.completed", "item": item}, ctx)

    _run(drive())
    replies = [c for t, _, c in ctx.created if t == "tool_result"]
    assert len(replies) == 2, replies


def test_close_agent_does_not_re_emit_an_echoed_reply():
    adapter = CodexAdapter()
    ctx = FakeCtx()
    tid = "01a08e82-5115-7512-966d-6bcdf6e975b7"
    states = {tid: {"status": "completed", "message": "done"}}

    async def drive():
        for i, tool in ((0, "wait"), (1, "close_agent")):
            item = _collab(f"item_{i}", tool, tids=[tid], states=states)
            await adapter.handle_chunk({"type": "item.started", "item": item}, ctx)
            await adapter.handle_chunk({"type": "item.completed", "item": item}, ctx)

    _run(drive())
    replies = [c for t, _, c in ctx.created if t == "tool_result"]
    assert replies == [f"[{CodexAdapter._short_tid(tid)} completed]\ndone"], replies


def test_finalized_detail_reaches_live_clients():
    """The thread id and status that only item.completed carries must be sent.

    Assigning msg.content server-side is invisible to connected clients:
    close_message broadcasts only the id, and markMessageDone clears
    `streaming` without touching content.
    """
    adapter = CodexAdapter()
    ctx = FakeCtx()
    tid = "01a08e82-5115-7512-966d-6bcdf6e975b7"

    async def drive():
        opening = _collab("item_0", "spawn_agent", prompt="做这件事")
        await adapter.handle_chunk({"type": "item.started", "item": opening}, ctx)
        await adapter.handle_chunk(
            {"type": "item.completed",
             "item": _collab("item_0", "spawn_agent", tids=[tid], prompt="做这件事",
                             states={tid: {"status": "pending_init", "message": None}})},
            ctx,
        )

    _run(drive())
    opening_content = ctx.created[0][2]
    msg_id = next(iter(ctx.messages))
    seen = ctx.client_view(msg_id, opening_content)
    label = CodexAdapter._short_tid(tid)
    assert label in seen, seen
    assert "pending_init" in seen, seen
    # And the client's view matches the authoritative server-side content.
    assert seen == ctx.messages[msg_id].content, (seen, ctx.messages[msg_id].content)


def test_concurrent_sub_agents_get_distinct_labels():
    """Thread ids are time-ordered, so same-turn agents share a prefix.

    These two ids are from a real run that spawned two sub-agents in one
    turn; labelling by the leading segment showed both as "01a08e82".
    """
    a = "01a08e82-5115-7512-966d-6bcdf6e975b7"
    b = "01a08e82-5133-7520-8a88-4dd599ecc862"
    assert CodexAdapter._short_tid(a) != CodexAdapter._short_tid(b)


def test_reordered_thread_ids_still_append_cleanly():
    """`receiver_thread_ids` order is not stable between started and completed.

    A real `wait` over two agents listed them in one order on item.started and
    the reverse on item.completed. Without a stable ordering the append-only
    diff failed and the bubble ended up with two `agents:` lines.
    """
    adapter = CodexAdapter()
    ctx = FakeCtx()
    a = "01a08e82-5115-7512-966d-6bcdf6e975b7"
    b = "01a08e82-5133-7520-8a88-4dd599ecc862"

    async def drive():
        await adapter.handle_chunk(
            {"type": "item.started", "item": _collab("item_0", "wait", tids=[a, b])}, ctx)
        await adapter.handle_chunk(
            {"type": "item.completed",
             "item": _collab("item_0", "wait", tids=[b, a],
                             states={b: {"status": "completed", "message": "mb"},
                                     a: {"status": "completed", "message": "ma"}})}, ctx)

    _run(drive())
    msg_id = next(iter(ctx.messages))
    seen = ctx.client_view(msg_id, ctx.created[0][2])
    assert seen.count("agents:") == 1, seen
    assert seen == ctx.messages[msg_id].content


def test_reset_clears_collab_state():
    adapter = CodexAdapter()
    adapter._collab_msgs["x"] = Message(role="agent", type="tool_use", content="c")
    adapter.reset()
    assert adapter._collab_msgs == {}
