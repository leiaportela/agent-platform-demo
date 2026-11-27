"""IAP authorization decisions of the outbound gateway, on real records:

* real_iap_samples.json: copy of demo/evidence/raw-run/iap-decisions.logs.json (keys deny_get_customer_contact,
  allow_tool, allow_handshake, allow_google_api_endpoint).
* real_iap_2213.logs.json: every IAP decision returned by the one live entries.list read made with the
  filter of evidence.build_filter (window 2026-11-26T22:13:00Z..22:14:00Z, 57 decisions).
* real_iap_2213.runs.json: the runs of results/scripted-x10.jsonl whose windows overlap that minute (s3#4-9,
  s4#0-9, s5, s6; concurrency 3), as recorded (events, times, engine).
"""
import copy
import json

import evidence
import grading
import log_fields as lf
import report
import run_remote
from conftest import load_sample

ENGINE = "projects/123456789012/locations/us-central1/reasoningEngines/1234567890123456789"
PRINCIPAL = ("principal://agents.global.org-111111111111.system.id.goog/resources/aiplatform/projects/123456789012"
             "/locations/us-central1/reasoningEngines/1234567890123456789")
SAMPLES = load_sample("real_iap_samples.json")
LOGS = load_sample("real_iap_2213.logs.json")
RUNS = load_sample("real_iap_2213.runs.json")
# the filter string the live read of 2026-11-26 was made with (HTTP 200, 450 entries, one page: 393 gateway rows
# + 57 IAP decisions, 10 of them granted:false for tools/call get_customer_contact)
VERIFIED_FILTER = (
    '((logName="projects/your-project-id/logs/networkservices.googleapis.com%2Fgateway_requests" AND '
    'resource.type="networkservices.googleapis.com/Gateway" AND resource.labels.gateway_name=("agent-demo-egress-gw" '
    'OR "agent-demo-ingress-gw")) OR (logName="projects/your-project-id/logs/cloudaudit.googleapis.com%2Fdata_access" '
    'AND protoPayload.serviceName="iap.googleapis.com" AND protoPayload.methodName="GatekeeperAuthorizer.AuthorizeUser" '
    'AND labels."iap.googleapis.com/audited_resource_type"="agentregistry.googleapis.com/McpServer" AND '
    f'protoPayload.authenticationInfo.principalSubject="{PRINCIPAL}")) AND timestamp>="2026-11-26T22:13:00.000Z" '
    'AND timestamp<="2026-11-26T22:14:00.000Z"')


def _fetch(entries):
    def fetch(flt):
        return [] if "modelarmor" in flt else list(entries)
    return fetch


def _collect(runs=RUNS, entries=LOGS):
    return {f"{r['prompt_id']}#{r['repeat_index']}": ev for r, ev in zip(runs, evidence.collect(runs, _fetch(entries)))}


def test_agent_principal_from_engine():
    assert lf.agent_principal(ENGINE) == PRINCIPAL
    assert lf.agent_principal("projects/your-project-id/locations/us-central1/reasoningEngines/1") is None  # needs the number
    assert lf.agent_principal(None) is None and lf.agent_principal("garbage") is None


def test_filter_is_the_one_verified_live():
    t = evidence.parse_ts
    assert evidence.build_filter("gateway", t("2026-11-26T22:13:00Z"), t("2026-11-26T22:14:00Z"), [PRINCIPAL]) == VERIFIED_FILTER
    # no principal -> the gateway clause alone, as before
    assert evidence.build_filter("gateway", 0, 1).startswith(f"{lf.GATEWAY_FILTER} AND timestamp")
    assert "cloudaudit" not in evidence.build_filter("model_armor", 0, 1, [PRINCIPAL])


