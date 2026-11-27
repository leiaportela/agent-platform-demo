"""Settings from the environment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# projects/<project id or number>/locations/<region>/reasoningEngines/<numeric id>
ENGINE_RE = re.compile(r"^projects/([A-Za-z0-9._:-]+)/locations/([a-z0-9-]+)/reasoningEngines/([0-9]+)$")

# Cloud Logging entries.list quota is ~60 requests/min per project, shared with everyone: never poll faster.
MIN_POLL_INTERVAL_S = 3.0
# Model Armor sanitize records can show up in entries.list long after the answer.
MIN_LINGER_S = 75.0
# The organization in the agent's Agent Identity principal comes from AGENT_ORG (infra/env.sh sets it from ORG_ID).


class SettingsError(ValueError):
    """A configuration error that must stop the backend at startup."""


@dataclass(frozen=True)
class Settings:
    engine: str | None = None
    project: str = field(default_factory=lambda: os.environ.get("PROJECT", ""))
    inbound_gw: str = "agent-demo-ingress-gw"
    outbound_gw: str = "agent-demo-egress-gw"
    poll_interval_s: float = MIN_POLL_INTERVAL_S  # one entries.list per tick for all open turns
    max_backoff_s: float = 60.0  # ceiling of the exponential backoff after HTTP 429
    lookback_s: float = 0.5  # a turn's window starts this long before t0
    linger_s: float = MIN_LINGER_S  # keep polling this long after the agent stream ends
    session_window_s: float = 60.0  # first turn of a session created this recently reaches back to its creation
    keepalive_s: float = 10.0
    send_traceparent: bool = True  # send a W3C traceparent with streamQuery (rows carrying it match exactly)
    dev_replay: str | None = None  # DEV_REPLAY: a recorded turn file (JSON lines); local development only
    record_dir: str | None = None  # RECORD_DIR: append every live turn's messages here (to make replays)
    engine_console_url: str | None = None
    agent_org: str | None = field(default_factory=lambda: os.environ.get("AGENT_ORG") or None)  # org number in the principal

    def __post_init__(self) -> None:
        if self.engine is not None and not ENGINE_RE.match(self.engine):
            raise SettingsError(
                f"ENGINE={self.engine!r} is not projects/<project>/locations/<region>/reasoningEngines/<id>"
            )

    @property
    def region(self) -> str | None:
        """The Agent Runtime region, taken from ENGINE (never configured separately)."""
        m = ENGINE_RE.match(self.engine or "")
        return m.group(2) if m else None

    @property
    def agent_principal(self) -> str | None:
        """The agent's Agent Identity principal, as IAP records it in protoPayload.authenticationInfo.
        principalSubject: built from ENGINE (project NUMBER, region, engine id) and the organization. None when
        ENGINE is unset or names the project by id (the principal needs the number), so no IAP clause is polled."""
        m = ENGINE_RE.match(self.engine or "")
        if not m or not m.group(1).isdigit() or not self.agent_org:
            return None
        project_number, region, engine_id = m.groups()
        return (f"principal://agents.global.org-{self.agent_org}.system.id.goog/resources/aiplatform/"
                f"projects/{project_number}/locations/{region}/reasoningEngines/{engine_id}")

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        settings = cls(
            engine=env("ENGINE") or None,
            project=env("PROJECT") or "",
            inbound_gw=env("INBOUND_GATEWAY") or "agent-demo-ingress-gw",
            outbound_gw=env("OUTBOUND_GATEWAY") or "agent-demo-egress-gw",
            poll_interval_s=max(MIN_POLL_INTERVAL_S, float(env("LOG_POLL_INTERVAL_S") or MIN_POLL_INTERVAL_S)),
            max_backoff_s=float(env("LOG_MAX_BACKOFF_S") or 60.0),
            lookback_s=float(env("LOG_LOOKBACK_S") or 0.5),
            linger_s=max(MIN_LINGER_S, float(env("LOG_LINGER_S") or MIN_LINGER_S)),
            send_traceparent=(env("SEND_TRACEPARENT") or "1").lower() not in ("0", "false", "no"),
            dev_replay=env("DEV_REPLAY") or None,
            record_dir=env("RECORD_DIR") or None,
            engine_console_url=env("ENGINE_CONSOLE_URL") or None,
            agent_org=env("AGENT_ORG") or None,
        )
        if not settings.project and not settings.dev_replay:
            raise SettingsError("PROJECT is not set: source infra/env.sh (or set PROJECT on the Cloud Run service)")
        region = env("REGION")
        if region and settings.region and region != settings.region:
            raise SettingsError(f"REGION={region!r} differs from the region in ENGINE ({settings.region!r})")
        return settings

    def engine_link(self) -> str | None:
        """Console page of the engine (URL pattern unverified; override with ENGINE_CONSOLE_URL)."""
        if self.engine_console_url:
            return self.engine_console_url
        if not self.engine:
            return None
        engine_id = self.engine.rstrip("/").rsplit("/", 1)[-1]
        return (
            "https://console.cloud.google.com/vertex-ai/agents/agent-engines/locations/"
            f"{self.region}/agent-engines/{engine_id}?project={self.project}"
        )
