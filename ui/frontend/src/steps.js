// Step view: folds one turn (turns.js state) into a few lines grouped by step instead of arrival order.
// Pure: no React, no fetching. Every ✓/✗ comes from a platform record that arrived for this turn:
//   ① prompt     the SANITIZE_USER_PROMPT record matched to this turn by text/trace; blocked = the platform's HTTP
//                error for streamQuery + that record's MATCH_FOUND
//   ② tools      per tool, the IAP decision (primary) or the outbound gateway's tools/call row (secondary)
//   ③ answer     the SANITIZE_MODEL_RESPONSE record matched by the answer (never the create-session screening)
// Until a record arrives a line says "waiting for … record…"; once the window closed, "no record arrived".
// Background rows (session API, telemetry, generateContent, CONNECT tunnels) stay in the detailed timeline.
import { isGateway, isIap } from "./turns.js";

const flat = (list) => (list || []).flatMap((r) => r.group || [r]);
const NB = "\u00a0"; // keeps "HTTP 403", "status 7 Permission Denied", filter names together when a line wraps
const stripDot = (s) => String(s).replace(/\.\s*$/, "");

// ------------------------------------------------------------------------------------------------
// Model Armor filter names, in words (unknown keys are shown as recorded)
// ------------------------------------------------------------------------------------------------

export const FILTER_NAMES = {
  pi_and_jailbreak: "prompt injection / jailbreak",
  sdp: "sensitive data",
  rai: "responsible AI",
  malicious_uris: "malicious URL",
  csam: "child safety (CSAM)",
};

const INFO_TYPE_NAMES = {
  US_SOCIAL_SECURITY_NUMBER: "US SSN",
  BRAZIL_CPF_NUMBER: "Brazil CPF",
  CREDIT_CARD_NUMBER: "credit card number",
  EMAIL_ADDRESS: "email address",
  PHONE_NUMBER: "phone number",
  IBAN_CODE: "IBAN",
  US_INDIVIDUAL_TAXPAYER_IDENTIFICATION_NUMBER: "US ITIN",
  US_PASSPORT: "US passport",
  STREET_ADDRESS: "street address",
};

const RAI_TYPE_NAMES = {
  sexually_explicit: "sexually explicit",
  hate_speech: "hate speech",
  harassment: "harassment",
  dangerous: "dangerous content",
};

const words = (key) => String(key).toLowerCase().replace(/_/g, " ");
export const infoTypeName = (t) => INFO_TYPE_NAMES[t] || words(t);
export const raiTypeName = (t) => RAI_TYPE_NAMES[t] || words(t);

/** "prompt injection / jailbreak", "sensitive data (US SSN, Brazil CPF)", "responsible AI (dangerous content,
 * harassment)", … for the matched filters of a list of Model Armor summaries. */
export function humanFilters(summaries, { keepTogether = false } = {}) {
  // keepTogether: each filter name and each detail does not break inside when a line wraps (non-breaking spaces)
  const nb = (s) => (keepTogether ? String(s).replace(/ /g, NB) : s);
  const byName = new Map(); // filter key -> Set of details (info types / RAI categories)
  for (const s of summaries) {
    const matched = (s.filters || []).filter((f) => f.matched);
    if (matched.length) {
      for (const f of matched) {
        const set = byName.get(f.name) || new Set();
        for (const t of f.info_types || []) set.add(infoTypeName(t));
        for (const t of f.matched_types || []) set.add(raiTypeName(t));
        byName.set(f.name, set);
      }
    } else {
      for (const name of s.matched_filters || []) if (!byName.has(name)) byName.set(name, new Set());
    }
  }
  if (!byName.size) return "filter not in record";
  return [...byName].map(([name, set]) => `${nb(FILTER_NAMES[name] || name)}${set.size ? ` (${[...set].map(nb).join(", ")})` : ""}`).join("; ");
}

// ------------------------------------------------------------------------------------------------
// Statuses: tone good | bad | neutral | waiting | none (none = the window closed without a record)
// ------------------------------------------------------------------------------------------------

const waitingFor = (what, closed) => (closed ? { tone: "none", text: "no record arrived" } : { tone: "waiting", text: `waiting for ${what} record…` });

