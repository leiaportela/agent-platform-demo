import React, { useEffect, useRef, useState } from "react";
import { Ban, ExternalLink, Radio, ShieldAlert, ShieldCheck } from "lucide-react";
import { secs, int } from "../format.js";
import { armorState, summarize } from "../turns.js";
import { buildSteps } from "../steps.js";
import RecordBox from "./RecordBox.jsx";
import StepView from "./StepView.jsx";

function useNow(active) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return undefined;
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [active]);
  return now;
}

function ArmorVerdict({ label, list, ended }) {
  const st = armorState(list);
  let value;
  if (st.kind === "none") value = <b className="muted">{ended ? "no record" : "listening…"}</b>;
  else if (st.kind === "match") value = <b className="bad-text"><ShieldAlert size={13} aria-hidden="true" /> MATCH_FOUND: {st.filters.join(", ") || "filter not in record"}</b>;
  else if (st.kind === "no_match") value = <b className="good-text"><ShieldCheck size={13} aria-hidden="true" /> No match</b>;
  else value = <b>{st.states.join(", ")}</b>;
  return <span>{label} {value}</span>;
}

function GatewayCounts({ g, ended }) {
  if (!g.rows) return <span>Gateway <b className="muted">{ended ? "no gateway records" : "no gateway records yet"}</b></span>;
  const parts = Object.entries(g.results).map(([result, n]) => (
    <b key={`gw-${result}`} className={result === "DENIED" ? "bad-text" : ""}>
      {result === "DENIED" && <Ban size={13} aria-hidden="true" />} {n} {result}
    </b>
  ));
  if (g.noResult) parts.push(<b key="gw-none">{g.noResult} without authz result</b>);
  return (
    <span className="gw-counts">Gateway rows {parts.map((p, i) => <React.Fragment key={`sep-${i}`}>{i > 0 && " · "}{p}</React.Fragment>)}</span>
  );
}

// "IAP: N allowed / M denied", counted from the IAP decision records (tools/call) attributed to this turn.
function IapCounts({ iap, ended, polled }) {
  if (!iap.decisions && polled === false) return <span>IAP: <b className="muted">decisions not polled</b></span>;
  if (!iap.decisions) return <span>IAP: <b className="muted">{ended ? "no decision records" : "no decision records yet"}</b></span>;
  return (
    <span className="gw-counts">
      IAP: <b>{iap.allowed} allowed</b> / <b className={iap.denied ? "bad-text" : ""}>{iap.denied > 0 && <Ban size={13} aria-hidden="true" />} {iap.denied} denied</b>
      {iap.unknown > 0 && <> / <b>{iap.unknown} granted not in record</b></>}
    </span>
  );
}

function SummaryLine({ turn }) {
  const s = summarize(turn);
  const others = [...new Set(s.armor.other.map((a) => a.operation_type || "type not in record"))];
  return (
    <div className="summary-line num" aria-label="Turn summary">
      <span>Latency <b>{turn.streamEnded ? secs(turn.latencyMs) : "running"}</b></span>
      <span>Tool calls <b>{s.toolCalls}</b>{s.toolErrors ? <>{" "}<b className="bad-text">({s.toolErrors} error{s.toolErrors === 1 ? "" : "s"})</b></> : null}</span>
      <IapCounts iap={s.iap} ended={turn.ended} polled={turn.iapPolled} />
      <GatewayCounts g={s.gateway} ended={turn.ended} />
      <span>Model Armor: </span>
      <ArmorVerdict label="prompt" list={s.armor.prompt} ended={turn.ended} />
      <ArmorVerdict label="response" list={s.armor.response} ended={turn.ended} />
      {others.length > 0 && <span>other operations <b>{others.join(", ")}</b></span>}
      {s.tokensIn > 0 && <span>Tokens <b>{int(s.tokensIn)} in / {int(s.tokensOut)} out</b></span>}
      {turn.traceUrls?.length > 0 && (
        <span>
          {turn.traceUrls.map((u, i) => (
            <a key={`tr-${u}`} href={u} target="_blank" rel="noreferrer">Trace{turn.traceUrls.length > 1 ? ` ${i + 1}` : ""} <ExternalLink size={12} /></a>
          ))}
          <span className="preview">Preview</span>
        </span>
      )}
    </div>
  );
}

function PollError({ turn }) {
  const e = turn.pollErrors[turn.pollErrors.length - 1];
  if (!e) return null;
  return <span className="bad-text">Cloud Logging {e.status ? `returned HTTP ${e.status}` : "read failed"}{e.message ? `: ${e.message}` : ""}</span>;
}

// What the step view is still waiting for (lines whose status is "waiting…"), in plain words.
function pendingItems(turn, config) {
  const { rows } = buildSteps(turn, { agentPrincipal: config?.agent_principal || null });
  const names = { prompt: "prompt screening", answer: "answer screening" };
  const out = [];
  for (const r of rows) {
    const waiting = r.status?.tone === "waiting" || (r.kind === "step" && r.tone === "waiting");
    if (!waiting) continue;
    if (r.kind === "tool") out.push(`${r.tool} decision`);
    else if (r.kind === "step" && names[r.key]) out.push(names[r.key]);
  }
  return [...new Set(out)];
}

