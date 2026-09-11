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

import pytest

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
        # message_id -> replacement text sent with message_done.
        self.finalized: dict[str, str] = {}
        # (input, output) per update_tokens call — the turns_info carrier.
        self.token_updates: list[tuple[int, int]] = []
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

    async def close_message(self, message_id, content=None):
        self.closed.append(message_id)
        if content is not None:
            self.finalized[message_id] = content

    def client_view(self, message_id, opening):
        """Replay what a live client holds for this bubble.

        The frontend appends message_chunk deltas and, on message_done,
        replaces content when the payload carries it. It never re-reads from
        the server, so this — not msg.content — is what the user sees without
        reloading the task.
        """
        if message_id in self.finalized:
            return self.finalized[message_id]
        return opening + "".join(d for mid, d in self.deltas if mid == message_id)

    async def save_session(self, session_id):
        pass

    async def update_tokens(self, input_tokens=0, output_tokens=0, *a, **k):
        self.token_updates.append((input_tokens, output_tokens))

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


def test_changing_status_replaces_instead_of_duplicating():
    """A blocking `wait` starts `running` and ends `completed`.

    The finalized text is a revision, not an extension, so appending it as a
    delta left the bubble with two `agents:` sections carrying contradictory
    statuses — the stale one still visible above the final one.
    """
    adapter = CodexAdapter()
    ctx = FakeCtx()
    tid = "01a08e82-5115-7512-966d-6bcdf6e975b7"

    async def drive():
        await adapter.handle_chunk(
            {"type": "item.started",
             "item": _collab("item_0", "wait", tids=[tid],
                             states={tid: {"status": "running", "message": None}})}, ctx)
        await adapter.handle_chunk(
            {"type": "item.completed",
             "item": _collab("item_0", "wait", tids=[tid],
                             states={tid: {"status": "completed", "message": "done"}})}, ctx)

    _run(drive())
    msg_id = next(iter(ctx.messages))
    seen = ctx.client_view(msg_id, ctx.created[0][2])
    assert seen.count("agents:") == 1, seen
    assert "running" not in seen, seen
    assert seen == ctx.messages[msg_id].content


