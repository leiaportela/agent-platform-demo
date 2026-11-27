"""THE ONLY place that knows Cloud Logging field names (Agent Gateway + IAP decisions + Model Armor).

Checked on 2026-11-26 against real entries of this project (copies in tests/samples/real_*.json, taken
from demo/evidence/raw-run/*.logs.json):

* Only the OUTBOUND gateway (resource.labels.gateway_name = agent-demo-egress-gw) writes request rows
  (log networkservices.googleapis.com%2Fgateway_requests). The inbound gateway writes none.
* Outbound row: httpRequest.status (int), httpRequest.requestUrl, httpRequest.requestMethod,
  jsonPayload.authzPolicyInfo.result (ALLOWED / DENIED) + .policies[].name,
  jsonPayload.agentGatewayInfo.mcpInfo.method / .parameter (tool name), top-level `trace` = bare
  32-hex id, `spanId`. CONNECT tunnel rows have no requestUrl and no authz result (hidden).
* The agent's session calls are outbound rows to .../reasoningEngines/<ENGINE>/sessions/<SESSION_ID>...
  and carry NO `trace` in the recorded entries; MCP rows (initialize, tools/list, tools/call) of one
  agent invocation share one trace id.
* Model Armor: log modelarmor.googleapis.com%2Fsanitize_operations, labels."modelarmor.googleapis.com/
  client_name" = AGENT_GATEWAY for gateway screening (direct API calls have no client_name),
  labels."modelarmor.googleapis.com/client_correlation_id" is shared by the SANITIZE_USER_PROMPT and the
  SANITIZE_MODEL_RESPONSE record of one streamQuery request, jsonPayload.sanitizationInput.byteItem.byteData
  = base64 of the screened text (prompt: the user message; response: the answer text, or the JSON of the
  create-session response, which contains the session id). Direct API calls log sanitizationInput.text.
* IAP authorization decisions of the outbound gateway (copies in tests/samples/real_iap_*.json, taken from
  demo/evidence/raw-run/iap-decisions.logs.json and the 22:13Z read): log cloudaudit.googleapis.com%2Fdata_access,
  protoPayload.serviceName = iap.googleapis.com, protoPayload.methodName = GatekeeperAuthorizer.AuthorizeUser,
  resource.type = audited_resource, labels."iap.googleapis.com/audited_resource_type" =
  agentregistry.googleapis.com/McpServer (MCP servers; .../Endpoint = Google API endpoints, not used here),
  protoPayload.authenticationInfo.principalSubject = the agent's principal, protoPayload.authorizationInfo[0]
  .granted (true/false; proto JSON may omit a false boolean) + .permission, protoPayload.status
  {code 7, "Permission Denied."} on denies, protoPayload.metadata.mcp_attributes.{mcp_method, mcp_tool_name,
  base_protocol_method}, .policy_version, .dryRun, protoPayload.request.httpRequest.url. No trace field.
  In a 10-run eval (2026-11-26 22:13Z) every denied tools/call had one of these records; the gateway request
  row was written for 3 of the 10.

Lookups are case- and underscore-insensitive (authzPolicyInfo == authz_policy_info). Every normalized
row keeps the raw entry under "raw".
"""

from __future__ import annotations

import base64
import binascii
import datetime
import os
import re
import urllib.parse
from typing import Any



def _env(*names: str) -> str:
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
    raise RuntimeError(f"{names[0]} is not set: run `source infra/env.sh` first (it holds the project's values)")


PROJECT = _env("PROJ_ID", "PROJECT")
EGRESS_GW = "agent-demo-egress-gw"
INGRESS_GW = "agent-demo-ingress-gw"
GATEWAYS = (INGRESS_GW, EGRESS_GW)
GATEWAY_CLIENT = "AGENT_GATEWAY"

# the agent identity of an Agent Runtime engine: org from env.sh, project number / region / engine id from ENGINE
AGENT_ORG = _env("ORG_ID", "AGENT_ORG")
IAP_SERVICE = "iap.googleapis.com"
IAP_METHOD = "GatekeeperAuthorizer.AuthorizeUser"
IAP_MCP_RESOURCE_TYPE = "agentregistry.googleapis.com/McpServer"
IAP_SOURCE_LABEL = "Outbound gateway · IAP decision"
IAP_MATCH_LABEL = "matched: agent principal + tool name + time"

