"""Field extraction checked on REAL platform records (fixtures/real_*, captured 2026-11-26)."""
import json

import records
import real

IN, OUT = "agent-demo-ingress-gw", "agent-demo-egress-gw"
GW_LOG = "networkservices.googleapis.com%2Fgateway_requests"


def gateway_rows(label):
    return [e for e in real.logs(label) if GW_LOG in e.get("logName", "")]


def test_real_only_outbound_gateway_rows_exist():
    for label in ("q1_dryrun", "q4_deny", "q4q5"):
        rows = gateway_rows(label)
        assert rows and {records.classify(e, IN, OUT) for e in rows} == {"outbound_gateway"}
        assert all(e["resource"]["labels"]["gateway_name"] == OUT for e in rows)


def test_real_deny_row():
    e = real.by_insert_id(real.logs("q4_deny"), next(
        x["insertId"] for x in real.logs("q4_deny") if x.get("httpRequest", {}).get("status") == 403))
    g = records.summarize_gateway(e)
    assert g["status"] == 403 and g["authz_result"] == "DENIED" and g["denied"] is True
    assert g["hidden"] is False
    assert g["mcp_method"] == "tools/call" and g["tool"] == "get_customer_contact"
    assert g["policies"] == ["agent-demo-egress-gw-authz-policy-iap"]
    assert g["trace_id"] == "5c380538565aa43a038f14984491c3cc"  # bare hex id, as recorded
    assert g["url"] == "https://agent-demo-mcp-123456789012.us-central1.run.app/mcp"


def test_real_allowed_rows_are_not_denied_and_status_is_separate():
    for e in gateway_rows("q1_dryrun") + gateway_rows("q4_deny"):
        g = records.summarize_gateway(e)
        assert g["denied"] == (g["authz_result"] == "DENIED")
        if g["authz_result"] == "ALLOWED":
            assert g["status"] in (200, 202)
    mcp = [records.summarize_gateway(e) for e in gateway_rows("q1_dryrun")]
    assert [g["mcp_method"] for g in mcp if g["mcp_method"]] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call", "tools/call"]
    assert {g["tool"] for g in mcp if g["mcp_method"] == "tools/call"} == {"get_model_summary"}


def test_real_connect_tunnels_are_the_only_hidden_rows():
    for label in ("q1_dryrun", "q4_deny", "q4q5"):
        for e in gateway_rows(label):
            g = records.summarize_gateway(e)
            connect = e["httpRequest"].get("requestMethod") == "CONNECT"
            assert g["hidden"] == connect, e["insertId"]
            if connect:  # status 200, but no URL and no authz result
                assert g["status"] == 200 and g["url"] is None and g["authz_result"] is None
            if g["authz_result"] == "DENIED":
                assert not g["hidden"]


def test_real_session_rows_carry_the_session_id_but_no_trace():
    """The review assumed session rows share the trace id of the turn's tools/call rows. On the real
    records they carry no trace at all: only MCP rows do. Attribution must not rely on that link."""
    rows = [records.summarize_gateway(e) for e in gateway_rows("q1_dryrun")]
    session_rows = [g for g in rows if g["session_ids"]]
    assert session_rows and {s for g in session_rows for s in g["session_ids"]} == {real.Q1_SESSION}
    assert all(g["trace_id"] is None for g in session_rows)
    traced = [g for g in rows if g["trace_id"]]
    assert traced and all(g["url"].endswith("/mcp") for g in traced)
    assert {g["trace_id"] for g in traced} == {"76be74fe0e2f7eb575178d62ed5dba98"}
    # The create-session POST (…/sessions) has no id in its URL.
    create = [g for g in rows if g["method"] == "POST" and str(g["url"]).endswith("/sessions")]
    assert len(create) == 1 and create[0]["session_ids"] == []


def test_real_model_armor_gateway_records():
    ma = [e for e in real.logs("q1_dryrun") if real.is_armor(e)]
    assert len(ma) == 3 and all(records.classify(e, IN, OUT) == "model_armor" for e in ma)
    prompt, create_resp, answer_resp = (records.summarize_model_armor(e) for e in ma)
    assert prompt["operation_type"] == "SANITIZE_USER_PROMPT" and prompt["operation"] == "prompt"
    assert prompt["screened_text"] == real.meta("q1_dryrun")["prompt"]
    assert prompt["match_state"] == "NO_MATCH_FOUND" and not prompt["matched"]
    assert prompt["verdict_short"] == "ALLOW" and prompt["client_name"] == "AGENT_GATEWAY"
    assert all("confidence" not in f for f in prompt["filters"])
    assert create_resp["operation"] == "response" and real.Q1_SESSION in create_resp["screened_text"]
    assert records.screened_inner_text(create_resp["screened_text"]).startswith('{\n  "output"')
    assert answer_resp["screened_text"].startswith("The latest pipeline run")
    # The prompt screening and the answer screening of one streamQuery share the client_correlation_id.
    assert prompt["correlation_id"] == answer_resp["correlation_id"] == "14683294536216392746"
    assert create_resp["correlation_id"] != prompt["correlation_id"]


