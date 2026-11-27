// node --test: the step view (steps.js) on the REAL live turns recorded 2026-11-27 with RECORD_DIR
// (backend/tests/fixtures/real_live2_*.jsonl) and the earlier recordings; constructed messages are marked so.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { applyMessage, newTurn } from "./turns.js";
import { armorStatus, buildSteps, humanFilters, kilo, recordsLink } from "./steps.js";

const FIX = new URL("../../backend/tests/fixtures/", import.meta.url);
const recording = (name) => readFileSync(new URL(name, FIX), "utf8").split("\n").filter(Boolean).map(JSON.parse)
  .filter((m) => m.kind !== "recording_meta");
const REAL_IAP = JSON.parse(readFileSync(new URL("./fixtures/real_iap_messages.json", import.meta.url), "utf8")).turns;
const AGENT = "principal://agents.global.org-111111111111.system.id.goog/resources/aiplatform/projects/123456789012/locations/us-central1/reasoningEngines/1234567890123456789";

const fold = (msgs, turn = newTurn("q", "s1")) => msgs.reduce(applyMessage, turn);
const view = (msgs, opts = { agentPrincipal: AGENT }) => buildSteps(fold(msgs), opts);
const txt = (s) => String(s ?? "").replace(/\u00a0/g, " ");
const step = (v, key) => v.rows.find((r) => r.kind === "step" && r.key === key);
const tools = (v) => v.rows.filter((r) => r.kind === "tool");
const statusOf = (r) => r.status && `${r.status.tone}: ${txt(r.status.text)}`;

const Q1 = recording("real_live2_q1.jsonl");
const Q4 = recording("real_live2_q4.jsonl");
const Q5 = recording("real_live2_q5.jsonl");
const upTo = (msgs, pred) => msgs.slice(0, msgs.findIndex(pred) + 1);
const isArmor = (op, method) => (m) => m.kind === "log" && m.source === "model_armor" && m.summary.operation === op && (!method || m.attribution.method === method);

test("normal turn (real live2 q1): 6 lines, grouped by step, statuses from IAP and Model Armor records", () => {
  const v = view(Q1);
  assert.equal(v.lines, 6);
  assert.equal(v.blocked, false);
  assert.equal(statusOf(step(v, "prompt")), "good: no match");
  assert.equal(step(v, "prompt").label, "Prompt screened by Model Armor (inbound gateway)");
  assert.equal(step(v, "tools").label, "Agent called 4 tools through the outbound gateway");
  assert.deepEqual(tools(v).map((r) => [r.tool, r.count, statusOf(r)]), [
    ["get_latest_run", 1, "good: allowed (IAP)"],
    ["get_model_summary", 3, "good: allowed (IAP)"],
  ]);
  // the line links every record for the tool: 3 IAP decisions + the 2 gateway tools/call rows that arrived (the
  // gateway wrote 2 of 3 rows for get_model_summary; IAP has all 3)
  const summary = tools(v)[1];
  assert.deepEqual(summary.records.map((r) => r.source).sort(), ["iap_decision", "iap_decision", "iap_decision", "outbound_gateway", "outbound_gateway"]);
  assert.equal(summary.link.n, 5);
  assert.equal(statusOf(step(v, "answer")), "good: no match");
  assert.equal(step(v, "answer").link.n, 1);
  assert.deepEqual({ ...v.footer, tokens: v.footer.tokens.total, trace: !!v.footer.trace }, {
    tokens: 6796, latencyMs: 14682, trace: true, traceCount: 2, gemini: 2, total: 33,
  });
  assert.match(v.footer.trace, /traceId=c2080cc1aaa56a300ef6179b40e69a7d/); // the trace the UI sent
});

