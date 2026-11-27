"""Every field extraction the UI depends on, in one place.

Field paths checked on 2026-11-26 against real records of this project (copies in
tests/fixtures/real_*): only the OUTBOUND gateway writes gateway_requests rows (the inbound gateway writes
none); `trace` on gateway rows is a bare hex id and only MCP rows carry one; Model Armor records written for
the gateway carry labels."modelarmor.googleapis.com/client_name" = "AGENT_GATEWAY" and the screened text as
base64 in jsonPayload.sanitizationInput.byteItem.byteData (direct API probes have no client_name and are
excluded). IAP authorization decisions for the outbound gateway's egress (checked 2026-11-26 on the real samples
in tests/fixtures/real_iap_decisions.logs.json) are Data Access audit entries of iap.googleapis.com /
GatekeeperAuthorizer.AuthorizeUser with labels."iap.googleapis.com/audited_resource_type" = the Agent Registry
resource type (McpServer for MCP calls, Endpoint for Google APIs), the agent's principal in
protoPayload.authenticationInfo.principalSubject, authorizationInfo[0].granted, status {code 7} on denies and the
MCP method/tool in protoPayload.metadata.mcp_attributes; they carry no trace. Everything here is defensive: a missing field gives None, never an exception, and callers always
forward the raw record next to the summary. When a real run shows a field somewhere else, adjust it here only.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import urllib.parse
from typing import Any

MODEL_ARMOR_LOG = "modelarmor.googleapis.com%2Fsanitize_operations"
GATEWAY_RESOURCE_TYPE = "networkservices.googleapis.com/Gateway"
MA_CLIENT_LABEL = "modelarmor.googleapis.com/client_name"
MA_CORRELATION_LABEL = "modelarmor.googleapis.com/client_correlation_id"
MA_GATEWAY_CLIENT = "AGENT_GATEWAY"
OP_PROMPT = "SANITIZE_USER_PROMPT"
OP_RESPONSE = "SANITIZE_MODEL_RESPONSE"
SCREENED_TEXT_MAX = 4000  # characters of decoded screened text kept in the summary (raw keeps all of it)

IAP_AUDIT_LOG = "cloudaudit.googleapis.com%2Fdata_access"
IAP_SERVICE = "iap.googleapis.com"
IAP_METHOD = "GatekeeperAuthorizer.AuthorizeUser"
IAP_RESOURCE_TYPE_LABEL = "iap.googleapis.com/audited_resource_type"
IAP_RESOURCE_NAME_LABEL = "iap.googleapis.com/audited_resource_name"
IAP_MCP_SERVER = "agentregistry.googleapis.com/McpServer"  # ".../Endpoint" (Google APIs) is not shown
IAP_STATUS_PERMISSION_DENIED = 7  # google.rpc.Code PERMISSION_DENIED

SOURCE_INBOUND = "inbound_gateway"
SOURCE_OUTBOUND = "outbound_gateway"
SOURCE_GATEWAY = "gateway"
SOURCE_MODEL_ARMOR = "model_armor"
SOURCE_IAP = "iap_decision"  # outbound gateway: the IAP authorization decision for an MCP call


def dig(obj: Any, *path: str, default: Any = None) -> Any:
    """obj[path[0]][path[1]]... or default when any step is missing or not a mapping."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def first(*values: Any) -> Any:
    """The first value that is not None and not an empty string."""
    for v in values:
        if v is not None and v != "":
            return v
    return None


# ------------------------------------------------------------------------------------------------
# Cloud Logging: query filter, classification, links
# ------------------------------------------------------------------------------------------------


def iap_clause(project: str, agent_principal: str) -> str:
    """The IAP authorization decisions IAP made for this agent's calls to MCP servers in the Agent Registry."""
    return (
        f'logName="projects/{project}/logs/{IAP_AUDIT_LOG}"'
        f' AND protoPayload.serviceName="{IAP_SERVICE}"'
        f' AND protoPayload.methodName="{IAP_METHOD}"'
        f' AND labels."{IAP_RESOURCE_TYPE_LABEL}"="{IAP_MCP_SERVER}"'
        f' AND protoPayload.authenticationInfo.principalSubject="{agent_principal}"'
    )


