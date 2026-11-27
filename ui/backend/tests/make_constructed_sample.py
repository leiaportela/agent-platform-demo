"""Rebuild fixtures/SAMPLE_replay_constructed.jsonl: a CONSTRUCTED replay sample.

The records in it are real (fixtures/real_q4_deny.*, real_q5_inject.meta.json, real_q4q5.logs.json), but
the sequence is constructed: the real records are fed through the backend's own turn/poller code by fake
clients, on a clock pinned to the real turn's t0; a log entry becomes visible at its receiveTimestamp, an ADK
event at its own timestamp. Linger is shortened. The file is labelled "constructed sample" (first line), so
the UI never presents it as a recording.

Run: .venv/bin/python tests/make_constructed_sample.py   (~40 s, no network)
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import attribution  # noqa: E402
import real  # noqa: E402
from platform_clients import HttpError  # noqa: E402
from poller import LogPoller  # noqa: E402
from settings import Settings  # noqa: E402
from turn import KEEPALIVE, run_turn  # noqa: E402

OUT = HERE / "fixtures" / "SAMPLE_replay_constructed.jsonl"
ENGINE = "projects/123456789012/locations/us-central1/reasoningEngines/1234567890123456789"
SETTINGS = Settings(engine=ENGINE, poll_interval_s=1.0, linger_s=20.0, keepalive_s=3600)
_real_time = time.time


class Clock:
    def __init__(self) -> None:
        self.t0 = 0.0
        self.mono0 = time.monotonic()

    def set(self, t0: float) -> None:
        self.t0, self.mono0 = t0, time.monotonic()

    def __call__(self) -> float:
        return self.t0 + (time.monotonic() - self.mono0)


CLOCK = Clock()


class ReplayEngine:
    def __init__(self, events=None, error=None, error_after=0.0):
        self.events, self.error, self.error_after = events or [], error, error_after

    async def stream_query(self, user_id, session_id, message, headers=None):
        if self.error:
            await asyncio.sleep(self.error_after)
            raise self.error
        for e in self.events:
            at = e.get("timestamp") if isinstance(e, dict) else None
            if isinstance(at, (int, float)):
                await asyncio.sleep(max(0.0, at - CLOCK()))
            yield e


class ReplayLogs:
    """Entries become visible at their receiveTimestamp (as Cloud Logging would return them)."""

    def __init__(self, entries):
        self.entries = entries

    async def list_entries(self, filter_):
        now = CLOCK()
        return [e for e in self.entries
                if (attribution.parse_ts(e.get("receiveTimestamp")) or attribution.parse_ts(e.get("timestamp")) or 0) <= now]


async def one_turn(t0, session_created_at, session_id, message, engine, entries) -> list[dict]:
    CLOCK.set(t0)
    poller = LogPoller(ReplayLogs(entries), SETTINGS)
    out = []
    async for msg in run_turn(engine=engine, poller=poller, settings=SETTINGS, user_id="spike",
                              session_id=session_id, message=message, session_created_at=session_created_at):
        if msg is not KEEPALIVE:
            out.append(msg)
    return out


async def main() -> None:
    time.time = CLOCK  # turn.py and poller.py read time.time(): pin it to the real turns' clock
    try:
        m4 = real.meta("q4_deny")
        t4 = attribution.parse_ts(m4["t0_utc"])
        turn4 = await one_turn(t4, t4, real.Q4_SESSION, m4["prompt"], ReplayEngine(real.sse("q4_deny")),
                               real.logs("q4_deny"))
        m5 = real.meta("q5_inject")
        t5 = attribution.parse_ts("2026-11-26T21:04:48.0Z")  # meta t0 is truncated to the second
        body = json.loads(m5["error_body"])
        armor5 = [e for e in real.logs("q4q5") if e["timestamp"] >= "2026-11-26T21:04:47"]
        turn5 = await one_turn(t5, t5 - 0.3, real.Q5_SESSION, m5["prompt"],
                               ReplayEngine(error=HttpError(403, body), error_after=0.9), armor5)
    finally:
        time.time = _real_time
    meta = {"kind": "recording_meta", "provenance": "constructed sample",
            "note": "real records from 2026-11-26 (q4-deny, q5-inject) fed through the turn/poller code by fake "
                    "clients; arrival timing constructed, linger shortened to 20 s"}
    lines = [meta] + turn4 + turn5
    OUT.write_text("".join(json.dumps(x, default=str, ensure_ascii=False) + "\n" for x in lines))
    print(f"wrote {OUT} ({len(lines)} lines)")


if __name__ == "__main__":
    asyncio.run(main())
