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

All project-specific values live in one file, **`infra/env.sh`**: project id, project number, organization, region,
presenter email, and (once `agent/deploy.py` has printed it) `ENGINE`. Everything else is derived from those, or
read from them:
- the staging bucket, MCP URL, gateway names and the agent principal;
- `agent/deploy.py`, the evals, the helper scripts and the UI/MCP backends read them from the environment, and
  stop with "source infra/env.sh" if they are missing;
- the gateway and authz YAMLs are templates in `infra/templates/`, and `infra/render.sh` fills in `${PROJ_ID}` and
  `${REGION}` into `infra/rendered/`;
- `infra/deploy_ui.sh` passes `PROJECT`, `REGION`, `ENGINE` and `AGENT_ORG` to the Cloud Run service.

```bash
$EDITOR infra/env.sh        # set PROJ_ID, PROJ_NO, ORG_ID, REGION, PRESENTER
source infra/env.sh         # in every shell you run the scripts from
infra/render.sh             # gateway / authz YAMLs for `gcloud ... import --source=infra/rendered/<name>.yaml`
```

The files in this repo still show placeholder defaults (`your-project-id`, `123456789012`, `111111111111`,
`presenter@example.com`); `env.sh` warns while `PROJ_ID` is the placeholder.

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

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
