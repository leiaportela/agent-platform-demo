"""Run the eval prompts against the deployed agent (Agent Runtime, through the inbound gateway) and grade them.

  .venv/bin/python run_remote.py --engine projects/P/locations/us-central1/reasoningEngines/ID \
      [--set scripted|benign|attack|all] [--repeat N] [--concurrency 1] [--out results/<stamp>.jsonl]

Each run: new session, one streamQuery, graded (tools, facts vs ground_truth.json, blocked vs expected,
no email in the answer). After all runs, Cloud Logging (read-only) is queried for outbound Agent Gateway
rows, IAP authorization decisions and Model Armor records, which are attached to each run (evidence.py) and the
runs are graded again with them (outbound deny, Model Armor attribution of a 403). Then results/REPORT.md is written (report.py).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime
import json
import os
import random
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine_client  # noqa: E402
import evidence  # noqa: E402
import grading  # noqa: E402

DEFAULT_REPEAT = {"scripted": 10, "benign": 1, "attack": 1}
SETS = ("scripted", "benign", "attack")


def load_prompts(path: str) -> dict[str, list[dict]]:
    import yaml

    with open(path) as f:
        data = yaml.safe_load(f)
    return {s: data.get(s) or [] for s in SETS}


def build_jobs(prompts: dict[str, list[dict]], which: str, repeat: int | None, only: list[str] | None = None) -> list[dict]:
    sets = SETS if which == "all" else (which,)
    jobs = []
    for s in sets:
        for spec in prompts[s]:
            if only and spec["id"] not in only:
                continue
            for i in range(repeat or DEFAULT_REPEAT[s]):
                jobs.append({"set": s, "spec": spec, "repeat_index": i})
    return jobs


def run_one(engine: str, job: dict, stamp: str, retries: int, use_session: bool, timeout: float,
            deadline: float = 120.0) -> dict:
    spec = job["spec"]
    user_id = f"evals-{stamp}-{spec['id']}-{job['repeat_index']}".lower()[:120]
    attempts = []
    for attempt in range(1, retries + 2):
        session_id, session_error, session_t0, session_t1 = None, None, None, None
        if use_session:
            session_t0 = time.time()
            session_id, session_error = engine_client.create_session(engine, user_id)
            session_t1 = time.time()
        trace = engine_client.new_trace()
        res = engine_client.stream_query(engine, user_id, session_id, spec["text"], timeout=timeout, trace=trace,
                                         deadline_s=deadline)
        rec = {
            "set": job["set"], "prompt_id": spec["id"], "prompt": spec["text"], "repeat_index": job["repeat_index"],
            "attempt": attempt, "engine": engine, "user_id": user_id, "session_id": session_id,
            "session_error": session_error, "session_t0": session_t0, "session_t1": session_t1, "trace_id": trace[0],
            "started_at": evidence._iso(res.t0), "ended_at": evidence._iso(res.t1), **res.to_dict(),
        }
        kind = grading.classify_http_error(res.http_error["status"], res.http_error.get("body")) if res.http_error else None
        retryable = kind in grading.RETRYABLE or bool(res.transport_error and not res.events)
        if not retryable or attempt > retries:
            rec["earlier_attempts"] = attempts
            return rec
        attempts.append({k: rec[k] for k in ("attempt", "http_error", "transport_error", "started_at", "trace_id",
                                             "session_id", "t0", "t1")})
        time.sleep(min(60, 5 * 3 ** (attempt - 1)) + random.random())
    raise AssertionError("unreachable")


def _needs_reread(rec: dict, ev: dict) -> bool:
    """A record the platform should have produced is not in the logs yet (they lag)."""
    expect = rec["spec"].get("expect") or {}
    s = ev["summary"]
    if expect.get("blocked") and not any(m.get("operation") == "SANITIZE_USER_PROMPT" for m in s.get("model_armor", [])):
        return True
    if (rec.get("grade") or {}).get("outcome") == "blocked_unattributed":
        return True
    # a deny confirmed by either record is enough (the gateway row is lossy; the IAP decision arrives within ~20 s)
    return any(not grading.deny_confirmed_by(s, t) for t in expect.get("denied") or [])


def attach_evidence(records: list[dict], gt: dict, log_wait: float, reread_wait: float = 45.0) -> None:
    wait = max(r["t1"] for r in records) + log_wait - time.time()
    if wait > 0:
        print(f"waiting {wait:.0f}s for gateway / Model Armor logs...", file=sys.stderr)
        time.sleep(wait)
    ev = evidence.collect(records)
    missing = [i for i, r in enumerate(records) if _needs_reread(r, ev[i])]
    if missing and reread_wait > 0:
        print(f"{len(missing)} run(s) still without the expected log record; waiting {reread_wait:.0f}s and reading again",
              file=sys.stderr)
        time.sleep(reread_wait)
        ev = evidence.collect(records)
    for r, e in zip(records, ev):
        r["evidence"] = e
        r["grade"] = grading.grade_run(r["spec"], r, gt, evidence=e["summary"])
        r["grade"]["evidence_checks"] = grading.grade_evidence(r["spec"], e["summary"])


def _print_final(records: list[dict]) -> None:
    for r in records:
        g = r["grade"]
        bad = [c["name"] + ("?" if c["ok"] is None else "") for c in g["checks"] if c["ok"] is not True]
        s = r["evidence"]["summary"]
        print(f"  {r['prompt_id']}#{r['repeat_index']} {g['outcome']:<20} {'PASS' if g['passed'] else 'FAIL ' + ','.join(bad)}  "
              f"outbound rows={s['gateway_rows']} tool denies: gateway row={s['tool_denies']} "
              f"IAP decision={s.get('iap_tool_denies', [])} "
              f"Model Armor records={s['model_armor_rows']} attributed_by={s['attributed_by']}", file=sys.stderr)


def _write(records: list[dict], out: str, no_report: bool) -> None:
    with open(out, "w") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")
    passed = sum(r["grade"]["passed"] for r in records)
    print(f"{passed}/{len(records)} runs passed -> {out}", file=sys.stderr)
    if not no_report:
        import report

        path = report.write_report([out], os.path.join(os.path.dirname(os.path.abspath(out)), "REPORT.md"))
        print(f"report -> {path}", file=sys.stderr)


def reattach(a: argparse.Namespace) -> int:
    with open(a.ground_truth) as f:
        gt = json.load(f)
    with open(a.reattach) as f:
        records = [json.loads(line) for line in f if line.strip()]
    attach_evidence(records, gt, 0.0, 0.0)
    _print_final(records)
    _write(records, a.out or a.reattach.removesuffix(".jsonl") + ".reread.jsonl", a.no_report)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--engine", default=os.environ.get("ENGINE"), help="projects/P/locations/L/reasoningEngines/ID (or $ENGINE)")
    ap.add_argument("--set", default="scripted", choices=[*SETS, "all"])
    ap.add_argument("--repeat", type=int, help="runs per prompt (default: scripted 10, benign/attack 1)")
    ap.add_argument("--only", nargs="*", help="prompt ids to run (default all in the set)")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="parallel runs, max 3 (default 1: outbound rows can only be attributed by time, "
                         "overlapping runs make that ambiguous)")
    ap.add_argument("--out", help="results JSONL (default results/<stamp>.jsonl)")
    ap.add_argument("--prompts", default=os.path.join(HERE, "prompts.yaml"))
    ap.add_argument("--ground-truth", default=os.path.join(HERE, "ground_truth.json"))
    ap.add_argument("--retries", type=int, default=2, help="retries on quota/5xx/transport errors")
    ap.add_argument("--timeout", type=float, default=300.0, help="seconds per socket read")
    ap.add_argument("--deadline", type=float, default=120.0, help="overall seconds per query (stream cut after)")
    ap.add_argument("--no-session", action="store_true", help="skip async_create_session (streamQuery creates one)")
    ap.add_argument("--no-logs", action="store_true", help="skip the Cloud Logging evidence step")
    ap.add_argument("--log-wait", type=float, default=75.0, help="seconds after the last run before reading logs "
                    "(gateway rows reach Cloud Logging ~10 s late)")
    ap.add_argument("--reread-wait", type=float, default=45.0, help="extra wait before one more log read when an "
                    "expected record is missing (0 = no second read)")
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--reattach", metavar="RESULTS_JSONL",
                    help="no new runs: read the logs again for an existing results file, regrade, write --out "
                         "(default <file>.reread.jsonl) and the report")
    a = ap.parse_args(argv)
    if a.reattach:
        return reattach(a)
    if not a.engine:
        ap.error("--engine (or $ENGINE) is required")
    try:
        engine_client.base_url(a.engine)
    except ValueError as exc:
        ap.error(str(exc))
    a.concurrency = max(1, min(a.concurrency, 3))

    with open(a.ground_truth) as f:
        gt = json.load(f)
    jobs = build_jobs(load_prompts(a.prompts), a.set, a.repeat, a.only)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = a.out or os.path.join(HERE, "results", f"{stamp}.jsonl")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    partial = out + ".partial"
    print(f"{len(jobs)} runs, concurrency {a.concurrency}, ground truth run {gt['latest_run']['run_id']} -> {out}", file=sys.stderr)
    if a.concurrency > 1 and not a.no_logs:
        print("note: concurrency > 1 -> outbound rows of overlapping runs get ambiguous attribution "
              "(a deny seen only in ambiguous rows does not pass)", file=sys.stderr)
    if a.no_logs:
        print("note: --no-logs -> checks that need a platform record (outbound deny) stay pending and do not pass",
              file=sys.stderr)

    records: list[dict] = []
    lock = threading.Lock()
    with open(partial, "w") as pf, cf.ThreadPoolExecutor(max_workers=a.concurrency) as pool:
        futs = {pool.submit(run_one, a.engine, j, stamp, a.retries, not a.no_session, a.timeout, a.deadline): j
                for j in jobs}
        for fut in cf.as_completed(futs):
            j = futs[fut]
            try:
                rec = fut.result()
            except Exception as exc:  # noqa: BLE001  local failure (e.g. ADC); recorded, not graded as a platform answer
                now = time.time()
                rec = {"set": j["set"], "prompt_id": j["spec"]["id"], "prompt": j["spec"]["text"],
                       "repeat_index": j["repeat_index"], "engine": a.engine, "events": [], "http_error": None,
                       "transport_error": f"local: {type(exc).__name__}: {exc}", "t0": now, "t1": now,
                       "session_id": None, "trace_id": None, "raw_lines": [], "earlier_attempts": [],
                       "started_at": evidence._iso(now), "ended_at": evidence._iso(now),
                       "timings": {"first_event_s": None, "total_s": None}}
            rec["grade"] = grading.grade_run(j["spec"], rec, gt)
            rec["spec"] = j["spec"]
            rec["ground_truth_run"] = gt["latest_run"]["run_id"]
            with lock:
                records.append(rec)
                pf.write(json.dumps(rec, default=str) + "\n")
                pf.flush()
            g = rec["grade"]
            failed = [c["name"] for c in g["checks"] if c["ok"] is False]
            verdict = "PASS" if g["passed"] else ("FAIL " + ",".join(failed) if failed else "PENDING " + ",".join(g["pending"]))
            if failed and g["pending"]:
                verdict += " PENDING " + ",".join(g["pending"])
            print(f"[{len(records)}/{len(jobs)}] {rec['prompt_id']}#{rec['repeat_index']} {g['outcome']:<20} {verdict}  "
                  f"first={rec['timings'].get('first_event_s') or '-'}s total={rec['timings'].get('total_s') or '-'}s", file=sys.stderr)

    records.sort(key=lambda r: (SETS.index(r["set"]), r["prompt_id"], r["repeat_index"]))
    if not a.no_logs:
        attach_evidence(records, gt, a.log_wait, a.reread_wait)
        _print_final(records)
    _write(records, out, a.no_report)
    os.remove(partial)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
