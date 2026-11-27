// Turn state: folds backend messages (see backend/turn.py) into what the chat and the panel show.
// No field extraction happens here: the backend's records.py puts every field in `summary`.

let seq = 0;

export function newTurn(message, sessionId = null) {
  seq += 1;
  return {
    id: `t${seq}-${Date.now()}`,
    n: seq,
    message,
    sessionId,
    startedAt: Date.now(),
    nextKey: 0, // unique React keys for every record of this turn
    records: [], // timeline, arrival order
    hidden: [], // gateway rows without authz result and without URL/status (CONNECT tunnels), collapsed
    unmatched: [], // Model Armor records in this turn's window whose screened text matches nothing of it, collapsed
    iapOther: [], // IAP decisions for MCP methods other than tools/call (the backend does not attribute them today), collapsed
    answer: "",
    partial: "",
    streamEnded: false,
    latencyMs: null,
    listeningUntilMs: null,
    ended: false,
    traceUrls: [],
    traceIdSent: null,
    httpError: null,
    transportError: null, // the UI backend could not reach the platform: a chat alert, not a panel record
    streamErrors: [], // in-stream error events ({code, message}), in arrival order
    pollErrors: [],
    clientError: null,
    replayUnmatched: null,
    replay: false,
    provenance: null,
  };
}

export const isGateway = (source) => source === "inbound_gateway" || source === "outbound_gateway" || source === "gateway";
// The IAP authorization decision the outbound gateway asked for (Data Access audit log; backend records.py).
export const isIap = (source) => source === "iap_decision";

export function applyMessage(turn, msg) {
  const t = { ...turn, replay: turn.replay || !!msg.replay, provenance: turn.provenance || msg.provenance || null, nextKey: turn.nextKey + 1 };
  const rec = { ...msg, id: `${turn.id}-r${turn.nextKey}` };
  switch (msg.kind) {
    case "turn_start":
      // iapPolled: false when the backend does not read IAP decisions (no agent principal, or Cloud Logging
      // refused the audit-log clause); undefined in recordings made before the field existed
      return { ...t, t0: msg.t0, windowFrom: msg.window_from, traceIdSent: msg.trace_id_sent || null, iapPolled: msg.iap_polled };
    case "listening":
      return { ...t, listeningUntilMs: msg.until_ms ?? null, listeningSince: Date.now() - (msg.t_ms || 0) };
    case "adk_event": {
      const s = msg.summary || {};
      let { answer, partial, streamErrors } = t;
      if (s.text && s.author !== "user") {
        if (s.partial) partial += s.text;
        else {
          answer = answer ? `${answer}\n\n${s.text}` : s.text;
          partial = "";
        }
      }
      if (s.is_error) streamErrors = [...streamErrors, { code: s.error_code, message: s.error_message }];
      return { ...t, answer, partial, streamErrors, records: [...t.records, rec] };
    }
    case "http_error":
      return { ...t, httpError: msg, records: [...t.records, rec] };
    case "transport_error":
      return { ...t, transportError: msg };
    case "stream_end":
      return { ...t, streamEnded: true, latencyMs: msg.latency_ms };
    case "log": {
      if (msg.attribution?.method === "unmatched") return { ...t, unmatched: [...t.unmatched, rec] };
      if (msg.hidden && isIap(msg.source)) return { ...t, iapOther: [...t.iapOther, rec] };
      if (msg.hidden) return { ...t, hidden: [...t.hidden, rec] };
      const s = msg.summary || {};
      if ((isGateway(msg.source) || isIap(msg.source)) && s.denied) {
        // ADK retries a failed MCP call once: group deny records for the same tool from the same source
        // (gateway rows with authz result DENIED; IAP decisions with granted: false).
        const i = t.records.findIndex((r) => r.kind === "log" && r.source === msg.source && r.summary?.denied && r.summary?.tool === s.tool);
        if (i >= 0) {
          const records = t.records.slice();
          const head = records[i];
          records[i] = { ...head, group: [...(head.group || [head]), rec] };
          return { ...t, records };
        }
      }
      return { ...t, records: [...t.records, rec] };
    }
    case "log_poll_error":
      return { ...t, pollErrors: [...t.pollErrors, msg] };
    case "replay_unmatched":
      return { ...t, replayUnmatched: msg, ended: true, streamEnded: true };
    case "turn_end":
      return { ...t, ended: true, streamEnded: true, latencyMs: msg.latency_ms ?? t.latencyMs, traceUrls: msg.trace_urls || [], hiddenRows: msg.hidden_rows };
    default:
      return t; // an unknown backend message kind is not a platform record
  }
}

