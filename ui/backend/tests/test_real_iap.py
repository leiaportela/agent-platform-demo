"""IAP authorization decisions (outbound gateway egress) on REAL records.

fixtures/real_iap_decisions.logs.json   copy of demo/evidence/raw-run/iap-decisions.logs.json (2026-11-26, 10x eval)
fixtures/real_iap_window_2213.logs.json every IAP entry the UI's filter returned for 22:13:00Z..22:14:00Z
                                        (one read-only entries.list, 2026-11-26 22:25Z)
fixtures/real_iap_runs.json             two runs of that eval (s3_churn_risk#9, s4_pii_contact#0) with their ADK
                                        events as received from streamQuery

Nothing here is edited; tests only re-time copies (real.shift) when they need records "now".
"""
import asyncio
import json
import time

import httpx
import pytest

import attribution
import real
import records
from attribution import HOLD
from main import create_app
from poller import LogPoller, TurnCtx
from settings import Settings

ENGINE = "projects/123456789012/locations/us-central1/reasoningEngines/1234567890123456789"
S = Settings(engine=ENGINE)
PRINCIPAL = ("principal://agents.global.org-111111111111.system.id.goog/resources/aiplatform/projects/123456789012/"
             "locations/us-central1/reasoningEngines/1234567890123456789")
IN, OUT = "agent-demo-ingress-gw", "agent-demo-egress-gw"


def samples():
    return json.loads((real.FIX / "real_iap_decisions.logs.json").read_text())


def window():
    return json.loads((real.FIX / "real_iap_window_2213.logs.json").read_text())


def run(prompt_id):
    return next(r for r in json.loads((real.FIX / "real_iap_runs.json").read_text())["runs"] if r["prompt_id"] == prompt_id)


def turn_from_run(r, start=None, cap=None):
    """A TurnCtx as turn.py builds it: window from t0 - lookback, tool calls from the run's real ADK events."""
    t = TurnCtx(id=r["session_id"], session_id=r["session_id"], message=r["prompt"],
                start=start if start is not None else r["t0"] - S.lookback_s, cap=cap, t0=r["t0"])
    for e in r["events"]:
        s = records.summarize_adk_event(e)
        t.tool_calls.extend((c["name"], float(s["timestamp"])) for c in s["calls"])
    return t


def drain(t):
    out = []
    while not t.queue.empty():
        m = t.queue.get_nowait()
        if m is not None:
            out.append(m)
    return out


# ---- settings and filter ---------------------------------------------------------------------------------


def test_agent_principal_is_derived_from_engine():
    assert S.agent_principal == PRINCIPAL
    # the principal recorded by IAP on every real record is exactly the derived one
    assert {e["protoPayload"]["authenticationInfo"]["principalSubject"] for v in samples().values() for e in v} == {PRINCIPAL}
    assert Settings().agent_principal is None
    assert Settings(engine="projects/your-project-id/locations/us-central1/reasoningEngines/1").agent_principal is None
    assert Settings(engine=ENGINE, agent_org="42").agent_principal.startswith("principal://agents.global.org-42.system.id.goog/")


def test_filter_has_one_iap_clause_with_the_verified_fields():
    f = records.log_filter("your-project-id", "2026-11-26T22:13:00Z", "2026-11-26T22:14:00Z", (IN, OUT), PRINCIPAL)
    assert f == (
        'timestamp>="2026-11-26T22:13:00Z" AND timestamp<"2026-11-26T22:14:00Z" AND '
        '((resource.type="networkservices.googleapis.com/Gateway" AND resource.labels.gateway_name=("agent-demo-ingress-gw" OR "agent-demo-egress-gw"))'
        ' OR (logName="projects/your-project-id/logs/modelarmor.googleapis.com%2Fsanitize_operations"'
        ' AND labels."modelarmor.googleapis.com/client_name"="AGENT_GATEWAY")'
        ' OR (logName="projects/your-project-id/logs/cloudaudit.googleapis.com%2Fdata_access"'
        ' AND protoPayload.serviceName="iap.googleapis.com" AND protoPayload.methodName="GatekeeperAuthorizer.AuthorizeUser"'
        ' AND labels."iap.googleapis.com/audited_resource_type"="agentregistry.googleapis.com/McpServer"'
        f' AND protoPayload.authenticationInfo.principalSubject="{PRINCIPAL}"))'
    )
    # every real IAP record satisfies the clause's field values
    for e in window():
        p = e["protoPayload"]
        assert e["logName"] == "projects/your-project-id/logs/cloudaudit.googleapis.com%2Fdata_access"
        assert (p["serviceName"], p["methodName"]) == ("iap.googleapis.com", "GatekeeperAuthorizer.AuthorizeUser")
        assert e["labels"]["iap.googleapis.com/audited_resource_type"] == "agentregistry.googleapis.com/McpServer"
        assert p["authenticationInfo"]["principalSubject"] == PRINCIPAL
    # no principal (ENGINE unset or named by project id): no IAP clause
    assert "cloudaudit" not in records.log_filter("p", "2026-11-26T22:13:00Z")


