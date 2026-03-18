from __future__ import annotations

import uuid
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


def _uid() -> str:
    return uuid.uuid4().hex[:12]


class TaskStatus(str, Enum):
    idle = "idle"
    running = "running"
    waiting = "waiting"
    success = "success"
    failed = "failed"


class Message(BaseModel):
    id: str = Field(default_factory=_uid)
    role: Literal["agent", "user"] = "agent"
    content: str = ""
    streaming: bool = False


class Task(BaseModel):
    id: str = Field(default_factory=_uid)
    agent_id: str
    name: str = ""
    prompt: str = ""
    status: TaskStatus = TaskStatus.idle
    messages: list[Message] = Field(default_factory=list)


class Agent(BaseModel):
    id: str = Field(default_factory=_uid)
    name: str
    command: str = "cco"
    task_ids: list[str] = Field(default_factory=list)
