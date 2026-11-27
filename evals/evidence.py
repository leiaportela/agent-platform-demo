"""Read-only Cloud Logging evidence for remote runs: fetch outbound-gateway rows, IAP authorization decisions and
Model Armor entries for the batch window and attach each one to the run it belongs to. Field names live in
log_fields.py. Two entries.list reads per batch: (gateway rows OR IAP decisions) and Model Armor records.

Attribution (`attributed_by` on every attached row), strongest first:

  gateway rows (only rows with a gateway_name of this demo; the inbound gateway writes none)
    session_id        the request URL contains the run's session id (.../sessions/<id>...)
    trace             the row's trace id is the trace id the harness sent with the streamQuery (traceparent),
                      or a trace id of the run's session_id rows. Seen live on 2026-11-26 21:37Z: the MCP rows
                      (initialize, tools/list, tools/call) carry the sent trace id; session rows carry no trace.
    time              timestamp inside the run window [session start / t0 - 2 s, t1 + 5 s]; when the padded
                      windows of consecutive runs overlap, the run whose unpadded window holds the row wins.
                      Rows that share a trace id are kept together (they go to the runs whose window holds all
                      of them). Used for trace-carrying rows only while no row of the batch was linked by
                      trace; once one is, unlinked trace rows stay unattributed. A row whose URL names a
                      session of no run in the batch is never attributed by time.
  IAP decisions (MCP server decisions for the agent principal derived from the runs' ENGINE; Endpoint decisions
  are not read)
    principal+tool+time  "matched: agent principal + tool name + time": principalSubject == the run's agent
                      principal AND timestamp inside the run window AND mcp_tool_name is a tool the run's ADK
                      events called (function_call names). Several runs qualifying: the run whose call of that tool
                      is closest in time wins, measured to the call's span [function_call event, its
                      function_response event] (0 inside). Inside several spans: a DENIED decision goes to the call
                      whose response event is closest (the deny is the answer: 14-20 ms later in all 10 records of
                      22:13Z); an allowed decision stays attached to all of them, flagged ambiguous (its response
                      comes after the tool ran, and calls issued ~0.3 s apart reach IAP 1.2-1.9 s later, in either
                      order). No trace / session link exists on these records. Only tools/call decisions are
                      attributed; tools/list and other MCP methods are counted in query.iap_decisions only.
  Model Armor records (only labels.client_name = AGENT_GATEWAY; direct API probes are excluded)
    session_id        SANITIZE_MODEL_RESPONSE whose screened text (decoded byteData) contains the run's session id
                      (the create-session response)
    answer_text       SANITIZE_MODEL_RESPONSE whose screened text is the run's final answer
    correlation_id    same client_correlation_id as a record attributed by session_id/answer_text
                      (prompt + response of one streamQuery)
    trace             the record's trace id is the trace id the harness sent (seen live 2026-11-26 21:37Z)
    prompt_text+time  SANITIZE_USER_PROMPT whose screened text equals the run's prompt, inside the run window
    time              only for records without any screened text

A row attached to several runs carries "ambiguous": true. Rows that match no run are counted per reason in
query.unattributed (they are other traffic in the project).
"""

from __future__ import annotations

import collections
import datetime
import random
import threading
import time
from typing import Any, Callable

import log_fields as lf

ENTRIES_URL = "https://logging.googleapis.com/v2/entries:list"
MIN_INTERVAL_S = 3.0   # entries.list quota (~60/min per project) is shared with everyone
_throttle_lock = threading.Lock()
_last_call = [0.0]


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    # trim nanoseconds to microseconds
    if "." in s:
        head, rest = s.split(".", 1)
        frac = "".join(ch for ch in rest if ch.isdigit())
        tz = rest[len(frac):]
        s = f"{head}.{frac[:6]}{tz}"
    try:
        return datetime.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def build_filter(kind: str, start: float, end: float, principals: list[str] | None = None) -> str:
    """kind "gateway": outbound gateway rows, OR'ed with the IAP decisions of the agent principal(s) when given
    (one read for both; Cloud Logging's OR binds tighter than AND, every clause is parenthesized anyway)."""
    if kind == "gateway":
        base = f"(({lf.GATEWAY_FILTER}) OR ({lf.iap_filter(principals)}))" if principals else lf.GATEWAY_FILTER
    else:
        base = lf.MODEL_ARMOR_FILTER
    return f'{base} AND timestamp>="{_iso(start)}" AND timestamp<="{_iso(end)}"'