@pytest.mark.asyncio
async def test_poller_sends_the_iap_clause_in_its_single_query():
    filters = []

    class Logs:
        async def list_entries(self, f):
            filters.append(f)
            return []

    poller = LogPoller(Logs(), S)
    poller.turns.append(TurnCtx(id="t", session_id="s", message="m", start=1795731190.0))
    await poller.tick()
    assert len(filters) == 1 and f'principalSubject="{PRINCIPAL}"' in filters[0]
    assert "modelarmor" in filters[0] and "networkservices.googleapis.com/Gateway" in filters[0]


# ---- field extraction ------------------------------------------------------------------------------------


def test_real_deny_decision_fields():
    for e in samples()["deny_get_customer_contact"]:
        assert records.classify(e, IN, OUT) == "iap_decision"
        s = records.summarize_iap(e)
        assert s["granted"] is False and s["granted_in_record"] is True and s["denied"] is True
        assert s["status_code"] == 7 and s["status_message"] == "Permission Denied."
        assert s["mcp_method"] == "tools/call" and s["tool"] == "get_customer_contact"
        assert s["base_protocol_method"] is False
        assert s["permission"] == "iap.webServiceVersions.egressViaIAP"
        assert s["policy_version"] == "POLICY_VERSION_V1" and s["dry_run"] is False
        assert s["principal"] == PRINCIPAL
        assert s["resource_type"] == "agentregistry.googleapis.com/McpServer"
        assert s["resource_name"] == "projects/123456789012/locations/us-central1/mcpServers/agentregistry-00000000-0000-0000-0000-000000000002"
        assert s["url"] == "https://agent-demo-mcp-123456789012.us-central1.run.app/mcp"
        assert s["trace_id"] is None and s["hidden"] is False and s["severity"] == "ERROR"
    first = samples()["deny_get_customer_contact"][0]
    assert records.console_url(first, "your-project-id") == (
        "https://console.cloud.google.com/logs/query;query=insertId%3D%227j45pje7gyqu%22;"
        "startTime=2026-11-26T22:03:15Z;endTime=2026-11-26T22:23:15Z?project=your-project-id")


def test_real_allow_decision_fields():
    for e in samples()["allow_tool"]:
        s = records.summarize_iap(e)
        assert records.classify(e, IN, OUT) == "iap_decision"
        assert s["granted"] is True and s["denied"] is False and s["granted_in_record"]
        assert s["status_code"] is None and s["status_message"] is None  # "status": {} on the real allow records
        assert s["tool"] == "list_top_churn_risk" and s["mcp_method"] == "tools/call" and not s["hidden"]


def test_real_google_api_endpoint_decisions_are_excluded():
    for e in samples()["allow_google_api_endpoint"]:
        assert e["labels"]["iap.googleapis.com/audited_resource_type"] == "agentregistry.googleapis.com/Endpoint"
        assert records.classify(e, IN, OUT) is None