def log_filter(project: str, since_rfc3339: str, until_rfc3339: str | None = None,
               gateways: tuple[str, ...] = ("agent-demo-ingress-gw", "agent-demo-egress-gw"),
               agent_principal: str | None = None) -> str:
    """The entries.list filter for the open turn windows: request rows of the two named gateways
    (resource.labels.gateway_name, verified on real rows), the Model Armor records the gateway asked for
    (client_name AGENT_GATEWAY; direct API probes have no client_name) and, when the agent's principal is known,
    the IAP decisions for its MCP calls (one query: a third OR clause, not another request)."""
    time_clause = f'timestamp>="{since_rfc3339}"'
    if until_rfc3339:
        time_clause += f' AND timestamp<"{until_rfc3339}"'
    names = " OR ".join(f'"{g}"' for g in gateways if g)
    gw = f'resource.type="{GATEWAY_RESOURCE_TYPE}"'
    if names:
        gw += f" AND resource.labels.gateway_name=({names})"
    iap = f" OR ({iap_clause(project, agent_principal)})" if agent_principal else ""
    return (
        f"{time_clause} AND (({gw})"
        f' OR (logName="projects/{project}/logs/{MODEL_ARMOR_LOG}"'
        f' AND labels."{MA_CLIENT_LABEL}"="{MA_GATEWAY_CLIENT}"){iap})'
    )


