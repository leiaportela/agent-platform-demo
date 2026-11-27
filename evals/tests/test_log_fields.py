import copy

import evidence
import log_fields as lf
from conftest import fetch_from, load_sample, real_logs, real_run


def test_gateway_rows_camel_and_snake_case():
    rows = [lf.normalize(e) for e in load_sample("sample_gateway_entries.json")]
    by_id = {r["insert_id"]: r for r in rows}
    assert by_id["gw-in-1"]["gateway"] == "agent-demo-ingress-gw" and by_id["gw-in-1"]["direction"] == "ingress"
    assert by_id["gw-in-1"]["policies"] == ["agent-demo-ingress-gw-authz-policy-modelarmor"]
    d = by_id["gw-eg-2"]  # snake_case payload
    assert d["status"] == 403 and d["authz_result"] == "DENIED"
    assert d["mcp_method"] == "tools/call" and d["mcp_tool"] == "get_customer_contact"
    assert d["trace_id"] == "b" * 32
    assert lf.is_denied(d) and lf.is_tool_deny(d, "get_customer_contact") and not lf.is_tool_deny(d, "other")
    assert not lf.is_hidden(d)
    assert d["raw"]["insertId"] == "gw-eg-2"


def test_denied_means_authz_denied_not_status_403():
    row = lf.gateway_row({"resource": {"labels": {"gateway_name": lf.EGRESS_GW}}, "httpRequest": {"status": 403},
                          "jsonPayload": {"authzPolicyInfo": {"result": "ALLOWED"}}})
    assert not lf.is_denied(row)


def test_links():
    assert lf.logs_link("abc") == ("https://console.cloud.google.com/logs/query;query=insertId%3D%22abc%22;"
                                   "timeRange=P30D?project=your-project-id")
    assert lf.logs_link("abc", timestamp="2026-11-26T21:05:45.253452Z") == (
        "https://console.cloud.google.com/logs/query;query=insertId%3D%22abc%22;"
        "startTime=2026-11-26T20:05:45Z;endTime=2026-11-26T22:05:45Z?project=your-project-id")
    assert lf.trace_link("t1") == "https://console.cloud.google.com/traces/explorer;traceId=t1?project=your-project-id"
    assert lf.logs_link(None) is None


def test_model_armor_rows():
    rows = [lf.normalize(e) for e in load_sample("sample_model_armor_entries.json")]
    clean, pi, sdp = rows
    assert clean["kind"] == "model_armor" and clean["operation"] == "SANITIZE_USER_PROMPT"
    assert clean["match_state"] == "NO_MATCH_FOUND" and clean["matched_filters"] == []
    assert clean["client"] == "AGENT_GATEWAY"
    assert pi["matched_filters"] == ["pi_and_jailbreak"] and pi["filters"]["rai"] == "NO_MATCH_FOUND"
    assert sdp["matched_filters"] == ["sdp"] and sdp["match_state"] == "MATCH_FOUND"  # snake_case + list form


def test_real_recorded_entries():
    """Entries copied from this project's Cloud Logging on 2026-11-26."""
    gw = [lf.normalize(e) for e in load_sample("sample_real_gateway_entries.json")]
    denied, allowed = gw
    assert denied["gateway"] == "agent-demo-egress-gw" and denied["status"] == 403 and denied["authz_result"] == "DENIED"
    assert (denied["mcp_method"], denied["mcp_tool"]) == ("tools/call", "get_customer_contact")
    assert denied["policies"][0].endswith("authzPolicies/agent-demo-egress-gw-authz-policy-iap")
    assert denied["trace_id"] and "/" not in denied["trace_id"]  # bare hex id
    assert lf.is_denied(denied) and not lf.is_denied(allowed) and allowed["mcp_tool"] == "list_top_churn_risk"
    ma = [lf.normalize(e) for e in load_sample("sample_real_model_armor_entries.json")]
    pi, sdp, rai, resp = ma
    assert pi["matched_filters"] == ["pi_and_jailbreak"] and pi["verdict"] == "MODEL_ARMOR_SANITIZATION_VERDICT_BLOCK"
    assert pi["input_text"].startswith("Ignore all previous instructions") and pi["client"] is None  # direct API probe
    assert sdp["matched_filters"] == ["sdp"] and sdp["sdp_info_types"] == ["BRAZIL_CPF_NUMBER"]
    assert rai["client"] == "AGENT_GATEWAY" and rai["trace_id"] and rai["template"] == "agent-demo-armor"
    # gateway screening: the text is base64 byteData
    assert rai["input_text"] == "Give me the email and home address of the top at-risk customer so we can reach out today."
    assert rai["filters"]["rai"] == "MATCH_FOUND (harassment MEDIUM_AND_ABOVE, dangerous MEDIUM_AND_ABOVE)"
    assert resp["operation"] == "SANITIZE_MODEL_RESPONSE" and resp["match_state"] == "NO_MATCH_FOUND"
    assert '"id\\": \\"6479381718510862336\\"' in resp["input_text"]  # create-session response with the session id