def test_missing_granted_is_false_only_with_status_7():
    deny = samples()["deny_get_customer_contact"][0]
    omitted = json.loads(json.dumps(deny))
    del omitted["protoPayload"]["authorizationInfo"][0]["granted"]  # proto JSON may omit a false boolean
    s = records.summarize_iap(omitted)
    assert s["granted"] is False and s["granted_in_record"] is False and s["denied"] is True
    allow = json.loads(json.dumps(samples()["allow_tool"][0]))
    del allow["protoPayload"]["authorizationInfo"][0]["granted"]
    s = records.summarize_iap(allow)
    assert s["granted"] is None and s["denied"] is False  # shown as "granted not in record"
    for garbage in ({}, {"protoPayload": "x"}, {"protoPayload": {"authorizationInfo": "x", "status": 3, "metadata": []}}):
        out = records.summarize_iap(garbage)
        assert out["granted"] is None and out["tool"] is None and out["hidden"] is True


def test_real_window_read_counts():
    """The one real read of 22:13:00Z..22:14:00Z: 10 deny decisions (one per s4 run of the 10x eval), granted
    tools/call decisions, and tools/list handshake decisions (no tool name; hidden, never attributed)."""
    got = {}
    for e in window():
        assert records.classify(e, IN, OUT) == "iap_decision"
        s = records.summarize_iap(e)
        key = (s["mcp_method"], s["tool"], s["granted"])
        got[key] = got.get(key, 0) + 1
    assert got == {
        ("tools/call", "get_customer_contact", False): 10,
        ("tools/call", "list_top_churn_risk", True): 13,
        ("tools/call", "get_latest_run", True): 3,
        ("tools/call", "get_churn_drivers", True): 1,
        ("tools/list", None, True): 30,
    }
    for e in window():
        s = records.summarize_iap(e)
        assert s["hidden"] == (s["mcp_method"] != "tools/call")
        if s["denied"]:
            assert s["status_code"] == 7 and s["granted_in_record"]
    handshake = next(e for e in window() if records.summarize_iap(e)["mcp_method"] == "tools/list")
    assert handshake["protoPayload"]["metadata"]["mcp_attributes"]["base_protocol_method"] is False  # as recorded


# ---- attribution -----------------------------------------------------------------------------------------


def test_real_s4_turn_gets_its_deny_decisions():
    """s4_pii_contact#0 alone (as one presenter turn): the deny decisions in its window are attributed by
    principal + tool name + time; the allow decisions before its t0 are not."""
    t = turn_from_run(run("s4_pii_contact"))
    assert [n for n, _ in t.tool_calls] == ["list_top_churn_risk", "get_customer_contact"]
    poller = LogPoller(None, S)
    entries = [e for v in samples().values() for e in v]
    poller._dispatch(entries, [t])
    msgs = drain(t)
    assert sorted(m["raw"]["insertId"] for m in msgs) == sorted(e["insertId"] for e in samples()["deny_get_customer_contact"])
    for m in msgs:
        assert m["source"] == "iap_decision" and m["summary"]["denied"] and not m["hidden"]
        assert m["attribution"]["method"] == "principal_tool_time"
        assert "get_customer_contact is a function_call of this turn" in m["attribution"]["detail"]
        assert m["console_url"].startswith("https://console.cloud.google.com/logs/query;query=insertId%3D%22")
        assert m["trace_url"] is None
    assert t.counts == {"principal_tool_time": 3}


def test_real_turn_without_that_tool_call_gets_no_deny():
    """s3_churn_risk#9 called list_top_churn_risk (granted decisions match) but never get_customer_contact:
    the deny decisions in its window are not attributed (held while it listens, dropped on its last poll)."""
    r = run("s3_churn_risk")
    t = turn_from_run(r)
    assert "get_customer_contact" not in [n for n, _ in t.tool_calls]
    poller = LogPoller(None, S)
    entries = samples()["allow_tool"] + samples()["deny_get_customer_contact"]
    poller._dispatch(entries, [t])
    msgs = drain(t)
    assert sorted(m["raw"]["insertId"] for m in msgs) == sorted(e["insertId"] for e in samples()["allow_tool"])
    assert all(m["summary"]["granted"] is True and m["attribution"]["method"] == "principal_tool_time" for m in msgs)
    deny = records.summarize_iap(samples()["deny_get_customer_contact"][0])
    ts = attribution.parse_ts(deny["timestamp"])
    assert attribution.attribute("iap_decision", deny, ts, [t], PRINCIPAL) == HOLD
    t.finalizing = True
    assert attribution.attribute("iap_decision", deny, ts, [t], PRINCIPAL) is None


