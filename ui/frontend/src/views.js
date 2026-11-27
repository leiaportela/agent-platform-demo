// Top-level views. The hash keeps the view across reloads (#/architecture); the chat is the default.
export const VIEWS = ["chat", "architecture"];

export function viewFromHash(hash) {
  const v = String(hash || "").replace(/^#\/?/, "").split(/[/?]/)[0].toLowerCase();
  return VIEWS.includes(v) ? v : "chat";
}

export function hashForView(view) {
  return view === "chat" ? "#/" : `#/${view}`;
}
