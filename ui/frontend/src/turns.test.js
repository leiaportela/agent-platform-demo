// node --test: turn folding (no extra dependencies). Message shapes are the backend's (backend/turn.py).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { applyMessage, armorState, ATTRIBUTION, newTurn, summarize, toolActivity } from "./turns.js";

// Backend log messages for the REAL IAP decision records (see the fixture's provenance field).
const REAL_IAP = JSON.parse(readFileSync(new URL("./fixtures/real_iap_messages.json", import.meta.url), "utf8")).turns;

const fold = (msgs, turn = newTurn("q", "s1")) => msgs.reduce(applyMessage, turn);

const gw = (over = {}) => ({
  kind: "log", t_ms: 100, source: "outbound_gateway", hidden: false, attribution: { method: "time", detail: "" },
  summary: { authz_result: "ALLOWED", status: 200, denied: false, url: "https://x/mcp", tool: null, ...over.summary },
  ...over,
});

test("keys are unique even for records with the same arrival time", () => {
  const t = fold([gw({ hidden: true }), gw({ hidden: true }), gw(), gw()]);
  const ids = [...t.records, ...t.hidden].map((r) => r.id);
  assert.equal(new Set(ids).size, ids.length);
  assert.equal(t.hidden.length, 2);
});

test("transport_error is a chat alert, not a panel record; http_error is both", () => {
  const t = fold([{ kind: "transport_error", t_ms: 5, error: "ConnectError: boom" },
    { kind: "http_error", t_ms: 6, status: 403, body: {}, message: "Model Armor: Prompt violates content security configurations" }]);
  assert.equal(t.transportError.error, "ConnectError: boom");
  assert.deepEqual(t.records.map((r) => r.kind), ["http_error"]);
});

test("in-stream errors are collected in every shape the backend reports", () => {
  const t = fold([
    { kind: "adk_event", t_ms: 1, summary: { parsed: true, is_error: true, error_code: "ValueError", error_message: "Tool not found" } },
    { kind: "adk_event", t_ms: 2, summary: { parsed: true, is_error: true, error_code: "498", error_message: "Tool not found" } },
  ]);
  assert.deepEqual(t.streamErrors.map((e) => e.code), ["ValueError", "498"]);
});

test("'calling…' becomes 'no result arrived' once the stream ended", () => {
  const call = { kind: "adk_event", t_ms: 1, summary: { parsed: true, calls: [{ id: "c1", name: "get_customer_contact" }], responses: [] } };
  assert.equal(toolActivity(fold([call]))[0].noResult, false);
  const ended = fold([call, { kind: "stream_end", t_ms: 2, latency_ms: 2 }]);
  assert.equal(toolActivity(ended)[0].noResult, true);
});

test("gateway counts use the authz result as recorded; DENIED is never hidden; none -> zero rows", () => {
  assert.equal(summarize(fold([])).gateway.rows, 0);
  const deny = gw({ summary: { authz_result: "DENIED", status: 403, denied: true, tool: "get_customer_contact" } });
  const forbiddenNoAuthz = gw({ summary: { authz_result: null, status: 403, denied: false } });
  const t = fold([gw(), deny, forbiddenNoAuthz]);
  const g = summarize(t).gateway;
  assert.deepEqual(g.results, { ALLOWED: 1, DENIED: 1 });
  assert.equal(g.noResult, 1);
  assert.equal(g.denied, 1);
  assert.equal(t.hidden.length, 0);
});

test("unmatched Model Armor records are collapsed, not in the timeline", () => {
  const t = fold([{ kind: "log", t_ms: 1, source: "model_armor", hidden: false, attribution: { method: "unmatched" }, summary: { operation: "prompt" } }]);
  assert.equal(t.records.length, 0);
  assert.equal(t.unmatched.length, 1);
});

test("Model Armor: 'No match' only on NO_MATCH_FOUND; unknown operations kept apart", () => {
  assert.deepEqual(armorState([]), { kind: "none" });
  assert.deepEqual(armorState([{ match_state: "NO_MATCH_FOUND" }]), { kind: "no_match" });
  assert.deepEqual(armorState([{ match_state: null }]), { kind: "other", states: ["state not in record"] });
  assert.deepEqual(armorState([{ match_state: "MATCH_FOUND", matched: true, matched_filters: ["pi_and_jailbreak"] }]),
    { kind: "match", filters: ["pi_and_jailbreak"] });
  const t = fold([
    { kind: "log", t_ms: 1, source: "model_armor", attribution: { method: "prompt_text" }, summary: { operation: "prompt", match_state: "NO_MATCH_FOUND" } },
    { kind: "log", t_ms: 2, source: "model_armor", attribution: { method: "time" }, summary: { operation: "other", operation_type: "SANITIZE_X" } },
  ]);
  const a = summarize(t).armor;
  assert.equal(a.prompt.length, 1);
  assert.equal(a.response.length, 0);
  assert.equal(a.other[0].operation_type, "SANITIZE_X");
});