export function armorStatus(summaries, closed) {
  if (!summaries.length) return waitingFor("Model Armor", closed);
  if (summaries.some((a) => a.matched)) return { tone: "bad", text: `match found: ${humanFilters(summaries)}` };
  if (summaries.every((a) => a.match_state === "NO_MATCH_FOUND")) return { tone: "good", text: "no match" };
  const states = [...new Set(summaries.map((a) => a.match_state || "state not in record"))];
  return { tone: "neutral", text: states.join(", ") };
}

/** One Logs Explorer link for a line's records: the record's own link, or one query for all their insertIds. */
export function recordsLink(records) {
  const urls = records.map((r) => r.console_url).filter(Boolean);
  if (!urls.length) return null;
  if (urls.length === 1) return { href: urls[0], n: 1 };
  const ids = records.filter((r) => r.console_url).map((r) => r.summary?.insert_id || r.raw?.insertId).filter(Boolean);
  const m = /;query=[^;?]*/.exec(urls[0]);
  if (m && ids.length === urls.length) {
    const query = ids.map((id) => `insertId="${id}"`).join(" OR ");
    return { href: urls[0].replace(m[0], `;query=${encodeURIComponent(query)}`), n: ids.length };
  }
  return { href: urls[0], n: urls.length, hrefs: urls };
}

// ------------------------------------------------------------------------------------------------
// ② tools: per tool, count calls by the status their records give
// ------------------------------------------------------------------------------------------------

const PROMPT_METHODS = new Set(["prompt_text", "correlation_id", "sent_trace", "trace_id"]);
const ANSWER_METHODS = new Set(["answer_text", "correlation_id", "sent_trace", "trace_id"]); // never session_text


function deniedText(iapDenied, gwDenied) {
  let text;
  if (iapDenied.length) {
    const s = iapDenied[0].summary;
    const granted = iapDenied.every((d) => d.summary.granted_in_record) ? `granted${NB}false` : `granted${NB}false (field omitted in the record)`;
    const message = s.status_message ? `${NB}${stripDot(s.status_message).replace(/ /g, NB)}` : "";
    const status = s.status_code != null ? ` · status${NB}${s.status_code}${message}` : "";
    text = `denied — IAP: ${granted}${status}`;
    if (gwDenied.length) text += ` · gateway${NB}${gwDenied[0].summary.status ?? "DENIED"}`;
  } else {
    const s = gwDenied[0].summary;
    text = `denied — gateway: authz DENIED${s.status != null ? ` · HTTP${NB}${s.status}` : ""}`;
  }
  return { tone: "bad", text };
}

function otherText(iapUnknown, gwOther) {
  if (iapUnknown.length) return { tone: "neutral", text: "IAP decision: granted not in record" };
  const results = [...new Set(gwOther.map((r) => r.summary.authz_result || "none"))];
  return { tone: "neutral", text: results.includes("none") && results.length === 1 ? "gateway row without authz result" : `gateway authz result: ${results.join(", ")}` };
}

/** "identity: this agent · reasoningEngines/…" with the full principal for a tooltip. */
export function identityOf(principal, agentPrincipal) {
  if (!principal) return { text: "identity: principal not in record", title: "" };
  const short = String(principal).split("/").slice(-2).join("/");
  const mine = !!agentPrincipal && principal === agentPrincipal;
  return { text: `identity: ${mine ? "this agent · " : ""}${short}`, title: principal };
}

