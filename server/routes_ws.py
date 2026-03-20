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


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    logger.info("WS client connected (%d total)", len(clients))

    # Send initial state
    await ws.send_text(
        json.dumps({"type": "state_sync", "data": app_state.snapshot()}, ensure_ascii=False)
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
        await broadcast({"type": "state_sync", "data": app_state.snapshot()})

    elif msg_type == "user_message":
        task_id = data["task_id"]
        content = data.get("content", "")
        task = app_state.get_task(task_id)
        if not task:
            return

        from server.models import Message

        user_msg = Message(role="user", content=content)
        task.messages.append(user_msg)
        app_state.save_tasks()
        await broadcast(
            {
                "type": "message",
                "task_id": task_id,
                "message": user_msg.model_dump(),
            }
        )

        # Send input to agent process
        from server.agent_runner import runner

        # Check if a specific command is provided in the message
        command = data.get("command")
        if command:
            # Temporarily override the agent's command for this run
            original_command = None
            agent = app_state.get_agent(task.agent_id)
            if agent:
                original_command = agent.command
                agent.command = command

        await runner.send_input(task_id, content)

        # Restore the original command if it was overridden
        if command and original_command is not None and agent:
            agent.command = original_command