test("replay refusal ends the turn and carries the recorded messages", () => {
  const t = fold([{ kind: "replay_unmatched", t_ms: 0, available: ["a", "b"], replay: true, provenance: "recorded" }]);
  assert.equal(t.ended, true);
  assert.deepEqual(t.replayUnmatched.available, ["a", "b"]);
  assert.equal(t.provenance, "recorded");
});

test("unknown message kinds are not turned into records", () => {
  assert.equal(fold([{ kind: "something_new", t_ms: 1 }]).records.length, 0);
});

test("real IAP decisions: deny grouped by tool, 'IAP: N allowed / M denied' counted from the records", () => {
  const deny = fold(REAL_IAP.s4_pii_contact);
  // three granted:false decisions for get_customer_contact: one timeline box, every decision counted
  assert.equal(deny.records.length, 1);
  assert.equal(deny.records[0].group.length, 3);
  assert.deepEqual(summarize(deny).iap, { decisions: 3, allowed: 0, denied: 3, unknown: 0 });
  const s = deny.records[0].summary;
  assert.equal(s.granted, false);
  assert.equal(s.status_code, 7);
  assert.equal(s.status_message, "Permission Denied.");
  assert.equal(s.tool, "get_customer_contact");
  assert.equal(s.policy_version, "POLICY_VERSION_V1");
  assert.match(s.principal, /^principal:\/\/agents\.global\.org-111111111111\.system\.id\.goog\/.*reasoningEngines\/1234567890123456789$/);
  assert.match(deny.records[0].console_url, /^https:\/\/console\.cloud\.google\.com\/logs\/query;query=insertId%3D%22/);
  assert.equal(ATTRIBUTION[deny.records[0].attribution.method], "matched: agent principal + tool name + time");
  // IAP decisions are not gateway rows: the gateway counts stay what the gateway rows say
  assert.equal(summarize(deny).gateway.rows, 0);
  assert.equal(summarize(deny).denied, 0);

  const allow = fold(REAL_IAP.s3_churn_risk);
  assert.equal(allow.records.length, 2); // granted decisions are not grouped
  assert.deepEqual(summarize(allow).iap, { decisions: 2, allowed: 2, denied: 0, unknown: 0 });
});

test("IAP decisions next to gateway rows are counted separately", () => {
  const gwDeny = gw({ summary: { authz_result: "DENIED", status: 403, denied: true, tool: "get_customer_contact" } });
  const t = fold([...REAL_IAP.s3_churn_risk, ...REAL_IAP.s4_pii_contact, gwDeny]);
  const s = summarize(t);
  assert.deepEqual(s.iap, { decisions: 5, allowed: 2, denied: 3, unknown: 0 });
  assert.deepEqual(s.gateway.results, { DENIED: 1 });
  // the gateway DENIED row is its own box, not grouped with the IAP decisions for the same tool
  assert.equal(t.records.filter((r) => r.source === "outbound_gateway").length, 1);
  assert.equal(t.records.filter((r) => r.source === "iap_decision").length, 3);
});

test("IAP: granted not in record is counted apart; non-tools/call decisions are collapsed, not counted", () => {
  const real = REAL_IAP.s3_churn_risk[0];
  const unknown = { ...real, summary: { ...real.summary, granted: null, granted_in_record: false } };
  const handshake = { ...real, hidden: true, summary: { ...real.summary, mcp_method: "tools/list", tool: null, hidden: true } };
  const t = fold([unknown, handshake]);
  assert.deepEqual(summarize(t).iap, { decisions: 1, allowed: 0, denied: 0, unknown: 1 });
  assert.equal(t.iapOther.length, 1);
  assert.equal(t.hidden.length, 0); // not mixed with the CONNECT tunnel rows
});

test("turn_start carries whether IAP decisions are polled", () => {
  assert.equal(fold([{ kind: "turn_start", t_ms: 0, iap_polled: false }]).iapPolled, false);
  assert.equal(fold([{ kind: "turn_start", t_ms: 0, iap_polled: true }]).iapPolled, true);
  assert.equal(fold([{ kind: "turn_start", t_ms: 0 }]).iapPolled, undefined); // older recordings
});