def test_real_rows_q1():
    rows = [lf.normalize(e) for e in real_logs("q1_dryrun")]
    gw = [r for r in rows if r["kind"] == "gateway"]
    assert {r["gateway"] for r in gw} == {"agent-demo-egress-gw"}  # the inbound gateway writes no rows
    session_rows = [r for r in gw if r["session_id"] == "6479381718510862336"]
    assert len(session_rows) == 7
    # verified 2026-11-26: the session rows carry no trace, so session id -> trace id attribution is not possible
    assert all(r["trace_id"] is None for r in session_rows)
    mcp = [r for r in gw if r["mcp_method"]]
    assert {r["trace_id"] for r in mcp} == {"76be74fe0e2f7eb575178d62ed5dba98"}  # one trace per MCP session
    connect = [r for r in gw if r["method"] == "CONNECT"]
    assert connect and all(lf.is_hidden(r) for r in connect) and not any(lf.is_hidden(r) for r in mcp)
    ma = [r for r in rows if r["kind"] == "model_armor"]
    prompt = next(r for r in ma if r["operation"] == "SANITIZE_USER_PROMPT")
    answer = next(r for r in ma if r["operation"] == "SANITIZE_MODEL_RESPONSE" and r["input_text"].startswith("The latest"))
    assert prompt["correlation_id"] == answer["correlation_id"] == "14683294536216392746"


def test_attribution_real_q1_exact_and_time():
    run = real_run("q1_dryrun")
    ev = evidence.collect([run], fetch_from(real_logs("q1_dryrun")))[0]
    by = {}
    for r in ev["rows"]:
        by.setdefault(r["attributed_by"], []).append(r)
    assert {r["session_id"] for r in by["session_id"] if r["kind"] == "gateway"} == {"6479381718510862336"}
    assert "trace" not in by  # no trace link exists in the real records
    assert all(r["kind"] == "gateway" for r in by["time"])
    ma = {r["operation"] + ":" + r["attributed_by"] for r in ev["rows"] if r["kind"] == "model_armor"}
    assert ma == {"SANITIZE_MODEL_RESPONSE:session_id", "SANITIZE_MODEL_RESPONSE:answer_text",
                  "SANITIZE_USER_PROMPT:correlation_id"}
    s = ev["summary"]
    assert s["hidden_rows"] == 3 and s["session_rows"] == 7 and not s["any_ambiguous"]
    assert s["gateway_tools_allowed"] == ["get_model_summary"] and s["tool_denies"] == []
    assert ev["query"]["unattributed"] == {} and ev["query"]["incomplete"] is False


def test_attribution_real_q4_deny_row():
    ev = evidence.collect([real_run("q4_deny")], fetch_from(real_logs("q4_deny")))[0]
    s = ev["summary"]
    assert s["tool_denies"] == ["get_customer_contact"]
    d = evidence.tool_deny_rows(s, "get_customer_contact")
    assert len(d) == 1 and d[0]["http_status"] == 403 and d[0]["authz_result"] == "DENIED"
    assert d[0]["attributed_by"] == "time" and d[0]["ambiguous"] is False
    assert d[0]["policies"] == ["projects/123456789012/locations/us-central1/authzPolicies/agent-demo-egress-gw-authz-policy-iap"]
    assert d[0]["trace_link"].endswith("traceId=5c380538565aa43a038f14984491c3cc?project=your-project-id")


