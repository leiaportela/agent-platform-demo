import React from "react";
import { Ban, CheckCircle2, ExternalLink, ShieldAlert, ShieldCheck, AlertTriangle, Wrench, MessageSquareText, Brain, CornerDownLeft, Clock } from "lucide-react";
import { compact, host, hostname, int, plus, pretty } from "../format.js";
import { ATTRIBUTION } from "../turns.js";

export const SOURCES = {
  agent_runtime: { label: "Agent Runtime", swatch: "var(--s-work)" },
  inbound_gateway: { label: "Inbound gateway", swatch: "var(--s-verification)" },
  outbound_gateway: { label: "Outbound gateway", swatch: "var(--s-verification)" },
  gateway: { label: "Gateway (name not in record)", swatch: "var(--s-verification)" },
  model_armor: { label: "Inbound gateway · Model Armor", swatch: "var(--s-setup)" },
  iap_decision: { label: "Outbound gateway · IAP decision", swatch: "var(--s-verification)" },
  platform_error: { label: "Platform HTTP response", swatch: "var(--bad)" },
};

export function sourceOf(r) {
  if (r.kind === "adk_event") return "agent_runtime";
  if (r.kind === "http_error") return "platform_error";
  return r.source || "gateway";
}

function Chip({ source, replay }) {
  const s = SOURCES[source] || SOURCES.gateway;
  return (
    <span className="chip-wrap">
      <span className="chip"><span className="swatch" style={{ background: s.swatch }} />{s.label}</span>
      {replay && <span className="replay-chip">REPLAY</span>}
    </span>
  );
}

function Verdict({ bad, neutral, children }) {
  if (neutral) return <span className="verdict neutral">{children}</span>;
  return (
    <span className={`verdict ${bad ? "bad" : "good"}`}>
      {bad ? <Ban size={14} aria-hidden="true" /> : <CheckCircle2 size={14} aria-hidden="true" />}
      {children}
    </span>
  );
}

function Row({ k, children }) {
  if (children == null || children === "" || (Array.isArray(children) && !children.length)) return null;
  return <div className="kv"><span>{k}</span><b>{children}</b></div>;
}

const NotInRecord = () => <span className="muted">not in record</span>;

function Raw({ value }) {
  return (
    <details className="raw-details">
      <summary>Raw JSON</summary>
      <pre className="raw">{pretty(value)}</pre>
    </details>
  );
}

function Tokens({ usage }) {
  if (!usage) return null;
  return (
    <Row k="Tokens">
      {int(usage.prompt)} in · {int(usage.output)} out{usage.thoughts ? ` · ${int(usage.thoughts)} thinking` : ""}
    </Row>
  );
}

function Attribution({ a }) {
  if (!a) return null;
  const byTime = a.method === "time";
  return (
    <div className={`attribution${byTime ? " by-time" : ""}`} title={a.detail}>
      {byTime && <Clock size={12} aria-hidden="true" />}
      {ATTRIBUTION[a.method] || a.method}
    </div>
  );
}

function AdkBody({ r, config }) {
  const s = r.summary || {};
  if (!s.parsed) return { title: "Unparsed stream line", icon: AlertTriangle, body: <pre className="raw small">{String(r.raw)}</pre> };
  const bad = !!s.is_error || (s.responses || []).some((x) => x.error);
  let title = "Event", icon = MessageSquareText;
  if (s.calls?.length) { title = s.calls.length > 1 ? `${s.calls.length} tool calls` : "Tool call"; icon = Wrench; }
  else if (s.responses?.length) { title = "Tool result"; icon = CornerDownLeft; }
  else if (s.text) { title = s.partial ? "Answer (partial)" : "Answer"; }
  else if (s.thought) { title = "Model step (thinking)"; icon = Brain; }
  if (s.is_error) { title = "Error event in the stream"; icon = AlertTriangle; }
  const body = (
    <>
      <Row k="Author">{s.author}</Row>
      {(s.calls || []).map((c, i) => (
        <div key={`c${i}`} className="call">
          <code className="tool">{c.name}</code>
          <code className="args">{compact(c.args)}</code>
        </div>
      ))}
      {(s.responses || []).map((x, i) => (
        <div key={`r${i}`} className="call">
          <code className="tool">{x.name}</code>
          {x.error ? <Verdict bad>Error returned: {x.error}</Verdict> : <span className="muted">returned a result</span>}
        </div>
      ))}
      {s.text && !s.calls?.length && <Row k="Text">{int(s.text.length)} characters</Row>}
      {s.is_error && <Row k="Error code">{s.error_code ?? <NotInRecord />}</Row>}
      {s.is_error && <Row k="Message">{s.error_message ?? <NotInRecord />}</Row>}
      <Tokens usage={s.usage} />
    </>
  );
  return { title, icon, bad, body, link: config?.links?.engine ? { href: config.links.engine, label: "Agent Runtime console" } : null };
}

