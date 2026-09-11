"""CodexAdapter — handles the codex exec --json JSONL protocol.

codex JSONL events:
  {"type":"thread.started","thread_id":"..."}
  {"type":"turn.started"}
  {"type":"item.started","item":{"type":"command_execution","command":"..."}}
  {"type":"item.completed","item":{"type":"command_execution","command":"...","aggregated_output":"..."}}
  {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
  {"type":"item.started","item":{"type":"collab_tool_call","tool":"spawn_agent",
      "receiver_thread_ids":[],"prompt":"...","agents_states":{}}}
  {"type":"item.completed","item":{"type":"collab_tool_call","tool":"wait",
      "receiver_thread_ids":["<tid>"],
      "agents_states":{"<tid>":{"status":"completed","message":"..."}}}}
  {"type":"turn.completed","usage":{"input_tokens":N,"output_tokens":N}}
  (process exits — no explicit result chunk)

The collab_tool_call shapes above were captured from a live `codex exec --json`
run, not inferred: the internal event enum also carries names like
``sub_agent_activity`` and ``inter_agent_communication`` that never surface on
this stream. ``tool`` is the sub-agent verb — note it is ``wait``, not
``wait_agent`` as the internal function_call name suggests.
"""

from __future__ import annotations

import logging
import shlex
from typing import Any

from server.adapters.base import BaseAdapter, ChunkContext
from server.models import Message, TaskStatus

logger = logging.getLogger(__name__)

# Sub-agent statuses that mean "this thread has not answered yet". Everything
# else — completed, interrupted, errored, not_found, shutdown, or any status a
# future codex adds — counts as settled.
#
# Deliberately a deny-list. The enum observed in the binary is
# pending_init | running | interrupted | errored | not_found | completed, but
# only pending_init and completed were seen on a live stream, and an
# allow-list of terminal values silently swallows a sub-agent's final error
# message for every value guessed wrong: the transcript then shows the call
# with no result. Listing the two states that certainly mean "still working"
# fails the safe way — an unknown status shows its message.
#
# Keying on the state at all (rather than on the calling verb) is also
# deliberate: a nonblocking dispatch completes before the target has answered,
# and agents_states still holds the *previous* message with a `running` status.
_PENDING_STATUSES = frozenset({"pending_init", "running"})