def test_real_deny_record_fields():
    deny = lf.normalize(SAMPLES["deny_get_customer_contact"][0])
    assert deny["kind"] == "iap_decision" and deny["insert_id"] == "7j45pje7gyqu"
    assert (deny["granted"], deny["granted_in_record"], deny["status_code"], deny["status_message"]) == (False, True, 7, "Permission Denied.")
    assert deny["decision"] == "denied" and lf.iap_decision_text(deny) == "granted: false · status 7 Permission Denied"
    assert (deny["mcp_method"], deny["mcp_tool"], deny["base_protocol_method"]) == ("tools/call", "get_customer_contact", False)
    assert deny["permission"] == "iap.webServiceVersions.egressViaIAP"
    assert deny["policy_version"] == "POLICY_VERSION_V1" and deny["dry_run"] is False
    assert deny["principal"] == PRINCIPAL and deny["resource_type"] == lf.IAP_MCP_RESOURCE_TYPE
    assert deny["url"] == "https://agent-demo-mcp-123456789012.us-central1.run.app/mcp" and deny["trace_id"] is None
    assert deny["logs_link"] == ("https://console.cloud.google.com/logs/query;query=insertId%3D%227j45pje7gyqu%22;"
                                 "startTime=2026-11-26T21:13:15Z;endTime=2026-11-26T23:13:15Z?project=your-project-id")
    assert lf.is_iap_tool_deny(deny, "get_customer_contact", PRINCIPAL)
    assert not lf.is_iap_tool_deny(deny, "list_top_churn_risk") and not lf.is_iap_tool_deny(deny, "get_customer_contact", "other")
    assert not lf.is_denied(deny)  # the gateway-row predicate never reads an IAP record


def test_real_allow_and_endpoint_records():
    allow = lf.normalize(SAMPLES["allow_tool"][0])
    assert allow["decision"] == "granted" and lf.iap_decision_text(allow) == "granted: true"
    assert lf.is_iap_mcp_tools_call(allow) and not lf.is_iap_tool_deny(allow, "list_top_churn_risk")
    assert allow["status_code"] is None  # status is {} on a grant
    ep = lf.normalize(SAMPLES["allow_google_api_endpoint"][0])
    assert ep["resource_type"] == "agentregistry.googleapis.com/Endpoint" and not lf.is_iap_mcp_tools_call(ep)
    assert SAMPLES["allow_handshake"] == []


def test_missing_granted_is_false_only_with_status_7():
    e = copy.deepcopy(SAMPLES["deny_get_customer_contact"][0])
    del e["protoPayload"]["authorizationInfo"][0]["granted"]
    r = lf.normalize(e)
    assert r["granted"] is None and not r["granted_in_record"] and r["decision"] == "denied"
    assert lf.iap_decision_text(r) == "granted not in record · status 7 Permission Denied"
    del e["protoPayload"]["status"]
    r = lf.normalize(e)
    assert r["decision"] is None and lf.iap_decision_text(r) == "granted not in record"
    assert not lf.is_iap_tool_deny(r, "get_customer_contact")


def test_dry_run_deny_does_not_confirm():
    e = copy.deepcopy(SAMPLES["deny_get_customer_contact"][0])
    e["protoPayload"]["metadata"]["dryRun"] = True
    assert not lf.is_iap_tool_deny(lf.normalize(e), "get_customer_contact")


def test_real_2213_every_s4_run_has_its_own_deny_decision():
    ev = _collect()
    denies = {}
    for key, e in ev.items():
        for d in evidence.iap_deny_records(e["summary"], "get_customer_contact"):
            denies.setdefault(key, []).append(d)
    s4 = [f"s4_pii_contact#{i}" for i in range(10)]
    assert sorted(denies) == sorted(s4)  # 10/10, and nothing to the other runs
    assert all(len(denies[k]) == 1 and not denies[k][0]["ambiguous"] for k in s4)
    assert len({denies[k][0]["insert_id"] for k in s4}) == 10
    # s4#8 / s4#9 called get_customer_contact 10 ms apart: each deny goes to the call it answered
    assert denies["s4_pii_contact#8"][0]["insert_id"] == "oh1rt4e7luje"   # 22:13:43.573, response event 43.589
    assert denies["s4_pii_contact#9"][0]["insert_id"] == "1oj2nqoe4t8mt"  # 22:13:43.294, response event 43.313
    d = denies["s4_pii_contact#0"][0]
    assert d["source"] == "Outbound gateway · IAP decision" and d["attribution_label"] == "matched: agent principal + tool name + time"
    assert d["decision_text"] == "granted: false · status 7 Permission Denied" and d["principal"] == PRINCIPAL
    q = ev["s4_pii_contact#0"]["query"]
    assert q["iap_decisions"] == {"MCP tools/list (handshake/other; counted, not shown)": 30,
                                  "tools/call granted": 17, "tools/call denied": 10}
    assert q["fetched_rows"] == {"gateway": 0, "iap_decision": 57, "model_armor": 0}
    assert PRINCIPAL in q["filters"]["gateway"] and q["iap_principals"] == [PRINCIPAL]
    assert not any(ev[f"s5_injection#{i}"]["summary"]["iap_decisions"] for i in range(10))  # blocked inbound: no tool calls