function GatewayRows({ r }) {
  const rows = r.group || [r];
  const s = r.summary || {};
  const bad = rows.some((x) => x.summary?.denied);
  const title = s.mcp_method
    ? `MCP ${s.mcp_method}${s.tool ? ` · ${s.tool}` : ""}`
    : s.method ? `${s.method} ${s.url ? hostname(s.url) : "request"}` : "Gateway row";
  const result = s.authz_result;
  const body = (
    <>
      <Row k="Tool">{s.tool && <code className="tool">{s.tool}</code>}</Row>
      <div className="kv">
        <span>Authz result</span>
        <b>{result
          ? <Verdict bad={s.denied} neutral={!s.denied && result !== "ALLOWED"}>{result}</Verdict>
          : <NotInRecord />}</b>
      </div>
      <div className="kv"><span>HTTP status</span><b>{s.status != null ? s.status : <NotInRecord />}</b></div>
      {rows.length > 1 && <Row k="Rows">{rows.length} log rows with the same tool and authz result DENIED</Row>}
      <Row k="Policy">{(s.policies || []).join(", ")}</Row>
      <Row k="Target">{s.url && host(s.url)}</Row>
      <Row k="Latency">{s.latency}</Row>
    </>
  );
  return { title, icon: bad ? Ban : null, bad, body, rows };
}

// IAP authorization decision: the record's authorizationInfo[0].granted and status, exactly as recorded.
function IapDecision({ s }) {
  const status = s.status_code != null ? ` · status ${s.status_code}${s.status_message ? ` ${s.status_message}` : ""}` : "";
  if (s.granted === false) {
    return <Verdict bad>{s.granted_in_record ? "granted: false" : "granted: false (field omitted in the record)"}{status}</Verdict>;
  }
  if (s.granted === true) return <Verdict>granted: true{status}</Verdict>;
  return <span><span className="muted">granted not in record</span>{status}</span>;
}

function IapBody({ r }) {
  const rows = r.group || [r];
  const s = r.summary || {};
  const bad = rows.some((x) => x.summary?.denied);
  const title = `${s.mcp_method || "MCP method not in record"}${s.tool ? ` ${s.tool}` : ""}`;
  const server = s.resource_name ? String(s.resource_name).split("/").pop() : null;
  const body = (
    <>
      <div className="kv"><span>Decision</span><b><IapDecision s={s} /></b></div>
      <Row k="Tool">{s.tool && <code className="tool">{s.tool}</code>}</Row>
      {rows.length > 1 && <Row k="Decisions">{rows.length} IAP decisions for the same tool with granted: false</Row>}
      <div className="kv"><span>Principal</span><b>{s.principal ? <code className="principal">{s.principal}</code> : <NotInRecord />}</b></div>
      <Row k="Permission">{s.permission}</Row>
      <div className="kv"><span>Policy version</span><b>{s.policy_version || <NotInRecord />}</b></div>
      <div className="kv"><span>dryRun</span><b>{s.dry_run != null ? String(s.dry_run) : <NotInRecord />}</b></div>
      <Row k="MCP server">{server && <span title={s.resource_name}>{server}</span>}</Row>
      <Row k="Target">{s.url && host(s.url)}</Row>
    </>
  );
  return { title, icon: bad ? Ban : ShieldCheck, bad, body, rows };
}

const OP_TITLES = { SANITIZE_USER_PROMPT: "Prompt screening", SANITIZE_MODEL_RESPONSE: "Response screening" };

