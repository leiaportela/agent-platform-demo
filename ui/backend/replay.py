"""Local development only: replay a recorded turn file instead of calling the platform.

Enabled only when DEV_REPLAY is set. The file is JSON lines of the messages a live turn produced
(RECORD_DIR writes them, starting with a {"kind": "recording_meta", "provenance": "recorded"} line).
Every replayed message is marked replay=true plus the file's provenance ("recorded" or "constructed
sample"), and /api/config reports both, so the UI labels it. A message that matches no recorded turn is
refused (replay_unmatched, listing the recorded messages): replay never shows another turn's records.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

MAX_GAP_S = 3.0  # long silences (the Model Armor lag) are shortened in replay
META_KIND = "recording_meta"
RECORDED = "recorded"
CONSTRUCTED = "constructed sample"
UNSTATED = "provenance not stated in the file"


def load(path: str) -> tuple[list[list[dict]], dict]:
    """(turns, meta). Turns are split at each turn_start; meta is the file's recording_meta line, if any."""
    turns: list[list[dict]] = []
    meta: dict = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("kind") == META_KIND:
            meta = meta or msg
            continue
        if msg.get("kind") == "turn_start" or not turns:
            turns.append([])
        turns[-1].append(msg)
    return turns, meta


def load_turns(path: str) -> list[list[dict]]:
    return load(path)[0]


def provenance_of(path: str, meta: dict) -> str:
    if meta.get("provenance") in (RECORDED, CONSTRUCTED):
        return meta["provenance"]
    name = Path(path).name.lower()
    if name.startswith("sample") or "constructed" in name:
        return CONSTRUCTED
    return UNSTATED


def _key(message: Any) -> str:
    return " ".join(str(message or "").split())


class Replayer:
    def __init__(self, path: str) -> None:
        self.path = path
        self.turns, self.meta = load(path)
        self.provenance = provenance_of(path, self.meta)

    @property
    def messages(self) -> list[str]:
        out: list[str] = []
        for turn in self.turns:
            msg = turn[0].get("message") if turn and turn[0].get("kind") == "turn_start" else None
            if msg and msg not in out:
                out.append(msg)
        return out

    def pick(self, message: str) -> list[dict] | None:
        for turn in self.turns:
            if turn and turn[0].get("kind") == "turn_start" and _key(turn[0].get("message")) == _key(message):
                return turn
        return None

    async def run(self, message: str) -> AsyncIterator[dict]:
        turn = self.pick(message)
        if turn is None:
            yield {"kind": "replay_unmatched", "t_ms": 0, "message": message, "available": self.messages,
                   "replay": True, "provenance": self.provenance}
            return
        start = time.time()
        offset = 0.0  # seconds removed by shortening gaps
        prev = 0.0
        for msg in turn:
            t = (msg.get("t_ms") or 0) / 1000
            gap = t - prev
            if gap > MAX_GAP_S:
                offset += gap - MAX_GAP_S
            prev = t
            wait = start + t - offset - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            out: dict[str, Any] = {**msg, "replay": True, "provenance": self.provenance}
            yield out
