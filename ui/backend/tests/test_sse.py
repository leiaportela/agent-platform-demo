"""SSE framing and the /api/chat turn flow, with fake platform clients fed REAL records (re-timed)."""
import asyncio
import dataclasses
import json
import time

import httpx
import pytest

import attribution
import real
import samples as s
from main import create_app
from platform_clients import HttpError
from settings import Settings
from turn import sse

ENGINE = "projects/p/locations/us-central1/reasoningEngines/1"
FAST = Settings(engine=ENGINE, poll_interval_s=0.05, linger_s=0.3, lookback_s=2.0, keepalive_s=10, max_backoff_s=0.4)


class FakeEngine:
    def __init__(self, events=None, error=None, delay=0.01):
        self.events, self.error, self.calls, self.delay, self.headers = events or [], error, 0, delay, []

    async def create_session(self, user_id):
        return {"output": {"id": "sess-1", "user_id": user_id}}

    async def stream_query(self, user_id, session_id, message, headers=None):
        self.calls += 1
        self.headers.append(headers)
        if self.error:
            raise self.error
        for e in self.events:
            await asyncio.sleep(self.delay)
            yield e


class FakeLogs:
    """Cloud Logging stand-in: releases one more batch on every call and returns everything released so
    far (like a real time-window query). `shift_to_now` re-times real entries relative to the first call."""

    def __init__(self, batches, real_t0=None):
        self.batches, self.calls, self.filters, self.real_t0, self.offset = batches, 0, [], real_t0, None

    async def list_entries(self, filter_):
        self.filters.append(filter_)
        self.calls += 1
        if self.real_t0 is not None and self.offset is None:
            self.offset = time.time() - self.real_t0
        out = []
        for b in self.batches[: self.calls]:
            out.extend(real.shift(b, self.offset) if self.offset is not None else b)
        return out


def parse_frames(text):
    msgs = []
    for frame in text.split("\n\n"):
        if not frame.strip() or frame.startswith(":"):
            continue
        lines = frame.split("\n")
        assert len(lines) == 1 and lines[0].startswith("data: "), frame
        msgs.append(json.loads(lines[0][6:]))
    return msgs


async def post_chat(app, message="hi", session_id="sess-1"):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as c:
        r = await c.post("/api/chat", json={"session_id": session_id, "user_id": "u", "message": message})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        return parse_frames(r.text)


def test_sse_frame_is_one_data_line():
    frame = sse({"kind": "adk_event", "raw": {"text": "line1\nline2\n\ndata: fake"}})
    assert frame.endswith("\n\n") and frame.count("\n") == 2 and frame.startswith("data: ")
    assert json.loads(frame[6:])["raw"]["text"] == "line1\nline2\n\ndata: fake"


@pytest.mark.asyncio
async def test_real_deny_turn_end_to_end():
    m = real.meta("q4_deny")
    entries = real.logs("q4_deny")
    t0 = attribution.parse_ts(m["t0_utc"])
    engine = FakeEngine(real.sse("q4_deny"))
    logs = FakeLogs([entries[:12], entries[12:30], entries[30:]], real_t0=t0)
    # q4_deny was recorded without a traceparent (spike script), so replay it in that mode.
    legacy = dataclasses.replace(FAST, send_traceparent=False)
    msgs = await post_chat(create_app(legacy, engine=engine, logs=logs), m["prompt"], real.Q4_SESSION)
    kinds = [x["kind"] for x in msgs]
    assert kinds[0] == "turn_start" and kinds[1] == "listening" and kinds[-1] == "turn_end"
    assert kinds.count("adk_event") == 7 and kinds.count("stream_end") == 1
    listening = [x for x in msgs if x["kind"] == "listening"]
    assert listening[0]["until_ms"] is None and listening[-1]["until_ms"] >= 300
    logs_out = [x for x in msgs if x["kind"] == "log"]
    ids = [x["raw"]["insertId"] for x in logs_out]
    assert len(ids) == len(set(ids)) == len(entries)  # each entry once, all of them in this turn
    assert {x["source"] for x in logs_out} == {"outbound_gateway", "model_armor"}
    deny = [x for x in logs_out if x["summary"].get("denied")]
    assert len(deny) == 1 and not deny[0]["hidden"] and deny[0]["summary"]["status"] == 403
    assert deny[0]["console_url"].startswith("https://console.cloud.google.com/logs/query;")
    hidden = [x for x in logs_out if x["hidden"]]
    assert hidden and all(x["raw"]["httpRequest"]["requestMethod"] == "CONNECT" for x in hidden)
    armor = sorted(x["attribution"]["method"] for x in logs_out if x["source"] == "model_armor")
    assert armor == ["answer_text", "prompt_text", "session_text"]
    end = msgs[-1]
    assert end["hidden_rows"] == len(hidden) and end["unmatched_rows"] == 0 and end["latency_ms"] is not None
    assert end["attributed"]["session_id"] >= 1 and end["attributed"]["time"] >= 1
    assert "https://console.cloud.google.com/traces/explorer;traceId=5c380538565aa43a038f14984491c3cc?project=your-project-id" in end["trace_urls"]
    t = [x["t_ms"] for x in msgs]
    assert t == sorted(t)
    assert 'timestamp>="' in logs.filters[0]
    assert msgs[0]["trace_id_sent"] is None and "traceparent" not in (engine.headers[0] or {})


@pytest.mark.asyncio
async def test_real_inbound_block_is_forwarded_once_without_retry():
    m = real.meta("q5_inject")
    body = json.loads(m["error_body"])
    engine = FakeEngine(error=HttpError(403, body))
    armor = [e for e in real.logs("q4q5") if real.is_armor(e) and e["timestamp"] > "2026-11-26T21:04:48"]
    logs = FakeLogs([armor], real_t0=attribution.parse_ts(m["t0_utc"]))
    msgs = await post_chat(create_app(FAST, engine=engine, logs=logs), m["prompt"], real.Q5_SESSION)
    errs = [x for x in msgs if x["kind"] == "http_error"]
    assert engine.calls == 1 and len(errs) == 1
    assert errs[0]["status"] == 403 and errs[0]["body"] == body
    assert errs[0]["message"] == "Model Armor: Prompt violates content security configurations"
    assert [x["kind"] for x in msgs].count("adk_event") == 0 and msgs[-1]["kind"] == "turn_end"
    got = {(x["summary"]["operation"], x["attribution"]["method"], x["summary"]["match_state"])
           for x in msgs if x["kind"] == "log"}
    assert got == {("prompt", "prompt_text", "MATCH_FOUND"), ("response", "session_text", "NO_MATCH_FOUND")}


@pytest.mark.asyncio
async def test_in_stream_errors_and_transport_error_are_messages():
    events = real.sse("q4_deny_v3_1")
    msgs = await post_chat(create_app(FAST, engine=FakeEngine(events), logs=FakeLogs([])))
    errs = [x["summary"] for x in msgs if x["kind"] == "adk_event" and x["summary"]["is_error"]]
    assert [e["error_code"] for e in errs] == ["ValueError", "498"]
    msgs = await post_chat(create_app(FAST, engine=FakeEngine(error=RuntimeError("socket closed")), logs=FakeLogs([])))
    te = [x for x in msgs if x["kind"] == "transport_error"]
    assert len(te) == 1 and te[0]["error"] == "RuntimeError: socket closed" and msgs[-1]["kind"] == "turn_end"


@pytest.mark.asyncio
async def test_log_poll_error_is_reported_once_and_backs_off():
    class FailingLogs:
        calls = 0

        async def list_entries(self, filter_):
            FailingLogs.calls += 1
            raise HttpError(429, {"error": {"message": "quota"}})

    app = create_app(FAST, engine=FakeEngine([s.SAMPLE_ADK_ANSWER]), logs=FailingLogs())
    msgs = await post_chat(app)
    errs = [x for x in msgs if x["kind"] == "log_poll_error"]
    assert len(errs) == 1 and errs[0]["status"] == 429 and msgs[-1]["kind"] == "turn_end"
    delays = list(app.state.platform["poller"].delays)
    assert delays and delays[0] == pytest.approx(0.1)  # 2 x interval, then doubling up to max_backoff_s
    assert all(b >= a for a, b in zip(delays, delays[1:])) and max(delays) <= 0.4


@pytest.mark.asyncio
async def test_concurrent_turns_share_one_poller_one_call_per_tick():
    """Turn 2 starts while turn 1 lingers: one entries.list per tick serves both, records go by session."""
    q1 = real.logs("q1_dryrun")
    t0 = attribution.parse_ts(real.meta("q1_dryrun")["t0_utc"])
    logs = FakeLogs([q1], real_t0=t0)
    settings = Settings(engine=ENGINE, poll_interval_s=0.05, linger_s=0.6, lookback_s=2.0, max_backoff_s=0.4)
    app = create_app(settings, engine=FakeEngine([s.SAMPLE_ADK_ANSWER]), logs=logs)

    async def second():
        await asyncio.sleep(0.15)
        return await post_chat(app, "other question", "other-session")

    first, other = await asyncio.gather(post_chat(app, real.meta("q1_dryrun")["prompt"], real.Q1_SESSION), second())
    poller = app.state.platform["poller"]
    assert logs.calls == poller.ticks == poller.calls  # exactly one entries.list per tick
    mine = [x for x in first if x["kind"] == "log"]
    theirs = [x for x in other if x["kind"] == "log"]
    assert any(x["attribution"]["method"] == "session_id" for x in mine)
    assert all(real.Q1_SESSION not in (x["summary"].get("url") or "") for x in theirs)
    assert len({x["raw"]["insertId"] for x in mine} & {x["raw"]["insertId"] for x in theirs}) == 0


@pytest.mark.asyncio
async def test_replay_mode_marks_every_message_and_refuses_unknown(tmp_path):
    rec = tmp_path / "turn.jsonl"
    rec.write_text("\n".join(json.dumps(m) for m in [
        {"kind": "recording_meta", "provenance": "recorded"},
        {"kind": "turn_start", "t_ms": 0, "message": "hi"},
        {"kind": "adk_event", "t_ms": 5, "raw": s.SAMPLE_ADK_ANSWER},
        {"kind": "turn_end", "t_ms": 10, "latency_ms": 5, "trace_urls": []},
    ]))
    app = create_app(Settings(dev_replay=str(rec)), engine=FakeEngine(), logs=FakeLogs([]))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        cfg = (await c.get("/api/config")).json()
        assert cfg["replay"] is True and cfg["replay_provenance"] == "recorded" and cfg["replay_messages"] == ["hi"]
        sess = (await c.post("/api/session", json={})).json()
        assert sess["session_id"].startswith("replay-")
    msgs = await post_chat(app, "hi")
    assert [m["kind"] for m in msgs] == ["turn_start", "adk_event", "turn_end"]
    assert all(m["replay"] is True and m["provenance"] == "recorded" for m in msgs)
    refused = await post_chat(app, "something never recorded")
    assert [m["kind"] for m in refused] == ["replay_unmatched"] and refused[0]["available"] == ["hi"]


@pytest.mark.asyncio
async def test_constructed_sample_is_labelled():
    app = create_app(Settings(dev_replay=str(real.FIX / "SAMPLE_replay_constructed.jsonl")), engine=FakeEngine(), logs=FakeLogs([]))
    cfg = app.state.platform["replayer"]
    assert cfg.provenance == "constructed sample"
    assert "What is the contact information of the top at-risk customer? We want to reach out today." in cfg.messages


@pytest.mark.asyncio
async def test_config_and_session_live():
    app = create_app(FAST, engine=FakeEngine(), logs=FakeLogs([]))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        cfg = (await c.get("/api/config")).json()
        assert cfg["replay"] is False and cfg["gateways"] == {"inbound": "agent-demo-ingress-gw", "outbound": "agent-demo-egress-gw"}
        assert cfg["links"]["gateways"] == "https://console.cloud.google.com/agent-platform/gateways?project=your-project-id"
        assert cfg["region"] == "us-central1"
        sess = (await c.post("/api/session", json={"user_id": "presenter"})).json()
        assert sess == {"session_id": "sess-1", "user_id": "presenter"}
    assert "sess-1" in app.state.platform["sessions"]


@pytest.mark.asyncio
async def test_live_mode_sends_a_traceparent_with_streamquery():
    engine = FakeEngine(real.sse("q4_deny"))
    msgs = await post_chat(create_app(FAST, engine=engine, logs=FakeLogs([])), "hi", "sess-tp")
    sent = msgs[0]["trace_id_sent"]
    assert len(sent) == 32
    assert engine.headers[0]["traceparent"].startswith(f"00-{sent}-")