GATEWAY_LOG = f"projects/{PROJECT}/logs/networkservices.googleapis.com%2Fgateway_requests"
IAP_LOG = f"projects/{PROJECT}/logs/cloudaudit.googleapis.com%2Fdata_access"
MODEL_ARMOR_LOG = f"projects/{PROJECT}/logs/modelarmor.googleapis.com%2Fsanitize_operations"
GATEWAY_FILTER = (f'logName="{GATEWAY_LOG}" AND resource.type="networkservices.googleapis.com/Gateway" AND '
                  f'resource.labels.gateway_name=("{EGRESS_GW}" OR "{INGRESS_GW}")')
MODEL_ARMOR_FILTER = (f'logName="{MODEL_ARMOR_LOG}" AND '
                      f'labels."modelarmor.googleapis.com/client_name"="{GATEWAY_CLIENT}"')

_ENGINE_RE = re.compile(r"^projects/(\d+)/locations/([a-z0-9-]+)/reasoningEngines/(\d+)$")


def agent_principal(engine: str | None) -> str | None:
    """principal://agents.global.org-<ORG>.system.id.goog/resources/aiplatform/projects/<NUM>/locations/<L>/
    reasoningEngines/<ID> from ENGINE = projects/<NUM>/locations/<L>/reasoningEngines/<ID>. None when ENGINE does
    not name the project by number (the principal uses the number)."""
    m = _ENGINE_RE.match(str(engine or "").strip())
    if not m:
        return None
    num, loc, eid = m.groups()
    return (f"principal://agents.global.org-{AGENT_ORG}.system.id.goog/resources/aiplatform/projects/{num}"
            f"/locations/{loc}/reasoningEngines/{eid}")


def iap_filter(principals: list[str] | tuple[str, ...] | str) -> str:
    """IAP authorization decisions on MCP servers made for the agent principal(s)."""
    ps = [principals] if isinstance(principals, str) else sorted(set(principals))
    who = f'"{ps[0]}"' if len(ps) == 1 else "(" + " OR ".join(f'"{p}"' for p in ps) + ")"
    return (f'logName="{IAP_LOG}" AND protoPayload.serviceName="{IAP_SERVICE}" AND '
            f'protoPayload.methodName="{IAP_METHOD}" AND '
            f'labels."iap.googleapis.com/audited_resource_type"="{IAP_MCP_RESOURCE_TYPE}" AND '
            f'protoPayload.authenticationInfo.principalSubject={who}')


PATHS = {
    # Agent Gateway request rows
    "gw.name": ["resource.labels.gateway_name", "labels.gateway_name", "jsonPayload.gatewayName"],
    "gw.status": ["httpRequest.status"],
    "gw.url": ["httpRequest.requestUrl"],
    "gw.method": ["httpRequest.requestMethod"],
    "gw.latency": ["httpRequest.latency"],
    "gw.authz_result": ["jsonPayload.authzPolicyInfo.result"],
    "gw.policies": ["jsonPayload.authzPolicyInfo.policies"],
    "gw.mcp_method": ["jsonPayload.agentGatewayInfo.mcpInfo.method"],
    "gw.mcp_tool": ["jsonPayload.agentGatewayInfo.mcpInfo.parameter"],
    # IAP authorization decisions (cloudaudit data_access)
    "iap.service": ["protoPayload.serviceName"],
    "iap.method": ["protoPayload.methodName"],
    "iap.resource_type": ["labels.iap.googleapis.com/audited_resource_type"],
    "iap.resource_name": ["labels.iap.googleapis.com/audited_resource_name"],
    "iap.principal": ["protoPayload.authenticationInfo.principalSubject"],
    "iap.authz_info": ["protoPayload.authorizationInfo"],
    "iap.status_code": ["protoPayload.status.code"],
    "iap.status_message": ["protoPayload.status.message"],
    "iap.mcp_method": ["protoPayload.metadata.mcp_attributes.mcp_method"],
    "iap.mcp_tool": ["protoPayload.metadata.mcp_attributes.mcp_tool_name"],
    "iap.base_protocol": ["protoPayload.metadata.mcp_attributes.base_protocol_method"],
    "iap.policy_version": ["protoPayload.metadata.policy_version"],
    "iap.dry_run": ["protoPayload.metadata.dryRun"],
    "iap.url": ["protoPayload.request.httpRequest.url"],
    # Model Armor sanitize operations
    "ma.operation": ["jsonPayload.operationType"],
    "ma.match_state": ["jsonPayload.sanitizationResult.filterMatchState"],
    "ma.filter_results": ["jsonPayload.sanitizationResult.filterResults"],
    "ma.invocation": ["jsonPayload.sanitizationResult.invocationResult"],
    "ma.verdict": ["jsonPayload.sanitizationResult.sanitizationVerdict"],
    "ma.verdict_reason": ["jsonPayload.sanitizationResult.sanitizationVerdictReason"],
    "ma.sdp_findings": ["jsonPayload.sanitizationResult.filterResults.sdp.sdpFilterResult.inspectResult.findings"],
    "ma.client": ["labels.modelarmor.googleapis.com/client_name"],
    "ma.correlation_id": ["labels.modelarmor.googleapis.com/client_correlation_id"],
    "ma.template": ["resource.labels.template_id", "jsonPayload.template", "jsonPayload.templateName"],
    "ma.input_bytes": ["jsonPayload.sanitizationInput.byteItem.byteData"],
    "ma.input_text": ["jsonPayload.sanitizationInput.text"],
    # common
    "trace": ["trace"],
    "span": ["spanId"],
    "insert_id": ["insertId"],
    "timestamp": ["timestamp"],
    "receive_timestamp": ["receiveTimestamp"],
    "severity": ["severity"],
}