/** The per-turn summary line, counted from the records that arrived. */
export function summarize(turn) {
  let toolCalls = 0, toolErrors = 0, tokensIn = 0, tokensOut = 0;
  const gateway = { rows: 0, results: {}, noResult: 0, denied: 0 };
  const armor = { prompt: [], response: [], other: [] };
  // IAP decisions for tools/call, as recorded: granted true / granted false (or omitted with status 7) / not in record
  const iap = { decisions: 0, allowed: 0, denied: 0, unknown: 0 };
  for (const r of turn.records) {
    if (r.kind === "adk_event") {
      toolCalls += r.summary?.calls?.length || 0;
      toolErrors += (r.summary?.responses || []).filter((x) => x.error).length;
      tokensIn += r.summary?.usage?.prompt || 0;
      tokensOut += r.summary?.usage?.output || 0;
    } else if (r.kind === "log" && isGateway(r.source)) {
      for (const row of r.group || [r]) {
        gateway.rows += 1;
        const result = row.summary?.authz_result;
        if (result) gateway.results[result] = (gateway.results[result] || 0) + 1;
        else gateway.noResult += 1;
        if (row.summary?.denied) gateway.denied += 1;
      }
    } else if (r.kind === "log" && isIap(r.source)) {
      for (const d of r.group || [r]) {
        iap.decisions += 1;
        if (d.summary?.granted === true) iap.allowed += 1;
        else if (d.summary?.granted === false) iap.denied += 1;
        else iap.unknown += 1;
      }
    } else if (r.kind === "log" && r.source === "model_armor") {
      const op = r.summary?.operation;
      (op === "prompt" ? armor.prompt : op === "response" ? armor.response : armor.other).push(r.summary);
    }
  }
  return { toolCalls, toolErrors, tokensIn, tokensOut, gateway, armor, iap, denied: gateway.denied };
}

/** What a list of Model Armor summaries says, without inferring anything a record does not say. */
export function armorState(list) {
  if (!list.length) return { kind: "none" };
  if (list.some((a) => a.matched)) {
    return { kind: "match", filters: [...new Set(list.flatMap((a) => a.matched_filters || []))] };
  }
  if (list.every((a) => a.match_state === "NO_MATCH_FOUND")) return { kind: "no_match" };
  const states = [...new Set(list.map((a) => a.match_state || "state not in record"))];
  return { kind: "other", states };
}

export function toolActivity(turn) {
  // Tool calls and their results from ADK events, in order (for the chat's compact line).
  const calls = [];
  for (const r of turn.records) {
    if (r.kind !== "adk_event") continue;
    for (const c of r.summary?.calls || []) calls.push({ name: c.name, id: c.id, error: null, done: false });
    for (const res of r.summary?.responses || []) {
      const c = calls.find((x) => !x.done && (x.id ? x.id === res.id : x.name === res.name));
      if (c) { c.done = true; c.error = res.error; }
    }
  }
  for (const c of calls) c.noResult = !c.done && turn.streamEnded;
  return calls;
}

export const ATTRIBUTION = {
  session_id: "matched: session id in the request URL",
  sent_trace: "matched: trace id the UI sent with this turn's request",
  trace_id: "matched: trace id of this turn",
  prompt_text: "matched: screened text is this turn's message",
  session_text: "matched: screened text contains this session's id",
  answer_text: "matched: screened text is this turn's answer",
  correlation_id: "matched: client_correlation_id of this turn's prompt screening",
  principal_tool_time: "matched: agent principal + tool name + time",
  time: "attributed by time",
  unmatched: "in this turn's time window; matches nothing of this turn",
};