def test_real_model_armor_blocks_and_confidence_only_from_matched_subfilters():
    ma = [records.summarize_model_armor(e) for e in real.logs("q4q5") if real.is_armor(e)]
    rai = next(m for m in ma if m["screened_text"] == real.Q4_OLD_PROMPT)
    assert rai["matched"] and rai["matched_filters"] == ["rai"] and rai["verdict_short"] == "BLOCK"
    f = next(x for x in rai["filters"] if x["name"] == "rai")
    assert f["matched_types"] == ["dangerous", "harassment"] and f["confidence"] == "MEDIUM_AND_ABOVE"
    assert all("confidence" not in x for x in rai["filters"] if x["name"] != "rai")
    inj = next(m for m in ma if m["screened_text"] == real.meta("q5_inject")["prompt"])
    assert inj["matched_filters"] == ["pi_and_jailbreak"] and inj["match_state"] == "MATCH_FOUND"
    pij = next(x for x in inj["filters"] if x["name"] == "pi_and_jailbreak")
    assert pij["confidence"] == "HIGH" and pij["match_state"] == "MATCH_FOUND"
    assert next(x for x in inj["filters"] if x["name"] == "sdp")["match_state"] == "NO_MATCH_FOUND"


def test_real_direct_api_probes_are_excluded():
    probes = json.loads((real.FIX / "recorded_model_armor_entries.json").read_text())
    for e in probes:
        assert records.ma_client_name(e) is None
        assert records.classify(e, IN, OUT) is None


def test_real_inbound_block_body():
    for label in ("q5_inject", "q6_ssn"):
        m = real.meta(label)
        assert m["status"] == 403
        body = records.http_error_body(m["error_body"])
        assert body == {"error": {"code": 403, "message": "Model Armor: Prompt violates content security configurations",
                                  "status": "PERMISSION_DENIED"}}
        assert records.http_error_message(body) == "Model Armor: Prompt violates content security configurations"


def test_real_in_stream_errors_in_every_shape():
    call, adk_err, final = real.sse("q4_deny_v3_1")
    c = records.summarize_adk_event(call)
    assert not c["is_error"] and c["calls"][0]["name"] == "pipelines_analyst:list_top_churn_risk"
    e = records.summarize_adk_event(adk_err)
    assert e["is_error"] and e["error_code"] == "ValueError"
    assert e["error_message"].startswith("Tool 'pipelines_analyst:list_top_churn_risk' not found.")
    f = records.summarize_adk_event(final)
    assert f["is_error"] and f["error_code"] == "498" and f["error_message"] == final["errorMessage"]
    # the closing line without the camelCase copies, and a Google API error object
    assert records.stream_error({"code": 498, "message": "boom"}) == ("498", "boom")
    assert records.stream_error({"error": {"code": 403, "message": "no", "status": "PERMISSION_DENIED"}}) == (
        "PERMISSION_DENIED", "no")
    for ev in real.sse("q1_dryrun") + real.sse("q4_deny"):
        assert not records.summarize_adk_event(ev)["is_error"]


def test_real_deny_turn_events():
    events = [records.summarize_adk_event(e) for e in real.sse("q4_deny")]
    calls = [c["name"] for ev in events for c in ev["calls"]]
    assert calls == ["list_top_churn_risk", "get_customer_contact", "get_customer_contact"]
    errors = [r["error"] for ev in events for r in ev["responses"] if r["error"]]
    assert len(errors) == 2 and all("MCP session connection lost" in x for x in errors)
    assert events[-1]["is_final_text"] and "blocked" in events[-1]["text"]


def test_real_log_filter_matches_verified_fields():
    f = records.log_filter("your-project-id", "2026-11-26T21:00:16.5Z", "2026-11-26T21:00:40Z", (IN, OUT))
    assert 'resource.labels.gateway_name=("agent-demo-ingress-gw" OR "agent-demo-egress-gw")' in f
    assert 'labels."modelarmor.googleapis.com/client_name"="AGENT_GATEWAY"' in f
    assert 'logName="projects/your-project-id/logs/modelarmor.googleapis.com%2Fsanitize_operations"' in f
