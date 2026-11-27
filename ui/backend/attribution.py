"""Which open turn a Cloud Logging record belongs to.

Logs are project-wide, so each record is matched to a turn by what the record itself contains, and only
when nothing in it identifies a turn, by time. Checked against real records (tests/fixtures/real_*):

  gateway row  - URL contains the turn's session id (…/sessions/<id>…)          -> "session_id"
               - trace equals the traceparent trace id the backend sent with this turn's streamQuery
                 (verified live 2026-11-26: the MCP rows carry it)               -> "sent_trace"
               - trace equals the trace of a row already matched by session id  -> "trace_id"
                 (on the 2026-11-26 records the session rows carry NO trace, so this gives nothing)
               - otherwise, the turn whose time window holds the timestamp      -> "time"
  Model Armor  - screened text == the turn's message (SANITIZE_USER_PROMPT)    -> "prompt_text"
  (gateway     - screened text contains the turn's session id (create-session
   records)      response)                                                       -> "session_text"
               - screened text is the turn's answer text                       -> "answer_text"
               - client_correlation_id of a prompt record matched to the turn  -> "correlation_id"
               - trace equals the sent traceparent trace id (verified live: the
                 streamQuery prompt and response screenings carry it)           -> "sent_trace"
               - otherwise, held until the turn whose window holds the timestamp closes, then given to it
                 as "unmatched" (shown collapsed, never in the timeline)
  IAP decision - principalSubject is this agent's principal AND the timestamp is in the turn's window AND
  (McpServer,    mcp_tool_name is a function_call name in the turn's ADK events     -> "principal_tool_time"
   tools/call)   (the records carry no trace or session; several candidate turns -> the one whose
                 function_call of that tool is closest in time; no call of that tool -> not attributed;
                 handshake methods carry no tool name and are not attributed)

Time windows never overlap: a newer turn caps every older window at its own window start.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import records


@dataclass(eq=False)
class TurnRef:
    """What attribution needs to know about one open turn."""

    id: str
    session_id: str
    message: str
    start: float  # window start (epoch seconds)
    cap: float | None = None  # window end, set when a newer turn opens
    answer_texts: list[str] = field(default_factory=list)
    trace_ids: set[str] = field(default_factory=set)  # traces of rows matched by session id
    sent_trace: str | None = None  # the traceparent trace id sent with this turn's streamQuery
    prompt_correlation_ids: set[str] = field(default_factory=set)
    tool_calls: list[tuple[str, float]] = field(default_factory=list)  # (function_call name, event time)
    finalizing: bool = False  # its last poll is running: held Model Armor records go to it now

    def holds(self, ts: float) -> bool:
        return ts >= self.start and (self.cap is None or ts < self.cap)


@dataclass
class Decision:
    turn: TurnRef
    method: str  # session_id | sent_trace | trace_id | prompt_text | session_text | answer_text | correlation_id |
    #              principal_tool_time | time | unmatched
    detail: str


HOLD = "hold"  # not attributable yet; look at it again on the next poll


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def _token_in(needle: str, haystack: str | None) -> bool:
    """needle appears in haystack as a whole token (session ids are long digit strings)."""
    if not needle or not haystack:
        return False
    return re.search(rf"(?<![0-9A-Za-z]){re.escape(needle)}(?![0-9A-Za-z])", haystack) is not None


def answer_matches(screened: str | None, answer_texts: list[str]) -> bool:
    """The screened response text is (or starts like) the answer text the turn streamed."""
    got = _norm(records.screened_inner_text(screened))
    if not got:
        return False
    candidates = list(answer_texts)
    if len(answer_texts) > 1:
        candidates += ["".join(answer_texts), "\n\n".join(answer_texts)]
    for text in candidates:
        want = _norm(text)
        if not want:
            continue
        if got == want:
            return True
        k = min(len(got), len(want), 200)
        if k >= 40 and (want[:k] in got or got[:k] in want):
            return True
    return False


def _pick(candidates: list[tuple[TurnRef, str, str]], ts: float | None) -> Decision | None:
    """One turn among exact candidates: the one whose window holds ts, else the closest window."""
    if not candidates:
        return None
    if ts is not None:
        for turn, method, detail in candidates:
            if turn.holds(ts):
                return Decision(turn, method, detail)

        def distance(c: tuple[TurnRef, str, str]) -> float:
            t = c[0]
            if ts < t.start:
                return t.start - ts
            return ts - (t.cap if t.cap is not None else ts)

        turn, method, detail = min(candidates, key=distance)
        return Decision(turn, method, detail)
    turn, method, detail = candidates[0]
    return Decision(turn, method, detail)


def time_owner(ts: float | None, turns: list[TurnRef]) -> TurnRef | None:
    if ts is None:
        return None
    owners = [t for t in turns if t.holds(ts)]
    return min(owners, key=lambda t: t.start) if owners else None


def match_gateway(summary: dict, turns: list[TurnRef]) -> list[tuple[TurnRef, str, str]]:
    out: list[tuple[TurnRef, str, str]] = []
    sessions = summary.get("session_ids") or []
    trace = summary.get("trace_id")
    for t in turns:
        if t.session_id and t.session_id in sessions:
            out.append((t, "session_id", f"request URL contains session {t.session_id}"))
        elif trace and trace == t.sent_trace:
            out.append((t, "sent_trace", f"trace {trace} is the traceparent sent with this turn's request"))
        elif trace and trace in t.trace_ids:
            out.append((t, "trace_id", f"trace {trace} is the trace of a row matched by this turn's session id"))
    return out


def match_armor(summary: dict, turns: list[TurnRef]) -> list[tuple[TurnRef, str, str]]:
    out: list[tuple[TurnRef, str, str]] = []
    text = summary.get("screened_text")
    inner = records.screened_inner_text(text)
    op = summary.get("operation")
    corr = summary.get("correlation_id")
    for t in turns:
        if op == "prompt" and inner is not None and _norm(inner) == _norm(t.message):
            out.append((t, "prompt_text", "screened text is this turn's message"))
        elif op != "prompt" and _token_in(t.session_id, text):
            out.append((t, "session_text", f"screened text contains session {t.session_id}"))
        elif op != "prompt" and answer_matches(text, t.answer_texts):
            out.append((t, "answer_text", "screened text is this turn's answer"))
        elif corr and corr in t.prompt_correlation_ids:
            out.append((t, "correlation_id", f"client_correlation_id {corr} of this turn's prompt screening"))
        elif summary.get("trace_id") and summary["trace_id"] == t.sent_trace:
            out.append((t, "sent_trace", f"trace {t.sent_trace} is the traceparent sent with this turn's request"))
    return out


def match_iap(summary: dict, ts: float | None, turns: list[TurnRef],
              agent_principal: str | None) -> Decision | str | None:
    """An IAP decision goes to a turn only on all three: the agent's principal, the turn's window, and a
    function_call of the same tool name in the turn's events. HOLD while the window's owner may still stream
    that call; None otherwise (never attributed by time alone)."""
    if not agent_principal or summary.get("principal") != agent_principal or ts is None:
        return None
    tool = summary.get("tool")
    if summary.get("mcp_method") != "tools/call" or not tool:
        return None
    best: tuple[float, TurnRef] | None = None
    for t in turns:
        if not t.holds(ts):
            continue
        times = [at for name, at in t.tool_calls if name == tool]
        if times:
            gap = min(abs(ts - at) for at in times)
            if best is None or gap < best[0]:
                best = (gap, t)
    if best is None:
        owner = time_owner(ts, turns)
        return HOLD if owner is not None and not owner.finalizing else None
    return Decision(best[1], "principal_tool_time",
                    f"principalSubject is this agent's principal; {tool} is a function_call of this turn "
                    f"(closest call {best[0]:.1f} s from the decision); timestamp in this turn's window")


def attribute(source: str, summary: dict, ts: float | None, turns: list[TurnRef],
              agent_principal: str | None = None) -> Decision | str | None:
    """A Decision, HOLD (look again on the next poll), or None (no open turn wants it; it is not marked
    seen, so it is looked at again while it stays inside the polled range)."""
    if source == records.SOURCE_IAP:
        return match_iap(summary, ts, turns, agent_principal)
    if source == records.SOURCE_MODEL_ARMOR:
        exact = _pick(match_armor(summary, turns), ts)
        if exact:
            return exact
        owner = time_owner(ts, turns)
        if owner is None:
            return None
        if owner.finalizing:
            return Decision(owner, "unmatched",
                            "in this turn's time window; screened text matches nothing of this turn")
        return HOLD
    exact = _pick(match_gateway(summary, turns), ts)
    if exact:
        return exact
    # A row that identifies someone else's request (another session in its URL, or a trace no open
    # turn sent or learned) is never given to a turn by time: it belongs to other traffic.
    foreign_session = bool(summary.get("session_ids"))  # names a session, and it is none of ours
    foreign_trace = bool(summary.get("trace_id")) and any(t.sent_trace for t in turns)
    if foreign_session or foreign_trace:
        return None
    owner = time_owner(ts, turns)
    if owner is None:
        return None
    return Decision(owner, "time", "nothing in the row identifies the turn; its timestamp is in this turn's window")


def learn(decision: Decision, source: str, summary: dict) -> None:
    """What an exact match teaches about its turn (trace ids of session rows, prompt correlation ids)."""
    t = decision.turn
    if source == records.SOURCE_MODEL_ARMOR:
        if decision.method == "prompt_text" and summary.get("correlation_id"):
            t.prompt_correlation_ids.add(summary["correlation_id"])
    elif decision.method == "session_id" and summary.get("trace_id"):
        t.trace_ids.add(summary["trace_id"])


def parse_ts(value: Any) -> float | None:
    """RFC 3339 timestamp (nanoseconds allowed) -> epoch seconds."""
    from datetime import datetime, timezone

    if not isinstance(value, str) or not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", s)
    if not m:
        return None
    frac = (m.group(2) or "")[:7]  # '.' + up to 6 digits
    try:
        dt = datetime.fromisoformat(m.group(1) + frac + (m.group(3) or "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
