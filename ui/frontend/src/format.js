export const secs = (ms) => (ms == null ? "–" : `${(ms / 1000).toFixed(1)} s`);
export const plus = (ms) => (ms == null ? "" : `+${(ms / 1000).toFixed(1)} s`);
export const int = (n) => (n == null ? "–" : Number(n).toLocaleString("en-US"));
export const pretty = (v) => {
  if (typeof v === "string") return v;
  try { return JSON.stringify(v, null, 2); } catch { return String(v); }
};
export const compact = (v) => {
  try { return JSON.stringify(v); } catch { return String(v); }
};
export const host = (url) => {
  try { const u = new URL(url); return u.host + u.pathname; } catch { return url || ""; }
};
export const hostname = (url) => {
  try { return new URL(url).host; } catch { return url || ""; }
};
