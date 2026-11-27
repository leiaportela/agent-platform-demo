import json
import os
import sys

import pytest

EVALS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
sys.path.insert(0, EVALS)


def sample_path(name: str) -> str:
    return os.path.join(SAMPLES, name)


def load_sample(name: str):
    with open(sample_path(name)) as f:
        return json.load(f)


def sample_events(name: str) -> list[dict]:
    import engine_client

    res = engine_client.StreamResult()
    with open(sample_path(name)) as f:
        engine_client.parse_sse_lines(f, res, 0.0, clock=lambda: 1.5)
    return res.events


@pytest.fixture
def gt():
    return load_sample("sample_ground_truth.json")


@pytest.fixture
def prompts():
    import run_remote

    return run_remote.load_prompts(os.path.join(EVALS, "prompts.yaml"))


@pytest.fixture
def specs(prompts):
    return {p["id"]: p for s in prompts.values() for p in s}


# ---- real records (copied from demo/evidence/raw-run, 2026-11-26) --------------------------------------

def real_run(label: str) -> dict:
    """A run record built from a real capture: real_<label>.meta.json (+ real_<label>.sse if present).
    t0 is the capture's t0_utc (whole seconds), t1 = t0 + total_s."""
    import evidence
    import grading

    meta = load_sample(f"real_{label}.meta.json")
    t0 = evidence.parse_ts(meta["t0_utc"])
    events = sample_events(f"real_{label}.sse") if os.path.exists(sample_path(f"real_{label}.sse")) else []
    http_error = {"status": meta["status"], "body": meta["error_body"]} if meta.get("error_body") else None
    return {"prompt": meta["prompt"], "session_id": meta["session_id"], "t0": t0, "t1": t0 + meta["total_s"],
            "events": events, "http_error": http_error, "transport_error": None,
            "final_text": grading.parse_events(events)["final_text"]}


def real_logs(*names: str) -> list[dict]:
    out = []
    for n in names:
        out += load_sample(f"real_{n}.logs.json")
    return out


def fetch_from(entries: list[dict]):
    """A fake entries.list: returns the entries of the log the filter asks for."""
    def fetch(flt):
        want_ma = "modelarmor" in flt
        return [e for e in entries if ("modelarmor" in str(e.get("logName"))) == want_ma]
    return fetch
