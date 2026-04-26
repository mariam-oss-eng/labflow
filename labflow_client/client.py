"""Synchronous + async LabFlow API client.

Implementation notes
--------------------
* Pure stdlib by default — uses ``urllib.request``. If ``httpx`` is
  installed it's preferred so users get connection pooling and HTTP/2
  for free without needing to wire it up themselves.
* The async variant requires ``httpx`` (asyncio + ``urllib`` is a
  footgun); we raise a clear ImportError if it isn't installed instead
  of silently falling back to a thread pool.
* The client never raises on non-2xx — it raises a typed
  :class:`LabFlowError` carrying the status code, response body, and
  the original :class:`urllib`/``httpx`` exception.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

log = logging.getLogger("labflow_client")

try:  # pragma: no cover — exercised only when httpx is present
    import httpx  # type: ignore
    _HAS_HTTPX = True
except Exception:  # noqa: BLE001
    httpx = None  # type: ignore
    _HAS_HTTPX = False


class LabFlowError(RuntimeError):
    """Raised when the server returns a non-2xx response."""

    def __init__(self, status: int, body: str, *, url: str = ""):
        super().__init__(f"LabFlow API error {status} on {url}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


def _join(base: str, path: str) -> str:
    if not base.endswith("/"):
        base += "/"
    return urljoin(base, path.lstrip("/"))


# ---------------------------------------------------------------- sync client
class LabFlow:
    """Synchronous LabFlow API client."""

    def __init__(
        self, base_url: str, *, api_key: str | None = None,
        timeout: float = 15.0, user_agent: str = "labflow-client/0.7.0",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.user_agent = user_agent
        self._httpx_client = None
        if _HAS_HTTPX:
            self._httpx_client = httpx.Client(  # type: ignore[union-attr]
                base_url=self.base_url, timeout=timeout,
                headers=self._headers(),
            )

    # ----------------------------------------------------- low-level transport
    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        h = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if content_type:
            h["Content-Type"] = content_type
        return h

    def _request(
        self, method: str, path: str, *,
        params: dict | None = None, json_body: Any = None,
        idempotency_key: str | None = None,
    ) -> Any:
        url = _join(self.base_url, path)
        if params:
            url = f"{url}?{urlencode({k: v for k, v in params.items() if v is not None})}"

        if self._httpx_client is not None:  # pragma: no cover
            headers = {}
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
            try:
                resp = self._httpx_client.request(
                    method, path, params=None,
                    json=json_body, headers=headers,
                )
            except httpx.HTTPError as exc:  # type: ignore[union-attr]
                raise LabFlowError(0, str(exc), url=url) from exc
            if resp.status_code >= 400:
                raise LabFlowError(resp.status_code, resp.text, url=url)
            if not resp.content:
                return None
            ctype = resp.headers.get("content-type", "")
            return resp.json() if "json" in ctype else resp.text

        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        headers = self._headers(content_type="application/json" if data else None)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        req = Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                if not body:
                    return None
                ctype = resp.headers.get("Content-Type", "")
                return json.loads(body) if "json" in ctype else body
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
            raise LabFlowError(exc.code, body, url=url) from exc
        except URLError as exc:
            raise LabFlowError(0, str(exc), url=url) from exc

    # ------------------------------------------------------------- public API
    def health(self) -> dict:
        return self._request("GET", "/healthz")

    def whoami(self) -> dict:
        return self._request("GET", "/api/me")

    # meetings -----------------------------------------------------------
    def create_meeting(
        self, *, title: str, transcript: str = "", notes: str = "",
        meeting_type: str = "standup", idempotency_key: str | None = None,
    ) -> dict:
        return self._request(
            "POST", "/api/meetings",
            json_body={"title": title, "transcript": transcript,
                       "notes": notes, "meeting_type": meeting_type},
            idempotency_key=idempotency_key,
        )

    def list_meetings(self, *, limit: int | None = None, offset: int = 0) -> dict:
        return self._request("GET", "/api/meetings",
                             params={"limit": limit, "offset": offset})

    def get_meeting(self, meeting_id: int) -> dict:
        return self._request("GET", f"/api/meetings/{meeting_id}/export.json")

    def finalize_meeting(self, meeting_id: int) -> dict:
        return self._request("POST", f"/api/meetings/{meeting_id}/finalize")

    def summarize_meeting(self, meeting_id: int, *, max_sentences: int = 5) -> dict:
        return self._request(
            "GET", f"/api/meetings/{meeting_id}/summary",
            params={"max_sentences": max_sentences},
        )

    # tasks --------------------------------------------------------------
    def list_tasks(
        self, *, status: str | None = None, owner: str | None = None,
        limit: int | None = None, offset: int = 0,
    ) -> dict:
        return self._request(
            "GET", "/api/tasks",
            params={"status": status, "owner": owner,
                    "limit": limit, "offset": offset},
        )

    def update_task(self, task_id: int, **fields) -> dict:
        return self._request("PATCH", f"/api/tasks/{task_id}", json_body=fields)

    def add_evidence(
        self, *, task_id: int, kind: str, uri: str, summary: str = "",
    ) -> dict:
        return self._request(
            "POST", "/api/evidence",
            json_body={"task_id": task_id, "kind": kind, "uri": uri,
                       "summary": summary},
        )

    # search / graph / analytics ----------------------------------------
    def search(self, query: str, *, limit: int = 25, alpha: float | None = None) -> dict:
        return self._request("GET", "/api/search",
                             params={"q": query, "limit": limit, "alpha": alpha})

    def decision_graph(self) -> dict:
        return self._request("GET", "/api/graph/decisions")

    def analytics(self, *, days: int = 30) -> dict:
        return self._request("GET", "/api/analytics", params={"days": days})

    # collab ------------------------------------------------------------
    def comments(self, *, entity_type: str, entity_id: int) -> list[dict]:
        out = self._request(
            "GET", "/api/comments",
            params={"entity_type": entity_type, "entity_id": entity_id},
        )
        return out.get("comments", []) if isinstance(out, dict) else out or []

    def add_comment(
        self, *, entity_type: str, entity_id: int, body: str,
        parent_id: int | None = None,
    ) -> dict:
        return self._request(
            "POST", "/api/comments",
            json_body={"entity_type": entity_type, "entity_id": entity_id,
                       "body": body, "parent_id": parent_id},
        )

    def react(self, *, entity_type: str, entity_id: int, emoji: str) -> dict:
        return self._request(
            "POST", "/api/reactions",
            json_body={"entity_type": entity_type, "entity_id": entity_id,
                       "emoji": emoji},
        )

    # graphql -----------------------------------------------------------
    def graphql(self, query: str) -> dict:
        return self._request("POST", "/graphql", json_body={"query": query})

    # context manager support ------------------------------------------
    def close(self) -> None:
        if self._httpx_client is not None:  # pragma: no cover
            self._httpx_client.close()

    def __enter__(self) -> "LabFlow":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


# --------------------------------------------------------------- async client
class AsyncLabFlow:
    """Asyncio LabFlow client. Requires ``httpx``."""

    def __init__(
        self, base_url: str, *, api_key: str | None = None,
        timeout: float = 15.0, user_agent: str = "labflow-client/0.7.0",
    ) -> None:
        if not _HAS_HTTPX:  # pragma: no cover
            raise ImportError(
                "AsyncLabFlow requires httpx — install with `pip install httpx`"
            )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        h = {"User-Agent": user_agent, "Accept": "application/json"}
        if api_key:
            h["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(  # type: ignore[union-attr]
            base_url=self.base_url, timeout=timeout, headers=h,
        )

    async def _request(self, method: str, path: str, *, params=None, json_body=None) -> Any:
        try:
            resp = await self._client.request(method, path, params=params, json=json_body)
        except httpx.HTTPError as exc:  # type: ignore[union-attr]
            raise LabFlowError(0, str(exc), url=path) from exc
        if resp.status_code >= 400:
            raise LabFlowError(resp.status_code, resp.text, url=path)
        return resp.json() if "json" in resp.headers.get("content-type", "") else resp.text

    async def health(self) -> dict:
        return await self._request("GET", "/healthz")

    async def search(self, q: str, *, limit: int = 25) -> dict:
        return await self._request("GET", "/api/search", params={"q": q, "limit": limit})

    async def list_tasks(self, **filters) -> dict:
        return await self._request("GET", "/api/tasks", params=filters)

    async def graphql(self, query: str) -> dict:
        return await self._request("POST", "/graphql", json_body={"query": query})

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncLabFlow":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()