def _throttle(sleep: Callable[[float], Any], clock: Callable[[], float]) -> None:
    with _throttle_lock:
        wait = min(MIN_INTERVAL_S, _last_call[0] + MIN_INTERVAL_S - clock())
        if wait > 0:
            sleep(wait)
        _last_call[0] = clock()


def _retry_after(resp: Any, attempt: int) -> float:
    ra = str((getattr(resp, "headers", None) or {}).get("Retry-After") or "")
    if ra.isdigit():
        return min(120.0, float(ra))
    return min(60.0, 5.0 * 2 ** (attempt - 1))


def fetch_entries(flt: str, project: str = lf.PROJECT, token: str | None = None, max_pages: int = 10,
                  post: Callable[..., Any] | None = None, sleep: Callable[[float], Any] = time.sleep,
                  clock: Callable[[], float] = time.time, max_retries: int = 6) -> dict:
    """{'entries', 'pages', 'truncated', 'retries', 'error'}.

    429 / 5xx answers are retried with backoff (Retry-After honoured) and do not use up page attempts.
    Stopping early (page limit reached with a nextPageToken left, or retries exhausted) sets truncated=True
    and keeps the rows read so far. Other HTTP errors raise.
    """
    import requests

    if token is None:
        from engine_client import access_token

        token = access_token()
    post = post or requests.post
    out: list[dict] = []
    body: dict[str, Any] = {"resourceNames": [f"projects/{project}"], "filter": flt, "orderBy": "timestamp asc",
                            "pageSize": 1000}
    pages = retries = consecutive = 0
    while pages < max_pages:
        _throttle(sleep, clock)
        r = post(ENTRIES_URL, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            consecutive += 1
            retries += 1
            if consecutive > max_retries:
                return {"entries": out, "pages": pages, "truncated": True, "retries": retries,
                        "error": f"entries.list HTTP {r.status_code} still failing after {max_retries} retries"}
            sleep(_retry_after(r, consecutive) + random.random())
            continue
        consecutive = 0
        r.raise_for_status()
        data = r.json()
        pages += 1
        out += data.get("entries") or []
        if not data.get("nextPageToken"):
            return {"entries": out, "pages": pages, "truncated": False, "retries": retries, "error": None}
        body["pageToken"] = data["nextPageToken"]
    return {"entries": out, "pages": pages, "truncated": True, "retries": retries,
            "error": f"stopped after {max_pages} pages with more rows left"}


# --------------------------------------------------------------------------------------------
# attribution
# --------------------------------------------------------------------------------------------

def _norm_text(s: str) -> str:
    return " ".join(str(s).split())


def _same_answer(final: str, screened: str, min_len: int = 20, prefix: int = 200) -> bool:
    if len(final) < min_len or len(screened) < min_len:
        return False
    return final == screened or final[:prefix] in screened or screened[:prefix] in final


def _windows(runs: list[dict], pad_before: float, pad_after: float) -> list[tuple[float, float]]:
    return [(min(r.get("session_t0") or r["t0"], r["t0"]) - pad_before, r["t1"] + pad_after) for r in runs]


def _final_text(run: dict) -> str:
    return run.get("final_text") or (run.get("grade") or {}).get("final_text") or ""


def attribute(gw_rows: list[dict], ma_rows: list[dict], runs: list[dict], pad_before: float = 2.0,
              pad_after: float = 5.0) -> tuple[dict[int, list[dict]], collections.Counter]:
    """({run index: [row + attributed_by/ambiguous/candidates]}, Counter of unattributed rows by reason).

    runs need t0, t1; optional session_t0, session_id, trace_id (sent), prompt, final_text / grade.final_text.
    """
    by_run: dict[int, list[dict]] = {i: [] for i in range(len(runs))}
    unattributed: collections.Counter = collections.Counter()
    win = _windows(runs, pad_before, pad_after)
    core = _windows(runs, 0.0, 0.0)

    def in_win(row: dict) -> list[int]:
        ts = parse_ts(row.get("timestamp"))
        if ts is None:
            return []
        hits = [i for i, (a, b) in enumerate(win) if a <= ts <= b]
        if len(hits) > 1:  # padded windows of consecutive runs overlap: the unpadded window decides
            inside = [i for i in hits if core[i][0] <= ts <= core[i][1]]
            if len(inside) == 1:
                return inside
        return hits

    def attach(row: dict, idx: list[int] | set[int], how: str) -> None:
        idx = sorted(idx)
        for i in idx:
            by_run[i].append({**row, "attributed_by": how, "ambiguous": len(idx) > 1, "candidates": len(idx)})

    sid_runs: dict[str, list[int]] = {}
    for i, r in enumerate(runs):
        if r.get("session_id"):
            sid_runs.setdefault(str(r["session_id"]), []).append(i)

    # ---- gateway rows
    trace_runs: dict[str, set[int]] = {}
    for i, r in enumerate(runs):
        if r.get("trace_id"):
            trace_runs.setdefault(str(r["trace_id"]).lower(), set()).add(i)
    pending: list[dict] = []
    for row in gw_rows:
        sid = row.get("session_id")
        if not sid:
            pending.append(row)
            continue
        if sid in sid_runs:
            attach(row, sid_runs[sid], "session_id")
            if row.get("trace_id"):
                trace_runs.setdefault(row["trace_id"], set()).update(sid_runs[sid])
            continue
        unknown = [i for i in in_win(row) if not runs[i].get("session_id")]  # --no-session runs
        if unknown:
            attach(row, unknown, "time")
        else:
            unattributed["gateway: URL names a session of no run in this batch"] += 1
    linked = [r for r in pending if r.get("trace_id") in trace_runs]
    for row in linked:
        attach(row, trace_runs[row["trace_id"]], "trace")
    groups: dict[str, list[dict]] = {}
    for row in pending:
        tid = row.get("trace_id")
        if tid in trace_runs:
            continue
        if tid:
            if linked:
                unattributed["gateway: trace id not linked to any run"] += 1
            else:
                groups.setdefault(tid, []).append(row)
            continue
        idx = in_win(row)
        if idx:
            attach(row, idx, "time")
        else:
            unattributed["gateway: outside every run window"] += 1
    for rows in groups.values():
        sets = [set(in_win(r)) for r in rows]
        common = set.intersection(*sets)
        for row, s in zip(rows, sets):
            idx = common or s
            if idx:
                attach(row, idx, "time")
            else:
                unattributed["gateway: outside every run window"] += 1

    # ---- Model Armor records
    finals = [_norm_text(_final_text(r)) for r in runs]
    prompts = [_norm_text(r.get("prompt") or "") for r in runs]
    corr_runs: dict[str, set[int]] = {}
    rest: list[dict] = []
    for row in ma_rows:
        txt = row.get("input_text")
        if txt and row.get("operation") == "SANITIZE_MODEL_RESPONSE":
            idx = [i for i, r in enumerate(runs) if r.get("session_id") and str(r["session_id"]) in txt]
            how = "session_id"
            if not idx:
                nt = _norm_text(txt)
                idx = [i for i, f in enumerate(finals) if _same_answer(f, nt)]
                how = "answer_text"
            if idx:
                attach(row, idx, how)
                if row.get("correlation_id"):
                    corr_runs.setdefault(str(row["correlation_id"]), set()).update(idx)
                continue
        rest.append(row)
    sent = {str(r["trace_id"]).lower(): i for i, r in enumerate(runs) if r.get("trace_id")}
    for row in rest:
        corr = str(row.get("correlation_id") or "")
        txt = row.get("input_text")
        if corr and corr in corr_runs:
            attach(row, corr_runs[corr], "correlation_id")
        elif row.get("trace_id") in sent:
            attach(row, [sent[row["trace_id"]]], "trace")
        elif txt is None:
            idx = in_win(row)
            if idx:
                attach(row, idx, "time")
            else:
                unattributed["model_armor: no screened text, outside every run window"] += 1
        elif row.get("operation") == "SANITIZE_USER_PROMPT":
            nt = _norm_text(txt)
            idx = [i for i in in_win(row) if prompts[i] and prompts[i] == nt]
            if idx:
                attach(row, idx, "prompt_text+time")
            else:
                unattributed["model_armor: prompt text of no run in its window"] += 1
        else:
            unattributed["model_armor: response text matches no run"] += 1
    return by_run, unattributed


def _event_ts(event: dict) -> float | None:
    ts = event.get("timestamp")
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    return parse_ts(ts) if isinstance(ts, str) else None


def tool_calls(run: dict) -> list[dict]:
    """[{name, id, t_call, t_response}] from the run's ADK events: each function_call with the timestamp of its
    event and of the event carrying its function_response (paired by id, else by name in order). Partial events
    are skipped; a missing timestamp stays None."""
    out: list[dict] = []
    for e in run.get("events") or []:
        if not isinstance(e, dict) or e.get("partial") is True:
            continue
        ts = _event_ts(e)
        content = e.get("content") or {}
        for p in (content.get("parts") or []) if isinstance(content, dict) else []:
            if not isinstance(p, dict):
                continue
            fc = p.get("function_call") or p.get("functionCall")
            fr = p.get("function_response") or p.get("functionResponse")
            if fc:
                out.append({"name": fc.get("name"), "id": fc.get("id"), "t_call": ts, "t_response": None, "_open": True})
            elif fr:
                c = next((c for c in out if c["_open"] and c["name"] == fr.get("name")
                          and (not fr.get("id") or not c["id"] or c["id"] == fr.get("id"))), None)
                if c:
                    c["t_response"], c["_open"] = ts, False
    return [{k: v for k, v in c.items() if k != "_open"} for c in out]


_FAR = (float("inf"), float("inf"))


def _call_distance(ts: float, calls: list[dict], to_response: bool) -> tuple[float, float]:
    """(distance from ts to the nearest call span [t_call, t_response], tie-breaker). The tie-breaker is the distance
    to that call's response event when `to_response` (a denied call is answered by the deny itself: in the 22:13Z
    records the function_response event follows the IAP decision by 14-20 ms), else 0 (an allowed call's response
    comes after the tool ran, so its time says nothing about which decision was this call's)."""
    best = _FAR
    for c in calls:
        if c.get("t_call") is None:
            continue
        a = c["t_call"]
        b = c["t_response"] if c.get("t_response") is not None else a
        d = 0.0 if a <= ts <= b else min(abs(ts - a), abs(ts - b))
        best = min(best, (d, abs(ts - b) if to_response else 0.0))
    return best


def attribute_iap(iap_rows: list[dict], runs: list[dict], principals: list[str] | None = None,
                  pad_before: float = 2.0, pad_after: float = 5.0
                  ) -> tuple[dict[int, list[dict]], collections.Counter, dict]:
    """IAP decisions -> runs, "matched: agent principal + tool name + time" (see the module docstring).

    Returns ({run index: [row + attributed_by/ambiguous/candidates/nearest_call_s]}, Counter of unattributed
    tools/call decisions by reason, counts of the decisions read)."""
    by_run: dict[int, list[dict]] = {i: [] for i in range(len(runs))}
    unattributed: collections.Counter = collections.Counter()
    stats: collections.Counter = collections.Counter()
    default = principals[0] if principals and len(principals) == 1 else None
    run_principal = [lf.agent_principal(r.get("engine")) or default for r in runs]
    win = _windows(runs, pad_before, pad_after)
    calls = [tool_calls(r) for r in runs]
    for row in iap_rows:
        if row.get("resource_type") != lf.IAP_MCP_RESOURCE_TYPE:
            stats["not an MCP server decision (excluded)"] += 1
            continue
        if not lf.is_iap_mcp_tools_call(row):
            stats[f"MCP {row.get('mcp_method') or 'method not in record'} (handshake/other; counted, not shown)"] += 1
            continue
        stats[f"tools/call {row.get('decision') or 'granted not in record'}"] += 1
        ts = parse_ts(row.get("timestamp"))
        tool = row["mcp_tool"]
        mine = [i for i, p in enumerate(run_principal) if p and p == row.get("principal")]
        if not mine:
            unattributed["iap: principal of no run in this batch"] += 1
            continue
        inside = [i for i in mine if ts is not None and win[i][0] <= ts <= win[i][1]]
        if not inside:
            unattributed["iap: outside every run window"] += 1
            continue
        cand = [i for i in inside if any(c["name"] == tool for c in calls[i])]
        if not cand:
            unattributed["iap: no run in its window called the tool"] += 1
            continue
        keys = {i: _call_distance(ts, [c for c in calls[i] if c["name"] == tool], row.get("decision") == "denied")
                for i in cand}
        best = min(keys.values())
        idx = [i for i in cand if keys[i] == best]
        near = None if best[0] == float("inf") else round(best[0], 3)
        for i in idx:
            by_run[i].append({**row, "attributed_by": "principal+tool+time", "ambiguous": len(idx) > 1,
                              "candidates": len(cand), "nearest_call_s": near})
    return by_run, unattributed, dict(stats)


# --------------------------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------------------------

def _gw_item(r: dict) -> dict:
    return {"timestamp": r.get("timestamp"), "gateway": r.get("gateway"), "http_status": r.get("status"),
            "authz_result": r.get("authz_result"), "mcp_method": r.get("mcp_method"), "tool": r.get("mcp_tool"),
            "request_method": r.get("method"), "url": r.get("url"), "policies": r.get("policies"),
            "insert_id": r.get("insert_id"), "logs_link": r.get("logs_link"), "trace_id": r.get("trace_id"),
            "trace_link": r.get("trace_link"), "attributed_by": r.get("attributed_by"), "ambiguous": r.get("ambiguous")}


def _ma_item(r: dict) -> dict:
    return {"timestamp": r.get("timestamp"), "operation": r.get("operation"), "match_state": r.get("match_state"),
            "filters": r.get("matched_filters"), "filter_details": r.get("filters"), "info_types": r.get("sdp_info_types"),
            "verdict": r.get("verdict"), "verdict_reason": r.get("verdict_reason"), "template": r.get("template"),
            "correlation_id": r.get("correlation_id"), "insert_id": r.get("insert_id"), "logs_link": r.get("logs_link"),
            "attributed_by": r.get("attributed_by"), "ambiguous": r.get("ambiguous")}


def _iap_item(r: dict) -> dict:
    return {"timestamp": r.get("timestamp"), "source": lf.IAP_SOURCE_LABEL, "mcp_method": r.get("mcp_method"),
            "tool": r.get("mcp_tool"), "granted": r.get("granted"), "granted_in_record": r.get("granted_in_record"),
            "status_code": r.get("status_code"), "status_message": r.get("status_message"),
            "decision": r.get("decision"), "decision_text": lf.iap_decision_text(r), "permission": r.get("permission"),
            "principal": r.get("principal"), "policy_version": r.get("policy_version"), "dry_run": r.get("dry_run"),
            "resource_name": r.get("resource_name"), "url": r.get("url"), "insert_id": r.get("insert_id"),
            "logs_link": r.get("logs_link"), "attributed_by": r.get("attributed_by"),
            "attribution_label": lf.IAP_MATCH_LABEL if r.get("attributed_by") == "principal+tool+time" else None,
            "ambiguous": r.get("ambiguous"), "candidates": r.get("candidates"), "nearest_call_s": r.get("nearest_call_s")}


def summarize(rows: list[dict]) -> dict:
    """Report-friendly summary of a run's evidence: only fields copied from the records (raw rows stay in the record)."""
    gw = [r for r in rows if r["kind"] == "gateway"]
    visible = [r for r in gw if not lf.is_hidden(r)]
    ma = [r for r in rows if r["kind"] == "model_armor"]
    iap = [r for r in rows if r["kind"] == "iap_decision" and lf.is_iap_mcp_tools_call(r)]
    calls = [r for r in visible if r.get("mcp_method") == "tools/call" and r.get("direction") == "egress"]
    return {
        "iap_decisions": [_iap_item(r) for r in iap],
        "iap_tool_denies": sorted({r["mcp_tool"] for r in iap if r.get("decision") == "denied"}),
        "iap_tools_granted": sorted({r["mcp_tool"] for r in iap if r.get("decision") == "granted"}),
        "gateway_rows": len(visible),
        "hidden_rows": len(gw) - len(visible),
        "outbound_tool_calls": [_gw_item(r) for r in calls],
        "gateway_denied": [_gw_item(r) for r in visible if lf.is_denied(r)],
        "tool_denies": sorted({r["mcp_tool"] for r in calls if lf.is_denied(r) and r.get("mcp_tool")}),
        "gateway_tools_allowed": sorted({r["mcp_tool"] for r in calls
                                         if str(r.get("authz_result") or "").upper() == "ALLOWED" and r.get("mcp_tool")}),
        "session_rows": sum(1 for r in visible if r.get("attributed_by") == "session_id"),
        "model_armor_rows": len(ma),
        "model_armor": [_ma_item(r) for r in ma],
        "model_armor_matches": [_ma_item(r) for r in ma if r.get("match_state") == "MATCH_FOUND"],
        "trace_ids": sorted({r["trace_id"] for r in calls if r.get("trace_id")}),
        "attributed_by": dict(collections.Counter(r.get("attributed_by") for r in visible + ma + iap)),
        "any_ambiguous": any(r.get("ambiguous") for r in visible + ma + iap),
    }


def tool_deny_rows(summary: dict | None, tool: str) -> list[dict]:
    """Attributed outbound rows that confirm a deny of `tool`: authz DENIED + tools/call + parameter == tool."""
    return [d for d in (summary or {}).get("outbound_tool_calls", [])
            if str(d.get("authz_result") or "").upper() == "DENIED" and d.get("mcp_method") == "tools/call"
            and d.get("tool") == tool]


def iap_deny_records(summary: dict | None, tool: str) -> list[dict]:
    """Attributed IAP decisions that confirm a deny of `tool`: granted false (or absent with status 7), MCP
    tools/call of the tool, not a dry run (attribution already required the agent principal)."""
    return [d for d in (summary or {}).get("iap_decisions", [])
            if d.get("decision") == "denied" and d.get("mcp_method") == "tools/call" and d.get("tool") == tool
            and d.get("dry_run") is not True and d.get("attributed_by") == "principal+tool+time"]


def collect(runs: list[dict], fetch: Callable[[str], Any] | None = None,
            principals: list[str] | None = None) -> list[dict]:
    """Evidence per run (same order as runs): {'rows': [...], 'summary': {...}, 'query': {...}}.

    principals: agent principals whose IAP decisions are read (default: derived from the runs' `engine`)."""
    if not runs:
        return []
    fetch = fetch or fetch_entries
    if principals is None:
        principals = sorted({p for p in (lf.agent_principal(r.get("engine")) for r in runs) if p})
    start = min(min(r.get("session_t0") or r["t0"], r["t0"]) for r in runs) - 5
    end = max(r["t1"] for r in runs) + 10
    gw: list[dict] = []
    ma: list[dict] = []
    iap: list[dict] = []
    query: dict[str, Any] = {"filters": {}, "fetched_rows": {}, "pages": {}, "truncated": {}, "errors": [],
                             "excluded": collections.Counter(), "read_at": _iso(time.time()),
                             "iap_principals": principals, "notes": []}
    if not principals:
        query["notes"].append("IAP decisions not read: no agent principal (no run has an ENGINE with a project number)")
    for kind in ("gateway", "model_armor"):
        flt = build_filter(kind, start, end, principals if kind == "gateway" else None)
        query["filters"][kind] = flt
        try:
            res = fetch(flt)
        except Exception as exc:  # noqa: BLE001  evidence is best effort; the error is recorded
            query["errors"].append(f"{kind}: {type(exc).__name__}: {exc}")
            query["truncated"][kind] = True
            continue
        if isinstance(res, list):
            res = {"entries": res, "pages": None, "truncated": False, "error": None}
        if res.get("error"):
            query["errors"].append(f"{kind}: {res['error']}")
        query["truncated"][kind] = bool(res.get("truncated"))
        query["pages"][kind] = res.get("pages")
        entries = res.get("entries") or []
        query["fetched_rows"][kind] = len(entries)
        for e in entries:
            row = lf.normalize(e)
            if kind == "gateway":
                if row["kind"] == "iap_decision":
                    iap.append(row)
                    continue
                if row["kind"] != "gateway":
                    continue
                if not row.get("gateway"):
                    query["excluded"]["gateway row without gateway_name"] += 1
                elif row["gateway"] not in lf.GATEWAYS:
                    query["excluded"]["row of another gateway"] += 1
                else:
                    gw.append(row)
            elif row["kind"] == "model_armor":
                if row.get("client") != lf.GATEWAY_CLIENT:
                    query["excluded"]["Model Armor record without client_name AGENT_GATEWAY"] += 1
                else:
                    ma.append(row)
    if principals:  # the gateway read count includes the IAP decisions it returned
        query["fetched_rows"]["iap_decision"] = len(iap)
        if "gateway" in query["fetched_rows"]:
            query["fetched_rows"]["gateway"] -= len(iap)
    by_run, unattributed = attribute(gw, ma, runs)
    iap_by_run, iap_unattributed, query["iap_decisions"] = attribute_iap(iap, runs, principals)
    for i, rows in iap_by_run.items():
        by_run[i] += rows
    unattributed.update(iap_unattributed)
    query["excluded"] = dict(query["excluded"])
    query["unattributed"] = dict(unattributed)
    query["incomplete"] = bool(query["errors"]) or any(query["truncated"].values())
    return [{"rows": by_run[i], "summary": summarize(by_run[i]), "query": query} for i in range(len(runs))]