def test_real_two_consecutive_turns_split_by_window_and_tool():
    """s3#9 then s4#0, as two presenter turns (the newer caps the older window): the list_top_churn_risk grants
    go to s3, the get_customer_contact denies to s4. Endpoint decisions are excluded."""
    s3, s4 = run("s3_churn_risk"), run("s4_pii_contact")
    b = turn_from_run(s4)
    a = turn_from_run(s3, cap=b.start)
    poller = LogPoller(None, S)
    poller._dispatch([e for v in samples().values() for e in v], [a, b])
    ma, mb = drain(a), drain(b)
    assert {m["summary"]["tool"] for m in ma} == {"list_top_churn_risk"} and len(ma) == 2
    assert {m["summary"]["tool"] for m in mb} == {"get_customer_contact"} and len(mb) == 3
    assert all(m["summary"]["granted"] for m in ma) and all(m["summary"]["denied"] for m in mb)


def test_iap_attribution_requires_principal_window_and_tools_call():
    deny = records.summarize_iap(samples()["deny_get_customer_contact"][0])
    ts = attribution.parse_ts(deny["timestamp"])
    t = attribution.TurnRef(id="t", session_id="s", message="m", start=ts - 5, tool_calls=[("get_customer_contact", ts - 1)])
    assert attribution.attribute("iap_decision", deny, ts, [t], PRINCIPAL).method == "principal_tool_time"
    assert attribution.attribute("iap_decision", deny, ts, [t], None) is None  # principal unknown
    assert attribution.attribute("iap_decision", {**deny, "principal": "principal://other"}, ts, [t], PRINCIPAL) is None
    assert attribution.attribute("iap_decision", deny, ts - 10, [t], PRINCIPAL) is None  # before the window
    assert attribution.attribute("iap_decision", deny, None, [t], PRINCIPAL) is None
    handshake = records.summarize_iap(next(e for e in window() if records.summarize_iap(e)["mcp_method"] == "tools/list"))
    assert attribution.attribute("iap_decision", handshake, ts, [t], PRINCIPAL) is None


def test_several_candidate_turns_pick_the_closest_function_call():
    """Windows never overlap in the UI, so this only matters for TurnRefs built without caps; the rule picks
    the turn whose call of that tool is closest to the decision."""
    deny = records.summarize_iap(samples()["deny_get_customer_contact"][0])
    ts = attribution.parse_ts(deny["timestamp"])
    far = attribution.TurnRef(id="far", session_id="1", message="m", start=ts - 30, tool_calls=[("get_customer_contact", ts - 20)])
    near = attribution.TurnRef(id="near", session_id="2", message="m", start=ts - 10, tool_calls=[("get_customer_contact", ts - 1.5)])
    assert attribution.attribute("iap_decision", deny, ts, [far, near], PRINCIPAL).turn is near


# ---- end to end through /api/chat --------------------------------------------------------------------------


class FakeEngine:
    def __init__(self, events):
        self.events = events

    async def stream_query(self, user_id, session_id, message, headers=None):
        for e in self.events:
            await asyncio.sleep(0.01)
            yield e


class FakeLogs:
    """Returns the real IAP samples re-timed so the s4 run's t0 is the first poll."""

    def __init__(self, entries, real_t0):
        self.entries, self.real_t0, self.offset, self.filters = entries, real_t0, None, []

    async def list_entries(self, filter_):
        self.filters.append(filter_)
        if self.offset is None:
            self.offset = time.time() - self.real_t0
        return real.shift(self.entries, self.offset)


