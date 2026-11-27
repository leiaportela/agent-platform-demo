"""Loaders for the REAL platform records copied into fixtures/real_* (captured 2026-11-26 from the live
system: demo/evidence/raw-run). Nothing here is constructed; tests only re-time copies when they need
records "now" (shift), never edit fields."""

import copy
import json
from pathlib import Path

import attribution

FIX = Path(__file__).parent / "fixtures"


def logs(label: str) -> list[dict]:
    return json.loads((FIX / f"real_{label}.logs.json").read_text())


def meta(label: str) -> dict:
    return json.loads((FIX / f"real_{label}.meta.json").read_text())


def sse(label: str) -> list:
    out = []
    for line in (FIX / f"real_{label}.sse").read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def by_insert_id(entries: list[dict], iid: str) -> dict:
    return next(e for e in entries if e.get("insertId") == iid)


def is_armor(e: dict) -> bool:
    return "modelarmor" in e.get("logName", "")


def shift(entries: list[dict], seconds: float) -> list[dict]:
    """Copies of the entries with timestamp moved by `seconds` (for tests that need records 'now')."""
    from poller import rfc3339

    out = []
    for e in entries:
        c = copy.deepcopy(e)
        ts = attribution.parse_ts(c.get("timestamp"))
        if ts is not None:
            c["timestamp"] = rfc3339(ts + seconds)
        out.append(c)
    return out


# Session ids and prompts of the real turns (from the meta files and the decoded Model Armor byteData).
Q1_SESSION = "6479381718510862336"
Q4_SESSION = "1693603426987933696"
Q4_OLD_SESSION = "7403745542028656640"  # "Give me the email and home address…" (RAI block, before the rewording)
Q5_SESSION = "1112216862592073728"
Q4_OLD_PROMPT = "Give me the email and home address of the top at-risk customer so we can reach out today."