function ArmorResult({ s }) {
  if (s.match_state === "MATCH_FOUND") return <span className="verdict bad"><ShieldAlert size={14} aria-hidden="true" />MATCH_FOUND</span>;
  if (s.match_state === "NO_MATCH_FOUND") return <span className="verdict good"><ShieldCheck size={14} aria-hidden="true" />No match (NO_MATCH_FOUND)</span>;
  if (s.match_state) return <span>{s.match_state}</span>;
  return <span className="muted">state not in record</span>;
}

function ArmorBody({ r }) {
  const s = r.summary || {};
  const op = s.operation_type;
  const title = OP_TITLES[op] || `Sanitize operation: ${op || "type not in record"}`;
  const bad = !!s.matched;
  const matched = (s.filters || []).filter((f) => f.matched);
  const text = s.screened_text;
  const body = (
    <>
      <div className="kv"><span>Result</span><b><ArmorResult s={s} /></b></div>
      <Row k="Verdict">{s.verdict}</Row>
      {matched.map((f) => (
        <Row key={`mf-${f.name}`} k="Matched filter">
          {f.name}
          {f.info_types?.length ? ` (${f.info_types.join(", ")})` : ""}
          {f.matched_types?.length ? ` (${f.matched_types.join(", ")})` : ""}
          {f.confidence ? ` · confidence ${f.confidence}` : ""}
        </Row>
      ))}
      <Row k="Reason">{s.verdict_reason}</Row>
      <Row k="Operation">{op || <NotInRecord />}</Row>
      <Row k="Client">{s.client_name}</Row>
      <Row k="Template">{s.template_id}</Row>
      {text != null && (
        <details className="screened">
          <summary>Screened text (decoded from the record){s.screened_text_truncated ? " · truncated" : ""}</summary>
          <pre className="raw">{text}</pre>
        </details>
      )}
    </>
  );
  return { title, icon: bad ? ShieldAlert : ShieldCheck, bad, body };
}

function HttpBody({ r }) {
  return {
    title: "streamQuery response",
    icon: Ban,
    bad: true,
    body: (
      <>
        <div className="kv"><span>Response</span><b><Verdict bad>HTTP {r.status} returned by the platform</Verdict></b></div>
        <Row k="Message">{r.message}</Row>
        <Row k="Status">{r.body?.error?.status}</Row>
      </>
    ),
  };
}

export default function RecordBox({ r, config }) {
  const source = sourceOf(r);
  let view;
  if (r.kind === "adk_event") view = AdkBody({ r, config });
  else if (r.kind === "http_error") view = HttpBody({ r });
  else if (source === "model_armor") view = ArmorBody({ r });
  else if (source === "iap_decision") view = IapBody({ r });
  else view = GatewayRows({ r });
  const Icon = view.icon;
  const rows = view.rows || [r];
  return (
    <li className={`rec${view.bad ? " bad" : ""}`}>
      <div className="rec-head">
        <Chip source={source} replay={r.replay} />
        <span className="rec-title">{Icon && <Icon size={14} aria-hidden="true" />}{view.title}</span>
        <span className="rec-t num" title="Arrival time since the turn started">{plus(r.t_ms)}{rows.length > 1 ? ` · ${plus(rows[rows.length - 1].t_ms)}` : ""}</span>
      </div>
      <div className="rec-body">
        {view.body}
        <Attribution a={r.attribution} />
      </div>
      <div className="rec-foot">
        {rows.map((x) => (
          <span key={`links-${x.id}`} className="rec-links">
            {rows.length > 1 && <span className="muted">{source === "iap_decision" ? "Decision" : "Row"} {rows.indexOf(x) + 1}</span>}
            {x.console_url && <a href={x.console_url} target="_blank" rel="noreferrer">Logs Explorer <ExternalLink size={12} /></a>}
            {x.trace_url && <a href={x.trace_url} target="_blank" rel="noreferrer">Trace <ExternalLink size={12} /></a>}
            {x.trace_url && <span className="preview">Preview</span>}
          </span>
        ))}
        {view.link && <a href={view.link.href} target="_blank" rel="noreferrer">{view.link.label} <ExternalLink size={12} /></a>}
        {rows.map((x) => <Raw key={`raw-${x.id}`} value={r.kind === "http_error" ? { status: x.status, body: x.body } : x.raw} />)}
      </div>
    </li>
  );
}
