import React from "react";
import { CheckCircle2, ChevronDown, ChevronRight, ExternalLink, Minus, XCircle } from "lucide-react";
import { int, secs } from "../format.js";
import { buildSteps, kilo } from "../steps.js";

// The compact view of one turn: a few lines grouped by step (steps.js decides every line and status).

function ToneIcon({ tone }) {
  if (tone === "good") return <CheckCircle2 size={14} aria-hidden="true" />;
  if (tone === "bad") return <XCircle size={14} aria-hidden="true" />;
  if (tone === "waiting") return <span className="wait-dot pulse" aria-hidden="true" />;
  if (tone === "none") return <Minus size={14} aria-hidden="true" />;
  return null;
}

function Status({ status, className = "" }) {
  if (!status) return <span className={`st ${className}`} />;
  return (
    <span className={`st st-${status.tone} ${className}`}>
      <ToneIcon tone={status.tone} />
      <span>{status.text}</span>
    </span>
  );
}

function RecordLink({ link }) {
  if (!link) return <span className="step-link" />;
  const label = link.n > 1 ? "records" : "record";
  return (
    <span className="step-link">
      <a href={link.href} target="_blank" rel="noreferrer" title={`Logs Explorer: ${link.n} platform record${link.n === 1 ? "" : "s"}`}>
        {label} <ExternalLink size={12} aria-hidden="true" />
      </a>
    </span>
  );
}

function Row({ r }) {
  if (r.kind === "step") {
    const bad = r.tone === "bad";
    return (
      <li className={`step-row${bad ? " bad" : ""}${r.tone === "waiting" ? " waiting" : ""}`}>
        <span className="step-n num" aria-hidden="true">{r.n}</span>
        <span className="step-label">
          <span className="sr-only">Step {r.n}: </span>
          {bad && <XCircle size={15} aria-hidden="true" />}
          {r.label}
        </span>
        <Status status={r.status} className="step-status" />
        <RecordLink link={r.link} />
      </li>
    );
  }
  if (r.kind === "tool") {
    return (
      <li className={`tool-row${r.status.tone === "bad" ? " bad" : ""}`}>
        <span />
        <span className="step-tool" title={r.countNote || undefined}>
          <code>{r.tool}</code>
          {r.count > 1 && <span className="times num">×{r.count}</span>}
          {r.noCall && <span className="muted small"> (no agent event)</span>}
        </span>
        <Status status={r.status} className={`tool-status${r.wide ? " wide" : ""}`} />
        {!r.wide && <RecordLink link={r.link} />}
      </li>
    );
  }
  if (r.kind === "identity") {
    return (
      <li className="identity-row bad">
        <span />
        <span className="identity" title={r.title}>{r.text}</span>
        <RecordLink link={r.link} />
      </li>
    );
  }
  return (
    <li className="note-row">
      <span />
      <Status status={r.status} className="note-status" />
      <span className="step-link" />
    </li>
  );
}

export default function StepView({ turn, config, open, onToggle, panelId }) {
  const { rows, footer } = buildSteps(turn, { agentPrincipal: config?.agent_principal || null });
  const t = footer.tokens;
  return (
    <section className="steps" aria-label="What happened, by step">
      <ol className="step-list">
        {rows.map((r) => <Row key={r.key} r={r} />)}
      </ol>
      <div className="steps-foot num">
        <span className="steps-facts">
          {t && (
            <span title={`${int(t.in)} in · ${int(t.out)} out${t.thinking ? ` · ${int(t.thinking)} thinking` : ""} (${t.responses} model response${t.responses === 1 ? "" : "s"} in the agent events)`}>
              {kilo(t.total)} tokens
            </span>
          )}
          <span>{footer.latencyMs != null ? secs(footer.latencyMs) : turn.streamEnded ? "–" : "running…"}</span>
          {footer.trace && (
            <span className="steps-traces">
              <a href={footer.trace} target="_blank" rel="noreferrer"
                title={`Cloud Trace (Preview)${footer.traceCount > 1 ? `; ${footer.traceCount} traces in this turn's records, all linked under All platform records` : ""}`}>
                Trace <ExternalLink size={12} aria-hidden="true" />
              </a>
            </span>
          )}
          {footer.gemini > 0 && (
            <span className="muted"
              title={`Outbound gateway log rows for Gemini generateContent, matched to this turn by time only. The gateway log may not have a row for every model call${t ? ` (the agent events show ${t.responses} model response${t.responses === 1 ? "" : "s"})` : ""}. Each row is under All platform records.`}>
              {footer.gemini} Gemini row{footer.gemini === 1 ? "" : "s"} in the gateway log
            </span>
          )}
        </span>
        <button type="button" className="expander" aria-expanded={open} aria-controls={panelId} onClick={onToggle}>
          All platform records ({footer.total}) {open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
        </button>
      </div>
    </section>
  );
}
