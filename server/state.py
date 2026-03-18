"""In-memory state store for agents and tasks."""

from __future__ import annotations

from server.models import Agent, Task


class AppState:
    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}
        self.tasks: dict[str, Task] = {}
        self._init_default_agents()

    def _init_default_agents(self) -> None:
        defaults = ["Scheduler", "Codegen", "Reviewer"]
        for name in defaults:
            agent = Agent(name=name)
            self.agents[agent.id] = agent

    def get_agent(self, agent_id: str) -> Agent | None:
        return self.agents.get(agent_id)

    def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def create_task(self, agent_id: str, prompt: str) -> Task:
        agent = self.agents[agent_id]
        task_num = len(agent.task_ids) + 1
        task = Task(
            agent_id=agent_id,
            name=f"Task {task_num}",
            prompt=prompt,
        )
        self.tasks[task.id] = task
        agent.task_ids.append(task.id)
        return task

    def delete_task(self, task_id: str) -> bool:
        task = self.tasks.pop(task_id, None)
        if task is None:
            return False
        agent = self.agents.get(task.agent_id)
        if agent and task_id in agent.task_ids:
            agent.task_ids.remove(task_id)
        return True

    def snapshot(self) -> dict:
        return {
            "agents": [a.model_dump() for a in self.agents.values()],
            "tasks": {tid: t.model_dump() for tid, t in self.tasks.items()},
        }


app_state = AppState()
