"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from server.routes_rest import router as rest_router
from server.routes_ws import router as ws_router

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from server.routes_ws import ensure_daily_summary_task, ensure_wiki_ingest_task
    ensure_daily_summary_task()
    ensure_wiki_ingest_task()
    yield
    from server.agent_runner import runner
    await runner.shutdown()


app = FastAPI(title="Agent Park", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(rest_router)
app.include_router(ws_router)


if __name__ == "__main__":
    import uvicorn
    from server.config import server_host, server_port

    uvicorn.run(
        "server.main:app",
        host=server_host(),
        port=server_port(),
        reload=True,
    )