test("background rows (session API, telemetry, generateContent, CONNECT) never appear in the step lines", () => {
  for (const msgs of [Q1, Q4, Q5]) {
    const t = fold(msgs);
    const v = buildSteps(t, { agentPrincipal: AGENT });
    const linked = new Set(v.rows.flatMap((r) => (r.records || [])).map((r) => r.id));
    for (const r of [...t.records, ...t.hidden]) {
      const url = r.summary?.url || "";
      if (r.kind === "log" && r.source === "outbound_gateway" && !r.summary?.mcp_method) {
        assert.ok(!linked.has(r.id), `background row linked: ${r.summary?.method} ${url}`);
      }
    }
    // the create-session response screening is never the answer screening
    const sessionArmor = t.records.find((r) => r.source === "model_armor" && r.attribution?.method === "session_text");
    assert.ok(sessionArmor);
    assert.ok(!linked.has(sessionArmor.id));
  }
});

test("deny turn (real live2 q4): 7 lines; the deny says what the IAP record says, one identity line, one link", () => {
  const v = view(Q4);
  assert.equal(v.lines, 7);
  assert.equal(step(v, "tools").label, "Agent called 2 tools through the outbound gateway");
  const [churn, contact] = tools(v);
  assert.equal(statusOf(churn), "good: allowed (IAP)");
  assert.equal(contact.tool, "get_customer_contact");
  assert.equal(contact.count, 1);
  assert.equal(statusOf(contact), "bad: denied — IAP: granted false · status 7 Permission Denied");
  const i = v.rows.indexOf(contact);
  const identity = v.rows[i + 1];
  assert.equal(identity.kind, "identity");
  assert.equal(identity.text, "identity: this agent · reasoningEngines/1234567890123456789");
  assert.equal(identity.title, AGENT);
  // the record link of the deny is on the identity line (the deny text runs the full width)
  assert.equal(contact.link, null);
  const iap = fold(Q4).records.find((r) => r.source === "iap_decision" && r.summary.denied);
  assert.equal(identity.link.href, iap.console_url);
  assert.equal(v.rows.filter((r) => r.kind === "identity").length, 1);
  assert.equal(statusOf(step(v, "answer")), "good: no match");
  // without the agent's principal from /api/config, the line does not claim "this agent"
  const anon = buildSteps(fold(Q4));
  assert.equal(anon.rows.find((r) => r.kind === "identity").text, "identity: reasoningEngines/1234567890123456789");
});

test("deny + the gateway's own 403 DENIED row: '· gateway 403' on the same line, no second line (row constructed)", () => {
  const real = fold(Q1).records.find((r) => r.source === "outbound_gateway" && r.summary?.mcp_method === "tools/call");
  const gwDeny = { ...real, id: undefined, console_url: "https://console.cloud.google.com/logs/query;query=insertId%3D%22constructed%22;timeRange=PT1H?project=p",
    summary: { ...real.summary, tool: "get_customer_contact", authz_result: "DENIED", status: 403, denied: true, insert_id: "constructed" } };
  const msgs = [...Q4.slice(0, -1), gwDeny, Q4[Q4.length - 1]];
  const v = view(msgs);
  const deny = tools(v).filter((r) => r.tool === "get_customer_contact");
  assert.equal(deny.length, 1);
  assert.equal(statusOf(deny[0]), "bad: denied — IAP: granted false · status 7 Permission Denied · gateway 403");
  assert.equal(v.lines, 7);
  assert.equal(v.rows.find((r) => r.kind === "identity").link.n, 2); // the IAP decision and the gateway row
});

test("blocked turn (real live2 q5): 3 lines; HTTP 403 + MATCH_FOUND, agent did not run, no answer step", () => {
  const v = view(Q5);
  assert.equal(v.lines, 3);
  assert.equal(v.blocked, true);
  const p = step(v, "prompt");
  assert.equal(txt(p.label), "Blocked by Model Armor (inbound gateway): prompt injection / jailbreak — HTTP 403");
  assert.equal(p.tone, "bad");
  assert.equal(p.link.n, 1);
  assert.equal(step(v, "tools").label, "Agent did not run (no agent events)");
  assert.equal(step(v, "answer"), undefined);
  assert.equal(v.footer.tokens, null);
  assert.equal(v.footer.total, 5);
});