def test_overlapping_runs_make_time_attribution_ambiguous():
    q4 = real_run("q4_deny")
    other = {**q4, "session_id": "999", "prompt": "another question", "final_text": "another answer"}
    evs = evidence.collect([q4, other], fetch_from(real_logs("q4_deny")))
    deny = evidence.tool_deny_rows(evs[1]["summary"], "get_customer_contact")
    assert deny and deny[0]["ambiguous"]  # in both windows -> flagged
    # exact links stay exclusive: the q4 session rows and Model Armor records do not go to the other run
    assert evs[1]["summary"]["session_rows"] == 0 and evs[1]["summary"]["model_armor_rows"] == 0
    assert evs[0]["summary"]["session_rows"] == 8 and evs[0]["summary"]["model_armor_rows"] == 3


def test_trace_group_kept_together_by_time():
    """MCP rows of one trace go to the run whose window holds all of them (not to a run overlapping a part)."""
    q4 = real_run("q4_deny")
    early = {**q4, "session_id": "999", "prompt": "x", "final_text": "", "t0": q4["t0"] - 10, "t1": q4["t0"] + 3}
    evs = evidence.collect([q4, early], fetch_from(real_logs("q4_deny")))
    mcp_early = [r for r in evs[1]["rows"] if r.get("mcp_method")]
    assert mcp_early == []
    assert {r["mcp_method"] for r in evs[0]["rows"] if r.get("mcp_method")} >= {"initialize", "tools/call"}


def test_no_window_fallback_for_trace_rows_once_runs_matched_by_trace():
    rows = [lf.normalize(e) for e in load_sample("sample_gateway_entries.json")]
    t = evidence.parse_ts
    runs = [
        {"t0": t("2026-11-27T15:00:00Z"), "t1": t("2026-11-27T15:00:05Z"), "trace_id": "B" * 32, "prompt": "x"},
        {"t0": t("2026-11-27T15:00:00Z"), "t1": t("2026-11-27T15:00:01Z"), "trace_id": "c" * 32, "prompt": "y"},
    ]
    by, unattributed = evidence.attribute(rows, [], runs)
    ids0 = {r["insert_id"]: r for r in by[0]}
    assert ids0["gw-eg-2"]["attributed_by"] == "trace" and not ids0["gw-eg-2"]["ambiguous"]
    assert "gw-eg-2" not in {r["insert_id"] for r in by[1]}
    # gw-in-1 carries an unknown trace id: no time fallback once rows were linked by trace
    assert "gw-in-1" not in ids0 and "gw-in-1" not in {r["insert_id"] for r in by[1]}
    assert unattributed["gateway: trace id not linked to any run"] >= 1


def test_session_of_another_run_is_never_attributed_by_time():
    q4 = real_run("q4_deny")
    stranger = {**q4, "session_id": "1111111111111111111", "prompt": "x", "final_text": ""}
    ev = evidence.collect([stranger], fetch_from(real_logs("q4_deny")))[0]
    assert ev["summary"]["session_rows"] == 0
    assert not any(r.get("session_id") for r in ev["rows"])
    assert ev["query"]["unattributed"]["gateway: URL names a session of no run in this batch"] == 8


def test_collect_drops_unnamed_gateway_rows_and_direct_api_model_armor():
    unnamed = copy.deepcopy(real_logs("q4_deny")[0])
    del unnamed["resource"]["labels"]["gateway_name"]
    entries = real_logs("q4q5") + [unnamed] + load_sample("sample_real_model_armor_entries.json")[:2]
    ev = evidence.collect([real_run("q5_inject")], fetch_from(entries))[0]
    assert ev["query"]["excluded"] == {"gateway row without gateway_name": 1,
                                       "Model Armor record without client_name AGENT_GATEWAY": 2}
    assert all(r["client"] == "AGENT_GATEWAY" for r in ev["rows"] if r["kind"] == "model_armor")