def test_real_2213_allowed_decisions_in_overlapping_call_spans_stay_ambiguous():
    ev = _collect()
    s5, s6 = ev["s4_pii_contact#5"]["summary"], ev["s4_pii_contact#6"]["summary"]
    shared = [d for d in s5["iap_decisions"] if d["tool"] == "list_top_churn_risk" and d["ambiguous"]]
    assert {d["insert_id"] for d in shared} <= {d["insert_id"] for d in s6["iap_decisions"]}
    assert shared and all(d["decision"] == "granted" for d in shared)
    # only tools/call decisions are shown
    assert all(d["mcp_method"] == "tools/call" for e in ev.values() for d in e["summary"]["iap_decisions"])


def test_attribution_needs_principal_window_and_called_tool():
    run = next(r for r in RUNS if r["prompt_id"] == "s4_pii_contact" and r["repeat_index"] == 0)
    deny = SAMPLES["deny_get_customer_contact"][0]  # 22:13:15.98, s4#0's denied call
    other = copy.deepcopy(deny)
    other["protoPayload"]["authenticationInfo"]["principalSubject"] = PRINCIPAL.replace("1234567890123456789", "1")
    other["insertId"] = "other-principal"
    late = copy.deepcopy(deny)
    late["timestamp"], late["insertId"] = "2026-11-26T22:20:00Z", "late"
    tool = copy.deepcopy(deny)
    tool["protoPayload"]["metadata"]["mcp_attributes"]["mcp_tool_name"], tool["insertId"] = "get_demand_outlook", "tool"
    endpoint = SAMPLES["allow_google_api_endpoint"][0]
    ev = evidence.collect([run], _fetch([deny, other, late, tool, endpoint]))[0]
    assert [d["insert_id"] for d in ev["summary"]["iap_decisions"]] == ["7j45pje7gyqu"]
    assert ev["query"]["unattributed"] == {"iap: principal of no run in this batch": 1, "iap: outside every run window": 1,
                                           "iap: no run in its window called the tool": 1}
    assert ev["query"]["iap_decisions"]["not an MCP server decision (excluded)"] == 1
    # a run without ENGINE: no principal, IAP decisions are not read
    bare = {k: v for k, v in run.items() if k != "engine"}
    ev = evidence.collect([bare], _fetch([deny]))[0]
    assert ev["summary"]["iap_decisions"] == [] and "cloudaudit" not in ev["query"]["filters"]["gateway"]
    assert ev["query"]["notes"] and not ev["query"]["incomplete"]


def test_tool_calls_pair_responses_by_id():
    run = next(r for r in RUNS if r["prompt_id"] == "s4_pii_contact" and r["repeat_index"] == 0)
    calls = evidence.tool_calls(run)
    assert [c["name"] for c in calls] == ["list_top_churn_risk", "get_customer_contact"]
    c = calls[1]
    assert c["id"] == "call_14813455714784408241" and round(c["t_response"] - c["t_call"], 3) == 1.925


def test_deny_check_passes_on_iap_decision_alone(gt, specs):
    ev = _collect()
    spec = specs["s4_pii_contact"]
    for r in RUNS:
        if r["prompt_id"] != "s4_pii_contact":
            continue
        g = grading.grade_run(spec, r, gt, evidence=ev[f"s4_pii_contact#{r['repeat_index']}"]["summary"])
        deny = next(c for c in g["checks"] if c["name"] == "denied:get_customer_contact")
        assert deny["ok"] is True and deny["confirmed_by"] == ["iap_decision"]  # no gateway rows in this fixture
        assert "IAP decision: tools/call get_customer_contact granted: false · status 7 Permission Denied" in deny["detail"]
        assert "(matched: agent principal + tool name + time)" in deny["detail"]
        assert "no attributed outbound row with authz DENIED" in deny["detail"]


