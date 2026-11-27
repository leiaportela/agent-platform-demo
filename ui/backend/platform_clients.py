"""Thin async clients for the two platform APIs the UI reads: Agent Runtime and Cloud Logging.

The chat endpoint only depends on the two small interfaces below (stream_query / create_session and
list_entries), so tests swap in fakes.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

import records

CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
LOGGING_URL = "https://logging.googleapis.com/v2/entries:list"


class TokenSource:
    """Application Default Credentials access token (scope cloud-platform); gcloud CLI as fallback."""

    def __init__(self) -> None:
        self._creds: Any = None
        self._cli_token: tuple[str, float] | None = None
        self._lock = asyncio.Lock()

    def _refresh_sync(self) -> str:
        try:
            import google.auth
            import google.auth.transport.requests

            if self._creds is None:
                self._creds, _ = google.auth.default(scopes=[CLOUD_SCOPE])
            if not self._creds.valid:
                self._creds.refresh(google.auth.transport.requests.Request())
            if self._creds.token:
                return self._creds.token
        except Exception:  # e.g. ADC not configured on a dev machine
            pass
        if self._cli_token and time.time() - self._cli_token[1] < 1800:
            return self._cli_token[0]
        gcloud = shutil.which("gcloud") or "/snap/bin/gcloud"
        token = subprocess.check_output([gcloud, "auth", "print-access-token"], text=True).strip()
        self._cli_token = (token, time.time())
        return token

    async def token(self) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._refresh_sync)


@dataclass
class HttpError(Exception):
    status: int
    body: Any

    def __str__(self) -> str:
        return f"HTTP {self.status}: {records.http_error_message(self.body)}"


class EngineClient:
    """Agent Runtime (reasoningEngines) over REST v1beta1, like esmeralda's query.py.
    The endpoint region is the location in the engine name (settings.Settings.region)."""

    def __init__(self, engine: str, region: str, tokens: TokenSource, timeout: float = 300.0) -> None:
        self.engine = engine
        self.region = region
        self.tokens = tokens
        self.timeout = timeout

    @property
    def base(self) -> str:
        return f"https://{self.region}-aiplatform.googleapis.com/v1beta1/{self.engine}"

    async def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.tokens.token()}", "Content-Type": "application/json",
                **(extra or {})}

    async def create_session(self, user_id: str) -> Any:
        payload = {"class_method": "async_create_session", "input": {"user_id": user_id}}
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{self.base}:query", json=payload, headers=await self._headers())
        if r.status_code != 200:
            raise HttpError(r.status_code, records.http_error_body(r.text))
        return r.json()

    async def stream_query(self, user_id: str, session_id: str, message: str,
                           headers: dict[str, str] | None = None) -> AsyncIterator[Any]:
        """Yields each SSE data line as parsed JSON (or the raw string if it does not parse).
        Raises HttpError on a non-200 response; never retries."""
        payload = {
            "class_method": "async_stream_query",
            "input": {"user_id": user_id, "session_id": session_id, "message": message},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST", f"{self.base}:streamQuery?alt=sse", json=payload, headers=await self._headers(headers)
            ) as r:
                if r.status_code != 200:
                    await r.aread()
                    raise HttpError(r.status_code, records.http_error_body(r.text))
                async for line in r.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":") or line.startswith("event:"):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        yield line


@dataclass
class LogPage:
    """One entries.list result: the entries, how many API calls it took, and whether pages were left."""

    entries: list[dict]
    calls: int = 1
    truncated: bool = False


class LoggingClient:
    """Cloud Logging entries.list (read only). Every page is one API call against the shared
    per-project quota, so pages are large and capped."""

    def __init__(self, project: str, tokens: TokenSource, page_size: int = 1000, max_pages: int = 3) -> None:
        self.project = project
        self.tokens = tokens
        self.page_size = page_size
        self.max_pages = max_pages
        self._client = httpx.AsyncClient(timeout=20)

    async def list_entries(self, filter_: str) -> LogPage:
        entries: list[dict] = []
        page_token = None
        calls = 0
        for _ in range(self.max_pages):
            body: dict[str, Any] = {
                "resourceNames": [f"projects/{self.project}"],
                "filter": filter_,
                "orderBy": "timestamp asc",
                "pageSize": self.page_size,
            }
            if page_token:
                body["pageToken"] = page_token
            calls += 1
            r = await self._client.post(
                LOGGING_URL, json=body, headers={"Authorization": f"Bearer {await self.tokens.token()}"}
            )
            if r.status_code != 200:
                raise HttpError(r.status_code, records.http_error_body(r.text))
            data = r.json()
            entries.extend(data.get("entries") or [])
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return LogPage(entries=entries, calls=calls, truncated=bool(page_token))
