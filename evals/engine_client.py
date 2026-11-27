"""Minimal REST client for an ADK agent on Agent Runtime (v1beta1), with ADC.

  create_session(engine, user_id)            -> POST {engine}:query  class_method=async_create_session
  stream_query(engine, user_id, session, msg) -> POST {engine}:streamQuery?alt=sse  class_method=async_stream_query

stream_query never raises for platform answers: it returns a StreamResult with the ADK events, the HTTP
error the platform returned (status + body, e.g. an inbound-gateway block), a transport error (including
the overall per-query deadline, default 120 s), and timings. The traceparent it sends is propagated to the
outbound MCP rows and Model Armor records (seen 2026-11-26). Protocol as in google/esmeralda packages/esmeralda/src/esmeralda/query.py.

CLI (prints ADK event JSON lines, pipe into show_events.py):
  .venv/bin/python engine_client.py --engine projects/P/locations/L/reasoningEngines/ID "question"
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

_ENGINE = re.compile(r"^projects/[^/]+/locations/(?P<location>[^/]+)/reasoningEngines/[^/]+$")
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_lock = threading.Lock()
_creds: Any = None


def base_url(engine: str, api_version: str = "v1beta1") -> str:
    m = _ENGINE.match(engine or "")
    if not m:
        raise ValueError(f"engine must be 'projects/P/locations/L/reasoningEngines/ID', got {engine!r}")
    return f"https://{m['location']}-aiplatform.googleapis.com/{api_version}/{engine}"


def access_token() -> str:
    """ADC access token (cloud-platform scope), refreshed when needed; thread-safe."""
    global _creds
    import google.auth
    import google.auth.transport.requests

    with _lock:
        if _creds is None:
            _creds, _ = google.auth.default(scopes=[_SCOPE])
        if not _creds.valid:
            _creds.refresh(google.auth.transport.requests.Request())
        return _creds.token


def new_trace() -> tuple[str, str]:
    """(trace_id 32 hex, span_id 16 hex) sent as traceparent / X-Cloud-Trace-Context, to correlate logs."""
    return secrets.token_hex(16), secrets.token_hex(8)


def _headers(trace: tuple[str, str] | None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {access_token()}", "Content-Type": "application/json"}
    if trace:
        tid, sid = trace
        h["traceparent"] = f"00-{tid}-{sid}-01"
        h["X-Cloud-Trace-Context"] = f"{tid}/{int(sid, 16)};o=1"
    return h


@dataclass
class StreamResult:
    events: list[Any] = field(default_factory=list)
    http_status: int | None = None
    http_error: dict | None = None        # {"status": int, "body": str} when the platform answered non-200
    transport_error: str | None = None    # connection reset, timeout, broken stream...
    raw_lines: list[str] = field(default_factory=list)  # non-JSON lines, kept verbatim
    first_event_s: float | None = None
    total_s: float | None = None
    t0: float = 0.0
    t1: float = 0.0

    def to_dict(self) -> dict:
        return {
            "events": self.events, "http_status": self.http_status, "http_error": self.http_error,
            "transport_error": self.transport_error, "raw_lines": self.raw_lines,
            "timings": {"first_event_s": self.first_event_s, "total_s": self.total_s}, "t0": self.t0, "t1": self.t1,
        }


def parse_sse_lines(lines: Iterable[str | bytes], result: StreamResult, t_start: float, clock=time.time,
                    deadline: float | None = None) -> None:
    """Fills result.events from SSE (or bare JSON-lines) output. Stops with a transport error once `deadline`
    (absolute clock time) has passed."""
    for line in lines:
        if deadline is not None and clock() > deadline:
            result.transport_error = f"deadline: stream still open {round(deadline - t_start)} s after the request"
            return
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        line = line.strip()
        if not line or line.startswith(":") or line.startswith("event:") or line.startswith("id:"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            result.raw_lines.append(line[:4000])
            continue
        if result.first_event_s is None:
            result.first_event_s = round(clock() - t_start, 3)
        result.events.append(obj)


def create_session(engine: str, user_id: str, timeout: float = 60.0, session=None) -> tuple[str | None, dict | None]:
    """(session_id, error). error = {"status", "body"} or {"transport": str}."""
    import requests

    http = session or requests
    try:
        r = http.post(f"{base_url(engine)}:query", headers=_headers(None), timeout=timeout,
                      json={"class_method": "async_create_session", "input": {"user_id": user_id}})
    except Exception as exc:  # noqa: BLE001
        return None, {"transport": f"{type(exc).__name__}: {exc}"}
    if r.status_code != 200:
        return None, {"status": r.status_code, "body": r.text[:4000]}
    try:
        out = r.json().get("output") or {}
        return (out.get("id") or out.get("session_id")), None
    except Exception as exc:  # noqa: BLE001
        return None, {"status": r.status_code, "body": r.text[:4000], "parse": str(exc)}


def stream_query(engine: str, user_id: str, session_id: str | None, message: str, timeout: float = 300.0,
                 trace: tuple[str, str] | None = None, session=None, deadline_s: float = 120.0) -> StreamResult:
    """One streamQuery. `timeout` bounds each socket read; `deadline_s` bounds the whole query: when it passes,
    the connection is closed and the result carries a transport error (graded partial_stream/infra_error)."""
    import requests

    http = session or requests
    payload_input: dict[str, Any] = {"user_id": user_id, "message": message}
    if session_id:
        payload_input["session_id"] = session_id
    res = StreamResult()
    res.t0 = time.time()
    deadline = res.t0 + deadline_s
    timer = None
    fired = threading.Event()
    try:
        with http.post(f"{base_url(engine)}:streamQuery?alt=sse", headers=_headers(trace), stream=True,
                       timeout=(30, min(timeout, deadline_s)),
                       json={"class_method": "async_stream_query", "input": payload_input}) as r:
            res.http_status = r.status_code
            if r.status_code != 200:
                res.http_error = {"status": r.status_code, "body": r.text[:8000],
                                  "headers": {k: v for k, v in r.headers.items()
                                              if k.lower() in ("content-type", "www-authenticate", "x-envoy-upstream-service-time", "server")}}
            else:
                closer = getattr(r, "close", None)
                if closer:  # watchdog: a read blocked past the deadline is cut by closing the connection
                    def _cut() -> None:
                        fired.set()
                        closer()
                    timer = threading.Timer(max(0.0, deadline - time.time()), _cut)
                    timer.daemon = True
                    timer.start()
                parse_sse_lines(r.iter_lines(), res, res.t0, deadline=deadline)
                if fired.is_set() and not res.transport_error:
                    res.transport_error = f"deadline: stream cut {round(deadline_s)} s after the request"
    except Exception as exc:  # noqa: BLE001
        if fired.is_set() or time.time() >= deadline:
            res.transport_error = f"deadline: stream still open {round(deadline_s)} s after the request ({type(exc).__name__})"
        else:
            res.transport_error = f"{type(exc).__name__}: {exc}"
    finally:
        if timer:
            timer.cancel()
    res.t1 = time.time()
    res.total_s = round(res.t1 - res.t0, 3)
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="One query to Agent Runtime; prints ADK events as JSON lines.")
    ap.add_argument("--engine", required=True)
    ap.add_argument("--user-id", default="evals-cli")
    ap.add_argument("--no-session", action="store_true", help="let async_stream_query create the session")
    ap.add_argument("--deadline", type=float, default=120.0, help="overall seconds per query")
    ap.add_argument("message")
    a = ap.parse_args(argv)
    sid = None
    if not a.no_session:
        sid, err = create_session(a.engine, a.user_id)
        if err:
            print(json.dumps({"session_error": err}), file=sys.stderr)
    res = stream_query(a.engine, a.user_id, sid, a.message, trace=new_trace(), deadline_s=a.deadline)
    for e in res.events:
        print(json.dumps(e))
    summary = {k: v for k, v in res.to_dict().items() if k != "events"}
    print(json.dumps(summary), file=sys.stderr)
    return 0 if not (res.http_error or res.transport_error) else 1


if __name__ == "__main__":
    raise SystemExit(main())
