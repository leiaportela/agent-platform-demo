# Cheat sheet — likely questions (presenter only)

**What is preview?** Agent Gateway itself is GA (2026-06-18), and Model Armor on the gateway is GA
(2026-06-24). Gateway/Model Armor spans in Cloud Trace (`apply_guardrail "Google Cloud Model Armor"`) are **Preview**. They show up in the same trace as the agent spans because the UI sends a traceparent. Agent Identity is GA. Say so if a trace link shows gateway spans.

**What runs where?** One project, `your-project-id`, us-central1.
- Chat UI: Cloud Run `agent-demo-ui`, behind IAP.
- Agent: Agent Runtime engine `1234567890123456789`, ADK 2.3, gemini-3.7-flash on the global endpoint,
  with an Agent Identity (`principal://agents.global.org-111111111111.system.id.goog/.../reasoningEngines/1234567890123456789`).
- Inbound gateway `agent-demo-ingress-gw` (CLIENT_TO_AGENT): Model Armor template `agent-demo-armor` on the prompt and on the answer.
- Outbound gateway `agent-demo-egress-gw` (AGENT_TO_ANYWHERE): every outbound call is authorized by IAP against the
  Agent Registry. 34 Google API endpoints + the MCP server are registered; anything else is denied.
- MCP server: Cloud Run `agent-demo-mcp`, IAM-only (unauthenticated → 403), 7 read-only tools over BigQuery.

**How is the contact tool denied?** One IAP policy on the MCP server's registry entry:
`roles/iap.egressor` for this agent's principal, with the condition
`mcp.toolName in [get_latest_run, get_model_summary, list_top_churn_risk, get_churn_drivers, get_clv_segments, get_demand_outlook, '']`.
`get_customer_contact` isn't listed, so the gateway answers **HTTP 403 "Egress request is not authorized."**.
The empty name lets the MCP handshake (initialize, tools/list) through.

**Isn't that just the model refusing?** No. The same agent code run locally with no gateway calls the tool and
gets the name, email and address (`evidence/control-2-contact-without-gateway.jsonl`). Through the gateway the call
gets a 403, and IAP records the decision: `granted: false`, status 7, this agent's principal, `mcp_tool_name: get_customer_contact`
(plus the gateway's `403 DENIED tools/call get_customer_contact` request row when it lands).

**What does Model Armor check?** Prompt injection/jailbreak at MEDIUM confidence, RAI (harassment, hate, sexual,
dangerous) at HIGH, malicious URLs, and sensitive data: US SSN and Brazilian CPF through a DLP inspect template.
- A blocked prompt returns HTTP 403 "Model Armor: Prompt violates content security configurations" in about 0.2–0.9 s, and the agent never runs.
- The answer is screened on the way out too.
- Tuned on measurements: RAI went MEDIUM → HIGH after "give me the email and home address…" was wrongly blocked;
  prompt injection went HIGH → MEDIUM after HIGH missed a hidden-tag injection. Result: 11/11 attacks blocked, 0/63
  wrong blocks on normal and scripted prompts across the runs. Tuning confidence levels is a real part of rolling this out.

**Why not Model Armor floor settings?** A floor setting is project-wide. It would also screen another agent in this
project, and it doesn't screen streamed model calls. The gateway template scopes screening to this agent's traffic.

**Latency** (evals, 2026-11-26): an answered question gets its first event in about 3.1 s (p95 3.7 s) and the full answer in about 10.6 s (p95 16.8 s); 2–4 tool calls per answer. A blocked prompt returns in 0.15–0.20 s (p95 0.26 s).

**Known gaps (say them if asked)**
- The gateway's request log doesn't always write the DENIED row for a denied MCP call: 3/10 in the 10x eval (7/15 overall).
  The **IAP authorization decision** (Data Access audit, `granted: false`, status 7) is recorded every time (10/10, 15/15),
  so it's the deny record the panel leads with ("Outbound gateway · IAP decision"). It lands about 2–20 s after the call.
- The inbound gateway writes no request rows of its own. Its evidence is the Model Armor records (prompt and answer)
  and the HTTP 403 it returns.
- Model Armor log records can lag (~10–60 s). The panel keeps listening for 75 s after each answer.

**Model quality (the agent says this too)**
- Churn: AUC-PR 0.227 vs a 0.069 base rate (~3.3× lift), AUC-ROC 0.761. The top-30 scores are near-tied (0.9812–0.9821).
- CLV: RMSE 43.11 vs a 43.93 baseline, R² 0.037. It barely beats predicting the mean.
- Demand: MAPE 39%, and the 90% bands cover only 50% of actuals.

**Cost (rough)**: Agent Runtime with 1 warm instance, two gateways, Cloud Run with min instance 1 (UI + MCP), and
Gemini tokens (~5k in / 300 out per turn). Teardown: `infra/teardown.sh`.
