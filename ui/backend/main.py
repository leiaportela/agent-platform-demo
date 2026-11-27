"""Pipelines analyst chat UI backend.

GET  /api/config   engine, gateways, console links
POST /api/session  creates an ADK session on Agent Runtime
POST /api/chat     one turn as text/event-stream (see turn.py for the message kinds)
GET  /             the built frontend (frontend/dist)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import records
from platform_clients import EngineClient, HttpError, LoggingClient, TokenSource
from poller import LogPoller
from replay import Replayer
from settings import Settings
from turn import KEEPALIVE, SSE_KEEPALIVE, run_turn, sse

DIST = Path(os.environ.get("FRONTEND_DIST") or Path(__file__).resolve().parent.parent / "frontend" / "dist")


class SessionIn(BaseModel):
    user_id: str | None = None


class ChatIn(BaseModel):
    session_id: str
    user_id: str
    message: str


def create_app(settings: Settings | None = None, engine: Any = None, logs: Any = None) -> FastAPI:
    settings = settings or Settings.from_env()  # raises SettingsError on a malformed ENGINE: fail at startup
    app = FastAPI(title="Pipelines analyst UI")
    tokens = TokenSource()
    logs = logs or LoggingClient(settings.project, tokens)
    state: dict[str, Any] = {
        "engine": engine or (EngineClient(settings.engine, settings.region, tokens) if settings.engine else None),
        "logs": logs,
        "poller": LogPoller(logs, settings),  # one per process, shared by every open turn
        "sessions": OrderedDict(),  # session id -> creation time (for the first turn's window)
        "replayer": Replayer(settings.dev_replay) if settings.dev_replay else None,
    }
    app.state.settings = settings
    app.state.platform = state

    @app.get("/api/config")
    def config() -> dict:
        replayer = state["replayer"]
        return {
            "engine": settings.engine,
            "project": settings.project,
            "region": settings.region,
            "gateways": {"inbound": settings.inbound_gw, "outbound": settings.outbound_gw},
            "agent_principal": settings.agent_principal,
            "links": {
                "gateways": records.gateways_url(settings.project),
                "traces": records.traces_url(settings.project),
                "engine": settings.engine_link(),
            },
            "linger_s": settings.linger_s,
            "poll_interval_s": settings.poll_interval_s,
            "replay": bool(replayer),
            "replay_file": Path(settings.dev_replay).name if settings.dev_replay else None,
            "replay_provenance": replayer.provenance if replayer else None,
            "replay_messages": replayer.messages if replayer else [],
        }

    @app.post("/api/session")
    async def session(body: SessionIn) -> JSONResponse:
        user_id = body.user_id or f"demo-{uuid.uuid4().hex[:8]}"
        if state["replayer"]:
            return JSONResponse({"session_id": f"replay-{uuid.uuid4().hex[:8]}", "user_id": user_id, "replay": True})
        if not state["engine"]:
            raise HTTPException(503, "ENGINE is not set")
        created_at = time.time()
        state["poller"].cap_open_windows(created_at - settings.lookback_s)  # its records are the new turn's
        try:
            out = await state["engine"].create_session(user_id)
        except HttpError as exc:
            return JSONResponse({"error": "create_session failed", "status": exc.status, "body": exc.body,
                                 "message": records.http_error_message(exc.body)}, status_code=502)
        session_id = records.session_id_from(out)
        if not session_id:
            return JSONResponse({"error": "no session id in response", "body": out}, status_code=502)
        sessions = state["sessions"]
        sessions[session_id] = created_at
        while len(sessions) > 500:
            sessions.popitem(last=False)
        return JSONResponse({"session_id": session_id, "user_id": user_id})

    @app.post("/api/chat")
    async def chat(body: ChatIn) -> StreamingResponse:
        if state["replayer"]:
            source = state["replayer"].run(body.message)
        else:
            if not state["engine"]:
                raise HTTPException(503, "ENGINE is not set")
            source = run_turn(engine=state["engine"], poller=state["poller"], settings=settings,
                              user_id=body.user_id, session_id=body.session_id, message=body.message,
                              session_created_at=state["sessions"].pop(body.session_id, None))

        record = _recorder(settings, body.session_id)

        async def frames():
            async for msg in source:
                if msg is KEEPALIVE:
                    yield SSE_KEEPALIVE
                    continue
                if record:
                    record(msg)
                yield sse(msg)

        return StreamingResponse(frames(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    if DIST.is_dir():
        if (DIST / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            target = (DIST / path).resolve()
            if path and target.is_file() and DIST.resolve() in target.parents:
                return FileResponse(target)
            return FileResponse(DIST / "index.html")

    return app


def _recorder(settings: Settings, session_id: str):
    """RECORD_DIR: append the live turn's messages to <dir>/<date>-<session>.jsonl (replay source). A new
    file starts with a recording_meta line so replay labels it "recorded"."""
    if not settings.record_dir or settings.dev_replay:
        return None
    directory = Path(settings.record_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{time.strftime('%Y%m%d')}-{session_id.rsplit('/', 1)[-1]}.jsonl"

    def write(msg: dict) -> None:
        new_file = not path.exists()
        with path.open("a") as fh:
            if new_file:
                meta = {"kind": "recording_meta", "provenance": "recorded", "engine": settings.engine,
                        "project": settings.project, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
            fh.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")

    return write


app = create_app()
