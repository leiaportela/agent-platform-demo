# Agent Platform demo: a governed agent over Vertex AI Pipelines predictions

An ADK agent on **Agent Runtime** answers questions about the churn, CLV and demand predictions that a
Vertex AI Pipelines run publishes to BigQuery. **Agent Gateway** governs it on both sides:

- the **inbound** gateway (client to agent) runs **Model Armor** on every prompt and on the agent's final answer;
- the **outbound** gateway (agent to anywhere) is default-deny. IAP checks every call against the **Agent Registry**,
  down to the MCP tool name.

A Cloud Run chat UI shows each answer next to a panel built only from platform records (Model Armor verdicts,
IAP decisions, gateway rows, Agent Runtime events, Cloud Trace). It also has an Architecture tab.

```
browser ──IAP──▶ chat UI (Cloud Run)
                   │ streamQuery + trace id
                   ▼
        inbound Agent Gateway ── Model Armor (prompt ①, final answer ③)
                   ▼
        Agent Runtime: ADK agent, gemini-3.7-flash, Agent Identity
                   ▼
        outbound Agent Gateway ── IAP + Agent Registry (② per-tool policy)
          ├──▶ Gemini (global endpoint)
          └──▶ MCP server (Cloud Run, IAM only, 7 read-only tools) ──▶ BigQuery
```

## Layout

| Path | What |
|---|---|
| `agent/` | ADK agent and `deploy.py`, which creates the engine with Agent Identity and both gateway bindings |
| `mcp_server/` | FastMCP server over the pipeline's BigQuery tables. `get_customer_contact` is the tool the outbound policy denies |
| `infra/` | Gateway, authz-extension and authz-policy YAMLs, Agent Registry and IAP scripts, Model Armor mode switch, UI deploy, teardown |
| `ui/` | FastAPI and React chat UI. See `ui/README.md` |
| `evals/` | Eval harness: scripted, benign and attack prompt sets graded against SQL ground truth, with platform records attached. See `evals/README.md` |
| `demo/` | Run of show (`FULL-SCRIPT.md`, `SCRIPT.md`), cheat sheet, BigQuery walkthrough, evidence |

## Using it in your own project

Project-specific values are placeholders:

- project `your-project-id`
- project number `123456789012`
- organization `111111111111`
- engine `1234567890123456789`
- presenter `presenter@example.com`

Set your own values in `infra/env.sh`. They are also hard-coded in:
- `agent/deploy.py`
- `ui/backend/settings.py`
- `mcp_server/main.py`
- `evals/log_fields.py`
- `evals/ground_truth.py`
- the `infra/*.yaml` files

The engine id exists only after `agent/deploy.py` has run.

The order that worked:
1. Outbound gateway with IAP authz in dry run, and the Agent Registry entries (`infra/register.sh`). Bind an engine
   before the allowlist exists and its calls fail with HTTP 498.
2. Model Armor templates, then the inbound gateway with its Model Armor authz extension and policy.
3. Engine created once, with both gateway bindings (`agent/deploy.py`). Changing a binding later archives every
   earlier revision.
4. Egress grants for the engine's Agent Identity (`infra/grant_endpoints.sh`), the tool-level policy on the MCP entry
   (`infra/grant_mcp.sh`), then IAP switched to enforced.
5. MCP server switched to IAM only, then the chat UI (`infra/deploy_ui.sh`).

`infra/teardown.sh` removes everything, and asks before it does.

## Evidence

`evals/results/` and `demo/evidence/` hold records from real runs on 2026-11-26/27. Project, organization, engine
and registry identifiers in them have been replaced with the placeholders above, so the links in them do not
resolve. The live MCP smoke tests (`mcp_server/tests/test_tools_live.py`) need a real project and fail with the
placeholders. The UI and eval test suites run offline.