test("wrapping: the filter names and '— HTTP 403' stay together; the deny keeps 'granted false' and the status message whole", () => {
  // the only plain spaces left are the places a line may break
  const blocked = step(view(Q5), "prompt").label;
  assert.ok(blocked.endsWith("(inbound gateway): prompt\u00a0injection\u00a0/\u00a0jailbreak\u00a0—\u00a0HTTP\u00a0403"), JSON.stringify(blocked));
  const deny = tools(view(Q4)).find((r) => r.tool === "get_customer_contact").status.text;
  assert.ok(deny.includes("granted\u00a0false"), JSON.stringify(deny));
  assert.ok(deny.endsWith("status\u00a07\u00a0Permission\u00a0Denied"), JSON.stringify(deny));
  // humanFilters elsewhere (the "match found: …" status) keeps ordinary spaces
  assert.equal(humanFilters([{ matched: true, filters: [{ name: "sdp", matched: true, info_types: ["US_SOCIAL_SECURITY_NUMBER", "BRAZIL_CPF_NUMBER"] }] }], { keepTogether: true }),
    "sensitive\u00a0data (US\u00a0SSN, Brazil\u00a0CPF)");
});

test("blocked: before the Model Armor record arrives the 403 is shown as rejected and the screening as waiting", () => {
  const early = upTo(Q5, (m) => m.kind === "listening" && m.until_ms != null);
  const v = view(early);
  const p = step(v, "prompt");
  assert.equal(txt(p.label), "Request rejected — HTTP 403: Model Armor: Prompt violates content security configurations");
  assert.equal(statusOf(p), "waiting: waiting for Model Armor record…");
  // the window closed and no Model Armor record came: neutral, never ✓/✗ for the screening
  const closed = view([...early, Q5[Q5.length - 1]]);
  assert.equal(statusOf(step(closed, "prompt")), "none: no record arrived");
});

test("live: 'waiting for … record…' until records arrive; 'no record arrived' once the window closed", () => {
  const calling = view(upTo(Q1, (m) => m.kind === "adk_event" && m.summary.calls.length));
  assert.equal(statusOf(step(calling, "prompt")), "waiting: waiting for Model Armor record…");
  assert.deepEqual(tools(calling).map((r) => [r.tool, r.count, statusOf(r)]), [
    ["get_latest_run", 1, "waiting: waiting for gateway record…"],
    ["get_model_summary", 3, "waiting: waiting for gateway record…"],
  ]);
  assert.equal(statusOf(step(calling, "answer")), "waiting: waiting for the answer…");
  assert.equal(calling.footer.latencyMs, null);

  const answered = view(upTo(Q1, (m) => m.kind === "stream_end"));
  assert.equal(statusOf(step(answered, "answer")), "waiting: waiting for Model Armor record…");

  // every log record missing, window closed: no ✓ and no ✗ anywhere
  const none = view(Q1.filter((m) => m.kind !== "log"));
  const tones = none.rows.flatMap((r) => [r.tone, r.status?.tone]).filter(Boolean);
  assert.ok(!tones.includes("good") && !tones.includes("bad"), tones.join());
  assert.equal(statusOf(step(none, "prompt")), "none: no record arrived");
  assert.ok(tools(none).every((r) => statusOf(r) === "none: no record arrived"));
  assert.equal(statusOf(step(none, "answer")), "none: no record arrived");
});

test("the answer step uses only the answer screening, never the create-session response screening", () => {
  const v = view(Q1.filter((m) => !isArmor("response", "answer_text")(m)));
  assert.equal(statusOf(step(v, "answer")), "none: no record arrived");
});