function toolRows(name, calls, iap, gw, closed, agentPrincipal) {
  const iapDenied = iap.filter((d) => d.summary.granted === false);
  const iapAllowed = iap.filter((d) => d.summary.granted === true);
  const iapUnknown = iap.filter((d) => d.summary.granted !== true && d.summary.granted !== false);
  const gwDenied = gw.filter((r) => r.summary.denied);
  const gwAllowed = gw.filter((r) => !r.summary.denied && r.summary.authz_result === "ALLOWED");
  const gwOther = gw.filter((r) => !r.summary.denied && r.summary.authz_result !== "ALLOWED");
  // Each source's records are distinct calls (a retried call can add a record): a status covers as many calls as
  // the source with more records for it says, never more than the agent made; conflicting records each get a line.
  const count = (n) => (calls > 0 ? Math.max(1, Math.min(calls, n)) : n);
  const rows = [];
  const base = { kind: "tool", tool: name, noCall: calls === 0 };
  const denied = Math.max(iapDenied.length, gwDenied.length);
  if (denied) {
    const records = [...iapDenied, ...gwDenied];
    const link = recordsLink(records);
    if (iapDenied.length) {
      // the deny line runs the full width; its record link sits on the identity line under it
      rows.push({ ...base, key: `tool-${name}-denied`, count: count(denied), status: deniedText(iapDenied, gwDenied), records, link: null, wide: true });
      rows.push({ kind: "identity", key: `tool-${name}-identity`, ...identityOf(iapDenied[0].summary.principal, agentPrincipal), link });
    } else {
      rows.push({ ...base, key: `tool-${name}-denied`, count: count(denied), status: deniedText(iapDenied, gwDenied), records, link });
    }
  }
  const allowed = Math.max(iapAllowed.length, gwAllowed.length);
  if (allowed) {
    const records = [...iapAllowed, ...gwAllowed];
    rows.push({ ...base, key: `tool-${name}-allowed`, count: count(allowed), status: { tone: "good", text: `allowed (${iapAllowed.length ? "IAP" : "gateway"})` }, records, link: recordsLink(records) });
  }
  const other = Math.max(iapUnknown.length, gwOther.length);
  if (other) {
    const records = [...iapUnknown, ...gwOther];
    rows.push({ ...base, key: `tool-${name}-other`, count: count(other), status: otherText(iapUnknown, gwOther), records, link: recordsLink(records) });
  }
  const covered = rows.filter((r) => r.kind === "tool").reduce((n, r) => n + r.count, 0);
  if (calls > 0 && covered > calls) {
    // More statuses than calls (e.g. a retried deny plus an allow): the records do not say how many calls each
    // status covers, so no line claims a count ("×N"); every line still links its records.
    for (const r of rows) {
      if (r.kind !== "tool") continue;
      r.count = 1;
      r.countNote = `${calls} call${calls === 1 ? "" : "s"} of ${name}; the records do not say which call each status belongs to`;
    }
  } else if (calls > covered) {
    rows.push({ ...base, key: `tool-${name}-pending`, count: calls - covered, status: waitingFor("gateway", closed), records: [], link: null });
  }
  return rows;
}

// ------------------------------------------------------------------------------------------------
// The turn
// ------------------------------------------------------------------------------------------------

const GEMINI = /:(stream)?generateContent\b/i;

/**
 * { rows, footer, blocked, lines }. rows: step lines ({kind: "step", n, label, status|null, tone, link}), tool
 * lines under ② ({kind: "tool", tool, count, status, link}), the identity line under a denied tool
 * ({kind: "identity", text, title, link}) and notes ({kind: "note", status}). footer: tokens, latency, trace,
 * Gemini rows and the number of records behind "All platform records (N)". lines = rows + footer.
 */
