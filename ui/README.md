# Pipelines analyst chat UI

Chat with the `pipelines_analyst` agent on Agent Runtime, with a "Behind this answer" panel that shows,
per turn, only records the platform produced: ADK events from `streamQuery`, the HTTP status/body the
platform returned for it, and Cloud Logging entries: the IAP authorization decisions for the agent's MCP tool
calls, the outbound gateway's request rows and the Model Armor screenings the inbound gateway asked for. (The
inbound gateway writes no request rows of its own: its evidence is the Model Armor records plus the streamQuery
HTTP response.)

- `backend/` FastAPI (Python 3.12). `records.py` holds every field extraction (checked against real records,
  see `tests/fixtures/real_*`); `attribution.py` decides which turn a log record belongs to; `poller.py` is
  the one Cloud Logging poller per process; `turn.py` merges the agent stream and the poller's records into
  one SSE stream; `platform_clients.py` calls Agent Runtime and Cloud Logging with ADC; `replay.py` is the
  dev-only replay.
- `frontend/` React + Vite, design system copied from `vertex-pipelines-demo/app` (DESIGN.md, styles.css).

## Run locally

```bash
# backend
cd ui/backend
uv venv .venv -p 3.12 && uv pip install --python .venv/bin/python -r requirements-dev.txt
gcloud auth application-default login            # ADC: needs aiplatform + logging read on the project
ENGINE=projects/123456789012/locations/us-central1/reasoningEngines/<ID> \
  .venv/bin/uvicorn main:app --port 8080 --reload
.venv/bin/python -m pytest -q                    # backend tests

# frontend (dev server on :5173, proxies /api to :8080; API_TARGET overrides the target)
cd ui/frontend && npm install && npm run dev
npm test                                         # turn-state tests (node --test, no extra deps)
npm run build                                    # or build once and let the backend serve it at /
```

## Environment

| var | default | |
|---|---|---|
| `ENGINE` | (required for live use) | `projects/P/locations/L/reasoningEngines/ID`; validated at startup (a malformed value stops the backend). The Agent Runtime region is `L` |
| `REGION` | unset | optional; if set it must equal the region in `ENGINE` (startup error otherwise) |
| `PROJECT` | `your-project-id` | project for Cloud Logging and console links |
| `AGENT_ORG` | `111111111111` | organization in the agent's principal. The principal (`principal://agents.global.org-<ORG>.system.id.goog/resources/aiplatform/projects/<number>/locations/<L>/reasoningEngines/<ID>`) is built from `ENGINE`, which must name the project by **number**; otherwise no IAP decisions are polled. `/api/config` shows it as `agent_principal` |
| `INBOUND_GATEWAY` / `OUTBOUND_GATEWAY` | `agent-demo-ingress-gw` / `agent-demo-egress-gw` | names matched against `resource.labels.gateway_name` |
| `LOG_POLL_INTERVAL_S` | `3` (minimum 3) | one `entries.list` per tick for all open turns |
| `LOG_MAX_BACKOFF_S` | `60` | after HTTP 429 the delay doubles (6 s, 12 s, …) up to this |
| `LOG_LOOKBACK_S` | `0.5` | a turn's window starts this long before t0 |
| `LOG_LINGER_S` | `75` (minimum 75) | keep listening after the agent stream ends (Model Armor records arrive late) |
| `SEND_TRACEPARENT` | `1` | send a W3C `traceparent` with streamQuery; rows carrying that trace id are matched exactly |
| `RECORD_DIR` | unset | append every live turn's SSE messages to `<dir>/<date>-<session>.jsonl` (first line: `recording_meta`, provenance `recorded`) |
| `DEV_REPLAY` | unset | **local development only**: replay a `.jsonl` instead of calling the platform; the UI shows REPLAY plus the file's provenance (`recorded` / `constructed sample`) everywhere. Messages that match no recorded turn are refused |
| `ENGINE_CONSOLE_URL` | derived | console link used on Agent Runtime records (derived URL pattern is unverified) |
| `FRONTEND_DIST` | `../frontend/dist` | built frontend served at `/` |

