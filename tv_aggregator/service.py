"""Source synchronization and automatic refresh loop."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from .database import Database
from .fetcher import FetchResult, SourceFetcher
from .normalizer import extract_sites, merge_sites, normalize_site


FetcherCallable = Callable[[dict[str, Any]], Awaitable[FetchResult]]


class SyncService:
    def __init__(self, db: Database, fetcher: SourceFetcher | FetcherCallable | None = None, *, scheduler_enabled: bool = True):
        self.db = db
        self.fetcher = fetcher or SourceFetcher()
        self.scheduler_enabled = scheduler_enabled
        self._locks: dict[str, asyncio.Lock] = {}
        self._scheduler_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def _fetch(self, source: dict[str, Any]) -> FetchResult:
        if callable(self.fetcher):
            return await self.fetcher(source)
        return await self.fetcher.fetch(source)

    async def sync_source(self, source_id: str, *, force: bool = False) -> dict[str, Any]:
        source = self.db.get_source(source_id)
        if source is None:
            raise KeyError(source_id)
        lock = self._locks.setdefault(source_id, asyncio.Lock())
        if lock.locked() and not force:
            return {"sourceId": source_id, "status": "syncing", "message": "该源正在同步"}
        async with lock:
            self.db.mark_syncing(source_id)
            try:
                result = await self._fetch(source)
                if result.not_modified:
                    self.db.mark_success(source_id, etag=result.etag or source.get("etag"), last_modified=result.last_modified or source.get("last_modified"), interval_minutes=source["interval_minutes"])
                    return {"sourceId": source_id, "status": "not_modified", "siteCount": self.db.count_sites(source_id)}
                candidates = extract_sites(result.text)
                normalized: list[dict[str, Any]] = []
                seen: set[str] = set()
                for candidate in candidates:
                    site = normalize_site(candidate, source_id, source["name"], source["url"])
                    if site["fingerprint"] in seen:
                        continue
                    seen.add(site["fingerprint"])
                    site["id"] = "site_" + hashlib.sha256(f"{source_id}:{site['fingerprint']}".encode()).hexdigest()[:24]
                    normalized.append(site)
                if not normalized:
                    raise ValueError("配置中没有找到可用的 sites/source 定义")
                self.db.replace_sites(source_id, normalized)
                self.db.mark_success(source_id, etag=result.etag, last_modified=result.last_modified, interval_minutes=source["interval_minutes"])
                return {"sourceId": source_id, "status": "ok", "siteCount": len(normalized)}
            except Exception as error:  # keep the last good snapshot when a feed is unavailable
                self.db.mark_failure(source_id, str(error), interval_minutes=source["interval_minutes"])
                return {"sourceId": source_id, "status": "error", "error": str(error), "siteCount": self.db.count_sites(source_id)}

    async def sync_due(self) -> list[dict[str, Any]]:
        due = self.db.due_sources()
        if not due:
            return []
        semaphore = asyncio.Semaphore(4)

        async def run(source: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                return await self.sync_source(source["id"])

        return list(await asyncio.gather(*(run(source) for source in due)))

    def merged_config(self, *, keyword: str = "", group: str = "") -> dict[str, Any]:
        merged = merge_sites(self.db.list_sites())
        keyword = keyword.strip().lower()
        group = group.strip().lower()
        if keyword:
            merged = [item for item in merged if keyword in str(item.get("name", "")).lower() or keyword in str(item.get("api", "")).lower() or any(keyword in alias.lower() for alias in item.get("aliases", []))]
        if group:
            merged = [item for item in merged if group in {str(value).lower() for value in item.get("groups", [])}]
        sources = self.db.list_sources()
        return {
            "version": 1,
            "updatedAt": max((source.get("updated_at") or "" for source in sources), default=None),
            "sites": merged,
            "meta": {
                "sourceCount": len([source for source in sources if source["enabled"]]),
                "providerCount": len(merged),
                "sources": [
                    {"id": source["id"], "name": source["name"], "url": source["url"], "status": source["status"], "lastSyncedAt": source["last_synced_at"], "lastError": source["last_error"]}
                    for source in sources
                ],
            },
        }

    async def scheduler(self) -> None:
        if not self.scheduler_enabled:
            return
        while not self._stop_event.is_set():
            try:
                await self.sync_due()
            except Exception:
                # A single malformed source must never stop future refreshes.
                pass
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                continue

    async def start(self) -> None:
        if self.scheduler_enabled and self._scheduler_task is None:
            self._stop_event.clear()
            self._scheduler_task = asyncio.create_task(self.scheduler())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._scheduler_task:
            await self._scheduler_task
            self._scheduler_task = None
