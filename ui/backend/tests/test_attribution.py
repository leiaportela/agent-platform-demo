"""Per-turn attribution on REAL records (fixtures/real_*), through the poller's dispatch path."""
import asyncio

import pytest

import attribution
import real
import records
from poller import LogPoller, TurnCtx
from settings import Settings

S = Settings(engine="projects/123456789012/locations/us-central1/reasoningEngines/1234567890123456789")


def turn(session, message, start, cap=None, answers=()):
    return TurnCtx(id=session, session_id=session, message=message, start=start, cap=cap,
                   answer_texts=list(answers))


def drain(t):
    out = []
    while not t.queue.empty():
        m = t.queue.get_nowait()
        if m is not None:
            out.append(m)
    return out


def dispatch(entries, turns):
    poller = LogPoller(logs=None, settings=S)
    poller._dispatch(entries, turns)
    return poller


def final_text(label):
    return [records.summarize_adk_event(e)["text"] for e in real.sse(label) if records.summarize_adk_event(e)["is_final_text"]]


def ts(value):
    return attribution.parse_ts(value)


@pytest.mark.asyncio
async def test_real_q1_turn_attribution():
    m = real.meta("q1_dryrun")
    t = turn(real.Q1_SESSION, m["prompt"], ts(m["t0_utc"]) - 0.5, answers=final_text("q1_dryrun"))
    dispatch(real.logs("q1_dryrun"), [t])
    msgs = drain(t)
    assert len(msgs) == 23  # every record of the window lands in this turn exactly once
    by_method = {}
    for x in msgs:
        by_method.setdefault(x["attribution"]["method"], []).append(x)
    armor = {x["summary"]["operation"] + ":" + x["attribution"]["method"] for x in msgs if x["source"] == "model_armor"}
    assert armor == {"prompt:prompt_text", "response:session_text", "response:answer_text"}
    session_rows = by_method["session_id"]
    assert len(session_rows) == 7 and all(real.Q1_SESSION in x["summary"]["url"] for x in session_rows)
    # No MCP row can be tied to the turn by trace (session rows carry no trace): they are labelled "time".
    assert "trace_id" not in by_method
    mcp = [x for x in msgs if x["summary"].get("mcp_method")]
    assert mcp and all(x["attribution"]["method"] == "time" for x in mcp)
    hidden = [x for x in msgs if x["hidden"]]
    assert len(hidden) == 3 and all(x["raw"]["httpRequest"]["requestMethod"] == "CONNECT" for x in hidden)
    assert all(x["source"] in ("outbound_gateway", "model_armor") for x in msgs)
    assert t.prompt_correlation_ids == {"14683294536216392746"}


@pytest.mark.asyncio
async def test_real_q4_deny_row_is_shown_and_armor_matched():
    m = real.meta("q4_deny")
    t = turn(real.Q4_SESSION, m["prompt"], ts(m["t0_utc"]) - 0.5, answers=final_text("q4_deny"))
    dispatch(real.logs("q4_deny"), [t])
    msgs = drain(t)
    deny = [x for x in msgs if x["summary"].get("denied")]
    assert len(deny) == 1 and not deny[0]["hidden"] and deny[0]["summary"]["status"] == 403
    assert deny[0]["summary"]["authz_result"] == "DENIED" and deny[0]["attribution"]["method"] == "time"
    armor = sorted((x["summary"]["operation"], x["attribution"]["method"]) for x in msgs if x["source"] == "model_armor")
    assert armor == [("prompt", "prompt_text"), ("response", "answer_text"), ("response", "session_text")]


@pytest.mark.asyncio
async def test_real_response_screening_matches_by_correlation_id_without_answer_text():
    """If the answer never reached the UI (e.g. the response was blocked), the response screening still
    matches through the client_correlation_id it shares with the turn's prompt screening."""
    m = real.meta("q4_deny")
    t = turn(real.Q4_SESSION, m["prompt"], ts(m["t0_utc"]) - 0.5, answers=[])
    dispatch([e for e in real.logs("q4_deny") if real.is_armor(e)], [t])
    got = sorted((x["summary"]["operation"], x["attribution"]["method"]) for x in drain(t))
    assert got == [("prompt", "prompt_text"), ("response", "correlation_id"), ("response", "session_text")]


