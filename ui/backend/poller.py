"""One Cloud Logging poller per process, shared by every open turn.

Each tick makes ONE entries.list query (a single page in practice; extra pages count as extra calls and
stretch the next sleep) covering the union of the open turn windows (gateway rows, Model Armor records and the
IAP decisions for the agent's MCP calls, all in one filter), then hands every new record to the turn
it belongs to (attribution.py). Ticks are at least settings.poll_interval_s apart (>= 3 s from the
environment), and an HTTP 429 doubles the delay up to settings.max_backoff_s. The quota is per project and
shared with everyone, so run one backend instance (Cloud Run max-instances=1).

A turn's window stays open until settings.linger_s after its agent stream ended; its last tick starts after
that moment, then the turn gets a final None on its queue.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import attribution
import records
from attribution import HOLD, Decision, TurnRef
from platform_clients import HttpError, LogPage
from settings import Settings


def rfc3339(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(eq=False)
class TurnCtx(TurnRef):
    """An open turn as the poller sees it: attribution state plus the queue its messages go to."""

    t0: float = 0.0
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    deadline: float | None = None  # stream end + linger; None while the agent is still answering
    trace_urls: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    last_error: Any = None
    closed: bool = False

    def ms(self, at: float | None = None) -> int:
        return int(((at if at is not None else time.time()) - self.t0) * 1000)


class LogPoller:
    def __init__(self, logs: Any, settings: Settings,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
        self.logs = logs
        self.settings = settings
        self.sleep = sleep
        self.turns: list[TurnCtx] = []
        self.seen: set[str] = set()
        self.backoff = 0.0
        self.ticks = 0
        self.calls = 0
        self.delays: deque[float] = deque(maxlen=100)  # recent sleeps, for tests and debugging
        # The IAP decisions live in the Data Access audit log (a private log: roles/logging.privateLogViewer).
        # If Cloud Logging refuses the query with that clause (HTTP 403), keep polling without it.
        self.include_iap = settings.agent_principal is not None
        self._task: asyncio.Task | None = None

    # -- turns ---------------------------------------------------------------------------------------

    def cap_open_windows(self, at: float) -> None:
        """Newer activity starts at `at`: no older window reaches past it (windows never overlap)."""
        for t in self.turns:
            if t.cap is None or t.cap > at:
                t.cap = max(at, t.start)

    def open(self, turn: TurnCtx) -> None:
        self.cap_open_windows(turn.start)
        self.turns.append(turn)
        self.ensure_running()

    def ensure_running(self) -> None:
        if self.turns and (self._task is None or self._task.done()):
            self._task = asyncio.get_running_loop().create_task(self._run())

    def close(self, turn: TurnCtx) -> None:
        turn.closed = True
        if turn in self.turns:
            self.turns.remove(turn)
        if not self.turns:
            self.seen.clear()

    # -- loop ----------------------------------------------------------------------------------------

    async def _run(self) -> None:
        while self.turns:
            delay = await self.tick()
            if not self.turns:
                break
            self.delays.append(delay)
            await self.sleep(delay)

    def _filter(self) -> str:
        since = min(t.start for t in self.turns)
        until = None if any(t.cap is None for t in self.turns) else max(t.cap for t in self.turns)
        return records.log_filter(
            self.settings.project, rfc3339(since), rfc3339(until) if until is not None else None,
            (self.settings.inbound_gw, self.settings.outbound_gw),
            self.settings.agent_principal if self.include_iap else None,
        )

    async def tick(self) -> float:
        """One poll for all open turns; returns the delay before the next one."""
        started = time.time()
        turns = list(self.turns)
        for t in turns:
            t.finalizing = t.deadline is not None and started >= t.deadline
        self.ticks += 1
        interval = self.settings.poll_interval_s
        delay = interval
        try:
            page = await self.logs.list_entries(self._filter())
            if isinstance(page, LogPage):
                entries, calls, truncated = page.entries, page.calls, page.truncated
            else:  # plain list (test fakes)
                entries, calls, truncated = list(page or []), 1, False
            self.calls += calls
            self.backoff = 0.0
            delay = interval * max(1, calls)
            for t in turns:
                t.last_error = None
            if truncated:
                self._error_to_all(turns, None, None,
                                   "Cloud Logging returned more pages than one poll reads; later records may be missing.")
            self._dispatch(entries, turns)
        except HttpError as exc:
            self.calls += 1
            message = records.http_error_message(exc.body)
            if exc.status == 403 and self.include_iap:
                self.include_iap = False  # gateway rows and Model Armor records keep flowing from the next tick
                message = ("Cloud Logging refused the query with the IAP decision clause (Data Access audit log; "
                           "reading it needs roles/logging.privateLogViewer): polling continues without IAP "
                           f"decisions. {message or ''}").strip()
            elif exc.status == 429:
                self.backoff = min(self.settings.max_backoff_s, max(2 * interval, self.backoff * 2))
                delay = self.backoff
            self._error_to_all(turns, exc.status, exc.body, message)
        except Exception as exc:  # noqa: BLE001 - shown to the presenter as a poll error
            self.calls += 1
            self._error_to_all(turns, None, None, f"{type(exc).__name__}: {exc}")
        for t in turns:
            if t.finalizing:
                if t in self.turns:
                    self.turns.remove(t)
                t.queue.put_nowait(None)  # the turn's log window is closed
        if not self.turns:
            self.seen.clear()
        return delay

    # -- records -------------------------------------------------------------------------------------

    def _dispatch(self, entries: list[dict], turns: list[TurnCtx]) -> None:
        prepared = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key = records.insert_id(entry)
            if key in self.seen:
                continue
            source = records.classify(entry, self.settings.inbound_gw, self.settings.outbound_gw)
            if source is None:
                continue
            prepared.append((key, entry, source, records.summarize_log(entry, source)))
        # Prompt screenings first: they teach the correlation id that matches the response screening.
        prepared.sort(key=lambda p: 0 if p[3].get("operation") == "prompt" else 1)
        for key, entry, source, summary in prepared:
            ts = attribution.parse_ts(entry.get("timestamp"))
            decision = attribution.attribute(source, summary, ts, turns, self.settings.agent_principal)
            if decision is None or decision == HOLD:
                continue
            assert isinstance(decision, Decision)
            attribution.learn(decision, source, summary)
            self.seen.add(key)
            self._emit(decision, source, summary, entry)

    def _emit(self, decision: Decision, source: str, summary: dict, entry: dict) -> None:
        turn = decision.turn
        assert isinstance(turn, TurnCtx)
        hidden = bool(summary.get("hidden"))
        unmatched = decision.method == "unmatched"
        bucket = "unmatched" if unmatched else "hidden" if hidden else decision.method
        turn.counts[bucket] = turn.counts.get(bucket, 0) + 1
        turl = records.trace_url(summary.get("trace_id"), self.settings.project)
        if turl and not hidden and not unmatched and turl not in turn.trace_urls:
            turn.trace_urls.append(turl)
        turn.queue.put_nowait({
            "kind": "log", "t_ms": turn.ms(), "source": source, "summary": summary, "raw": entry,
            "console_url": records.console_url(entry, self.settings.project), "trace_url": turl,
            "hidden": hidden, "attribution": {"method": decision.method, "detail": decision.detail},
        })

    def _error_to_all(self, turns: list[TurnCtx], status: int | None, body: Any, message: str | None) -> None:
        key = (status, str(body), message)
        for t in turns:
            if t.last_error == key:  # each distinct error once in a row per turn
                continue
            t.last_error = key
            t.queue.put_nowait({"kind": "log_poll_error", "t_ms": t.ms(), "status": status, "body": body,
                                "message": message})