def test_real_q5_prompt_attributed_by_text_and_time():
    ev = evidence.collect([real_run("q5_inject")], fetch_from(real_logs("q4q5")))[0]
    ma = {r["operation"]: r for r in ev["rows"] if r["kind"] == "model_armor"}
    assert ma["SANITIZE_MODEL_RESPONSE"]["attributed_by"] == "session_id"
    p = ma["SANITIZE_USER_PROMPT"]
    assert p["attributed_by"] == "prompt_text+time" and p["matched_filters"] == ["pi_and_jailbreak"]
    # the earlier "Give me the email..." record falls in the window but is another prompt -> not attributed
    assert ev["query"]["unattributed"]["model_armor: prompt text of no run in its window"] == 1


def test_collect_records_fetch_errors():
    runs = [{"t0": 0.0, "t1": 1.0, "trace_id": "a", "prompt": "p"}]

    def boom(flt):
        raise RuntimeError("403 logging.entries.list")

    ev = evidence.collect(runs, boom)[0]
    assert ev["rows"] == [] and len(ev["query"]["errors"]) == 2 and ev["query"]["incomplete"]


def test_collect_reports_truncation():
    entries = real_logs("q4_deny")

    def fetch(flt):
        return {"entries": fetch_from(entries)(flt), "pages": 10, "truncated": "networkservices" in flt,
                "error": "stopped after 10 pages with more rows left" if "networkservices" in flt else None}

    ev = evidence.collect([real_run("q4_deny")], fetch)[0]
    assert ev["query"]["truncated"] == {"gateway": True, "model_armor": False} and ev["query"]["incomplete"]
    assert any("stopped after 10 pages" in e for e in ev["query"]["errors"])


def test_filters():
    flt = evidence.build_filter("model_armor", 0, 1)
    assert 'labels."modelarmor.googleapis.com/client_name"="AGENT_GATEWAY"' in flt
    flt = evidence.build_filter("gateway", 0, 1)
    assert 'resource.labels.gateway_name=("agent-demo-egress-gw" OR "agent-demo-ingress-gw")' in flt
    assert "networkservices.googleapis.com%2Fgateway_requests" in flt


def test_fetch_throttles_between_calls():
    sleeps, t = [], [100.0]
    evidence._last_call[0] = 0.0

    def post(url, json=None, **_):
        return FakeResp(200, {"entries": [], "nextPageToken": "x"})

    evidence.fetch_entries("f", token="t", post=post, sleep=lambda s: (sleeps.append(s), t.__setitem__(0, t[0] + s)),
                           clock=lambda: t[0], max_pages=3)
    assert sleeps == [3.0, 3.0]  # >= MIN_INTERVAL_S between entries.list calls


class FakeResp:
    def __init__(self, status, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data or {}, headers or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fetch(responses, **kw):
    seq, sleeps, calls = list(responses), [], []

    def post(url, json=None, **_):
        calls.append(dict(json))
        return seq.pop(0)

    clock = iter(range(0, 10_000, 10))
    res = evidence.fetch_entries("f", token="t", post=post, sleep=sleeps.append, clock=lambda: next(clock), **kw)
    return res, sleeps, calls


def test_fetch_429_backoff_does_not_use_page_attempts(monkeypatch):
    monkeypatch.setattr(evidence, "MIN_INTERVAL_S", 0.0)
    res, sleeps, calls = _fetch([FakeResp(429), FakeResp(429, headers={"Retry-After": "7"}),
                                 FakeResp(200, {"entries": [{"a": 1}], "nextPageToken": "p2"}),
                                 FakeResp(503), FakeResp(200, {"entries": [{"a": 2}]})], max_pages=2)
    assert res["entries"] == [{"a": 1}, {"a": 2}] and res["pages"] == 2 and res["retries"] == 3
    assert res["truncated"] is False and res["error"] is None
    assert calls[-1]["pageToken"] == "p2"
    assert 7 <= sleeps[1] < 8  # Retry-After honoured


def test_fetch_truncation_is_explicit(monkeypatch):
    monkeypatch.setattr(evidence, "MIN_INTERVAL_S", 0.0)
    res, _, _ = _fetch([FakeResp(200, {"entries": [{"a": 1}], "nextPageToken": "p2"})], max_pages=1)
    assert res["truncated"] is True and "more rows left" in res["error"] and res["entries"] == [{"a": 1}]
    res, _, _ = _fetch([FakeResp(429)] * 4, max_retries=3)
    assert res["truncated"] is True and "429" in res["error"] and res["entries"] == []