def test_deny_check_either_record_and_ambiguity(gt, specs):
    spec = specs["s4_pii_contact"]
    rec = next(r for r in RUNS if r["prompt_id"] == "s4_pii_contact")
    iap = {"decision": "denied", "mcp_method": "tools/call", "tool": "get_customer_contact", "dry_run": False,
           "attributed_by": "principal+tool+time", "ambiguous": False, "decision_text": "granted: false · status 7 Permission Denied"}
    gw = {"authz_result": "DENIED", "http_status": 403, "mcp_method": "tools/call", "tool": "get_customer_contact",
          "attributed_by": "trace", "ambiguous": False}

    def deny(summary):
        g = grading.grade_run(spec, rec, {}, evidence={"gateway_rows": 1, **summary})
        return next(c for c in g["checks"] if c["name"] == "denied:get_customer_contact")

    assert deny({"iap_decisions": [iap], "outbound_tool_calls": [gw]})["confirmed_by"] == ["iap_decision", "gateway_row"]
    assert deny({"outbound_tool_calls": [gw]})["ok"] is True
    d = deny({"iap_decisions": [{**iap, "ambiguous": True}]})
    assert d["ok"] is False and "ambiguous" in d["detail"]
    for bad in [{**iap, "decision": "granted"}, {**iap, "tool": "list_top_churn_risk"}, {**iap, "mcp_method": "tools/list"},
                {**iap, "dry_run": True}, {**iap, "attributed_by": None}]:
        assert deny({"iap_decisions": [bad]})["ok"] is False, bad


def test_report_counts_both_records(gt, specs, tmp_path):
    ev = _collect()
    recs = []
    for r in RUNS:
        if r["prompt_id"] != "s4_pii_contact":
            continue
        e = ev[f"s4_pii_contact#{r['repeat_index']}"]
        rec = {**r, "set": "scripted", "spec": specs["s4_pii_contact"], "evidence": e,
               "timings": {"first_event_s": 1.0, "total_s": 8.0}}
        rec["grade"] = grading.grade_run(rec["spec"], rec, gt, evidence=e["summary"])
        recs.append(rec)
    m = report.metrics(recs)
    assert (m["denied_ok"], m["denied_by_iap"], m["denied_by_gateway"], m["denied_runs"]) == (10, 10, 0, 10)
    md = report.build(recs)
    assert ("| outbound deny confirmed by IAP decision (granted: false · status 7, MCP tools/call of the tool, agent "
            "principal; matched: agent principal + tool name + time) | 10/10 (100%) |") in md
    assert "| outbound deny confirmed by gateway request row (authzPolicyInfo.result DENIED, tools/call of the tool) | 0/10 (0%) |" in md
    assert (f"Outbound gateway · IAP decision: tools/call `get_customer_contact`, granted: false · status 7 Permission Denied, "
            f"principal `{PRINCIPAL}`, policy_version=POLICY_VERSION_V1: [log](https://console.cloud.google.com/logs/query;"
            "query=insertId%3D%227j45pje7gyqu%22;startTime=2026-11-26T21:13:15Z;endTime=2026-11-26T23:13:15Z"
            "?project=your-project-id) (matched: agent principal + tool name + time)") in md
    assert "outbound gateway row with authzPolicyInfo.result=DENIED for tools/call `get_customer_contact`: none attributed" in md
    assert "IAP decisions read (same entries.list as the gateway rows" in md
    assert "## Evidence per run" in md and "granted: true" in md


def test_no_reread_when_iap_decision_confirms_the_deny(specs):
    ev = _collect()
    r = next(r for r in RUNS if r["prompt_id"] == "s4_pii_contact")
    rec = {**r, "spec": specs["s4_pii_contact"], "grade": {"outcome": "answered"}}
    assert not run_remote._needs_reread(rec, ev["s4_pii_contact#0"])
    empty = json.loads(json.dumps(ev["s4_pii_contact#0"]))
    empty["summary"]["iap_decisions"] = []
    assert run_remote._needs_reread(rec, empty)
