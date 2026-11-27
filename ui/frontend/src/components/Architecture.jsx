import React from "react";
import { ExternalLink } from "lucide-react";

// Static architecture sheet. Names and settings match infra/ (checked against the live resources on
// 2026-11-27); the engine, gateways and project come from /api/config when available. The step numbers
// ① ② ③ are the same as the steps in the chat panel.

const base = (name) => (name ? String(name).replace(/\/+$/, "").split("/").pop() : null);

function Step({ n }) {
  return <span className="step-n num arch-step"><span className="sr-only">step </span>{n}</span>;
}

function Console({ href, label = "Console" }) {
  if (!href) return null;
  return (
    <a className="arch-link" href={href} target="_blank" rel="noreferrer">
      {label} <ExternalLink size={12} aria-hidden="true" />
    </a>
  );
}

function Node({ kind, name, children, right, tone = "", className = "" }) {
  return (
    <section className={`box arch-node ${tone ? `arch-${tone}` : ""} ${className}`}>
      <div className="box-head">
        <span>{kind}</span>
        {right && <span className="arch-right">{right}</span>}
      </div>
      <div className="box-body">
        <div className="arch-name">{name}</div>
        {children && <ul className="arch-facts">{children}</ul>}
      </div>
    </section>
  );
}

function Down({ label, from = "main" }) {
  return (
    <div className={`arch-v arch-v-${from}`} aria-hidden="true">
      <span className="arch-v-line" />
      {label && <span className="arch-v-label">{label}</span>}
    </div>
  );
}

function Side({ steps, label }) {
  return (
    <div className="arch-h" aria-hidden="true">
      <span className="arch-h-line" />
      <span className="arch-h-label">
        {steps?.map((n) => <Step key={n} n={n} />)}
        {label && <span>{label}</span>}
      </span>
    </div>
  );
}

export default function Architecture({ config }) {
  const project = config?.project || "…";
  const region = config?.region || "…";
  const inbound = base(config?.gateways?.inbound) || "agent-demo-ingress-gw";
  const outbound = base(config?.gateways?.outbound) || "agent-demo-egress-gw";
  const engineId = base(config?.engine);
  const links = config?.links || {};

  return (
    <main className="arch-view" id="view-architecture" role="tabpanel" aria-labelledby="tab-architecture">
      <div className="arch-sheet">
        <header className="arch-head">
          <div>
            <h1>Architecture</h1>
            <p className="arch-sub">
              One project, <b>{project}</b>: Agent Runtime and both gateways in {region}. Prompts enter through the
              inbound gateway; every call the agent makes leaves through the outbound one. Steps{" "}
              <Step n={1} /> <Step n={2} /> <Step n={3} /> match the chat panel.
            </p>
          </div>
        </header>

        <div className="arch-diagram">
          {/* chat UI */}
          <Node kind="Cloud Run · behind IAP (Google sign-in)" name="agent-demo-ui (this app)">
            <li>Calls the agent with <code>streamQuery</code> and a trace id</li>
            <li>Reads the platform's records for the panel</li>
          </Node>
          <div />
          <div />
          <Down label="prompt, streamed answer" />
          <div />
          <div />

          {/* inbound gateway + Model Armor */}
          <Node kind="Agent Gateway · inbound" name={inbound} tone="gw" right={<Console href={links.gateways} />}>
            <li>Client → agent: sits in front of Agent Runtime</li>
            <li>Screens every prompt <Step n={1} /> and the agent's final answer <Step n={3} /></li>
          </Node>
          <Side steps={[1, 3]} />
          <Node kind="Model Armor" name="template agent-demo-armor" tone="ctl" right="inspect and block">
            <li>Prompt injection and jailbreak (medium+), responsible AI (high)</li>
            <li>Malicious URLs, sensitive data (US SSN, Brazil CPF)</li>
            <li className="arch-outcome">Blocked prompt: HTTP 403, the agent never runs</li>
          </Node>
          <Down label="screened prompt" />
          <div />
          <div />

          {/* agent */}
          <Node kind="Agent Runtime" name="agent-demo-agent" right={<Console href={links.engine} />}>
            <li>Runs as its own <b>Agent Identity</b>, not a service account</li>
            <li>ADK 2.3 · {engineId ? <>engine …{engineId.slice(-8)} · </> : null}1–3 instances, 1 always warm</li>
          </Node>
          <div />
          <div />
          <Down label="every outbound call: model and tools" />
          <div />
          <div />

          {/* outbound gateway + IAP / Agent Registry */}
          <Node kind="Agent Gateway · outbound" name={outbound} tone="gw" right={<Console href={links.gateways} />}>
            <li>Agent → anywhere: every call the agent makes</li>
            <li>Authorizes each call <Step n={2} />, down to the MCP tool</li>
          </Node>
          <Side steps={[2]} />
          <Node kind="IAP + Agent Registry" name="default deny" tone="ctl">
            <li>Only registered hosts: 34 Google API hosts + the MCP server</li>
            <li>Tool policy for this agent's identity: 6 analytics tools</li>
            <li className="arch-outcome">
              <code>get_customer_contact</code> isn't listed: HTTP 403
            </li>
          </Node>
          <div className="arch-split" aria-hidden="true">
            <span className="s-stem" />
            <span className="s-bar" />
            <span className="s-drop s-left" />
            <span className="s-drop s-right" />
          </div>
          <div />
          <div />

          {/* destinations */}
          <div className="arch-pair">
            <Node kind="Gemini · global endpoint" name="gemini-3.7-flash">
              <li>aiplatform.googleapis.com</li>
            </Node>
            <Node kind="Cloud Run · IAM only" name="agent-demo-mcp">
              <li>MCP server, 7 read-only tools</li>
              <li>No token: HTTP 403</li>
            </Node>
          </div>
          <Side label="SQL" />
          <Node kind="BigQuery · US" name="vertex_demo_dashboard">
            <li>14 tables written by the Vertex AI Pipelines run</li>
            <li className="arch-aside">
              Contact data: <code>bigquery-public-data.thelook_ecommerce.users</code> (public, fictitious), read only
              by the denied tool
            </li>
          </Node>
        </div>

        <section className="arch-band" aria-label="Observability">
          <h2>Observability</h2>
          <div className="arch-band-grid">
            <Node kind="Cloud Trace" name="one trace per turn" right={<Console href={links.traces} label="Traces" />}>
              <li>Gateway and Model Armor spans (Preview), the agent, Gemini calls, tool calls</li>
              <li>The chat UI sends the trace id with each request</li>
            </Node>
            <Node kind="Cloud Logging" name="platform records">
              <li>Model Armor verdicts on the prompt <Step n={1} /> and the answer <Step n={3} /></li>
              <li>IAP decision for each tool call <Step n={2} /> (Data Access audit)</li>
              <li>Outbound gateway request rows</li>
            </Node>
            <Node kind="Chat panel" name="platform records only">
              <li>These records, plus Agent Runtime's events and the HTTP status</li>
              <li>Matched to each turn by trace id, session, screened text, or agent + tool + time</li>
            </Node>
          </div>
        </section>
      </div>
    </main>
  );
}