_TRACE_RE = re.compile(r"traces/([0-9a-fA-F]+)")
_SESSION_RE = re.compile(r"/reasoningEngines/[^/]+/sessions/([0-9A-Za-z_-]+)")


def _k(s: str) -> str:
    return s.replace("_", "").lower()


def dig(obj: Any, path: str) -> Any:
    """Value at a dotted path, matching keys case/underscore-insensitively; labels keys may contain dots."""
    cur = obj
    parts = path.split(".")
    i = 0
    while i < len(parts):
        if not isinstance(cur, dict):
            return None
        found = False
        # longest dotted key first, for labels like "modelarmor.googleapis.com/client_name"
        for j in range(len(parts), i, -1):
            want = _k(".".join(parts[i:j]))
            for key, val in cur.items():
                if _k(key) == want:
                    cur, i, found = val, j, True
                    break
            if found:
                break
        if not found:
            return None
    return cur


def get(entry: dict, field: str) -> Any:
    for p in PATHS[field]:
        v = dig(entry, p)
        if v not in (None, "", [], {}):
            return v
    return None


def trace_id(entry: dict) -> str | None:
    t = get(entry, "trace")
    if not t:
        return None
    m = _TRACE_RE.search(str(t))
    return (m.group(1) if m else str(t)).lower()


def session_id_in_url(url: str | None) -> str | None:
    m = _SESSION_RE.search(url or "")
    return m.group(1) if m else None


def _iso_shift(ts: str, seconds: int) -> str | None:
    try:
        base = datetime.datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (base + datetime.timedelta(seconds=seconds)).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def logs_link(insert_id: str | None, project: str = PROJECT, timestamp: str | None = None) -> str | None:
    """Logs Explorer link to one entry. With the entry's timestamp the time range is pinned around it (a
    relative range such as 'last hour' stops finding the entry later); otherwise the last 30 days."""
    if not insert_id:
        return None
    q = urllib.parse.quote(f'insertId="{insert_id}"', safe="")
    start, end = (_iso_shift(timestamp, -3600), _iso_shift(timestamp, 3600)) if timestamp else (None, None)
    rng = f";startTime={start};endTime={end}" if start and end else ";timeRange=P30D"
    return f"https://console.cloud.google.com/logs/query;query={q}{rng}?project={project}"


def trace_link(tid: str | None, project: str = PROJECT) -> str | None:
    return f"https://console.cloud.google.com/traces/explorer;traceId={tid}?project={project}" if tid else None


GATEWAYS_PAGE = f"https://console.cloud.google.com/agent-platform/gateways?project={PROJECT}"


