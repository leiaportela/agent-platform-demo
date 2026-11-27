"""One chat turn: the Agent Runtime stream plus the records the shared log poller attributes to it.

Messages (each one SSE data line, see sse()):
  turn_start   {t0, window_from, message, session_id, trace_id_sent, iap_polled}
  listening    {until_ms}                 until_ms is null while the agent is still answering
  adk_event    {raw, summary}             one per streamQuery line (summary.is_error for in-stream errors)
  http_error   {status, body, message}    the platform answered streamQuery with a non-200 (never retried)
  transport_error {error}                 the UI backend could not reach the platform (a chat alert only)
  stream_end   {latency_ms}
  log          {source, summary, raw, console_url, trace_url, hidden, attribution{method, detail}}
  log_poll_error {status, body, message}
  turn_end     {latency_ms, trace_urls, hidden_rows, unmatched_rows, attributed}
Every message carries kind and t_ms (milliseconds since the turn started).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import records
from platform_clients import HttpError
from poller import LogPoller, TurnCtx, rfc3339
from settings import Settings

KEEPALIVE = object()


def sse(message: dict) -> str:
    """One SSE frame. json.dumps escapes newlines, so a frame is always a single data line."""
    return "data: " + json.dumps(message, separators=(",", ":"), default=str, ensure_ascii=False) + "\n\n"


SSE_KEEPALIVE = ": keepalive\n\n"


def window_start(t0: float, settings: Settings, session_created_at: float | None) -> float:
    """t0 - lookback; for the first turn of a session created moments ago, the session's creation (its
    create-session response is screened by Model Armor too)."""
    start = t0 - settings.lookback_s
    if session_created_at is not None and 0 <= t0 - session_created_at <= settings.session_window_s:
        start = min(start, session_created_at - settings.lookback_s)
    return start


async def run_turn(
    *,
    engine: Any,
    poller: LogPoller,
    settings: Settings,
    user_id: str,
    session_id: str,
    message: str,
    session_created_at: float | None = None,
) -> AsyncIterator[Any]:
    """Yields message dicts (and KEEPALIVE markers while idle)."""
    t0 = time.time()
    trace = secrets.token_hex(16) if settings.send_traceparent else None
    turn = TurnCtx(id=uuid.uuid4().hex, session_id=session_id, message=message,
                   start=window_start(t0, settings, session_created_at), t0=t0)
    turn.sent_trace = trace
    queue = turn.queue
    stream_end: list[float] = []

    async def put(kind: str, **fields: Any) -> None:
        await queue.put({"kind": kind, "t_ms": turn.ms(), **fields})

    async def stream() -> None:
        headers = {"traceparent": f"00-{trace}-{secrets.token_hex(8)}-01"} if trace else None
        try:
            async for event in engine.stream_query(user_id, session_id, message, headers=headers):
                summary = records.summarize_adk_event(event)
                if summary["text"] and summary["author"] != "user":
                    turn.answer_texts.append(summary["text"])
                if summary["calls"]:  # IAP decisions are matched to the turn by these tool names
                    at = summary["timestamp"]
                    at = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else time.time()
                    turn.tool_calls.extend((c["name"], at) for c in summary["calls"] if c.get("name"))
                await put("adk_event", raw=event, summary=summary)
        except HttpError as exc:
            await put("http_error", status=exc.status, body=exc.body,
                      message=records.http_error_message(exc.body))
        except Exception as exc:  # noqa: BLE001 - shown to the presenter as a backend error
            await put("transport_error", error=f"{type(exc).__name__}: {exc}")
        finally:
            end = time.time()
            stream_end.append(end)
            turn.deadline = end + settings.linger_s
            await put("stream_end", latency_ms=turn.ms(end))
            await put("listening", until_ms=turn.ms(end + settings.linger_s))

    poller.open(turn)
    task = None
    try:
        yield {"kind": "turn_start", "t_ms": 0, "t0": rfc3339(t0), "window_from": rfc3339(turn.start),
               "message": message, "session_id": session_id, "trace_id_sent": trace,
               "iap_polled": poller.include_iap}
        yield {"kind": "listening", "t_ms": 0, "until_ms": None}
        task = asyncio.create_task(stream())
        window_closed = False
        safety = settings.max_backoff_s + 4 * settings.poll_interval_s + 10
        last_sent = time.time()
        while not (window_closed and task.done() and queue.empty()):
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                poller.ensure_running()
                if turn.deadline is not None and time.time() > turn.deadline + safety:
                    window_closed = True  # the poller stopped answering for this turn: do not hang
                if time.time() - last_sent >= settings.keepalive_s:
                    last_sent = time.time()
                    yield KEEPALIVE
                continue
            if item is None:  # the poller's last tick for this turn is done
                window_closed = True
                continue
            last_sent = time.time()
            yield item
        if task.exception():  # surface unexpected crashes in the server log
            raise task.exception()  # type: ignore[misc]
        counts = dict(turn.counts)
        yield {"kind": "turn_end", "t_ms": turn.ms(), "latency_ms": turn.ms(stream_end[0]) if stream_end else None,
               "trace_urls": turn.trace_urls, "hidden_rows": counts.pop("hidden", 0),
               "unmatched_rows": counts.pop("unmatched", 0), "attributed": counts}
    finally:
        if task:
            task.cancel()
        poller.close(turn)