def test_repeated_wait_polling_does_not_replay_shown_replies():
    """Agents finishing at different times means `wait` is called repeatedly.

    Each later agents_states still carries threads that completed during an
    earlier wait, so treating every wait as fresh printed A, then A and B.
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
             "item": _collab("item_0", "wait", tids=[a, b],
                             states={a: {"status": "completed", "message": "reply-A"}})}, ctx)
        await adapter.handle_chunk(
            {"type": "item.started", "item": _collab("item_1", "wait", tids=[b])}, ctx)
        await adapter.handle_chunk(
            {"type": "item.completed",
             "item": _collab("item_1", "wait", tids=[a, b],
                             states={a: {"status": "completed", "message": "reply-A"},
                                     b: {"status": "completed", "message": "reply-B"}})}, ctx)

    _run(drive())
    replies = [c for t, _, c in ctx.created if t == "tool_result"]
    assert replies == [f"[{CodexAdapter._short_tid(a)} completed]\nreply-A",
                       f"[{CodexAdapter._short_tid(b)} completed]\nreply-B"], replies


@pytest.mark.parametrize("status", [
    "completed", "interrupted", "errored", "not_found", "shutdown",
    "some_future_status",
])
def test_every_non_pending_status_surfaces_its_message(status):
    """A failed sub-agent's final message must not be swallowed.

    The status set is a deny-list of the two pending states, not an allow-list
    of terminal ones: the binary's enum includes interrupted / errored /
    not_found, only some of which were ever seen live, and each value missing
    from an allow-list means a call rendered with no result at all.
    """
    adapter = CodexAdapter()
    ctx = FakeCtx()
    tid = "01a08e82-5115-7512-966d-6bcdf6e975b7"

    async def drive():
        await adapter.handle_chunk(
            {"type": "item.completed",
             "item": _collab("item_0", "wait", tids=[tid],
                             states={tid: {"status": status, "message": "final"}})}, ctx)

    _run(drive())
    replies = [c for t, _, c in ctx.created if t == "tool_result"]
    assert replies == [f"[{CodexAdapter._short_tid(tid)} {status}]\nfinal"], replies


@pytest.mark.parametrize("status", ["pending_init", "running"])
def test_pending_statuses_do_not_surface_a_stale_message(status):
    adapter = CodexAdapter()
    ctx = FakeCtx()
    tid = "01a08e82-5115-7512-966d-6bcdf6e975b7"

    async def drive():
        await adapter.handle_chunk(
            {"type": "item.completed",
             "item": _collab("item_0", "send_message", tids=[tid], prompt="x",
                             states={tid: {"status": status, "message": "stale"}})}, ctx)

    _run(drive())
    assert [c for t, _, c in ctx.created if t == "tool_result"] == []


def test_nonblocking_dispatch_does_not_replay_the_previous_answer():
    """A dispatch can complete before the target has answered.

    agents_states then still holds the thread's *previous* message with a
    non-terminal status. Classifying by verb re-emitted that stale answer as
    if it were new, so the settled status is the judge instead.
    """
    adapter = CodexAdapter()
    ctx = FakeCtx()
    tid = "01a08e82-5115-7512-966d-6bcdf6e975b7"

    async def drive():
        await adapter.handle_chunk(
            {"type": "item.completed",
             "item": _collab("item_0", "wait", tids=[tid],
                             states={tid: {"status": "completed", "message": "old"}})}, ctx)
        await adapter.handle_chunk(
            {"type": "item.completed",
             "item": _collab("item_1", "send_message", tids=[tid], prompt="新任务",
                             states={tid: {"status": "running", "message": "old"}})}, ctx)
        await adapter.handle_chunk(
            {"type": "item.completed",
             "item": _collab("item_2", "wait", tids=[tid],
                             states={tid: {"status": "completed", "message": "new"}})}, ctx)

    _run(drive())
    label = CodexAdapter._short_tid(tid)
    replies = [c for t, _, c in ctx.created if t == "tool_result"]
    assert replies == [f"[{label} completed]\nold",
                       f"[{label} completed]\nnew"], replies


def test_turn_without_usage_still_broadcasts_the_count():
    """update_tokens is the only path that carries turns_info to clients.

    Skipping it on the no-usage early return left the displayed turn count
    lagging until some later turn happened to report usage.
    """
    task = Task(id="t-broadcast", agent_id="a1", name="n")
    app_state.tasks["t-broadcast"] = task
    adapter = CodexAdapter()
    ctx = FakeCtx("t-broadcast")

    async def drive():
        await adapter.handle_chunk({"type": "turn.started"}, ctx)
        await adapter.handle_chunk({"type": "turn.completed", "usage": {}}, ctx)

    _run(drive())
    assert task.num_turns == 1
    assert ctx.token_updates == [(0, 0)], ctx.token_updates


def test_prompting_one_agent_does_not_replay_another():
    """Only the addressed threads' marks reset on a prompt-bearing call.

    agents_states echoes every live thread, so clearing by that set replayed
    an unrelated agent's already-shown answer next to the real new reply.
    """
    adapter = CodexAdapter()
    ctx = FakeCtx()
    a = "01a08e82-5115-7512-966d-6bcdf6e975b7"
    b = "01a08e82-5133-7520-8a88-4dd599ecc862"

    async def drive():
        # Both answers surface on one wait.
        await adapter.handle_chunk(
            {"type": "item.started", "item": _collab("item_0", "wait", tids=[a, b])}, ctx)
        await adapter.handle_chunk(
            {"type": "item.completed",
             "item": _collab("item_0", "wait", tids=[a, b],
                             states={a: {"status": "completed", "message": "ans-A"},
                                     b: {"status": "completed", "message": "ans-B"}})}, ctx)
        # Then only A is prompted again; B's state is merely echoed.
        await adapter.handle_chunk(
            {"type": "item.started",
             "item": _collab("item_1", "send_input", tids=[a], prompt="再确认")}, ctx)
        await adapter.handle_chunk(
            {"type": "item.completed",
             "item": _collab("item_1", "send_input", tids=[a], prompt="再确认",
                             states={a: {"status": "completed", "message": "ans-A2"},
                                     b: {"status": "completed", "message": "ans-B"}})}, ctx)

    _run(drive())
    replies = [c for t, _, c in ctx.created if t == "tool_result"]
    assert "ans-B" not in replies[-1], replies[-1]
    assert replies[-1] == f"[{CodexAdapter._short_tid(a)} completed]\nans-A2"


def test_spawn_then_wait_shows_the_first_reply_once():
    """spawn_agent carries a prompt but no receiver ids yet on item.started."""
    adapter = CodexAdapter()
    ctx = FakeCtx()
    tid = "01a08e82-5115-7512-966d-6bcdf6e975b7"

    async def drive():
        await adapter.handle_chunk(
            {"type": "item.started",
             "item": _collab("item_0", "spawn_agent", prompt="干活")}, ctx)
        await adapter.handle_chunk(
            {"type": "item.completed",
             "item": _collab("item_0", "spawn_agent", tids=[tid], prompt="干活",
                             states={tid: {"status": "pending_init", "message": None}})}, ctx)
        await adapter.handle_chunk(
            {"type": "item.started", "item": _collab("item_1", "wait", tids=[tid])}, ctx)
        await adapter.handle_chunk(
            {"type": "item.completed",
             "item": _collab("item_1", "wait", tids=[tid],
                             states={tid: {"status": "completed", "message": "first"}})}, ctx)

    _run(drive())
    replies = [c for t, _, c in ctx.created if t == "tool_result"]
    assert replies == [f"[{CodexAdapter._short_tid(tid)} completed]\nfirst"], replies


def test_reordered_thread_ids_render_stably():
    """`receiver_thread_ids` order is not stable between started and completed.

    A real `wait` over two agents listed them in one order on item.started and
    the reverse on item.completed. Sorting keeps the bubble from reshuffling
    as the call resolves.
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
