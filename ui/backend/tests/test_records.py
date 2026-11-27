import json
from pathlib import Path

import records
import samples as s

IN, OUT = "agent-demo-ingress-gw", "agent-demo-egress-gw"
RECORDED_MA = json.loads((Path(__file__).parent / "fixtures" / "recorded_model_armor_entries.json").read_text())


def test_classify_by_gateway_name():
    assert records.classify(s.SAMPLE_OUTBOUND_ALLOWED, IN, OUT) == "outbound_gateway"
    assert records.classify(s.SAMPLE_INBOUND_ALLOWED, IN, OUT) == "inbound_gateway"
    assert records.classify(s.SAMPLE_UNKNOWN_GATEWAY, IN, OUT) == "gateway"
    assert records.classify(s.SAMPLE_MODEL_ARMOR_RESPONSE_CLEAN, IN, OUT) == "model_armor"
    assert records.classify({"resource": {"type": "cloud_run_revision"}}, IN, OUT) is None
    for e in RECORDED_MA:  # direct API probes (no client_name AGENT_GATEWAY): not gateway screening
        assert records.classify(e, IN, OUT) is None


def test_gateway_allowed():
    g = records.summarize_gateway(s.SAMPLE_OUTBOUND_ALLOWED)
    assert g["status"] == 200 and g["authz_result"] == "ALLOWED" and not g["denied"] and not g["hidden"]
    assert g["tool"] == "get_latest_run" and g["mcp_method"] == "tools/call"
    assert g["policies"] == ["agent-demo-egress-gw-authz-policy-iap"]
    assert g["trace_id"] == "aaaabbbbccccdddd0000111122223333"
    assert g["gateway_name"] == OUT


def test_gateway_denied_never_hidden_and_denied_only_on_authz():
    d = records.summarize_gateway(s.SAMPLE_OUTBOUND_DENIED)
    assert d["denied"] and d["status"] == 403 and d["authz_result"] == "DENIED" and not d["hidden"]
    assert d["tool"] == "get_customer_contact"
    # DENIED without a status or URL is still shown
    bare = {"jsonPayload": {"authzPolicyInfo": {"result": "DENIED"}}}
    assert records.summarize_gateway(bare)["denied"] and not records.summarize_gateway(bare)["hidden"]
    # HTTP 403 with no authz result: shown with its status, but not called denied
    forbidden = {"httpRequest": {"status": 403, "requestUrl": "https://x/mcp"}}
    f = records.summarize_gateway(forbidden)
    assert f["status"] == 403 and not f["denied"] and f["authz_result"] is None and not f["hidden"]
    # A row with an authz result is shown even without status (the old 'teardown' guess hid it)
    no_status = {"httpRequest": {"requestUrl": "https://x/mcp"}, "jsonPayload": {"authzPolicyInfo": {"result": "ALLOWED"}}}
    assert not records.summarize_gateway(no_status)["hidden"]
    # authz result exactly as recorded (no case folding)
    assert records.summarize_gateway({"jsonPayload": {"authzPolicyInfo": {"result": "Allowed"}}})["authz_result"] == "Allowed"


def test_gateway_garbage_never_raises():
    for entry in ({}, {"jsonPayload": "x"}, {"jsonPayload": {"authzPolicyInfo": "x", "agentGatewayInfo": 3}},
                  {"httpRequest": {"status": "abc"}}, {"jsonPayload": {"authzPolicyInfo": {"policies": "x"}}}):
        out = records.summarize_gateway(entry)
        assert out["status"] is None and out["tool"] is None


def test_model_armor_recorded_block_entries():
    by_reason = {}
    for e in RECORDED_MA:
        m = records.summarize_model_armor(e)
        assert m["operation_type"] == "SANITIZE_USER_PROMPT"
        assert m["matched"] and m["match_state"] == "MATCH_FOUND"
        assert m["verdict_short"] == "BLOCK"
        by_reason[tuple(m["matched_filters"])] = m
    assert ("sdp",) in by_reason and ("pi_and_jailbreak",) in by_reason
    sdp = next(f for f in by_reason[("sdp",)]["filters"] if f["name"] == "sdp")
    assert sdp["info_types"] in (["US_SOCIAL_SECURITY_NUMBER"], ["BRAZIL_CPF_NUMBER"])
    pij = next(f for f in by_reason[("pi_and_jailbreak",)]["filters"] if f["name"] == "pi_and_jailbreak")
    assert pij["confidence"] == "HIGH"


