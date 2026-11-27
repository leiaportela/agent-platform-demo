# Run of show: the pipeline, then the agent (with console)

Audience: the customer's data/ML team. About 28 minutes; the 20-minute cut is at the end.
Everything runs in one project, `your-project-id`, region us-central1.
Story in one line: **"Train models with three methods in one pipeline, publish the results, then let people ask
questions about them through an agent that is governed on both sides."**

## T-15 min: open these tabs, in this order

Open every tab once before the slot. Each console page can ask you to sign in or pick the project the first time.
Tabs marked *(check)* use a URL I have not opened in a browser; if one doesn't load, use the menu path instead.

| # | Tab | URL / path |
|---|---|---|
| 1 | Pipeline run graph | https://console.cloud.google.com/vertex-ai/locations/us-central1/pipelines/runs/vertex-demo-e2e-4-20261126?project=your-project-id |
| 2 | Model Registry, churn (AutoML) | https://console.cloud.google.com/vertex-ai/models/locations/us-central1/models/3333333333333333333/versions/1?project=your-project-id |
| 3 | Model Registry, CLV (custom XGBoost) | https://console.cloud.google.com/vertex-ai/models/locations/us-central1/models/2222222222222222222/versions/6?project=your-project-id |
| 4 | Dashboard | https://vertex-demo-123456789012.us-central1.run.app (window at least 1000 px tall) |
| 5 | BigQuery | https://console.cloud.google.com/bigquery?project=your-project-id, then expand `vertex_demo_dashboard` |
| 6 | Agent Runtime engine *(check)* | https://console.cloud.google.com/vertex-ai/agents/agent-engines/locations/us-central1/agent-engines/1234567890123456789?project=your-project-id |
| 7 | Agent Gateways | https://console.cloud.google.com/agent-platform/gateways?project=your-project-id |
| 8 | Chat UI | https://agent-demo-ui-123456789012.us-central1.run.app. Hard-refresh it; the right panel must read **"Behind each answer"** |
| 9 | Model Armor *(check)* | Security › Model Armor › template `agent-demo-armor` |
| 10 | Logs Explorer | https://console.cloud.google.com/logs/query?project=your-project-id, with the IAP query below pasted in, not yet run |

Check that the dashboard Overview shows run `vertex-demo-e2e-4-20261126`. In the chat UI, the top bar should show
"Agent Runtime · …23456789".

**Don't** start a pipeline rerun before the slot, rehearsals included. A run takes about 1 hour and republishes the
results. The source data (thelook) changes all the time, so CLV and demand would move away from the numbers in this
script and in the agent's checks.

---

## Part 1: the pipeline (≈11 min)

### 1.1 Setup line (30 s, on tab 1)
"This is one Vertex AI pipeline that trains three models on a synthetic e-commerce dataset. Each model uses a
different Google Cloud training method, and the results are published to BigQuery for a dashboard and, later, for an
agent."

### 1.2 Pipeline run graph (2 min, tab 1)
- Show the green run and the graph. `build_features` and the data checks come first. Then three branches run side by
  side:
  - **churn**: AutoML Tabular → quality gate → batch prediction;
  - **CLV**: XGBoost trainer + hyperparameter tuning job → Model Registry → batch prediction;
  - **demand**: BigQuery ML `ARIMA_PLUS` → evaluate → forecast.
  - All three feed **publish**.
- Click the **CLV tuning** step and follow its link to the tuning job, `vertex-demo-clv-tuning`: 12 trials, 3 in
  parallel, CPU only. Say: "Our own trainer code; Vertex AI ran the search and kept the best trial."
- Back on the graph, click **publish**: it writes the 14 dashboard tables.
- Open the runs list (breadcrumb **Runs**). It shows the history, including one failed run (`vertex-demo-e2e-1`).
  Say: "That failure is real. It was fixed, and the next three runs are green." Each run takes about an hour.
- Say the timing honestly: the latest run is from early **Thursday** (04:41–05:41 São Paulo), not last night.

### 1.3 Model Registry (1 min, tabs 2–3)
- `vertex-demo-churn` (AutoML Tabular, version 1) and `vertex-demo-clv` (custom XGBoost, version 6; each run
  registers a new version).
- Say: "Nothing is deployed to an endpoint. All predictions are batch, written straight to BigQuery." The demand model
  lives in BigQuery ML (`vertex_demo.demand_model`), not in the registry.
- Optional: **Pipelines › Schedules** shows `vertex-demo-weekly`, Mondays 06:00 São Paulo, next run 2026-11-30.
  AutoML retraining is off in the schedule: it's a flag, because AutoML costs about $21 per node-hour.

