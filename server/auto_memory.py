"""Auto-memory: layered, per-effective-id agent memory.

The *effective id* (eid) groups agents that share one memory store — several
worktree agents on the same project consolidate into a single set of documents.

This module owns the single definition of that grouping. ``memory.py`` and
``knowledge.py`` re-export from here so the rule cannot drift between the two.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def effective_id(agent_id: str) -> str:
    """Return the id of the agent whose memory store *agent_id* reads/writes.

    Falls back to *agent_id* itself when no sharing is configured, when the
    agent is unknown, or when ``shared_memory_agent_id`` points at a
    nonexistent agent. Resolution is a single hop by design: chains would let
    a config typo silently redirect an entire project's memory.
    """
    from server.state import app_state

    agent = app_state.get_agent(agent_id)
    if not agent:
        return agent_id
    target = agent.shared_memory_agent_id
    if not target:
        return agent_id
    if target not in app_state.agents:
        logger.warning(
            "Agent %s shares memory with unknown agent %s; using own id",
            agent_id, target,
        )
        return agent_id
    return target


def eid_members(eid: str) -> list[str]:
    """Return the ids of every agent that maps to *eid*, archived included.

    Callers that consolidate must aggregate across all members: reading only
    one member's tasks and then overwriting the shared documents discards the
    other members' data.
    """
    from server.state import app_state

    return [aid for aid in app_state.agents if effective_id(aid) == eid]


def active_eids() -> list[str]:
    """Return every eid that has at least one non-archived member agent.

    Archived agents are excluded because their history is frozen: re-running
    consolidation over them burns LLM calls and rewrites documents for work
    that will never continue.
    """
    from server.state import app_state

    seen: dict[str, None] = {}
    for aid, agent in app_state.agents.items():
        if agent.archived:
            continue
        seen.setdefault(effective_id(aid), None)
    return list(seen)


# ── Context assembly ──────────────────────────────────────────────────────────

# Wording, not just concatenation order, is what makes priority land: the model
# has no way to know that an earlier block outranks a later one.
_MEMORY_HEADER = (
    "The following is persistent memory about this project and the user's "
    "preferences, accumulated across previous sessions."
)


def build_context(agent_id: str) -> str:
    """Assemble this agent's persistent memory into one injectable block.

    Pure read: no LLM, no side effects, no writes. Returns "" when there is
    nothing to inject, so callers can skip the flag entirely.

    The ``<memory>`` body is byte-identical to what the previous inline
    injection produced, so this phase changes only the channel. Only the header
    sentence is new, and it is needed because a system prompt arrives with no
    surrounding conversation to explain what the block is.
    """
    from server.config import memory_config
    from server.memory import load_memory

    lines = load_memory(agent_id, memory_config()["max_lines"])
    if not lines:
        return ""
    return f"{_MEMORY_HEADER}\n\n<memory>\n" + "\n".join(lines) + "\n</memory>"

