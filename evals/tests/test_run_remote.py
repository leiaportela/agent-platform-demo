"""run_remote.main end to end with a fake engine and fake Cloud Logging (no network), replaying real records:
the fake engine answers s4/s5/s6 with the captured responses at their captured times and the fake
entries.list serves the captured log entries, so attribution runs on real data."""
import json
import os

import engine_client
import evidence
import run_remote
from conftest import EVALS, load_sample, real_logs, real_run, sample_events, sample_path

REAL_BY_PROMPT = {"contact information of the top": "q4_deny", "Ignore all previous": "q5_inject", "SSN": "q6_ssn"}


def test_main_offline(monkeypatch, tmp_path):
    calls = {"stream": 0, "q4": 0}
    real = {k: real_run(v) for k, v in REAL_BY_PROMPT.items()}

    def which(message):
        return next((k for k in REAL_BY_PROMPT if k in message), None)

    def fake_session(engine, user_id, **kw):
        return {"s4_pii": real["contact information of the top"]["session_id"],
                "s5_injection": real["Ignore all previous"]["session_id"],
                "s6_ssn": real["SSN"]["session_id"]}.get(next((k for k in ("s4_pii", "s5_injection", "s6_ssn") if k in user_id), ""),
                                                          f"s-{user_id}"), None

    def fake_stream(engine, user_id, session_id, message, timeout=300.0, trace=None, deadline_s=120.0, **kw):
        calls["stream"] += 1
        key = which(message)
        if key:
            r = real[key]
            res = engine_client.StreamResult(t0=r["t0"], t1=r["t1"])
            res.total_s = round(r["t1"] - r["t0"], 3)
            if key == "contact information of the top":
                calls["q4"] += 1
                if calls["q4"] == 1:  # first attempt hits quota once -> retried
                    res.http_status, res.http_error = 429, {"status": 429, "body": "RESOURCE_EXHAUSTED"}
                    return res
            if r["http_error"]:
                res.http_status, res.http_error = r["http_error"]["status"], r["http_error"]
            else:
                res.http_status, res.events, res.first_event_s = 200, r["events"], 5.0
            return res
        res = engine_client.StreamResult(t0=1795680000.0 + calls["stream"], t1=1795680004.0 + calls["stream"])
        res.total_s, res.http_status, res.events, res.first_event_s = 4.0, 200, sample_events("sample_events_s1_answered.sse"), 1.0
        return res

    reads = []

    def fake_fetch(flt):
        reads.append(flt)
        entries = real_logs("q4_deny", "q4q5")
        want_ma = "modelarmor" in flt
        return {"entries": [e for e in entries if ("modelarmor" in e["logName"]) == want_ma], "pages": 1,
                "truncated": False, "retries": 0, "error": None}

    monkeypatch.setattr(engine_client, "create_session", fake_session)
    monkeypatch.setattr(engine_client, "stream_query", fake_stream)
    monkeypatch.setattr(evidence, "fetch_entries", fake_fetch)
    monkeypatch.setattr(run_remote.time, "sleep", lambda s: None)

    out = tmp_path / "res.jsonl"
    rc = run_remote.main(["--engine", "projects/123456789012/locations/us-central1/reasoningEngines/1234567890123456789",
                          "--set", "scripted", "--repeat", "1", "--out", str(out), "--log-wait", "0",
                          "--ground-truth", sample_path("sample_ground_truth.json")])
    assert rc == 0
    recs = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["prompt_id"] for r in recs] == ["s1_run_and_quality", "s2_demand_stock_up", "s3_churn_risk",
                                              "s4_pii_contact", "s5_injection", "s6_ssn"]
    by = {r["prompt_id"]: r for r in recs}
    assert by["s1_run_and_quality"]["grade"]["passed"]
    s4 = by["s4_pii_contact"]
    assert s4["attempt"] == 2 and s4["earlier_attempts"][0]["http_error"]["status"] == 429
    deny = next(c for c in s4["grade"]["checks"] if c["name"] == "denied:get_customer_contact")
    assert deny["ok"] is True and "attributed by time" in deny["detail"]
    assert s4["grade"]["passed"] and s4["evidence"]["summary"]["session_rows"] == 8
    s5 = by["s5_injection"]
    assert s5["grade"]["outcome"] == "blocked_inbound" and s5["grade"]["passed"]
    ev5 = {c["name"]: c["ok"] for c in s5["grade"]["evidence_checks"]}
    assert ev5["log:model_armor_prompt_match"] is True
    assert by["s6_ssn"]["grade"]["passed"]  # body names Model Armor; no logs for that window
    assert all("evidence" in r and "evidence_checks" in r["grade"] for r in recs)
    assert len(reads) == 4  # s6 has no Model Armor record -> one re-read
    assert not os.path.exists(str(out) + ".partial")
    md = (tmp_path / "REPORT.md").read_text()
    assert "authzPolicyInfo.result=DENIED, httpRequest.status=403" in md and "(attributed by time)" in md
    # the facts of s2/s3 are not in the s1 sample answer -> graded as failures, not crashes
    assert not by["s2_demand_stock_up"]["grade"]["passed"]


def test_no_logs_leaves_deny_pending(monkeypatch, tmp_path):
    def fake_stream(engine, user_id, session_id, message, **kw):
        res = engine_client.StreamResult(t0=1.0, t1=2.0)
        res.http_status, res.events = 200, sample_events("sample_events_s4_denied.sse")
        return res

    monkeypatch.setattr(engine_client, "create_session", lambda engine, user_id, **kw: ("s", None))
    monkeypatch.setattr(engine_client, "stream_query", fake_stream)
    out = tmp_path / "r.jsonl"
    run_remote.main(["--engine", "projects/p/locations/us-central1/reasoningEngines/1", "--only", "s4_pii_contact",
                     "--repeat", "1", "--no-logs", "--out", str(out), "--no-report",
                     "--ground-truth", sample_path("sample_ground_truth.json")])
    g = json.loads(out.read_text())["grade"]
    assert g["pending"] == ["denied:get_customer_contact"] and not g["passed"]
    assert g["agent_reported_denied"] == ["get_customer_contact"]


def test_build_jobs_defaults():
    prompts = run_remote.load_prompts(os.path.join(EVALS, "prompts.yaml"))
    assert len(run_remote.build_jobs(prompts, "scripted", None)) == 60
    assert len(run_remote.build_jobs(prompts, "all", None)) == 60 + len(prompts["benign"]) + len(prompts["attack"])
    assert len(run_remote.build_jobs(prompts, "attack", 2, only=["a08_cpf"])) == 2


def test_real_http_block_sample_is_the_recorded_body():
    assert load_sample("real_http_block_model_armor.json")["status"] == 403


def test_reattach_rereads_logs_without_new_runs(monkeypatch, tmp_path, specs):
    from conftest import fetch_from

    runs = load_sample("real_live_2137.runs.json")
    path = tmp_path / "live.jsonl"
    path.write_text("".join(json.dumps({**r, "set": "scripted", "repeat_index": 0, "spec": specs[r["prompt_id"]],
                                        "grade": {}, "timings": {}}) + "\n" for r in runs))
    monkeypatch.setattr(evidence, "fetch_entries", fetch_from(load_sample("real_live_2137.logs.json")))
    monkeypatch.setattr(engine_client, "stream_query", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no runs")))
    assert run_remote.main(["--reattach", str(path), "--no-report", "--ground-truth", sample_path("sample_ground_truth.json")]) == 0
    recs = {r["prompt_id"]: r for r in map(json.loads, (tmp_path / "live.reread.jsonl").read_text().splitlines())}
    assert recs["s1_run_and_quality"]["grade"]["passed"] and recs["s5_injection"]["grade"]["passed"]
    assert not recs["s4_pii_contact"]["grade"]["passed"]