function Listening({ turn, config }) {
  const linger = config?.linger_s;
  const active = !turn.ended;
  const now = useNow(active);
  const elapsed = now - turn.startedAt;
  if (turn.ended) {
    return (
      <div className="listening closed compact">
        <span>Log window closed{turn.cut ? " (connection ended early)" : ""}.</span>
        <PollError turn={turn} />
      </div>
    );
  }
  const until = turn.listeningUntilMs;
  const left = until != null ? Math.max(0, until - (now - (turn.listeningSince || turn.startedAt))) : null;
  const pending = turn.streamEnded ? pendingItems(turn, config) : null;
  if (pending && pending.length === 0) {
    // Every step line has its record: nothing to explain; extra records still land in "All platform records".
    return (
      <div className="listening closed compact" role="status">
        <span><ShieldCheck size={13} aria-hidden="true" /> All records for this turn's steps arrived.</span>
        <PollError turn={turn} />
      </div>
    );
  }
  const pct = until != null ? Math.min(100, ((until - left) / until) * 100) : null;
  return (
    <div className="listening" role="status" aria-live="polite">
      <span className="listening-label"><Radio size={14} aria-hidden="true" className="pulse" /> Listening for logs</span>
      <span className="muted">
        {until == null
          ? `Polling Cloud Logging while the agent answers (${secs(elapsed)}).`
          : `Waiting for ${pending.join(", ")} · ${secs(left)} left (these records can reach Cloud Logging after the answer).`}
      </span>
      {pct != null && <span className="listen-bar" aria-hidden="true"><span style={{ width: `${pct}%` }} /></span>}
      <PollError turn={turn} />
    </div>
  );
}

function Collapsed({ list, summary, config }) {
  if (!list.length) return null;
  return (
    <details className="raw-details hidden-rows">
      <summary>{summary}</summary>
      <ol className="timeline">{list.map((r) => <RecordBox key={r.id} r={r} config={config} />)}</ol>
    </details>
  );
}

// The detailed timeline of every record in arrival order (unchanged), behind the step view's expander.
function AllRecords({ turn, config }) {
  return (
    <>
      <SummaryLine turn={turn} />
      {turn.records.length === 0 ? (
        <div className="empty small-empty">{turn.ended ? "No records arrived for this turn." : "No records have arrived yet."}</div>
      ) : (
        <ol className="timeline">
          {turn.records.map((r) => <RecordBox key={r.id} r={r} config={config} />)}
        </ol>
      )}
      <Collapsed list={turn.hidden} config={config}
        summary={`${turn.hidden.length} gateway row${turn.hidden.length === 1 ? "" : "s"} without authz result and without request URL or status (CONNECT tunnels)`} />
      <Collapsed list={turn.iapOther || []} config={config}
        summary={`${(turn.iapOther || []).length} IAP decision${(turn.iapOther || []).length === 1 ? "" : "s"} for MCP methods other than tools/call`} />
      <Collapsed list={turn.unmatched} config={config}
        summary={`${turn.unmatched.length} Model Armor record${turn.unmatched.length === 1 ? "" : "s"} in this turn's time window whose screened text matches nothing of this turn`} />
    </>
  );
}

// One turn: the step view, the listening state, and (collapsed per turn) all platform records.
function TurnDetail({ turn, config }) {
  const [open, setOpen] = useState(false);
  const panelId = `all-records-${turn.id}`;
  return (
    <>
      <StepView turn={turn} config={config} open={open} onToggle={() => setOpen((o) => !o)} panelId={panelId} />
      <Listening turn={turn} config={config} />
      <div id={panelId} className="all-records" hidden={!open}>
        {open && <AllRecords turn={turn} config={config} />}
      </div>
    </>
  );
}

// One card per turn, newest first. Earlier turns stay in the panel and keep updating while they
// listen for late records (e.g. Model Armor screenings), so asking the next question hides nothing.
function TurnCard({ turn, config, selected, latest, cardRef }) {
  const pending = !turn.ended && turn.streamEnded ? pendingItems(turn, config) : [];
  return (
    <section ref={cardRef} className={`turn-card${selected ? " selected" : ""}${latest ? " latest" : ""}`}
      aria-label={`Turn ${turn.n}`}>
      <div className="turn-card-head">
        <span className="turn-card-n">Turn {turn.n}</span>
        <span className="turn-card-q" title={turn.message}>{turn.message}</span>
        {pending.length > 0 && <span className="turn-card-state">listening…</span>}
      </div>
      {turn.replayUnmatched
        ? <div className="empty small-empty">No recorded turn for this message, so nothing is shown.</div>
        : <TurnDetail turn={turn} config={config} />}
    </section>
  );
}

export default function Panel({ turns, selected, config }) {
  const provenance = config?.replay_provenance;
  const refs = useRef({});
  const ordered = [...turns].reverse();
  useEffect(() => {
    const el = selected && refs.current[selected];
    if (el) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [selected]);
  return (
    <aside className="panel" aria-label="Behind each answer">
      <div className="panel-head">
        <h2>Behind each answer</h2>
        {turns.length > 0 && <span className="muted panel-q">Newest first. Earlier turns stay here and keep updating while they listen for late records.</span>}
      </div>
      {config?.replay && (
        <div className="replay-banner" role="status">
          <b>REPLAY · {provenance}</b> {provenance === "recorded" ? "Recorded turns" : "Not a recording"}{config.replay_file ? ` from ${config.replay_file}` : ""}. Nothing here is live.
        </div>
      )}
      {turns.length === 0 ? (
        <div className="empty">
          <h3>No turn yet</h3>
          <p>Only records produced by the platform are shown here: ADK events from Agent Runtime, the IAP authorization decisions for the agent's MCP tool calls and the outbound gateway's request rows, the Model Armor screenings the inbound gateway asked for (from Cloud Logging), and the HTTP status the platform returned.</p>
        </div>
      ) : ordered.map((t, i) => (
        <TurnCard key={t.id} turn={t} config={config} selected={t.id === selected} latest={i === 0}
          cardRef={(el) => { if (el) refs.current[t.id] = el; else delete refs.current[t.id]; }} />
      ))}
    </aside>
  );
}