def _status(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def gateway_row(entry: dict) -> dict:
    name = get(entry, "gw.name")
    policies = get(entry, "gw.policies") or []
    pol_names = [p.get("name") if isinstance(p, dict) else str(p) for p in policies] if isinstance(policies, list) else [str(policies)]
    tid = trace_id(entry)
    iid = get(entry, "insert_id")
    url = get(entry, "gw.url")
    direction = "egress" if name == EGRESS_GW else "ingress" if name == INGRESS_GW else None
    return {
        "kind": "gateway", "insert_id": iid, "timestamp": get(entry, "timestamp"),
        "gateway": name, "direction": direction, "status": _status(get(entry, "gw.status")),
        "url": url, "method": get(entry, "gw.method"), "latency": get(entry, "gw.latency"),
        "authz_result": (get(entry, "gw.authz_result") or None), "policies": pol_names,
        "mcp_method": get(entry, "gw.mcp_method"), "mcp_tool": get(entry, "gw.mcp_tool"),
        "session_id": session_id_in_url(url),
        "trace_id": tid, "logs_link": logs_link(iid, timestamp=get(entry, "timestamp")), "trace_link": trace_link(tid),
        "raw": entry,
    }


def is_hidden(row: dict) -> bool:
    """Gateway rows with nothing to show: CONNECT tunnels / rows with neither a URL nor an authz result."""
    if row.get("kind") != "gateway":
        return False
    return str(row.get("method") or "").upper() == "CONNECT" or (not row.get("url") and not row.get("authz_result"))


def is_denied(row: dict) -> bool:
    """authzPolicyInfo.result says DENIED (the HTTP status is a separate field, never used in its place)."""
    return row.get("kind") == "gateway" and str(row.get("authz_result") or "").upper() == "DENIED"


def is_tool_deny(row: dict, tool: str) -> bool:
    """Outbound row with authz DENIED for `tools/call <tool>`: the only record that confirms a tool deny."""
    return (is_denied(row) and row.get("direction") == "egress" and row.get("mcp_method") == "tools/call"
            and row.get("mcp_tool") == tool)


def _filter_details(results: Any) -> tuple[list[str], dict]:
    """(filters with a MATCH_FOUND anywhere inside, {filter: "MATCH_FOUND (harassment MEDIUM_AND_ABOVE)"|state})."""
    items: list[tuple[str, Any]] = []
    if isinstance(results, dict):
        items = list(results.items())
    elif isinstance(results, list):  # [{"key": "sdp", "value": {...}}] style
        for it in results:
            if isinstance(it, dict):
                items.append((str(it.get("key") or it.get("name") or "?"), it.get("value", it)))

    def walk(o: Any, path: str, out: list) -> None:
        if isinstance(o, dict):
            st = next((v for k, v in o.items() if _k(k) == "matchstate" and isinstance(v, str)), None)
            if st:
                conf = next((v for k, v in o.items() if _k(k) == "confidencelevel" and isinstance(v, str)), None)
                out.append((path, st, conf))
            for k, v in o.items():
                if isinstance(v, (dict, list)):
                    walk(v, k, out)
        elif isinstance(o, list):
            for v in o:
                walk(v, path, out)

    matched, details = [], {}
    for name, val in items:
        found: list[tuple[str, str, str | None]] = []
        walk(val, name, found)
        states = [s for _, s, _ in found]
        if "MATCH_FOUND" in states:
            matched.append(name)
            # sub-filters (e.g. rai harassment), not the filter's own result wrapper
            subs = [f"{p}{' ' + c if c else ''}" for p, s, c in found
                    if s == "MATCH_FOUND" and _k(p) != _k(name) and not _k(p).endswith("filterresult")
                    and _k(p) != "inspectresult"]
            conf = next((c for _, s, c in found if s == "MATCH_FOUND" and c), None)
            details[name] = "MATCH_FOUND" + (f" ({', '.join(subs)})" if subs else (f" ({conf})" if conf else ""))
        else:
            details[name] = states[0] if states else None
    return matched, details


def decode_input(entry: dict) -> str | None:
    """The screened text: base64 byteData (gateway screening) or plain sanitizationInput.text (direct API)."""
    raw = get(entry, "ma.input_bytes")
    if raw:
        try:
            return base64.b64decode(str(raw), validate=False).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return None
    txt = get(entry, "ma.input_text")
    return str(txt) if txt else None


def model_armor_row(entry: dict) -> dict:
    matched, details = _filter_details(get(entry, "ma.filter_results"))
    tid = trace_id(entry)
    iid = get(entry, "insert_id")
    return {
        "kind": "model_armor", "insert_id": iid, "timestamp": get(entry, "timestamp"),
        "receive_timestamp": get(entry, "receive_timestamp"),
        "operation": get(entry, "ma.operation"), "match_state": get(entry, "ma.match_state"),
        "matched_filters": matched, "filters": details, "invocation": get(entry, "ma.invocation"),
        "verdict": get(entry, "ma.verdict"), "verdict_reason": get(entry, "ma.verdict_reason"),
        "sdp_info_types": sorted({f.get("infoType") or f.get("info_type") for f in (get(entry, "ma.sdp_findings") or [])
                                  if isinstance(f, dict) and (f.get("infoType") or f.get("info_type"))}),
        "client": get(entry, "ma.client"), "correlation_id": get(entry, "ma.correlation_id"),
        "template": get(entry, "ma.template"),
        "input_text": decode_input(entry),
        "trace_id": tid, "logs_link": logs_link(iid, timestamp=get(entry, "timestamp")), "trace_link": trace_link(tid),
        "raw": entry,
    }


def iap_row(entry: dict) -> dict:
    """One IAP authorization decision. `granted` is the recorded authorizationInfo[0].granted (None = the field is
    not in the record: proto JSON can omit a false boolean). `decision`: "denied" if granted is false, or absent
    with status.code 7; "granted" if granted is true; None otherwise ("granted not in record")."""
    infos = get(entry, "iap.authz_info")
    info = infos[0] if isinstance(infos, list) and infos and isinstance(infos[0], dict) else {}
    granted_key = next((k for k in info if _k(k) == "granted"), None)
    granted = info.get(granted_key) if granted_key else None
    granted = granted if isinstance(granted, bool) else None
    code = _status(get(entry, "iap.status_code"))
    decision = ("granted" if granted is True else "denied" if granted is False or (granted is None and code == 7)
                else None)
    iid = get(entry, "insert_id")
    base = get(entry, "iap.base_protocol")
    dry = get(entry, "iap.dry_run")
    return {
        "kind": "iap_decision", "insert_id": iid, "timestamp": get(entry, "timestamp"),
        "receive_timestamp": get(entry, "receive_timestamp"),
        "service": get(entry, "iap.service"), "method_name": get(entry, "iap.method"),
        "resource_type": get(entry, "iap.resource_type"), "resource_name": get(entry, "iap.resource_name"),
        "principal": get(entry, "iap.principal"),
        "granted": granted, "granted_in_record": granted is not None,
        "permission": info.get("permission"),
        "status_code": code, "status_message": get(entry, "iap.status_message"),
        "decision": decision,
        "mcp_method": get(entry, "iap.mcp_method"), "mcp_tool": get(entry, "iap.mcp_tool"),
        "base_protocol_method": base if isinstance(base, bool) else None,
        "policy_version": get(entry, "iap.policy_version"), "dry_run": dry if isinstance(dry, bool) else None,
        "url": get(entry, "iap.url"),
        "trace_id": None, "logs_link": logs_link(iid, timestamp=get(entry, "timestamp")), "trace_link": None,
        "raw": entry,
    }


def is_iap_entry(entry: dict) -> bool:
    log_name = str(entry.get("logName") or "")
    return "cloudaudit.googleapis.com" in log_name and dig(entry, "protoPayload.serviceName") == IAP_SERVICE


def is_iap_mcp_tools_call(row: dict) -> bool:
    """An IAP decision on an MCP server for an MCP tools/call (the only kind shown / attributed)."""
    return (row.get("kind") == "iap_decision" and row.get("resource_type") == IAP_MCP_RESOURCE_TYPE
            and row.get("mcp_method") == "tools/call" and bool(row.get("mcp_tool")))


def is_iap_tool_deny(row: dict, tool: str, principal: str | None = None) -> bool:
    """IAP decision that denies `tools/call <tool>`: granted false (or absent with status 7) on an MCP server, not
    a dry run, for the given agent principal when one is given."""
    return (is_iap_mcp_tools_call(row) and row.get("decision") == "denied" and row.get("mcp_tool") == tool
            and row.get("dry_run") is not True and (principal is None or row.get("principal") == principal))


def iap_decision_text(row: dict) -> str:
    """'granted: false · status 7 Permission Denied' / 'granted: true', from the record's own fields."""
    g = row.get("granted")
    head = "granted not in record" if g is None else f"granted: {'true' if g else 'false'}"
    if row.get("status_code") not in (None, 0):
        msg = str(row.get("status_message") or "").strip().rstrip(".")
        head += f" · status {row['status_code']}" + (f" {msg}" if msg else "")
    return head


def normalize(entry: dict) -> dict:
    """Gateway, IAP decision or Model Armor row, chosen by logName/resource type."""
    log_name = str(entry.get("logName") or "")
    if "modelarmor" in log_name or "sanitize_operations" in log_name:
        return model_armor_row(entry)
    if is_iap_entry(entry):
        return iap_row(entry)
    return gateway_row(entry)


def compact(row: dict) -> dict:
    """The row without the raw entry (for summaries); evidence files keep the raw entry."""
    return {k: v for k, v in row.items() if k != "raw"}