@pytest.mark.asyncio
async def test_real_s4_turn_end_to_end_collects_tool_calls_and_emits_iap_decisions():
    r = run("s4_pii_contact")
    entries = [e for v in samples().values() for e in v]
    logs = FakeLogs(entries, r["t0"] - 1.0)
    settings = Settings(engine=ENGINE, poll_interval_s=0.05, linger_s=0.4, lookback_s=2.0, max_backoff_s=0.4)
    app = create_app(settings, engine=FakeEngine(r["events"]), logs=logs)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as c:
        cfg = (await c.get("/api/config")).json()
        assert cfg["agent_principal"] == PRINCIPAL
        resp = await c.post("/api/chat", json={"session_id": r["session_id"], "user_id": "u", "message": r["prompt"]})
    msgs = [json.loads(f[6:]) for f in resp.text.split("\n\n") if f.startswith("data: ")]
    iap = [m for m in msgs if m["kind"] == "log" and m["source"] == "iap_decision"]
    # re-timed so the s4 run's t0 is 1 s before the first poll: the deny decisions (5.6-13.8 s after t0) are
    # in the window, the allow decisions (before t0) are not, the Endpoint ones are excluded
    assert sorted(m["raw"]["insertId"] for m in iap) == sorted(e["insertId"] for e in samples()["deny_get_customer_contact"])
    assert all(m["attribution"]["method"] == "principal_tool_time" and m["summary"]["denied"] for m in iap)
    assert msgs[-1]["kind"] == "turn_end" and msgs[-1]["attributed"].get("principal_tool_time") == 3
    assert all(f'principalSubject="{PRINCIPAL}"' in f for f in logs.filters)


def test_frontend_fixture_matches_backend_output():
    """frontend/src/fixtures/real_iap_messages.json (used by the frontend tests) is what this backend emits for
    the real records: same records, summaries, attribution and links (t_ms aside)."""
    fixture = json.loads((real.FIX.parents[2] / "frontend" / "src" / "fixtures" / "real_iap_messages.json").read_text())
    s3, s4 = run("s3_churn_risk"), run("s4_pii_contact")
    b = turn_from_run(s4)
    a = turn_from_run(s3, cap=b.start)
    LogPoller(None, S)._dispatch([e for v in samples().values() for e in v], [a, b])
    strip = lambda msgs: [{k: v for k, v in m.items() if k != "t_ms"} for m in msgs]  # noqa: E731
    assert strip(fixture["turns"]["s3_churn_risk"]) == strip(drain(a))
    assert strip(fixture["turns"]["s4_pii_contact"]) == strip(drain(b))


@pytest.mark.asyncio
async def test_403_on_the_iap_clause_drops_it_and_keeps_polling():
    """The IAP decisions are in a private log (Data Access audit). If the runtime account cannot read it and
    Cloud Logging refuses the query, the next tick polls gateway rows and Model Armor records without it."""
    from platform_clients import HttpError

    filters = []
    refusal = HttpError(403, {"error": {"code": 403, "message": "The caller does not have permission", "status": "PERMISSION_DENIED"}})

    class Logs:
        async def list_entries(self, f):
            filters.append(f)
            if "cloudaudit" in f:
                raise refusal
            return []

    poller = LogPoller(Logs(), S)
    t = TurnCtx(id="t", session_id="s", message="m", start=1795731190.0)
    poller.turns.append(t)
    assert await poller.tick() == S.poll_interval_s  # no backoff
    await poller.tick()
    assert "cloudaudit" in filters[0] and "cloudaudit" not in filters[1] and not poller.include_iap
    assert "modelarmor" in filters[1]
    (err,) = drain(t)
    assert err["kind"] == "log_poll_error" and err["status"] == 403
    assert "polling continues without IAP decisions" in err["message"] and "privateLogViewer" in err["message"]
    # a 403 without the IAP clause is reported as before
    poller.include_iap = False
    refusal_no_iap = HttpError(403, {"error": {"message": "nope"}})

    class Refusing:
        async def list_entries(self, f):
            raise refusal_no_iap

    p2 = LogPoller(Refusing(), Settings())
    t2 = TurnCtx(id="t2", session_id="s", message="m", start=0.0)
    p2.turns.append(t2)
    await p2.tick()
    assert drain(t2)[0]["message"] == "nope"
