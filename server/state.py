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
TASKS_FILE = DATA_DIR / "tasks.json"


def _stable_agent_id(name: str) -> str:
    """Derive a deterministic 12-char hex ID from agent name."""
    return hashlib.sha256(name.encode()).hexdigest()[:12]


class AppState:
    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}
        self.tasks: dict[str, Task] = {}
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

    def _load_persisted_data(self) -> None:
        """Load agents and tasks from disk."""
        if not TASKS_FILE.exists():
            return
        try:
            raw = json.loads(TASKS_FILE.read_text(encoding="utf-8"))

            # Load persisted agents (those not already from config)
            for aid, adata in raw.get("agents", {}).items():
                if aid not in self.agents:
                    self.agents[aid] = Agent(**adata)

            for tid, tdata in raw.get("tasks", {}).items():
                agent_id = tdata.get("agent_id")
                if agent_id not in self.agents:
                    logger.warning("Skipping task %s: agent %s not found", tid, agent_id)
                    continue
                # Reset running/waiting tasks to failed (subprocess is gone)
                if tdata.get("status") in ("running", "waiting"):
                    tdata["status"] = "failed"
                # Ensure no messages are left in streaming state
                for msg in tdata.get("messages", []):
                    msg["streaming"] = False
                task = Task(**tdata)
                self.tasks[tid] = task
                agent = self.agents[agent_id]
                if tid not in agent.task_ids:
                    agent.task_ids.append(tid)
        except Exception:
            logger.exception("Failed to load persisted tasks")

    def save_tasks(self) -> None:
        """Persist non-default agents and all tasks to disk (atomic write)."""
        DATA_DIR.mkdir(exist_ok=True)
        default_ids = {_stable_agent_id(cfg["name"]) for cfg in agent_defaults()}
        payload = {
            "agents": {
                aid: a.model_dump()
                for aid, a in self.agents.items()
                if aid not in default_ids
            },
            "tasks": {tid: t.model_dump() for tid, t in self.tasks.items()},
        }
        tmp = TASKS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(TASKS_FILE)

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
        self.save_tasks()
        return task

    def create_agent(self, name: str, command: str = "cco", cwd: str = "") -> Agent:
        aid = _stable_agent_id(name)
        if aid in self.agents:
            raise ValueError(f"Agent with name '{name}' already exists")
        agent = Agent(id=aid, name=name, command=command, cwd=cwd)
        self.agents[aid] = agent
        self.save_tasks()
        return agent

    def delete_task(self, task_id: str) -> bool:
        task = self.tasks.pop(task_id, None)
        if task is None:
            return False
        agent = self.agents.get(task.agent_id)
        if agent and task_id in agent.task_ids:
            agent.task_ids.remove(task_id)
        self.save_tasks()
        return True

    def snapshot(self) -> dict:
        return {
            "agents": [a.model_dump() for a in self.agents.values()],
            "tasks": {tid: t.model_dump() for tid, t in self.tasks.items()},
        }


app_state = AppState()