def test_model_armor_clean_response():
    m = records.summarize_model_armor(s.SAMPLE_MODEL_ARMOR_RESPONSE_CLEAN)
    assert m["operation_type"] == "SANITIZE_MODEL_RESPONSE" and m["operation"] == "response" and not m["matched"]
    assert m["match_state"] == "NO_MATCH_FOUND"
    assert m["matched_filters"] == [] and m["client_name"] == "AGENT_GATEWAY"
    empty = records.summarize_model_armor({})
    assert empty["matched"] is False and empty["match_state"] is None and empty["operation"] == "other"
    odd = records.summarize_model_armor({"jsonPayload": {"operationType": "SANITIZE_SOMETHING_NEW"}})
    assert odd["operation"] == "other" and odd["operation_type"] == "SANITIZE_SOMETHING_NEW"


def test_adk_event_summaries():
    c = records.summarize_adk_event(s.SAMPLE_ADK_CALL)
    assert c["calls"] == [{"id": "c1", "name": "get_customer_contact", "args": {"user_id": 9044}}]
    assert c["thought"] and c["text"] == "" and not c["is_final_text"]
    assert c["usage"] == {"prompt": 1200, "output": 20, "thoughts": 30, "total": 1250}
    r = records.summarize_adk_event(s.SAMPLE_ADK_DENIED_RESULT)
    assert r["responses"][0]["name"] == "get_customer_contact" and "403" in r["responses"][0]["error"]
    a = records.summarize_adk_event(s.SAMPLE_ADK_ANSWER)
    assert a["is_final_text"] and a["text"].startswith("I can't")
    assert records.summarize_adk_event("not json")["parsed"] is False
    mcp_err = records.summarize_adk_event({"content": {"parts": [{"function_response": {
        "name": "x", "response": {"isError": True, "content": [{"type": "text", "text": "boom"}]}}}]}})
    assert mcp_err["responses"][0]["error"] == "boom"


def test_links_and_filter():
    url = records.console_url(s.SAMPLE_OUTBOUND_DENIED, "your-project-id")
    # pinned to +/-10 min around the entry's timestamp so the link still works after an hour
    assert url.startswith("https://console.cloud.google.com/logs/query;query=insertId%3D%22sample-gw-deny-1%22;startTime=")
    assert ";endTime=" in url and url.endswith("?project=your-project-id")
    assert records.console_url({"insertId": "x"}, "p").endswith(";timeRange=P7D?project=p")
    assert records.trace_url("abc", "p") == "https://console.cloud.google.com/traces/explorer;traceId=abc?project=p"
    f = records.log_filter("p", "2026-11-26T21:00:00Z", "2026-11-26T21:05:00Z")
    assert 'timestamp>="2026-11-26T21:00:00Z"' in f and 'timestamp<"2026-11-26T21:05:00Z"' in f
    assert 'resource.type="networkservices.googleapis.com/Gateway"' in f
    assert 'logName="projects/p/logs/modelarmor.googleapis.com%2Fsanitize_operations"' in f
    assert 'labels."modelarmor.googleapis.com/client_name"="AGENT_GATEWAY"' in f
    assert "timestamp<" not in records.log_filter("p", "2026-11-26T21:00:00Z")
    assert records.insert_id({"a": 1}).startswith("sha1:")
    assert records.trace_id({"trace": "projects/p/traces/ABC"}) == "abc"
    assert records.trace_id({"trace": "76be74fe0e2f7eb575178d62ed5dba98"}) == "76be74fe0e2f7eb575178d62ed5dba98"


def test_session_and_error_helpers():
    assert records.session_id_from({"output": {"id": "123", "user_id": "u"}}) == "123"
    body = records.http_error_body('{"error": {"code": 403, "message": "blocked by policy"}}')
    assert records.http_error_message(body) == "blocked by policy"
    assert records.http_error_body("<html>nope") == "<html>nope"
