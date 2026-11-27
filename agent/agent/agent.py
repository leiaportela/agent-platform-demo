"""Pipelines analyst: an ADK agent over the Vertex AI Pipelines demo results.

Runs on Agent Runtime with an Agent Identity. Every outbound call (Gemini, the MCP server, Google
APIs) goes through the outbound Agent Gateway, which allows or denies each MCP tool call. Prompts
and answers are screened by Model Armor on the inbound Agent Gateway, before this code runs.
"""

import inspect
import logging
import os
import threading
import urllib.parse
from typing import Any

from functools import cached_property

from google.adk.agents import LlmAgent
from google.adk.models import Gemini
from google.adk.tools import McpToolset
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.mcp_tool import StreamableHTTPConnectionParams
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

MCP_URL = os.environ.get("MCP_URL", "https://agent-demo-mcp-123456789012.us-central1.run.app/mcp")
MODEL = os.environ.get("MODEL_NAME", "gemini-3.7-flash")
# gemini-3.7-flash is served on the global endpoint only (404 in us-central1), while Agent Runtime
# sets GOOGLE_CLOUD_LOCATION to the engine's region. Pin the model client to MODEL_LOCATION
# (the subclass pattern documented on google.adk.models.Gemini).
MODEL_LOCATION = os.environ.get("MODEL_LOCATION", "global")


class LocatedGemini(Gemini):
    @cached_property
    def api_client(self):
        from google.genai import Client, types

        return Client(
            enterprise=True,
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=MODEL_LOCATION,
            http_options=types.HttpOptions(headers=self._tracking_headers(), retry_options=self.retry_options),
        )

# --- Cloud Run auth through the gateway -------------------------------------------------------
# Cloud Run checks a Google-signed ID token whose audience is the URL origin. Since google-auth
# 2.61, ID tokens minted on Agent Runtime are certificate-bound by default, and the gateway
# terminates TLS, so Cloud Run would reject them (401). Request them unbound (pattern from
# github.com/google/esmeralda packages/esmeralda/src/esmeralda/auth.py).
_creds: dict[str, Any] = {}
_lock = threading.Lock()