@pytest.mark.asyncio
async def test_real_two_consecutive_turns_never_swap_records():
    """q4q5: two sessions 3 s apart (old q4 wording, then the injection). Each record goes to its turn."""
    entries = real.logs("q4q5")
    first_start = ts("2026-11-26T21:04:43.5Z")
    second_start = ts(real.meta("q5_inject")["t0_utc"]) - 0.5
    a = turn(real.Q4_OLD_SESSION, real.Q4_OLD_PROMPT, first_start, cap=second_start)
    b = turn(real.Q5_SESSION, real.meta("q5_inject")["prompt"], second_start)
    dispatch(entries, [a, b])
    ma, mb = drain(a), drain(b)
    assert len(ma) + len(mb) == len(entries)
    armor_a = {(x["summary"]["operation"], x["attribution"]["method"], x["summary"]["match_state"]) for x in ma if x["source"] == "model_armor"}
    armor_b = {(x["summary"]["operation"], x["attribution"]["method"], x["summary"]["match_state"]) for x in mb if x["source"] == "model_armor"}
    assert armor_a == {("response", "session_text", "NO_MATCH_FOUND"), ("prompt", "prompt_text", "MATCH_FOUND")}
    assert armor_b == {("response", "session_text", "NO_MATCH_FOUND"), ("prompt", "prompt_text", "MATCH_FOUND")}
    inj = next(x for x in mb if x["summary"].get("operation") == "prompt")
    assert inj["summary"]["matched_filters"] == ["pi_and_jailbreak"]
    assert any(real.Q4_OLD_SESSION in (x["summary"].get("url") or "") and x["attribution"]["method"] == "session_id" for x in ma)


@pytest.mark.asyncio
async def test_real_unrelated_armor_record_is_held_then_unmatched():
    """An AGENT_GATEWAY record in the window whose screened text matches nothing of the turn: held while
    the turn listens, then handed over as 'unmatched' (collapsed in the UI) on its last poll."""
    rec = [e for e in real.logs("q1_dryrun") if real.is_armor(e)][0]
    t = turn("someone-else", "another question", ts(rec["timestamp"]) - 1)
    poller = dispatch([rec], [t])
    assert drain(t) == [] and not poller.seen
    t.finalizing = True
    poller._dispatch([rec], [t])
    (msg,) = drain(t)
    assert msg["attribution"]["method"] == "unmatched" and t.counts == {"unmatched": 1}


@pytest.mark.asyncio
async def test_real_record_outside_every_window_is_not_attributed():
    rec = gateway = next(e for e in real.logs("q4_deny") if e.get("httpRequest", {}).get("status") == 403)
    t = turn("x", "y", ts(rec["timestamp"]) + 10)
    poller = dispatch([gateway], [t])
    assert drain(t) == [] and not poller.seen


@pytest.mark.asyncio
async def test_trace_id_sent_with_the_request_matches_exactly():
    rec = next(e for e in real.logs("q4_deny") if e.get("httpRequest", {}).get("status") == 403)
    t = turn("x", "y", ts(rec["timestamp"]) + 100)  # far outside the window
    t.sent_trace = rec["trace"]
    dispatch([rec], [t])
    (msg,) = drain(t)
    assert msg["attribution"]["method"] == "sent_trace" and msg["summary"]["denied"]


@pytest.mark.asyncio
async def test_trace_of_a_session_row_matches_other_rows():
    """The generic chain (session row -> its trace -> rows with that trace). Real session rows carry no
    trace, so this only fires if a later platform version adds one; checked with a copied real row."""
    deny = next(e for e in real.logs("q4_deny") if e.get("httpRequest", {}).get("status") == 403)
    session_row = dict(next(e for e in real.logs("q4_deny") if real.Q4_SESSION in e.get("httpRequest", {}).get("requestUrl", "")))
    session_row["trace"] = deny["trace"]
    t = turn(real.Q4_SESSION, "y", ts(deny["timestamp"]) + 100)
    dispatch([session_row], [t])
    dispatch([deny], [t])
    got = [m["attribution"]["method"] for m in drain(t)]
    assert got == ["session_id", "trace_id"]


@pytest.mark.asyncio
async def test_armor_record_matches_by_sent_trace_when_text_does_not():
    rec = [e for e in real.logs("q1_dryrun") if real.is_armor(e)][2]  # the answer screening
    t = turn("other", "other question", ts(rec["timestamp"]) - 1)
    t.sent_trace = rec["trace"]
    dispatch([rec], [t])
    (msg,) = drain(t)
    assert msg["attribution"]["method"] == "sent_trace"


@pytest.mark.asyncio
async def test_windows_never_overlap():
    poller = LogPoller(logs=None, settings=S)
    a, b = turn("a", "q", 100.0), turn("b", "q", 110.0)
    poller.turns.append(a)
    poller.cap_open_windows(b.start)
    poller.turns.append(b)
    assert a.cap == 110.0 and b.cap is None
    assert attribution.time_owner(109.9, [a, b]) is a and attribution.time_owner(110.0, [a, b]) is b
    await asyncio.sleep(0)


def test_foreign_session_or_trace_rows_are_not_attributed_by_time():
    import attribution as a
    turn = a.TurnRef(id="t1", session_id="111", message="m", start=0.0, sent_trace="aaaa")
    assert a.attribute("outbound_gateway", {"session_ids": ["999"]}, 5.0, [turn]) is None
    assert a.attribute("outbound_gateway", {"trace_id": "ffff"}, 5.0, [turn]) is None
    d = a.attribute("outbound_gateway", {}, 5.0, [turn])
    assert d.method == "time"
    legacy = a.TurnRef(id="t2", session_id="111", message="m", start=0.0)  # no traceparent sent
    assert a.attribute("outbound_gateway", {"trace_id": "ffff"}, 5.0, [legacy]).method == "time"