### 1.4 Dashboard (6 min, tab 4)
- **Overview**: the pipeline drawn step by step, with status and duration, plus one box per model (method, version,
  headline metric). Say: "Same pipeline, three methods." If the churn stage reads "Reused model", that's the AutoML
  flag being off for that run.
- **Churn**: PR and ROC curves, a confusion matrix with a threshold slider, feature importance, and the most at-risk
  customers with their reasons.
  Say: "Churn means not reordering. But only about 7% of customers reorder, so I score that minority class.
  AUC-PR is 0.227 against a 7% base rate, about 3× lift; AUC-ROC is 0.761. AUC-PR on the 93% class would look great
  and say nothing."
- **CLV**: predicted vs actual, error by segment, revenue by predicted decile, and the tuning trials.
  Say: "R² is 0.037: RMSE $43.1 against $43.9 for always guessing the mean. The model ranks customers (the top
  decile earns far more than the bottom one), but it doesn't predict one customer's spend." The scatter's axes don't
  share a scale, so read it for ordering.
- **Demand**: pick a category to show actuals, the forecast and the 90% band, plus holdout MAPE per category.
  Say: "Mean MAPE is 39%, and the 90% band covers only about half the holdout, so the band is too narrow. I left out
  the last six days before the cutoff because the synthetic data spikes there." The header has both dates: source
  data to Nov 26, training to Nov 20.

### 1.5 Bridge (1 min, tab 5)
- In BigQuery, expand `vertex_demo_dashboard`: the 14 run-versioned tables the pipeline published.
- Say: "The dashboard reads these tables. A dashboard answers the questions you designed it for. Next, an agent
  answers the ones you didn't, over the same tables, and the platform decides what it's allowed to touch."

---

## Part 2: the agent (≈15 min)

### 2.1 Architecture in the console (2 min, tabs 6–7)
- **Agent Runtime engine** (tab 6), `agent-demo-agent`:
  - an ADK agent with gemini-3.7-flash;
  - it runs as an **Agent Identity**, its own principal and not a service account;
  - one warm instance.
  - Its deployment binds it to two gateways. If the page doesn't show the bindings, say it: the engine's spec has
    `clientToAgentConfig` → `agent-demo-ingress-gw` and `agentToAnywhereConfig` → `agent-demo-egress-gw`.
- **Agent Gateways** (tab 7): the two gateways.
  - `agent-demo-ingress-gw`, *client to agent*, between the chat UI and the agent. Model Armor screens every prompt
    and every answer here.
  - `agent-demo-egress-gw`, *agent to anywhere*, between the agent and everything it calls. It's default-deny: only
    hosts in the Agent Registry are reachable, and IAP authorizes each call, down to the MCP tool name.
- Say: "Everything you'll see on the right side of the chat is a platform record (Model Armor, gateway, IAP), not
  something the app wrote about itself."

### 2.2 Act 1: useful on your predictions (5–6 min, tab 8)
Use the preset buttons. Click **New session** (↻, top right) before each prompt. Earlier turns stay in the panel,
newest first.

| # | Preset | What they should see / say |
|---|---|---|
| 1 | Pipeline run | `get_latest_run` + `get_model_summary`, each allowed by the outbound gateway. The answer gives the same honest numbers as the dashboard (AUC-PR 0.23 vs 7%, r² 0.04, MAPE 39% with half coverage). Say: "It reads the evaluation tables, so it reports quality the way we just did." The button says "last night's", and the agent answers with the run's real date. |
| 2 | Demand forecast | `get_demand_outlook`; the top categories with ranges, plus the caveat that the bands are too narrow. |
| 3 | Churn risk | `list_top_churn_risk` (+ drivers). It notes the top scores are nearly tied (~0.98), so their order means little. |

Then, on any answered turn, click **Trace** at the bottom of its card. That opens Cloud Trace, one trace for the
whole turn:
- `apply_guardrail "Google Cloud Model Armor"` on the request and response paths (**Preview**: say so);
- `invoke_agent pipelines_analyst`;
- `call_llm` / `generate_content gemini-3.7-flash`;
- `execute_tool …`.

Say: "One trace, from the gateway's screening through the model and the tools."

### 2.3 Act 2: governed on both sides (6–7 min)

**4. Outbound deny: preset "Customer contact"** (new session first)
- The agent tries `get_customer_contact`. The outbound gateway answers **403 "Egress request is not authorized."** and
  the agent says the platform blocked it.