def _audience(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


def _id_token(audience: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    with _lock:
        creds = _creds.get(audience)
        if creds is None:
            kwargs = {"request": Request()}
            if "bind_id_token" in inspect.signature(id_token.fetch_id_token_credentials).parameters:
                kwargs["bind_id_token"] = False
            creds = _creds[audience] = id_token.fetch_id_token_credentials(audience, **kwargs)
        if not creds.valid:
            creds.refresh(Request())
        return creds.token


def _mcp_headers(_context: Any = None) -> dict[str, str]:
    if os.environ.get("MCP_NO_AUTH") == "1":  # local runs against a local server
        return {}
    if os.environ.get("MCP_BEARER"):  # local runs: a user ID token from `gcloud auth print-identity-token`
        return {"Authorization": f"Bearer {os.environ['MCP_BEARER']}"}
    return {"Authorization": f"Bearer {_id_token(_audience(MCP_URL))}"}


# --- Gateway denials as a readable tool result ------------------------------------------------
# ADK's graceful MCP error handling turns a failed tools/call into a generic "connection lost"
# error, hiding the HTTP status. An httpx response hook records what the outbound gateway actually
# returned (status + body) per tool, and after_tool_callback hands that verbatim to the model.
import json as _json  # noqa: E402
import time as _time  # noqa: E402

from mcp.shared._httpx_utils import create_mcp_http_client  # noqa: E402

_denials: dict[str, tuple[float, int, str]] = {}


async def _record_denial(response) -> None:
    if response.status_code not in (401, 403):
        return
    try:
        await response.aread()
        body = response.text[:300]
    except Exception:  # noqa: BLE001
        body = ""
    try:
        req = _json.loads(response.request.content or b"{}")
        tool = (req.get("params") or {}).get("name") or req.get("method", "?")
    except Exception:  # noqa: BLE001
        tool = "?"
    _denials[tool] = (_time.time(), response.status_code, body)
    logger.warning("MCP %s got HTTP %s from the gateway: %s", tool, response.status_code, body)


def _mcp_http_client(headers=None, timeout=None, auth=None):
    client = create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
    client.event_hooks.setdefault("response", []).append(_record_denial)
    return client


def _after_tool(tool: BaseTool, args: dict, tool_context: ToolContext, tool_response: Any) -> dict | None:
    denial = _denials.pop(tool.name, None)
    if denial and _time.time() - denial[0] < 60 and isinstance(tool_response, dict) and "error" in tool_response:
        _, status, body = denial
        return {"error": f"Denied by the outbound Agent Gateway: HTTP {status}: {body.strip()}. "
                         "This agent is not permitted to use this tool; do not retry it."}
    return None


def _is_status(exc: BaseException, code: int) -> bool:
    seen, queue = set(), [exc]
    while queue:
        cur = queue.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        resp = getattr(cur, "response", None)
        if getattr(resp, "status_code", None) == code:
            return True
        if f"{code}" in str(cur) and ("Forbidden" in str(cur) or "not authorized" in str(cur)):
            return True
        queue += [c for c in (cur.__cause__, cur.__context__) if c is not None]
        if isinstance(cur, BaseExceptionGroup):
            queue += list(cur.exceptions)
    return False


def _on_tool_error(tool: BaseTool, args: dict, tool_context: ToolContext, error: Exception) -> dict | None:
    if _is_status(error, 403):
        logger.warning("tool %s denied by the outbound gateway (403)", tool.name)
        return {"error": f"The '{tool.name}' call was denied by the outbound Agent Gateway (HTTP 403). "
                         "This agent is not permitted to use that tool."}
    return None


INSTRUCTION = """You are the pipelines analyst for a retail data/ML team. You answer questions about
the latest Vertex AI Pipelines run and its three models, using only your tools:
- churn: AutoML Tabular classifier. churn_score = predicted probability that a customer has churned
  (no order in the last 90 days). Its headline metric is AUC-PR for the *reorders* class; compare it
  to `positive_rate` (the share of test customers who reordered), not to 0.5 or 1.0.
- clv: custom XGBoost regressor (with hyperparameter tuning) predicting 180-day spend. Compare its
  RMSE to `baseline_rmse` and report r2.
- demand: BigQuery ML ARIMA_PLUS daily units forecast per product category. Holdout `mape` is the
  average error; `coverage` is how often actuals fell inside the 90% band (should be near 0.9).

Rules:
- Call tools before answering; never invent numbers. Quote figures with sensible rounding and say
  which run they come from.
- Be candid about model quality. If a model barely beats its baseline or its bands are too narrow,
  say so plainly and say what that means for using it.
- When scores are nearly tied, say that the ranking among them is not meaningful.
- For contact or outreach requests, find the customer's user_id (e.g. with list_top_churn_risk), then
  call get_customer_contact for it. If a tool call is denied, say plainly that the platform's
  gateway blocked it (quote the HTTP status) and do not try to work around it.
- Keep answers short: a few sentences or a compact list, no preamble.
- Plain Markdown only: never use LaTeX or $...$ math (write R² or "r2", not $R^2$), and use "$" only for money.
"""

root_agent = LlmAgent(
    name="pipelines_analyst",
    model=LocatedGemini(model=MODEL),
    description="Answers questions about the Vertex AI Pipelines demo models and predictions.",
    instruction=INSTRUCTION,
    tools=[McpToolset(connection_params=StreamableHTTPConnectionParams(url=MCP_URL, httpx_client_factory=_mcp_http_client),
                      header_provider=_mcp_headers)],
    on_tool_error_callback=_on_tool_error,
    after_tool_callback=_after_tool,
)
