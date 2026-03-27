"""WebSocket endpoint – single ws://host/ws connection per client."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from server.state import app_state

router = APIRouter()
logger = logging.getLogger(__name__)

# Connected clients
clients: set[WebSocket] = set()


async def broadcast(msg: dict[str, Any]) -> None:
    payload = json.dumps(msg, ensure_ascii=False)
    dead: list[WebSocket] = []
    for ws in clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


def task_created_message(task) -> dict[str, Any]:
    agent = app_state.get_agent(task.agent_id)
    return {
        "type": "task_created",
        "agent_id": task.agent_id,
        "task": task.model_dump(),
        "task_ids": list(agent.task_ids) if agent else [task.id],
    }


def agents_reordered_message(order: list[str], request_id: int | None = None) -> dict[str, Any]:
    return {
        "type": "agents_reordered",
        "order": order,
        "request_id": request_id,
    }


def task_updated_message(task) -> dict[str, Any]:
    return {
        "type": "task_updated",
        "task_id": task.id,
        "fields": {"name": task.name, "status": task.status, "updated_at": task.updated_at},
    }


def agent_updated_message(agent, fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "agent_updated",
        "agent_id": agent.id,
        "fields": fields,
    }


def agent_created_message(agent, order: list[str]) -> dict[str, Any]:
    return {
        "type": "agent_created",
        "agent": agent.model_dump(),
        "order": order,
    }


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    logger.info("WS client connected (%d total)", len(clients))

    # Send initial state
    from server.agent_runner import runner as _runner
    await ws.send_text(
        json.dumps({"type": "state_sync", "data": app_state.snapshot(_runner._session_ids)}, ensure_ascii=False)
    )

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            await _handle_client_message(data, ws)
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        logger.info("WS client disconnected (%d remaining)", len(clients))


async def _handle_client_message(data: dict, ws: WebSocket) -> None:
    msg_type = data.get("type")

    if msg_type == "create_task":
        agent_id = data["agent_id"]
        name = data.get("name", "")
        if agent_id not in app_state.agents:
            return
        task = app_state.create_task(agent_id, name)
        await broadcast(task_created_message(task))

    elif msg_type == "user_message":
        task_id = data["task_id"]
        content = data.get("content", "")
        task = app_state.get_task(task_id)
        if not task:
            return

        from server.models import Message

        user_msg = Message(role="user", content=content)
        task.messages.append(user_msg)
        app_state.save_agent_tasks(task.agent_id)
        await broadcast(
            {
                "type": "message",
                "task_id": task_id,
                "message": user_msg.model_dump(),
            }
        )

        # Send input to agent process
        from server.agent_runner import runner

        await runner.send_input(task_id, content)
