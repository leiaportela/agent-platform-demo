import React, { useCallback, useEffect, useRef, useState } from "react";
import { Moon, Sun, ExternalLink, RotateCcw } from "lucide-react";
import { createSession, getConfig, streamChat } from "./api.js";
import { applyMessage, newTurn } from "./turns.js";
import Chat, { presetsFor } from "./components/Chat.jsx";
import Panel from "./components/Panel.jsx";
import Architecture from "./components/Architecture.jsx";
import { hashForView, viewFromHash } from "./views.js";

function Mark() {
  return (
    <svg width="22" height="16" viewBox="0 0 22 16" aria-hidden="true">
      <path d="M1 4h6v8h5V4h9" fill="none" stroke="var(--shell-ink)" strokeWidth="2" strokeLinejoin="miter" />
    </svg>
  );
}

export default function App() {
  const [theme, setTheme] = useState(() => localStorage.getItem("theme") || "light");
  const [config, setConfig] = useState(null);
  const [configError, setConfigError] = useState(null);
  const [session, setSession] = useState(null);
  const [sessionError, setSessionError] = useState(null);
  const [turns, setTurns] = useState([]);
  const [selected, setSelected] = useState(null);
  const [busy, setBusy] = useState(false);
  const aborts = useRef([]);
  const [view, setView] = useState(() => viewFromHash(window.location.hash));

  // The chat stays mounted while another view is shown: streams, late records and the draft keep going.
  useEffect(() => {
    const onHash = () => setView(viewFromHash(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const go = (v) => {
    if (window.location.hash !== hashForView(v)) window.location.hash = hashForView(v);
    setView(v);
  };

  document.documentElement.dataset.theme = theme;
  useEffect(() => { localStorage.setItem("theme", theme); }, [theme]);

  useEffect(() => {
    getConfig().then(setConfig).catch((e) => setConfigError(e.message));
  }, []);

  // Sessions are created when the first message of a session is sent (not on page load), so the
  // create-session records (its Model Armor response screening, the session rows) fall in that turn's window.
  const startSession = useCallback(async () => {
    setSessionError(null);
    try {
      const s = await createSession();
      setSession(s);
      return s;
    } catch (e) {
      const b = e.body && typeof e.body === "object" ? e.body : {};
      setSessionError({ message: e.message, status: e.status, body: e.body, platformStatus: b.status, platformMessage: b.message });
      return null;
    }
  }, []);

  const update = (id, fn) => setTurns((ts) => ts.map((t) => (t.id === id ? fn(t) : t)));

  const send = useCallback(async (message) => {
    const text = message.trim();
    if (!text || busy) return;
    setBusy(true);
    const s = session || (await startSession());
    if (!s) { setBusy(false); return; }
    const turn = newTurn(text, s.session_id);
    setTurns((ts) => [...ts, turn]);
    setSelected(turn.id);
    const ctrl = new AbortController();
    aborts.current.push(ctrl);
    try {
      await streamChat({ sessionId: s.session_id, userId: s.user_id, message: text }, (msg) => {
        update(turn.id, (t) => applyMessage(t, msg));
        // The next question is allowed as soon as the agent stream ends; this turn keeps listening.
        if (msg.kind === "stream_end" || msg.kind === "turn_end" || msg.kind === "replay_unmatched") setBusy(false);
      }, ctrl.signal);
    } catch (e) {
      if (e.name !== "AbortError") update(turn.id, (t) => ({ ...t, clientError: { message: e.message, status: e.status, body: e.body }, ended: true, streamEnded: true }));
    } finally {
      setBusy(false);
      aborts.current = aborts.current.filter((c) => c !== ctrl);
      update(turn.id, (t) => (t.ended ? t : { ...t, ended: true, streamEnded: true, cut: true }));
    }
  }, [busy, session, startSession]);

  // New session: the next message creates a new ADK session. Earlier turns stay on screen and keep
  // listening for their own late records (nothing is aborted).
  const reset = () => {
    setSession(null);
    setSessionError(null);
  };

  const current = turns.find((t) => t.id === selected) || turns[turns.length - 1] || null;
  const replay = !!config?.replay;

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <Mark />
            <span>Pipelines analyst</span>
            <span className="brand-sub">governed by Agent Gateway + Model Armor</span>
          </div>
          <nav className="tabs" role="tablist" aria-label="Views">
            <button type="button" role="tab" id="tab-chat" aria-controls="view-chat" className="tab"
              aria-selected={view === "chat"} onClick={() => go("chat")}>Chat</button>
            <button type="button" role="tab" id="tab-architecture" aria-controls="view-architecture" className="tab"
              aria-selected={view === "architecture"} onClick={() => go("architecture")}>Architecture</button>
          </nav>
          {replay && (
            <div className="replay-badge" role="status" title={config.replay_file || ""}>
              REPLAY <span>{config.replay_provenance}, not live</span>
            </div>
          )}
          <div className="topbar-right">
            {config && !replay && (
              <span className="fresh" title={config.engine || "ENGINE not set"}>
                <span className={`dot${config.engine ? "" : " stale"}`} />
                {config.engine ? `Agent Runtime · …${config.engine.slice(-8)}` : "ENGINE not set"}
              </span>
            )}
            <span className="fresh" title={session?.session_id || "A new session is created with the next message"}>
              {session ? `Session …${String(session.session_id).slice(-6)}` : "New session on next message"}
            </span>
            {config && (
              <a className="shell-link" href={config.links.gateways} target="_blank" rel="noreferrer">
                Agent Gateways <ExternalLink size={13} />
              </a>
            )}
            <button type="button" className="icon-btn" aria-label="New session" title="New session" onClick={reset}>
              <RotateCcw size={15} />
            </button>
            <button type="button" className="icon-btn" aria-label={theme === "light" ? "Switch to blueprint (dark)" : "Switch to drawing (light)"}
              onClick={() => setTheme(theme === "light" ? "dark" : "light")}>
              {theme === "light" ? <Moon size={16} /> : <Sun size={16} />}
            </button>
          </div>
        </div>
      </header>

      <main className="workspace" id="view-chat" role="tabpanel" aria-labelledby="tab-chat" hidden={view !== "chat"}>
        <Chat
          turns={turns}
          presets={presetsFor(config)}
          selected={current?.id}
          onSelect={setSelected}
          onSend={send}
          busy={busy}
          blocker={configError ? `Backend not reachable: ${configError}` : sessionError ? sessionError : null}
          onRetrySession={startSession}
          active={view === "chat"}
        />
        <Panel turns={turns} selected={current?.id} config={config} />
      </main>
      {view === "architecture" && <Architecture config={config} />}
    </div>
  );
}