Replay the three real turns recorded live on 2026-11-27 01:31-01:36Z, with IAP decisions (pipeline question,
contact request denied by IAP, injection blocked):
`ENGINE=<same engine> DEV_REPLAY=tests/fixtures/real_live2_demo.jsonl .venv/bin/uvicorn main:app --port 8080`
(ENGINE only gives `/api/config` the agent principal for the "this agent" identity line; replay calls nothing).
The earlier recording from 2026-11-26, made before the IAP decisions were polled, is `tests/fixtures/real_live_demo.jsonl`.
`tests/fixtures/SAMPLE_replay_constructed.jsonl` is a constructed sample (real records fed through the code
by fakes, `tests/make_constructed_sample.py`) and is labelled so.

## The panel: step view

For each turn the panel shows a step view of a few lines (`frontend/src/steps.js`, a pure function tested in
`steps.test.js` on the real recordings):
① the prompt screening, from the SANITIZE_USER_PROMPT record matched to the turn by text or trace;
② the agent's tool calls grouped by tool, each with the status the IAP decision gives (or, when no decision
arrived, the outbound gateway's tools/call row);
③ the answer screening, from the SANITIZE_MODEL_RESPONSE record matched by the answer text. The create-session
screening only appears in the timeline.

A line says "waiting for … record…" until its record arrives and "no record arrived" once the window closes. It
never shows ✓/✗ without a record. "×N" groups calls only when they all have the same status; when a tool's
records give more statuses than it had calls (e.g. a retried deny plus an allow), no line shows a count. A denied tool shows
what the IAP record says (`granted false · status 7 …`), adds "· gateway 403" when the gateway's own DENIED row also
arrived, and has one identity line. A blocked turn (the platform's HTTP 403 plus the MATCH_FOUND record) shows the
filters in words. Each line links to its record(s); several insertIds become one Logs Explorer query. Everything
else stays behind "All platform records (N)", which opens the detailed timeline below unchanged: session API,
telemetry, generateContent and CONNECT rows, plus the per-source counts. The footer's "N Gemini rows in the
gateway log" counts outbound gateway generateContent rows matched by time only; the gateway log may have fewer
rows than the agent's model calls.

## How a turn works

The browser creates the ADK session when the first message of a session is sent (so the create-session
records fall into that turn's window). `POST /api/chat` records t0, opens `:streamQuery?alt=sse` (never
retried) and registers the turn with the shared poller. The poller makes **one** `entries.list` per tick
(>= 3 s apart, pageSize 1000; extra pages count as extra calls and stretch the next sleep; 429 backs off
exponentially) over the union of the open turn windows, filtered to the two gateways' request rows, Model
Armor records with `client_name = AGENT_GATEWAY`, and (third OR clause of the same filter) the IAP decisions
`cloudaudit.googleapis.com%2Fdata_access` / `iap.googleapis.com` / `GatekeeperAuthorizer.AuthorizeUser` with
`audited_resource_type = agentregistry.googleapis.com/McpServer` and `principalSubject` = the agent's principal. A turn listens until 75 s after its stream ends; the next
question can be asked meanwhile, and every turn keeps its own window (a newer turn caps older windows at its
own window start, so windows never overlap).

Message kinds: `turn_start` (`trace_id_sent`), `listening`, `adk_event` (`summary.is_error` for in-stream
errors in all three shapes), `http_error`, `transport_error` (UI backend failure: a chat alert, never a panel
record), `stream_end`, `log` (`source`: `outbound_gateway` | `model_armor` | `iap_decision` | …, `summary`, `raw`, `console_url`, `trace_url`, `hidden`,
`attribution {method, detail}`), `log_poll_error`, `turn_end`, `replay_unmatched` (replay only).

Which turn a record belongs to (`attribution.py`, checked on the real records):

| record | matched by | label |
|---|---|---|
| outbound row | URL contains the turn's session id | matched: session id in the request URL |
| outbound row | trace = the `traceparent` sent with this turn (live 2026-11-26: MCP rows carry it) | matched: trace id the UI sent |
| Model Armor | decoded `byteData` = the turn's message (prompt) / contains its session id (create-session response) / is its answer | matched: screened text … |
| Model Armor | `client_correlation_id` of the turn's prompt screening, or the sent trace id | matched: … |
| outbound row, nothing above | timestamp in the turn's window | **attributed by time** |
| IAP decision (McpServer, `tools/call`) | `principalSubject` = the agent's principal AND timestamp in the turn's window AND `mcp_tool_name` = a `function_call` name in the turn's ADK events (several candidates: the closest call; no call of that tool: not attributed) | matched: agent principal + tool name + time |
| Model Armor, nothing above | held; on the turn's last poll shown collapsed as "matches nothing of this turn" | |

The session rows carry no trace (only MCP rows do), so the "session row → trace → rows" chain the review
proposed finds nothing on today's records; it is kept generic. Rows without authz result and without request
URL or status (CONNECT tunnels) are collapsed; a row with an authz result (so every DENIED row) is never hidden.
`denied` means `authzPolicyInfo.result = DENIED` only; the HTTP status is shown separately.

IAP decisions (the primary platform record of outbound authorization for MCP tool calls). The gateway's
request log is lossy for denies: in the 10x eval of 2026-11-26 22:13Z it wrote the `403 DENIED tools/call
get_customer_contact` row for 3 of 10 denied calls; the IAP decision audit log has all 10 (one read of
22:13:00-22:14:00Z with the UI's filter: 10 `granted: false` + 17 granted `tools/call` + 30 `tools/list`).
The panel shows each `tools/call` decision as "Outbound gateway · IAP decision": `tools/call <tool>`, `granted:
false · status 7 Permission Denied.` (or `granted: true`) as recorded, principal, permission, policy version,
dryRun and a Logs Explorer link; a `granted: false` decision is shown as a deny, and decisions for the same tool
are grouped. Proto JSON may omit a false boolean: a missing `granted` counts as false only with status code 7
(labelled "field omitted in the record"), otherwise "granted not in record". The records carry no trace or
session id, so the label never claims one. Decisions about Google API `Endpoint`s are not shown;
`tools/list`/handshake decisions carry no tool name and are not attributed. The summary line counts
"IAP: N allowed / M denied" next to the gateway rows. Real samples: `tests/fixtures/real_iap_*`
(`frontend/src/fixtures/real_iap_messages.json` is the backend's output for them, kept in sync by a test).

Recorded on 2026-11-26 21:42 (`real_live_q4_contact.jsonl`): the agent's `get_customer_contact` call came
back with an error naming the outbound gateway, but no 403/DENIED gateway row was written for it (none in
Cloud Logging 3.5 min later). The panel shows the ADK tool result as recorded and no deny row.

## Deploy notes (not done here)

- `docker build -t agent-demo-ui ui/` (two stages: node:22-slim builds Vite, python:3.12-slim runs uvicorn on `$PORT`).
- Cloud Run behind IAP, env `ENGINE`, **max-instances=1** (the poller is per process and the Cloud Logging
  read quota, ~60 `entries.list`/min, is per project and shared), request timeout >= 300 s (a turn's SSE stays
  open ~75 s after the answer).
- Runtime service account needs: `aiplatform.reasoningEngines.query` (e.g. `roles/aiplatform.user`) on the engine,
  `roles/logging.viewer` (entries.list) on the project, plus whatever the inbound gateway requires of callers.
  The IAP decisions are in the Data Access audit log, a private log: reading it needs
  `logging.privateLogEntries.list` (`roles/logging.privateLogViewer`), which `roles/logging.viewer` does not
  include. Not verified for the Cloud Run account here. If Cloud Logging answers the query with HTTP 403, the
  poller drops the IAP clause, says so once as a poll error, and keeps polling gateway rows and Model Armor
  records; later turns show "IAP: decisions not polled". If it instead returns no audit entries silently, the
  panel shows "IAP: no decision records".
- The backend sends `: keepalive` SSE comments every 10 s and `X-Accel-Buffering: no`.
