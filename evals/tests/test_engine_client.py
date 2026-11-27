import json

import pytest

import engine_client as ec


def test_base_url():
    e = "projects/p/locations/us-central1/reasoningEngines/123"
    assert ec.base_url(e) == f"https://us-central1-aiplatform.googleapis.com/v1beta1/{e}"
    with pytest.raises(ValueError):
        ec.base_url("reasoningEngines/123")


def test_parse_sse_lines_keeps_raw_and_timing():
    res = ec.StreamResult()
    lines = [b"", b": keepalive", b'data: {"author": "a", "content": {"parts": [{"text": "hi"}]}}', b"",
             b'{"author": "a", "error_code": "X"}', b"data: not json"]
    ec.parse_sse_lines(lines, res, 10.0, clock=lambda: 10.25)
    assert [e["author"] for e in res.events] == ["a", "a"]
    assert res.raw_lines == ["not json"]
    assert res.first_event_s == 0.25


class FakeResp:
    def __init__(self, status, body="", lines=()):
        self.status_code, self.text, self._lines, self.headers = status, body, lines, {"content-type": "x"}

    def iter_lines(self):
        return iter(self._lines)

    def json(self):
        return json.loads(self.text)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeHTTP:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self.resp


@pytest.fixture(autouse=True)
def no_adc(monkeypatch):
    monkeypatch.setattr(ec, "access_token", lambda: "tok")


E = "projects/p/locations/us-central1/reasoningEngines/1"


def test_stream_query_ok_sends_protocol_and_trace():
    http = FakeHTTP(FakeResp(200, lines=[b'data: {"author": "x"}']))
    res = ec.stream_query(E, "u", "s1", "hello", trace=("a" * 32, "0" * 15 + "1"), session=http)
    url, kw = http.calls[0]
    assert url.endswith(":streamQuery?alt=sse")
    assert kw["json"] == {"class_method": "async_stream_query", "input": {"user_id": "u", "message": "hello", "session_id": "s1"}}
    assert kw["headers"]["traceparent"] == f"00-{'a' * 32}-{'0' * 15}1-01"
    assert kw["headers"]["X-Cloud-Trace-Context"] == f"{'a' * 32}/1;o=1"
    assert res.events == [{"author": "x"}] and res.http_error is None and res.total_s is not None


def test_stream_query_http_error_is_returned_not_raised():
    res = ec.stream_query(E, "u", None, "m", session=FakeHTTP(FakeResp(403, body="blocked by Model Armor")))
    assert res.http_status == 403 and res.http_error["body"] == "blocked by Model Armor" and res.events == []


def test_create_session():
    http = FakeHTTP(FakeResp(200, body='{"output": {"id": "sess-9", "user_id": "u"}}'))
    assert ec.create_session(E, "u", session=http) == ("sess-9", None)
    assert http.calls[0][0].endswith(":query")
    assert http.calls[0][1]["json"] == {"class_method": "async_create_session", "input": {"user_id": "u"}}
    sid, err = ec.create_session(E, "u", session=FakeHTTP(FakeResp(500, body="boom")))
    assert sid is None and err == {"status": 500, "body": "boom"}


def test_parse_sse_deadline_stops_with_transport_error():
    res = ec.StreamResult()
    t = iter([0.5, 1.0, 200.0])  # deadline check, first-event timing, deadline check
    lines = [b'{"author": "a", "content": {"parts": [{"function_call": {"name": "x"}}]}}', b'{"author": "b"}', b'{"author": "c"}']
    ec.parse_sse_lines(lines, res, 0.0, clock=lambda: next(t), deadline=120.0)
    assert [e["author"] for e in res.events] == ["a"]
    assert res.transport_error.startswith("deadline")


class SlowResp(FakeResp):
    """iter_lines blocks until close() is called (a stream that never ends)."""
    def __init__(self):
        super().__init__(200)
        import threading
        self.closed = threading.Event()

    def iter_lines(self):
        yield b'data: {"author": "a"}'
        self.closed.wait(5)
        if self.closed.is_set():
            raise ConnectionError("closed")

    def close(self):
        self.closed.set()


def test_stream_query_deadline_cuts_a_stream_that_never_ends():
    res = ec.stream_query(E, "u", "s1", "hello", session=FakeHTTP(SlowResp()), deadline_s=0.3)
    assert res.events == [{"author": "a"}]
    assert res.transport_error.startswith("deadline") and res.total_s < 3