test("×N only when all N calls have the same status; partial arrival splits off the waiting calls (constructed)", () => {
  const call = (n) => ({ kind: "adk_event", t_ms: 1, summary: { parsed: true, calls: Array.from({ length: n }, (_, i) => ({ id: `c${i}`, name: "get_model_summary" })), responses: [] } });
  const allow = REAL_IAP.s3_churn_risk[0];
  const iap = (granted, k) => ({ ...allow, summary: { ...allow.summary, tool: "get_model_summary", granted, denied: granted === false, insert_id: `i${k}` } });
  const mixed = view([call(3), iap(true, 1), iap(false, 2), iap(true, 3)]);
  assert.deepEqual(tools(mixed).map((r) => [r.count, r.status.tone]), [[1, "bad"], [2, "good"]]);
  const partial = view([call(3), iap(true, 1)]);
  assert.deepEqual(tools(partial).map((r) => [r.count, statusOf(r)]), [[1, "good: allowed (IAP)"], [2, "waiting: waiting for gateway record…"]]);
});

test("no ×N when a tool's records give more statuses than it had calls (constructed: 2 calls, the real s4 retried deny + an allow)", () => {
  const call = { kind: "adk_event", t_ms: 1, summary: { parsed: true, calls: [{ id: "c1", name: "get_customer_contact" }, { id: "c2", name: "get_customer_contact" }], responses: [] } };
  const deny = REAL_IAP.s4_pii_contact; // 3 granted:false decisions for one call
  assert.equal(deny.length, 3);
  const allow = { ...REAL_IAP.s3_churn_risk[0], summary: { ...REAL_IAP.s3_churn_risk[0].summary, tool: "get_customer_contact", granted: true, denied: false, insert_id: "allow1" } };
  for (const msgs of [[call, ...deny, allow], [call, allow, ...deny]]) {
    const v = view(msgs);
    const rows = tools(v);
    assert.deepEqual(rows.map((r) => [r.count, r.status.tone]), [[1, "bad"], [1, "good"]]);
    assert.ok(rows.every((r) => r.countNote), "the tool name explains why there is no count");
    assert.equal(v.rows.find((r) => r.kind === "identity").link.n, 3); // every decision is still linked
    assert.equal(rows.filter((r) => r.status.tone === "waiting").length, 0);
  }
  // the counts never sum to more than the calls for any arrival prefix
  const msgs = [call, ...deny, allow];
  for (let k = 1; k <= msgs.length; k++) {
    const rows = tools(view(msgs.slice(0, k)));
    assert.ok(rows.reduce((n, r) => n + r.count, 0) <= 2, `prefix ${k}`);
  }
});

test("retried deny (real IAP s4: 1 call, 3 granted:false decisions): one deny line linking all three decisions", () => {
  const call = { kind: "adk_event", t_ms: 1, summary: { parsed: true, calls: [{ id: "c1", name: "get_customer_contact" }], responses: [] } };
  const v = view([call, ...REAL_IAP.s4_pii_contact]);
  const deny = tools(v);
  assert.equal(deny.length, 1);
  assert.equal(deny[0].count, 1);
  const link = v.rows.find((r) => r.kind === "identity").link;
  assert.equal(link.n, 3);
  for (const m of REAL_IAP.s4_pii_contact) assert.ok(decodeURIComponent(link.href).includes(`insertId="${m.summary.insert_id}"`));
});

test("earlier recordings (no IAP decisions): gateway rows are the secondary source; nothing is synthesized", () => {
  const q1 = view(recording("real_live_q1_pipeline_run.jsonl"));
  assert.deepEqual(tools(q1).map((r) => [r.tool, r.count, statusOf(r)]), [
    ["get_latest_run", 1, "good: allowed (gateway)"], ["get_model_summary", 3, "good: allowed (gateway)"],
  ]);
  // 2026-11-26 21:42: the call came back with an error, but no platform record about it arrived
  const q4 = view(recording("real_live_q4_contact.jsonl"));
  assert.equal(statusOf(tools(q4).find((r) => r.tool === "get_customer_contact")), "none: no record arrived");
  assert.equal(view(recording("real_live_q5_injection.jsonl")).lines, 3);
});

