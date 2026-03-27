"""In-memory state store for agents and tasks, with JSON file persistence."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from server.config import agent_defaults
from server.models import Agent, Task

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
AGENTS_FILE = DATA_DIR / "agents.json"
TASKS_DIR = DATA_DIR / "tasks"


def _stable_agent_id(name: str) -> str:
    """Derive a deterministic 12-char hex ID from agent name."""
    return hashlib.sha256(name.encode()).hexdigest()[:12]


class AppState:
    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}
        self.tasks: dict[str, Task] = {}
        self._agent_order: list[str] = []  # ordered list of agent ids
        self._init_default_agents()
        self._load_persisted_data()

    def _init_default_agents(self) -> None:
        for cfg in agent_defaults():
            agent = Agent(
                id=_stable_agent_id(cfg["name"]),
                name=cfg["name"],
                command=cfg.get("command", "cco"),
                cwd=cfg.get("cwd", ""),
            )
            self.agents[agent.id] = agent
            self._agent_order.append(agent.id)

    def _load_persisted_data(self) -> None:
        """Load agents and tasks from disk (new split-file structure)."""
        if not AGENTS_FILE.exists():
            return
        try:
            raw = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))

            # Load persisted agents; update existing (default) agents with saved fields
            for aid, adata in raw.get("agents", {}).items():
                if aid in self.agents:
                    for field in ("name", "command", "cwd", "shared_memory_agent_id", "pinned"):
                        if field in adata:
                            setattr(self.agents[aid], field, adata[field])
                else:
                    self.agents[aid] = Agent(**adata)
                    self._agent_order.append(aid)

            # Restore persisted order (only ids that still exist)
            saved_order = raw.get("agent_order", [])
            if saved_order:
                existing = set(self.agents.keys())
                restored = [aid for aid in saved_order if aid in existing]
                restored += [aid for aid in self._agent_order if aid not in restored]
                self._agent_order = restored

        except Exception:
            logger.exception("Failed to load persisted agents")

        # Load per-agent task files
        if TASKS_DIR.exists():
            for task_file in TASKS_DIR.glob("*.json"):
                try:
                    tasks_raw = json.loads(task_file.read_text(encoding="utf-8"))
                    for tid, tdata in tasks_raw.get("tasks", {}).items():
                        agent_id = tdata.get("agent_id")
                        if agent_id not in self.agents:
                            logger.warning("Skipping task %s: agent %s not found", tid, agent_id)
                            continue
                        if tdata.get("status") in ("running", "waiting"):
                            tdata["status"] = "failed"
                        for msg in tdata.get("messages", []):
                            msg["streaming"] = False
                        task = Task(**tdata)
                        self.tasks[tid] = task
                        agent = self.agents[agent_id]
                        if tid not in agent.task_ids:
                            agent.task_ids.append(tid)
                except Exception:
                    logger.exception("Failed to load task file %s", task_file)

    def save_agents(self) -> None:
        """Persist agent metadata and order to agents.json (atomic write)."""
        DATA_DIR.mkdir(exist_ok=True)
        payload = {
            "agent_order": self._agent_order,
            "agents": {aid: a.model_dump() for aid, a in self.agents.items()},
        }
        tmp = AGENTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(AGENTS_FILE)

    def save_agent_tasks(self, agent_id: str) -> None:
        """Persist tasks for a single agent to tasks/{agent_id}.json (atomic write)."""
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        agent = self.agents.get(agent_id)
        if agent is None:
            return
        agent_tasks = {
            tid: self.tasks[tid].model_dump()
            for tid in agent.task_ids
            if tid in self.tasks
        }
        payload = {"tasks": agent_tasks}
        task_file = TASKS_DIR / f"{agent_id}.json"
        tmp = task_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(task_file)

    def get_agent(self, agent_id: str) -> Agent | None:
        return self.agents.get(agent_id)

    def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def create_task(self, agent_id: str, name: str) -> Task:
        agent = self.agents[agent_id]
        task_num = len(agent.task_ids) + 1
        task = Task(
            agent_id=agent_id,
            name=name or f"Task {task_num}",
        )
        self.tasks[task.id] = task
        agent.task_ids.append(task.id)
        self.save_agent_tasks(agent_id)
        return task

    def create_agent(self, name: str, command: str = "cco", cwd: str = "", shared_memory_agent_id: str | None = None) -> Agent:
        aid = _stable_agent_id(name)
        if aid in self.agents:
            raise ValueError(f"Agent with name '{name}' already exists")
        agent = Agent(id=aid, name=name, command=command, cwd=cwd, shared_memory_agent_id=shared_memory_agent_id)
        self.agents[aid] = agent
        # Insert after all pinned agents
        insert_pos = sum(1 for oid in self._agent_order if self.agents.get(oid, Agent(name="")).pinned)
        self._agent_order.insert(insert_pos, aid)
        self.save_agents()
        return agent

    def pin_agent(self, agent_id: str) -> None:
        """Pin an agent: mark it pinned and move it to the pinned section top."""
        agent = self.agents.get(agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")
        agent.pinned = True
        if agent_id in self._agent_order:
            self._agent_order.remove(agent_id)
        self._agent_order.insert(0, agent_id)
        self.save_agents()

    def unpin_agent(self, agent_id: str) -> None:
        """Unpin an agent: clear pinned flag and move it below all pinned agents."""
        agent = self.agents.get(agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")
        agent.pinned = False
        if agent_id in self._agent_order:
            self._agent_order.remove(agent_id)
        insert_pos = sum(1 for oid in self._agent_order if self.agents.get(oid, Agent(name="")).pinned)
        self._agent_order.insert(insert_pos, agent_id)
        self.save_agents()

    def delete_task(self, task_id: str) -> bool:
        task = self.tasks.pop(task_id, None)
        if task is None:
            return False
        agent = self.agents.get(task.agent_id)
        if agent and task_id in agent.task_ids:
            agent.task_ids.remove(task_id)
        self.save_agent_tasks(task.agent_id)
        return True

    def reorder_agents(self, ordered_ids: list[str]) -> None:
        """Persist a new agent display order."""
        existing = set(self.agents.keys())
        valid = [aid for aid in ordered_ids if aid in existing]
        valid += [aid for aid in self._agent_order if aid not in valid]
        self._agent_order = valid
        self.save_agents()

    def ordered_agent_ids(self) -> list[str]:
        return [aid for aid in self._agent_order if aid in self.agents]

    def snapshot(self, sessions: dict[str, str] | None = None) -> dict:
        ordered = [self.agents[aid] for aid in self.ordered_agent_ids()]
        return {
            "agents": [a.model_dump() for a in ordered],
            "tasks": {tid: t.model_dump() for tid, t in self.tasks.items()},
            "sessions": sessions or {},
        }


app_state = AppState()