export function buildSteps(turn, { agentPrincipal = null } = {}) {
  const closed = !!turn.ended;
  const records = flat(turn.records);
  const logs = records.filter((r) => r.kind === "log");
  const adk = records.filter((r) => r.kind === "adk_event");
  const armor = logs.filter((r) => r.source === "model_armor");
  const prompt = armor.filter((r) => r.summary?.operation === "prompt" && PROMPT_METHODS.has(r.attribution?.method));
  const answer = armor.filter((r) => r.summary?.operation === "response" && ANSWER_METHODS.has(r.attribution?.method));
  const promptSummaries = prompt.map((r) => r.summary);
  const http = turn.httpError;
  const agentRan = adk.length > 0;
  const rows = [];

  // ① prompt screening (inbound gateway)
  const blocked = !!http && !agentRan;
  const httpText = http ? `HTTP${NB}${http.status}${http.message ? `: ${http.message}` : ""}` : "";
  let httpShown = false;
  if (blocked && promptSummaries.some((a) => a.matched)) {
    httpShown = true;
    rows.push({ kind: "step", key: "prompt", n: 1, tone: "bad", status: null, records: prompt, link: recordsLink(prompt),
      label: `Blocked by Model Armor (inbound gateway): ${humanFilters(promptSummaries, { keepTogether: true })}${NB}—${NB}HTTP${NB}${http.status}` });
  } else if (blocked && !prompt.length) {
    httpShown = true;
    // The platform's HTTP response is a record; which screening produced it is only said by the Model Armor record.
    rows.push({ kind: "step", key: "prompt", n: 1, tone: "bad", link: null,
      label: `Request rejected — ${httpText}`, status: waitingFor("Model Armor", closed) });
  } else {
    rows.push({ kind: "step", key: "prompt", n: 1, label: "Prompt screened by Model Armor (inbound gateway)",
      status: armorStatus(promptSummaries, closed), records: prompt, link: recordsLink(prompt) });
  }

  // ② agent and its tool calls (outbound gateway)
  const calls = [];
  for (const r of adk) for (const c of r.summary?.calls || []) if (c.name) calls.push(c.name);
  if (!agentRan) {
    const label = http ? "Agent did not run (no agent events)" : turn.streamEnded ? "No agent events arrived" : "Agent is working…";
    rows.push({ kind: "step", key: "tools", n: 2, label, status: null, link: null, tone: turn.streamEnded ? null : "waiting" });
  } else if (!calls.length) {
    rows.push({ kind: "step", key: "tools", n: 2, label: "Agent answered without calling tools", status: null, link: null });
  } else {
    rows.push({ kind: "step", key: "tools", n: 2, status: null, link: null,
      label: `Agent called ${calls.length} tool${calls.length === 1 ? "" : "s"} through the outbound gateway` });
  }
  const iap = logs.filter((r) => isIap(r.source) && !r.hidden && r.summary?.tool);
  const gw = logs.filter((r) => isGateway(r.source) && r.summary?.mcp_method === "tools/call" && r.summary?.tool);
  const names = [...new Set([...calls, ...iap.map((r) => r.summary.tool), ...gw.map((r) => r.summary.tool)])];
  for (const name of names) {
    rows.push(...toolRows(name, calls.filter((c) => c === name).length,
      iap.filter((r) => r.summary.tool === name), gw.filter((r) => r.summary.tool === name), closed, agentPrincipal));
  }
  const errors = adk.filter((r) => r.summary?.is_error);
  if (errors.length) {
    const codes = [...new Set(errors.map((r) => r.summary.error_code ?? "no code in record"))];
    rows.push({ kind: "note", key: "stream-error", status: { tone: "bad", text: `agent stream returned ${errors.length === 1 ? "an error event" : `${errors.length} error events`}: ${codes.join(", ")}` } });
  }
  if (http && !httpShown) rows.push({ kind: "note", key: "http-error", status: { tone: "bad", text: `platform returned ${httpText}` } });

  // ③ answer screening (only when the agent ran, or may still run)
  if (agentRan || (!http && !turn.streamEnded)) {
    let status;
    if (answer.length) status = armorStatus(answer.map((r) => r.summary), closed);
    else if (!turn.answer && !turn.streamEnded) status = { tone: "waiting", text: "waiting for the answer…" };
    else if (!turn.answer) status = { tone: "none", text: "no answer text arrived" };
    else status = armorStatus([], closed);
    rows.push({ kind: "step", key: "answer", n: 3, label: "Answer screened by Model Armor", status, records: answer, link: recordsLink(answer) });
  }

  // footer
  let tIn = 0, tOut = 0, tThink = 0, tTotal = 0, responses = 0;
  for (const r of adk) {
    const u = r.summary?.usage;
    if (!u) continue;
    responses += 1;
    tIn += u.prompt || 0;
    tOut += u.output || 0;
    tThink += u.thoughts || 0;
    tTotal += u.total || (u.prompt || 0) + (u.output || 0) + (u.thoughts || 0);
  }
  const gemini = [...logs, ...flat(turn.hidden)].filter((r) => isGateway(r.source) && GEMINI.test(r.summary?.url || ""));
  const traces = turn.traceUrls || [];
  const footer = {
    tokens: responses ? { total: tTotal, in: tIn, out: tOut, thinking: tThink, responses } : null,
    latencyMs: turn.streamEnded ? turn.latencyMs ?? null : null,
    // one trace link: the trace the UI sent with this turn when a record carried it (all of them are in the timeline)
    trace: (turn.traceIdSent && traces.find((u) => u.includes(turn.traceIdSent))) || traces[0] || null,
    traceCount: traces.length,
    gemini: gemini.length,
    total: records.length + flat(turn.hidden).length + flat(turn.unmatched).length + flat(turn.iapOther).length,
  };
  return { rows, footer, blocked, lines: rows.length + 1 };
}

/** 4812 -> "4.8k", 950 -> "950". */
export function kilo(n) {
  if (n == null) return "–";
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k`;
}
