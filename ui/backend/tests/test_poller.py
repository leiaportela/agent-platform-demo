"""Settings validation and the shared poller's pacing (quota: one entries.list per tick, >= 3 s, backoff)."""
import pytest

from platform_clients import HttpError, LogPage
from poller import LogPoller, TurnCtx
from settings import MIN_LINGER_S, MIN_POLL_INTERVAL_S, Settings, SettingsError

ENGINE = "projects/123456789012/locations/us-central1/reasoningEngines/1234567890123456789"


def test_engine_format_is_validated_and_region_comes_from_it():
    assert Settings(engine=ENGINE).region == "us-central1"
    assert Settings(engine="projects/p/locations/europe-west4/reasoningEngines/12").region == "europe-west4"
    assert Settings().region is None
    for bad in ("1234567890123456789", "projects/p/locations/us-central1/reasoningEngines/",
                "projects/p/reasoningEngines/1", f"{ENGINE}/sessions/1", "https://x/" + ENGINE):
        with pytest.raises(SettingsError):
            Settings(engine=bad)


def test_from_env_clamps_quota_and_linger(monkeypatch):
    monkeypatch.setenv("ENGINE", ENGINE)
    monkeypatch.setenv("LOG_POLL_INTERVAL_S", "1")
    monkeypatch.setenv("LOG_LINGER_S", "30")
    monkeypatch.delenv("REGION", raising=False)
    s = Settings.from_env()
    assert s.poll_interval_s == MIN_POLL_INTERVAL_S == 3.0 and s.linger_s == MIN_LINGER_S == 75.0
    assert s.lookback_s == 0.5
    monkeypatch.setenv("REGION", "europe-west1")
    with pytest.raises(SettingsError):
        Settings.from_env()
    monkeypatch.setenv("REGION", "us-central1")
    assert Settings.from_env().region == "us-central1"
    monkeypatch.setenv("ENGINE", "not-an-engine")
    with pytest.raises(SettingsError):
        Settings.from_env()


class ScriptedLogs:
    def __init__(self, results):
        self.results, self.calls = list(results), 0

    async def list_entries(self, filter_):
        self.calls += 1
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def open_turn(poller, start=0.0):
    t = TurnCtx(id="t", session_id="s", message="m", start=start)
    poller.turns.append(t)
    return t


@pytest.mark.asyncio
async def test_backoff_doubles_on_429_up_to_the_ceiling_and_resets():
    quota = HttpError(429, {"error": {"code": 429, "message": "Quota exceeded"}})
    logs = ScriptedLogs([quota, quota, quota, quota, quota, [], quota])
    poller = LogPoller(logs, Settings(engine=ENGINE, poll_interval_s=3.0, max_backoff_s=20.0))
    t = open_turn(poller)
    delays = [await poller.tick() for _ in range(7)]
    assert delays == [6.0, 12.0, 20.0, 20.0, 20.0, 3.0, 6.0]
    assert logs.calls == 7 == poller.ticks
    errors = []
    while not t.queue.empty():
        errors.append(t.queue.get_nowait())
    # reported once per streak, not once per tick
    assert [e["status"] for e in errors] == [429, 429] and errors[0]["message"] == "Quota exceeded"


@pytest.mark.asyncio
async def test_extra_pages_stretch_the_next_sleep():
    logs = ScriptedLogs([LogPage(entries=[], calls=3, truncated=True)])
    poller = LogPoller(logs, Settings(engine=ENGINE, poll_interval_s=3.0))
    t = open_turn(poller)
    assert await poller.tick() == 9.0 and poller.calls == 3
    msg = t.queue.get_nowait()
    assert msg["kind"] == "log_poll_error" and "more pages" in msg["message"]


@pytest.mark.asyncio
async def test_turn_closes_only_after_a_tick_that_started_past_its_deadline():
    logs = ScriptedLogs([[], [], []])
    poller = LogPoller(logs, Settings(engine=ENGINE))
    t = open_turn(poller)
    await poller.tick()
    assert t in poller.turns and t.queue.empty()  # still answering (no deadline)
    t.deadline = 1e12
    await poller.tick()
    assert t in poller.turns
    t.deadline = 1.0  # long past
    await poller.tick()
    assert t not in poller.turns and t.queue.get_nowait() is None


@pytest.mark.asyncio
async def test_one_query_covers_all_open_windows():
    logs = ScriptedLogs([[]])
    filters = []
    orig = logs.list_entries

    async def capture(f):
        filters.append(f)
        return await orig(f)

    logs.list_entries = capture
    poller = LogPoller(logs, Settings(engine=ENGINE))
    a = open_turn(poller, start=1795726816.5)
    poller.cap_open_windows(1795726900.0)
    b = TurnCtx(id="b", session_id="s2", message="m2", start=1795726900.0)
    poller.turns.append(b)
    await poller.tick()
    assert len(filters) == 1
    assert 'timestamp>="2026-11-26T21:00:16.500000Z"' in filters[0] and "timestamp<" not in filters[0]
    assert a.cap == b.start