- The panel step for that tool shows the **IAP decision**: `granted: false`, status 7, this agent's principal. It
  arrives about 2–20 s after the call; keep talking while it lands.
- Console, two clicks:
  1. Tab 10, Logs Explorer: run the query below. The newest entry is that decision, with `mcp_tool_name:
     get_customer_contact`.
  2. The policy is one IAP binding on the MCP server's Agent Registry entry: `roles/iap.egressor` for this agent's
     principal, with the condition `mcp.toolName in [the six analytics tools]`. `get_customer_contact` isn't listed.
     If the console page for the registry entry is slow to find, show the backup command's output (below).
- Say: "The model wasn't asked nicely. The call never left the platform. Run the same agent code with no gateway and
  it returns the name, email and address. We tested that."

**5. Inbound block: preset "Maintenance mode"** (new session)
- The inbound gateway rejects it in about 0.2 s: **HTTP 403 "Model Armor: Prompt violates content security
  configurations"**. The panel shows **"Blocked by Model Armor (inbound gateway): prompt injection / jailbreak —
  HTTP 403"** and **"Agent did not run (no agent events)"**.
- Say: "The prompt never reached the agent. There's no agent span and no session event."

**6. Sensitive data: preset "SSN"** (new session)
- Blocked at the inbound gateway: the panel reads "Blocked by Model Armor (inbound gateway): sensitive data" with
  the info type US SSN. The same template also catches Brazilian CPF.
- Tab 9, the Model Armor template `agent-demo-armor`:
  - prompt injection/jailbreak at medium confidence;
  - RAI at high;
  - malicious URLs;
  - sensitive data through a DLP inspect template (US SSN + Brazil CPF).
- Say: "We tuned these on measurements. RAI at medium wrongly blocked a normal contact question, so it went to high.
  Prompt injection at high missed a hidden-tag injection, so it went to medium. The result: 11 of 11 attacks blocked,
  0 wrong blocks in 63 normal prompts."

### 2.4 Close (1 min, tab 7)
- Agent Gateways → the egress gateway's **Observability**: requests and denies over the last hour, including the
  ones you just made.
- Say what's what: Agent Gateway and Model Armor on the gateway are GA; the gateway and Model Armor spans in Cloud
  Trace are Preview.
- Last line: "Same data, two consumers. The pipeline made the predictions, and the platform governs who can ask
  about them and what the agent can reach."

---

## Paste-ready

**Logs Explorer, IAP tool decisions (step 4):**
```
logName="projects/your-project-id/logs/cloudaudit.googleapis.com%2Fdata_access"
protoPayload.serviceName="iap.googleapis.com"
protoPayload.methodName="GatekeeperAuthorizer.AuthorizeUser"
labels."iap.googleapis.com/audited_resource_type"="agentregistry.googleapis.com/McpServer"
```
**Model Armor verdicts from the gateway:**
```
logName="projects/your-project-id/logs/modelarmor.googleapis.com%2Fsanitize_operations"
labels."modelarmor.googleapis.com/client_name"="AGENT_GATEWAY"
```
**Gateway request rows:** `resource.type="networkservices.googleapis.com/Gateway"`. These are lossy for denies:
about 3 in 10 denies get a row. The IAP decision above is complete.

**Backup: show the tool policy from a terminal**
```bash
MCP_RID=$(gcloud agent-registry services describe us-central1-agent-demo-mcp --location=us-central1 \
  --project=your-project-id --format='value(registryResource)'); MCP_RID=${MCP_RID##*/}
gcloud beta iap web get-iam-policy --resource-type=agent-registry --mcp-server=$MCP_RID \
  --region=us-central1 --project=your-project-id
```
(The same policy, as applied, is in `infra/mcp-policy.json`.)

## 20-minute cut
Skip the tuning-job click (1.2), Schedules (1.3), the CLV page (1.4), the BigQuery bridge (1.5, say its line over
the dashboard instead), prompt 2, and the Model Armor template tab (say the settings over prompt 6).

## If something breaks
- **Chat UI misbehaves:** use the same prompts in the engine's **Playground** (tab 6). The records are still in Logs
  Explorer.
- **Panel record late:** Model Armor and IAP records can take 10–60 s. The card says what it's waiting for, and you
  can move on: earlier cards keep updating.
- **Dashboard slow on first load:** a cold start takes about 2 s.
- **Don't** run `pipeline/run.py --train-automl` (paid AutoML training, capped), and don't switch Model Armor modes
  (`infra/armor_mode.sh`) live.
- Recorded clean run: `demo/evidence/recording.*` (if made this morning).
