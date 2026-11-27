// Backend calls. /api/chat is a POST that answers text/event-stream, so it is read with fetch.

async function json(res) {
  const text = await res.text();
  let body;
  try { body = JSON.parse(text); } catch { body = text; }
  if (!res.ok) {
    const err = new Error(typeof body === "object" ? body.detail || body.error || `HTTP ${res.status}` : `HTTP ${res.status}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

export const getConfig = () => fetch("/api/config").then(json);

export const createSession = (userId) =>
  fetch("/api/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user_id: userId || null }) }).then(json);

/** Streams one turn; calls onMessage for every SSE data line (parsed JSON). */
export async function streamChat({ sessionId, userId, message }, onMessage, signal) {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ session_id: sessionId, user_id: userId, message }),
    signal,
  });
  if (!res.ok || !res.body) {
    await json(res); // throws with the body
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of frame.split("\n")) {
        if (!line.startsWith("data:")) continue; // ": keepalive" comments
        try { onMessage(JSON.parse(line.slice(5).trim())); } catch { /* malformed frame: ignore */ }
      }
    }
  }
}