test("every ✓/✗ line links to its record(s)", () => {
  for (const msgs of [Q1, Q4, Q5, recording("real_live_q1_pipeline_run.jsonl")]) {
    const { rows } = view(msgs);
    rows.forEach((r, i) => {
      const decided = r.tone === "bad" || ["good", "bad"].includes(r.status?.tone);
      if (!decided || r.kind === "note") return;
      const link = r.link || (r.wide && rows[i + 1]?.link);
      assert.ok(link?.href, `${r.key} has no record link`);
    });
  }
});

test("Model Armor filters in words", () => {
  assert.equal(humanFilters([{ matched: true, filters: [{ name: "pi_and_jailbreak", matched: true, confidence: "HIGH" }] }]), "prompt injection / jailbreak");
  // shapes as records.py summarizes the recorded Model Armor entries (backend/tests/fixtures/recorded_model_armor_entries.json)
  assert.equal(humanFilters([
    { matched: true, filters: [{ name: "sdp", matched: true, info_types: ["US_SOCIAL_SECURITY_NUMBER"] }, { name: "rai", matched: false }] },
    { matched: true, filters: [{ name: "sdp", matched: true, info_types: ["BRAZIL_CPF_NUMBER"] }] },
  ]), "sensitive data (US SSN, Brazil CPF)");
  assert.equal(humanFilters([{ matched: true, filters: [{ name: "rai", matched: true, matched_types: ["dangerous", "harassment"] }] }]), "responsible AI (dangerous content, harassment)");
  assert.equal(humanFilters([{ matched: true, filters: [{ name: "malicious_uris", matched: true }, { name: "new_filter_x", matched: true }] }]), "malicious URL; new_filter_x");
  assert.equal(humanFilters([{ matched: true, matched_filters: ["sdp"] }]), "sensitive data");
  assert.deepEqual(armorStatus([{ match_state: null }], true), { tone: "neutral", text: "state not in record" });
});

test("blocked by sensitive data: info types in the line (Model Armor summary constructed from the recorded SSN entry)", () => {
  const ssn = { kind: "log", t_ms: 9000, source: "model_armor", hidden: false, console_url: "https://console.cloud.google.com/logs/query;query=x;timeRange=PT1H?project=p",
    attribution: { method: "prompt_text" }, summary: { operation: "prompt", match_state: "MATCH_FOUND", matched: true, matched_filters: ["sdp"],
      filters: [{ name: "sdp", matched: true, match_state: "MATCH_FOUND", info_types: ["US_SOCIAL_SECURITY_NUMBER"] }] } };
  const http = { kind: "http_error", t_ms: 900, status: 403, body: {}, message: "Model Armor: Prompt violates content security configurations" };
  const v = view([http, { kind: "stream_end", t_ms: 900, latency_ms: 900 }, ssn]);
  assert.equal(txt(step(v, "prompt").label), "Blocked by Model Armor (inbound gateway): sensitive data (US SSN) — HTTP 403");
});

test("record links: the record's own link, or one Logs Explorer query for all insertIds", () => {
  const a = { console_url: "https://console.cloud.google.com/logs/query;query=insertId%3D%22a%22;timeRange=PT1H?project=p", summary: { insert_id: "a" } };
  const b = { console_url: "https://console.cloud.google.com/logs/query;query=insertId%3D%22b%22;timeRange=PT1H?project=p", summary: { insert_id: "b" } };
  assert.equal(recordsLink([]), null);
  assert.deepEqual(recordsLink([a]), { href: a.console_url, n: 1 });
  assert.deepEqual(recordsLink([a, b]), {
    href: "https://console.cloud.google.com/logs/query;query=insertId%3D%22a%22%20OR%20insertId%3D%22b%22;timeRange=PT1H?project=p", n: 2,
  });
});

test("'All platform records (N)' counts everything the detailed timeline shows", () => {
  const t = fold(Q4);
  const shown = t.records.flatMap((r) => r.group || [r]).length + t.hidden.length + t.unmatched.length + t.iapOther.length;
  assert.equal(buildSteps(t).footer.total, shown);
  assert.equal(shown, 37);
  assert.equal(kilo(950), "950");
  assert.equal(kilo(4812), "4.8k");
  assert.equal(kilo(12345), "12k");
});