class CodexAdapter(BaseAdapter):
    def __init__(self) -> None:
        # Track the currently streaming tool_use message for command_execution
        self._current_tool_msg: Message | None = None
        # Streaming tool_use messages for collab_tool_call, keyed by item id.
        # A dict rather than one slot because sub-agent calls interleave: a
        # `wait` on one agent can open while another agent's `send_input` is
        # still in flight, and a single slot would close the wrong bubble.
        self._collab_msgs: dict[str, Message] = {}
        # Last reply text surfaced per sub-agent thread. agents_states echoes
        # every thread on every call, so this is what makes "emit each reply
        # once" possible; a fresh prompt to a thread clears its entry.
        self._shown_replies: dict[str, str] = {}
        # Number of task.messages observed when the current turn started.
        # Used to scope per-turn usage attachment so it never bleeds into
        # earlier turns (e.g. when the current turn produces only ignored
        # item types and has no fresh agent_message/command_execution).
        self._turn_start_msg_count: int | None = None

    # ── build_args ──────────────────────────────────────────────────────

    def build_args(
        self,
        command: str,
        prompt: str,
        session_id: str | None,
        fork_sid: str | None,
        agent_cwd: str,
        resume_at: str | None = None,
        plan_mode: bool = False,
        system_prompt: str = "",
    ) -> list[str]:
        # system_prompt is ignored: `codex exec` has no --append-system-prompt
        # equivalent. supports_system_prompt() returns False, so the caller
        # keeps folding memory into the prompt text for this adapter.
        # command might be "ept codex", "ept codex -m k2", etc.
        parts = shlex.split(command)

        common = ["--json", "--dangerously-bypass-approvals-and-sandbox"]
        if agent_cwd:
            common += ["-C", agent_cwd]

        if session_id:
            # resume: -C is not accepted by `exec resume`, omit it
            resume_flags = ["--json", "--dangerously-bypass-approvals-and-sandbox"]
            return parts + ["exec", "resume"] + resume_flags + [session_id, prompt]
        else:
            # new session: ept codex exec --json ... PROMPT
            return parts + ["exec"] + common + [prompt]

    # ── needs_pty ───────────────────────────────────────────────────────

    def needs_pty(self) -> bool:
        return False  # codex exec --json outputs pure JSONL, no PTY needed

    # ── handle_chunk ────────────────────────────────────────────────────

    async def handle_chunk(self, chunk: dict[str, Any], ctx: ChunkContext) -> None:
        chunk_type = chunk.get("type", "")

        if chunk_type == "thread.started":
            thread_id = chunk.get("thread_id", "")
            if thread_id:
                await ctx.save_session(thread_id)
            return

        if chunk_type == "turn.started":
            await self._handle_turn_started(chunk, ctx)
            return

        if chunk_type == "item.started":
            await self._handle_item_started(chunk, ctx)
            return

        if chunk_type == "item.completed":
            await self._handle_item_completed(chunk, ctx)
            return

        if chunk_type == "turn.completed":
            await self._handle_turn_completed(chunk, ctx)
            return

    # ── item.started ────────────────────────────────────────────────────

    async def _handle_item_started(self, chunk: dict, ctx: ChunkContext) -> None:
        item = chunk.get("item", {})
        item_type = item.get("type", "")

        if item_type == "command_execution":
            command = item.get("command", "")
            msg = await ctx.create_message(
                "agent", "tool_use", command,
                tool_name="Bash", streaming=True,
            )
            self._current_tool_msg = msg

        elif item_type == "collab_tool_call":
            await self._handle_collab_started(item, ctx)

    # ── item.completed ──────────────────────────────────────────────────

    async def _handle_item_completed(self, chunk: dict, ctx: ChunkContext) -> None:
        item = chunk.get("item", {})
        item_type = item.get("type", "")

        if item_type == "command_execution":
            command = item.get("command", "")
            output = item.get("aggregated_output", "")

            # Close the tool_use message
            cur = self._current_tool_msg
            if cur:
                # Update content to the command (may already be set)
                cur.content = command
                cur.streaming = False
                await ctx.close_message(cur.id)
                self._current_tool_msg = None

            # Send tool_result with the output
            await ctx.create_message(
                "agent", "tool_result", output, streaming=False,
            )

        elif item_type == "agent_message":
            text = item.get("text", "")
            if text:
                await ctx.create_message(
                    "agent", "text", text, streaming=False,
                )

        elif item_type == "collab_tool_call":
            await self._handle_collab_completed(item, ctx)

    # ── collab_tool_call (sub-agents) ───────────────────────────────────

    async def _handle_collab_started(self, item: dict, ctx: ChunkContext) -> None:
        """Open a tool_use bubble for a sub-agent call.

        Without this the whole sub-agent layer was invisible in the UI: only
        command_execution and agent_message were handled, so a run that
        delegated its actual work to sub-agents showed the orchestration
        shell and none of the work. On one real task 107 of 222 tool calls
        were dropped this way.
        """
        item_id = item.get("id", "")
        if not item_id:
            return
        msg = await ctx.create_message(
            "agent", "tool_use", self._collab_summary(item),
            tool_name=self._collab_tool_label(item), streaming=True,
        )
        self._collab_msgs[item_id] = msg

    async def _handle_collab_completed(self, item: dict, ctx: ChunkContext) -> None:
        item_id = item.get("id", "")
        msg = self._collab_msgs.pop(item_id, None)
        if msg is not None:
            # item.completed both adds detail (the spawned thread id) and
            # revises it (a blocking `wait` starts with a thread `running` and
            # ends `completed`). Because it is a revision, not an extension,
            # the finalized text is sent as a replacement: a delta would leave
            # the client showing two `agents:` sections with contradictory
            # statuses, and msg.content alone never reaches connected clients
            # (markMessageDone previously only cleared `streaming`).
            msg.content = self._collab_summary(item)
            msg.streaming = False
            await ctx.close_message(msg.id, msg.content)
        else:
            # No matching item.started (interrupted run, or a verb that only
            # emits a completion). Emit the call itself so it is not lost.
            await ctx.create_message(
                "agent", "tool_use", self._collab_summary(item),
                tool_name=self._collab_tool_label(item), streaming=False,
            )

        # The sub-agent's reply lives in agents_states[tid].message. This is
        # the payload worth reading — it is what the sub-agent handed back.
        #
        # agents_states echoes every live thread's latest message on every
        # call, so each reply has to be surfaced exactly once. Three failure
        # modes bound the rule, all of them observed:
        #
        #  - spawn→wait→close printed the same answer three times, because a
        #    lifecycle verb only echoes what was already there.
        #  - Repeated `wait` polling is normal when agents finish at different
        #    times: waiting on A and B surfaces A, and the next wait echoes A
        #    alongside B. Only B is new.
        #  - A nonblocking dispatch completes before the target has answered,
        #    and the echo still carries the *previous* message.
        #
        # Hence: a reply counts once the thread is no longer pending, and only
        # when its text differs from the last one shown for that thread. A
        # prompt to that thread clears the mark, because two confirmations both
        # answered "OK" are two distinct replies and swallowing the second
        # would leave its tool call with no visible result. Marks are cleared
        # per receiver_thread_ids, not per echoed thread, or prompting A would
        # replay B's last answer.
        states = item.get("agents_states") or {}
        if item.get("prompt"):
            for tid in item.get("receiver_thread_ids") or []:
                self._shown_replies.pop(tid, None)
        replies = []
        for tid in sorted(states, key=self._short_tid):
            state = states[tid]
            if not isinstance(state, dict):
                continue
            if state.get("status") in _PENDING_STATUSES:
                # Back to pending means this thread will answer again, so
                # whatever it says next is a new reply even if the text
                # repeats. This is what covers resume_agent, which carries no
                # prompt to trigger the reset above: a resumed agent returning
                # its previous text would otherwise be read as an echo and its
                # tool call would render with no result. Keyed on the observed
                # state transition rather than on the verb, so an
                # interrupt/resume pair spelled any other way is covered too.
                self._shown_replies.pop(tid, None)
                continue
            text = (state.get("message") or "").strip()
            if not text or self._shown_replies.get(tid) == text:
                continue
            self._shown_replies[tid] = text
            status = state.get("status", "")
            replies.append(f"[{self._short_tid(tid)} {status}]\n{text}")
        if replies:
            await ctx.create_message(
                "agent", "tool_result", "\n\n".join(replies), streaming=False,
            )

    @staticmethod
    def _collab_tool_label(item: dict) -> str:
        """Display label for a sub-agent call, e.g. ``agent:spawn_agent``.

        Prefixed so these read as a distinct class of call next to Bash in
        the transcript, rather than looking like another shell command.
        """
        return f"agent:{item.get('tool') or 'collab'}"

    def _collab_summary(self, item: dict) -> str:
        """Human-readable parameters for the tool_use bubble.

        Ordered prompt-first so that the fields item.completed adds (thread
        ids, statuses) extend the tail. _handle_collab_completed relies on
        that: it diffs against the opening summary and streams the difference
        as a delta, which is only correct while growth is append-only.
        """
        lines = []
        prompt = (item.get("prompt") or "").strip()
        if prompt:
            lines.append(prompt)
        tids = item.get("receiver_thread_ids") or []
        if tids:
            if lines:
                lines.append("")
            # Sorted: a real `wait` over two agents listed them in one order on
            # item.started and the reverse on item.completed, which broke the
            # append-only diff and duplicated the whole line in the bubble.
            labels = sorted(self._short_tid(t) for t in tids)
            lines.append(f"agents: {', '.join(labels)}")
        states = item.get("agents_states") or {}
        for tid in sorted(states, key=self._short_tid):
            state = states[tid]
            if isinstance(state, dict) and state.get("status"):
                lines.append(f"  {self._short_tid(tid)}: {state['status']}")
        return "\n".join(lines)

    @staticmethod
    def _short_tid(thread_id: str) -> str:
        """Last segment of a thread uuid — the part that actually varies.

        Not the first segment: codex thread ids are time-ordered, so agents
        spawned in the same turn share it. Two concurrent sub-agents really
        came back as ``01a08e82-5115-…`` and ``01a08e82-5133-…``, which the
        leading-segment form rendered as the same label for both.
        """
        return thread_id.rsplit("-", 1)[-1][:8] if thread_id else "?"

    # ── turn.started ────────────────────────────────────────────────────

    async def _handle_turn_started(self, chunk: dict, ctx: ChunkContext) -> None:
        from server.state import app_state

        task = app_state.get_task(ctx.task_id)
        # Snapshot the message count so turn.completed can constrain its
        # reverse search to messages produced by THIS turn.
        self._turn_start_msg_count = len(task.messages) if task else 0

    # ── turn.completed ──────────────────────────────────────────────────

    async def _handle_turn_completed(self, chunk: dict, ctx: ChunkContext) -> None:
        from server.state import app_state

        # Count the turn before the usage early-return below. codex has no
        # result chunk carrying an authoritative num_turns (that is cco's
        # `finish` path), so counting turn.completed here is the only source:
        # without it num_turns stayed 0 for the whole task, which also left
        # auto_memory's `high_turns > 20` lesson signal permanently dead.
        task = app_state.get_task(ctx.task_id)
        if task is not None:
            task.num_turns += 1

        usage = chunk.get("usage", {})
        if not usage:
            self._turn_start_msg_count = None
            # update_tokens is what normally carries turns_info to clients, and
            # it is skipped here. A zero-token update keeps the displayed count
            # from lagging until some later turn happens to report usage.
            if task is not None:
                await ctx.update_tokens(0, 0)
            return
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        model = usage.get("model", "")
        # update_tokens broadcasts turns_info, so the num_turns bump above
        # reaches the UI here rather than needing its own broadcast.
        await ctx.update_tokens(in_tok, out_tok, model=model)

        # Attach usage to the LAST agent message PRODUCED BY THIS TURN.
        # If the turn produced no eligible messages (e.g. it only emitted
        # ignored item types like file_changes/reasoning), skip attachment
        # rather than overwriting an earlier turn's badge.
        if task is None:
            self._turn_start_msg_count = None
            return
        turn_start = self._turn_start_msg_count or 0
        self._turn_start_msg_count = None
        last_msg = None
        for m in reversed(task.messages[turn_start:]):
            if m.role == "agent" and m.type in ("text", "tool_use"):
                last_msg = m
                break
        if last_msg is None:
            return
        await ctx.attach_usage(last_msg.id, {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "context_window": 0,
            "model": model,
        })

    # ── cleanup ─────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear internal state between sessions."""
        self._current_tool_msg = None
        self._collab_msgs.clear()
        self._shown_replies.clear()
        self._turn_start_msg_count = None