def insert_id(entry: dict) -> str:
    """The dedupe key: insertId, else a hash of the entry."""
    iid = entry.get("insertId") if isinstance(entry, dict) else None
    if iid:
        return str(iid)
    return "sha1:" + hashlib.sha1(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()


def gateway_name(entry: dict) -> str | None:
    """gateway_name from the entry labels or the resource labels; else a name found in the resource."""
    name = first(
        dig(entry, "labels", "gateway_name"),
        dig(entry, "resource", "labels", "gateway_name"),
        dig(entry, "jsonPayload", "gatewayName"),
        dig(entry, "jsonPayload", "agentGatewayInfo", "gatewayName"),
    )
    if name:
        return str(name).rsplit("/", 1)[-1]
    return None


def is_model_armor(entry: dict) -> bool:
    return MODEL_ARMOR_LOG in str(entry.get("logName", "")) or "SanitizeOperationLogEntry" in str(
        dig(entry, "jsonPayload", "@type", default="")
    )


def ma_client_name(entry: dict) -> str | None:
    labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else {}
    return labels.get(MA_CLIENT_LABEL) or None


def is_iap_decision(entry: dict) -> bool:
    proto = entry.get("protoPayload") if isinstance(entry, dict) else None
    return isinstance(proto, dict) and proto.get("serviceName") == IAP_SERVICE and proto.get("methodName") == IAP_METHOD


def iap_resource_type(entry: dict) -> str | None:
    labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else {}
    return labels.get(IAP_RESOURCE_TYPE_LABEL) or None


def classify(entry: dict, inbound_gw: str, outbound_gw: str) -> str | None:
    """model_armor | iap_decision | inbound_gateway | outbound_gateway | gateway, or None for an entry the UI does
    not show (unrelated, a Model Armor record without client_name AGENT_GATEWAY, i.e. a direct API call, or an
    IAP decision about anything but an Agent Registry McpServer, e.g. the Google API Endpoint decisions)."""
    if is_model_armor(entry):
        return SOURCE_MODEL_ARMOR if ma_client_name(entry) == MA_GATEWAY_CLIENT else None
    if is_iap_decision(entry):
        return SOURCE_IAP if iap_resource_type(entry) == IAP_MCP_SERVER else None
    if dig(entry, "resource", "type") != GATEWAY_RESOURCE_TYPE:
        return None
    name = gateway_name(entry)
    if name == inbound_gw:
        return SOURCE_INBOUND
    if name == outbound_gw:
        return SOURCE_OUTBOUND
    # The label may be missing: fall back to a name mentioned anywhere in the resource labels.
    resource = json.dumps(dig(entry, "resource", "labels", default={}))
    if inbound_gw and inbound_gw in resource:
        return SOURCE_INBOUND
    if outbound_gw and outbound_gw in resource:
        return SOURCE_OUTBOUND
    return SOURCE_GATEWAY


def trace_id(entry: dict) -> str | None:
    """The trace id of an entry: real gateway and Model Armor rows carry a bare hex id; the documented
    'projects/P/traces/ID' form is accepted too."""
    trace = entry.get("trace") if isinstance(entry, dict) else None
    if not trace:
        return None
    return str(trace).rsplit("/", 1)[-1].lower() or None


_SESSION_IN_URL = re.compile(r"/sessions/([^/:?#]+)")


def session_ids_in_url(url: Any) -> list[str]:
    """Session ids in an Agent Runtime sessions URL (…/reasoningEngines/E/sessions/S[/events|:appendEvent])."""
    return _SESSION_IN_URL.findall(url) if isinstance(url, str) else []


def console_url(entry: dict, project: str) -> str | None:
    """Logs Explorer link to one entry, pinned to an absolute window around its timestamp, so the link
    still works after an hour (replays, later review). Falls back to the last 7 days without a timestamp."""
    iid = entry.get("insertId") if isinstance(entry, dict) else None
    if not iid:
        return None
    query = urllib.parse.quote(f'insertId="{iid}"', safe="")
    from datetime import datetime, timedelta, timezone

    from attribution import parse_ts  # local import: attribution imports this module

    epoch = parse_ts(entry.get("timestamp"))
    if epoch is None:
        window = "timeRange=P7D"
    else:
        t = datetime.fromtimestamp(epoch, tz=timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        window = f"startTime={(t - timedelta(minutes=10)).strftime(fmt)};endTime={(t + timedelta(minutes=10)).strftime(fmt)}"
    return f"https://console.cloud.google.com/logs/query;query={query};{window}?project={project}"


def trace_url(trace: str | None, project: str) -> str | None:
    if not trace:
        return None
    return f"https://console.cloud.google.com/traces/explorer;traceId={trace}?project={project}"


def gateways_url(project: str) -> str:
    return f"https://console.cloud.google.com/agent-platform/gateways?project={project}"


def traces_url(project: str) -> str:
    return f"https://console.cloud.google.com/traces/explorer?project={project}"


# ------------------------------------------------------------------------------------------------
# Gateway entries
# ------------------------------------------------------------------------------------------------


def _status(entry: dict) -> int | None:
    raw = dig(entry, "httpRequest", "status")
    if raw in (None, "", 0, "0"):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _policies(info: Any) -> list[str]:
    out: list[str] = []
    policies = info.get("policies") if isinstance(info, dict) else None
    for p in policies if isinstance(policies, list) else []:
        if isinstance(p, dict):
            name = first(p.get("name"), p.get("policy"))
            if name:
                out.append(str(name).rsplit("/", 1)[-1])
        elif p:
            out.append(str(p).rsplit("/", 1)[-1])
    return out


def summarize_gateway(entry: dict) -> dict:
    payload = entry.get("jsonPayload") if isinstance(entry.get("jsonPayload"), dict) else {}
    authz = payload.get("authzPolicyInfo")
    authz = authz if isinstance(authz, dict) else {}
    mcp = dig(payload, "agentGatewayInfo", "mcpInfo")
    mcp = mcp if isinstance(mcp, dict) else {}
    status = _status(entry)
    result = first(authz.get("result"))
    result = str(result) if result else None  # exactly as recorded
    url = first(dig(entry, "httpRequest", "requestUrl"))
    tool = first(mcp.get("parameter"), dig(mcp, "params", "name"), mcp.get("toolName"), mcp.get("tool"))
    return {
        "gateway_name": gateway_name(entry),
        "status": status,
        "method": dig(entry, "httpRequest", "requestMethod"),
        "url": url,
        "latency": dig(entry, "httpRequest", "latency"),
        "authz_result": result,
        "policies": _policies(authz),
        "mcp_method": first(mcp.get("method")),
        "tool": tool,
        # 'denied' only when the record's authz result says DENIED (an HTTP 403 alone is shown as a status).
        "denied": (result or "").upper() == "DENIED",
        "severity": entry.get("severity"),
        "timestamp": entry.get("timestamp"),
        "insert_id": entry.get("insertId"),
        "trace_id": trace_id(entry),
        "session_ids": session_ids_in_url(url),
        # CONNECT tunnel rows carry no request URL and no authz result (their status is 200): hidden behind a
        # collapsed section. A row with an authz result (so every DENIED row) is never hidden.
        "hidden": result is None and (status is None or url is None),
    }


# ------------------------------------------------------------------------------------------------
# Model Armor entries
# ------------------------------------------------------------------------------------------------


def _match_states(obj: Any) -> list[str]:
    """Every matchState value under obj (filter results nest it at different depths)."""
    found: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "matchState" and isinstance(v, str):
                found.append(v)
            else:
                found.extend(_match_states(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_match_states(v))
    return found


def _matched_confidences(obj: Any) -> list[str]:
    """confidenceLevel of every node under obj whose own matchState is MATCH_FOUND (a filter or one of its
    sub-filters, e.g. raiFilterTypeResults.harassment); confidence of non-matching nodes is ignored."""
    found: list[str] = []
    if isinstance(obj, dict):
        if obj.get("matchState") == "MATCH_FOUND" and isinstance(obj.get("confidenceLevel"), str):
            found.append(obj["confidenceLevel"])
        for v in obj.values():
            found.extend(_matched_confidences(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_matched_confidences(v))
    return found


def _filter_details(name: str, result: Any) -> dict:
    states = _match_states(result)
    detail: dict[str, Any] = {"name": name, "matched": "MATCH_FOUND" in states,
                              "match_state": "MATCH_FOUND" if "MATCH_FOUND" in states else (states[0] if states else None)}
    rai_types = _find_key(result, "raiFilterTypeResults")
    if isinstance(rai_types, dict):
        detail["matched_types"] = sorted(t for t, r in rai_types.items() if "MATCH_FOUND" in _match_states(r))
    confidences = sorted(set(_matched_confidences(result)))
    if confidences and detail["matched"]:
        detail["confidence"] = ", ".join(confidences)
    findings = _find_key(result, "findings")
    if isinstance(findings, list):
        detail["info_types"] = sorted(
            {str(f.get("infoType")) for f in findings if isinstance(f, dict) and f.get("infoType")}
        )
    return detail


def _find_key(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            hit = _find_key(v, key)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_key(v, key)
            if hit is not None:
                return hit
    return None


def screened_text(entry: dict) -> str | None:
    """The text Model Armor screened: base64 byteData for gateway records (UTF-8), else a plain text field."""
    item = dig(entry, "jsonPayload", "sanitizationInput", "byteItem")
    data = item.get("byteData") if isinstance(item, dict) else None
    if isinstance(data, str) and data:
        try:
            return base64.b64decode(data, validate=False).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            return None
    text = dig(entry, "jsonPayload", "sanitizationInput", "text")
    return text if isinstance(text, str) else None


def screened_inner_text(text: str | None) -> str | None:
    """The human text inside a screened payload: response payloads are a JSON list of {"type":"text","text"}
    parts (session creation) or the plain answer text; prompts are the plain user message."""
    if not text:
        return text
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return text
    if isinstance(parsed, list):
        parts = [p.get("text") for p in parsed if isinstance(p, dict) and isinstance(p.get("text"), str)]
        if parts:
            return "".join(parts)
    return text


def operation_kind(operation_type: Any) -> str:
    """prompt | response | other (an operation type the UI does not know is shown as recorded)."""
    if operation_type == OP_PROMPT:
        return "prompt"
    if operation_type == OP_RESPONSE:
        return "response"
    return "other"


def summarize_model_armor(entry: dict) -> dict:
    payload = entry.get("jsonPayload") if isinstance(entry.get("jsonPayload"), dict) else {}
    result = first(
        payload.get("sanitizationResult"), dig(payload, "sanitizationResponse", "sanitizationResult")
    )
    result = result if isinstance(result, dict) else {}
    filters = result.get("filterResults") or {}
    details = [_filter_details(n, r) for n, r in filters.items()] if isinstance(filters, dict) else []
    match_state = result.get("filterMatchState")
    verdict = result.get("sanitizationVerdict")
    verdict_short = str(verdict).rsplit("_", 1)[-1] if verdict else None  # ..._VERDICT_BLOCK -> BLOCK
    labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else {}
    operation = first(payload.get("operationType"), labels.get("modelarmor.googleapis.com/operation_type"))
    text = screened_text(entry)
    return {
        "operation_type": operation,
        "operation": operation_kind(operation),
        "match_state": match_state,  # exactly as recorded, None when the record has none
        "matched": match_state == "MATCH_FOUND",
        "matched_filters": [d["name"] for d in details if d["matched"]],
        "filters": details,
        "verdict": verdict,
        "verdict_short": verdict_short,
        "verdict_reason": result.get("sanitizationVerdictReason"),
        "invocation_result": result.get("invocationResult"),
        "client_name": labels.get(MA_CLIENT_LABEL),
        "correlation_id": labels.get(MA_CORRELATION_LABEL),
        "template_id": dig(entry, "resource", "labels", "template_id"),
        "screened_text": text[:SCREENED_TEXT_MAX] if isinstance(text, str) else None,
        "screened_text_truncated": isinstance(text, str) and len(text) > SCREENED_TEXT_MAX,
        "timestamp": entry.get("timestamp"),
        "insert_id": entry.get("insertId"),
        "trace_id": trace_id(entry),
        "hidden": False,
    }


# ------------------------------------------------------------------------------------------------
# IAP authorization decisions (outbound gateway egress)
# ------------------------------------------------------------------------------------------------


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def summarize_iap(entry: dict) -> dict:
    """An IAP authorization decision. `granted` is the record's authorizationInfo[0].granted; proto JSON may
    omit a false boolean, so a missing field counts as false only when the record's status code is 7
    (PERMISSION_DENIED), and `granted_in_record` says whether the field was there. Otherwise granted is None
    (shown as "granted not in record")."""
    proto = entry.get("protoPayload") if isinstance(entry.get("protoPayload"), dict) else {}
    infos = proto.get("authorizationInfo")
    info = infos[0] if isinstance(infos, list) and infos and isinstance(infos[0], dict) else {}
    status = proto.get("status") if isinstance(proto.get("status"), dict) else {}
    status_code = _int_or_none(status.get("code"))
    raw_granted = info.get("granted")
    granted_in_record = isinstance(raw_granted, bool)
    if granted_in_record:
        granted: bool | None = raw_granted
    elif status_code == IAP_STATUS_PERMISSION_DENIED:
        granted = False
    else:
        granted = None
    meta = proto.get("metadata") if isinstance(proto.get("metadata"), dict) else {}
    mcp = meta.get("mcp_attributes") if isinstance(meta.get("mcp_attributes"), dict) else {}
    labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else {}
    mcp_method = first(mcp.get("mcp_method"))
    dry_run = meta.get("dryRun")
    base = mcp.get("base_protocol_method")
    return {
        "service": proto.get("serviceName"),
        "method": proto.get("methodName"),
        "resource_type": labels.get(IAP_RESOURCE_TYPE_LABEL),
        "resource_name": labels.get(IAP_RESOURCE_NAME_LABEL),
        "principal": dig(proto, "authenticationInfo", "principalSubject"),
        "granted": granted,
        "granted_in_record": granted_in_record,
        "permission": info.get("permission"),
        "status_code": status_code,
        "status_message": first(status.get("message")),
        "mcp_method": mcp_method,
        "tool": first(mcp.get("mcp_tool_name")),
        "base_protocol_method": base if isinstance(base, bool) else None,
        "policy_version": first(meta.get("policy_version")),
        "dry_run": dry_run if isinstance(dry_run, bool) else None,
        "url": first(dig(proto, "request", "httpRequest", "url")),
        # a granted:false decision (as recorded, or omitted with status 7) is a deny
        "denied": granted is False,
        "severity": entry.get("severity"),
        "timestamp": entry.get("timestamp"),
        "insert_id": entry.get("insertId"),
        "trace_id": trace_id(entry),  # the real records carry none
        # only tools/call decisions go in the timeline (handshake methods carry no tool name)
        "hidden": mcp_method != "tools/call",
    }


def summarize_log(entry: dict, source: str) -> dict:
    if source == SOURCE_MODEL_ARMOR:
        return summarize_model_armor(entry)
    if source == SOURCE_IAP:
        return summarize_iap(entry)
    return summarize_gateway(entry)


# ------------------------------------------------------------------------------------------------
# ADK events (Agent Runtime streamQuery)
# ------------------------------------------------------------------------------------------------


def _usage(event: dict) -> dict | None:
    u = event.get("usage_metadata") or event.get("usageMetadata")
    if not isinstance(u, dict):
        return None
    pick = lambda snake, camel: first(u.get(snake), u.get(camel))  # noqa: E731
    return {
        "prompt": pick("prompt_token_count", "promptTokenCount"),
        "output": pick("candidates_token_count", "candidatesTokenCount"),
        "thoughts": pick("thoughts_token_count", "thoughtsTokenCount"),
        "total": pick("total_token_count", "totalTokenCount"),
    }


def _response_error(response: Any) -> str | None:
    """A tool result that reports an error: {'error': ...} or MCP's {'isError': true, ...}."""
    if not isinstance(response, dict):
        return None
    if response.get("error"):
        return str(response["error"])
    if response.get("isError") or response.get("is_error"):
        content = response.get("content")
        if isinstance(content, list):
            texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("text")]
            if texts:
                return " ".join(texts)
        return "tool reported an error"
    result = response.get("result")
    if isinstance(result, dict) and (result.get("isError") or result.get("is_error")):
        return _response_error({"isError": True, **result})
    return None


def stream_error(event: Any) -> tuple[str | None, str | None]:
    """(code, message) when a stream line is an error, in every shape seen from streamQuery:
    ADK's {"error_code", "error_message"}, the closing {"code": 498, "message", "errorCode", "errorMessage"},
    and a Google API error {"error": {"code", "message", "status"}}. (None, None) otherwise."""
    if not isinstance(event, dict):
        return None, None
    code = first(event.get("error_code"), event.get("errorCode"))
    message = first(event.get("error_message"), event.get("errorMessage"))
    if code is not None or message is not None:
        return (str(code) if code is not None else None), message
    err = event.get("error")
    if isinstance(err, dict) and (err.get("code") is not None or err.get("message")):
        code = first(err.get("status"), err.get("code"))
        return (str(code) if code is not None else None), err.get("message")
    if "content" not in event and event.get("code") is not None and event.get("message") is not None:
        return str(event["code"]), str(event["message"])
    return None, None


def summarize_adk_event(event: Any) -> dict:
    if not isinstance(event, dict):
        return {"parsed": False, "author": None, "calls": [], "responses": [], "text": "", "thought": False,
                "usage": None, "error_code": None, "error_message": None, "is_error": False, "partial": False,
                "invocation_id": None, "timestamp": None, "is_final_text": False}
    parts = dig(event, "content", "parts", default=[]) or []
    calls, responses, texts, thought = [], [], [], False
    for p in parts if isinstance(parts, list) else []:
        if not isinstance(p, dict):
            continue
        fc = first(p.get("function_call"), p.get("functionCall"))
        fr = first(p.get("function_response"), p.get("functionResponse"))
        if isinstance(fc, dict):
            calls.append({"id": fc.get("id"), "name": fc.get("name"), "args": fc.get("args") or {}})
        elif isinstance(fr, dict):
            resp = fr.get("response")
            responses.append({"id": fr.get("id"), "name": fr.get("name"), "error": _response_error(resp)})
        elif p.get("thought"):
            thought = True
        elif isinstance(p.get("text"), str):
            texts.append(p["text"])
    text = "".join(texts)
    error_code, error_message = stream_error(event)
    return {
        "parsed": True,
        "author": event.get("author"),
        "calls": calls,
        "responses": responses,
        "text": text,
        "thought": thought,
        "usage": _usage(event),
        "error_code": error_code,
        "error_message": error_message,
        "is_error": error_code is not None or error_message is not None,
        "partial": bool(event.get("partial")),
        "invocation_id": first(event.get("invocation_id"), event.get("invocationId")),
        "timestamp": event.get("timestamp"),
        "is_final_text": bool(text) and not calls and not event.get("partial") and event.get("author") != "user",
    }


def session_id_from(output: Any) -> str | None:
    """The session id in an async_create_session :query response ({'output': {'id': ...}})."""
    out = output.get("output", output) if isinstance(output, dict) else None
    if isinstance(out, dict):
        return first(out.get("id"), out.get("session_id"), out.get("name"))
    return None


def http_error_body(text: str) -> Any:
    """An error body as JSON when it parses, else the raw text."""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def http_error_message(body: Any) -> str | None:
    """The human message of a Google API error body ({'error': {'message': ...}})."""
    if isinstance(body, list) and body:
        body = body[0]
    if isinstance(body, dict):
        return first(dig(body, "error", "message"), body.get("message"), body.get("detail"))
    if isinstance(body, str):
        return body[:500]
    return None
