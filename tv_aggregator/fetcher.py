"""HTTP fetching with conditional requests and bounded response sizes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class FetchResult:
    text: str = ""
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


class SourceFetcher:
    def __init__(self, timeout: float = 20.0, max_bytes: int = 8 * 1024 * 1024):
        self.timeout = timeout
        self.max_bytes = max_bytes

    async def fetch(self, source: dict[str, Any]) -> FetchResult:
        headers = {"User-Agent": "TVConfigAggregator/1.0", **(source.get("headers") or {})}
        if source.get("etag"):
            headers["If-None-Match"] = source["etag"]
        if source.get("last_modified"):
            headers["If-Modified-Since"] = source["last_modified"]
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(source["url"], headers=headers)
        if response.status_code == 304:
            return FetchResult(not_modified=True, etag=source.get("etag"), last_modified=source.get("last_modified"))
        response.raise_for_status()
        content = response.content
        if len(content) > self.max_bytes:
            raise ValueError(f"响应超过 {self.max_bytes // (1024 * 1024)} MB 限制")
        return FetchResult(
            text=content.decode(response.encoding or "utf-8", errors="replace"),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )
