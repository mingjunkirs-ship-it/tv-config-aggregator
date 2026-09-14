"""Small SQLite repository used by the aggregator."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    interval_minutes INTEGER NOT NULL DEFAULT 360,
                    headers_json TEXT NOT NULL DEFAULT '{}',
                    etag TEXT,
                    last_modified TEXT,
                    last_synced_at TEXT,
                    next_sync_at TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sites (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                    fingerprint TEXT NOT NULL,
                    site_key TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    api TEXT NOT NULL DEFAULT '',
                    type TEXT,
                    searchable INTEGER NOT NULL DEFAULT 1,
                    quick_search INTEGER NOT NULL DEFAULT 1,
                    filterable INTEGER NOT NULL DEFAULT 0,
                    group_name TEXT NOT NULL DEFAULT '影视',
                    ext_json TEXT,
                    raw_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_id, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_sites_source ON sites(source_id);
                CREATE INDEX IF NOT EXISTS idx_sites_fingerprint ON sites(fingerprint);
                """
            )

    @staticmethod
    def _source(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["headers"] = json.loads(result.pop("headers_json") or "{}")
        return result

    def create_source(self, source: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO sources (id,name,url,enabled,interval_minutes,headers_json,next_sync_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?, ?,?)
                """,
                (
                    source["id"],
                    source["name"],
                    source["url"],
                    int(source.get("enabled", True)),
                    int(source.get("interval_minutes", 360)),
                    json.dumps(source.get("headers", {}), ensure_ascii=False),
                    now,
                    now,
                    now,
                ),
            )
        return self.get_source(source["id"])  # type: ignore[return-value]

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            return self._source(db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone())

    def get_source_by_url(self, url: str) -> dict[str, Any] | None:
        with self.connect() as db:
            return self._source(db.execute("SELECT * FROM sources WHERE url = ?", (url,)).fetchone())

    def list_sources(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM sources ORDER BY created_at, id").fetchall()
        return [self._source(row) for row in rows]  # type: ignore[list-item]

    def update_source(self, source_id: str, values: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"name", "url", "enabled", "interval_minutes", "headers"}
        values = {key: value for key, value in values.items() if key in allowed and value is not None}
        if not values:
            return self.get_source(source_id)
        updates: list[str] = []
        params: list[Any] = []
        for key, value in values.items():
            column = "headers_json" if key == "headers" else key
            if key == "headers":
                value = json.dumps(value, ensure_ascii=False)
            if key == "enabled":
                value = int(value)
            updates.append(f"{column} = ?")
            params.append(value)
        updates.append("updated_at = ?")
        params.extend([utc_now(), source_id])
        with self.connect() as db:
            db.execute(f"UPDATE sources SET {', '.join(updates)} WHERE id = ?", params)
        return self.get_source(source_id)

    def delete_source(self, source_id: str) -> bool:
        with self.connect() as db:
            cursor = db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
            return cursor.rowcount > 0

    def mark_syncing(self, source_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE sources SET status = 'syncing', last_error = NULL, updated_at = ? WHERE id = ?", (utc_now(), source_id))

    def mark_success(self, source_id: str, *, etag: str | None, last_modified: str | None, interval_minutes: int) -> None:
        now = datetime.now(timezone.utc)
        next_sync = (now.timestamp() + interval_minutes * 60)
        next_sync_at = datetime.fromtimestamp(next_sync, timezone.utc).isoformat(timespec="seconds")
        now_text = now.isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute(
                "UPDATE sources SET status = 'ok', etag = ?, last_modified = ?, last_synced_at = ?, next_sync_at = ?, last_error = NULL, updated_at = ? WHERE id = ?",
                (etag, last_modified, now_text, next_sync_at, now_text, source_id),
            )

    def mark_not_modified(self, source_id: str, *, interval_minutes: int) -> None:
        self.mark_success(source_id, etag=None, last_modified=None, interval_minutes=interval_minutes)

    def mark_failure(self, source_id: str, error: str, *, interval_minutes: int) -> None:
        now = datetime.now(timezone.utc)
        retry_minutes = min(max(interval_minutes, 5), 60)
        next_sync_at = datetime.fromtimestamp(now.timestamp() + retry_minutes * 60, timezone.utc).isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute(
                "UPDATE sources SET status = 'error', last_error = ?, next_sync_at = ?, updated_at = ? WHERE id = ?",
                (error[:1000], next_sync_at, now.isoformat(timespec="seconds"), source_id),
            )

    def replace_sites(self, source_id: str, sites: Iterable[dict[str, Any]]) -> int:
        now = utc_now()
        site_list = list(sites)
        with self.connect() as db:
            db.execute("DELETE FROM sites WHERE source_id = ?", (source_id,))
            for site in site_list:
                db.execute(
                    """
                    INSERT INTO sites (id,source_id,fingerprint,site_key,name,api,type,searchable,quick_search,filterable,group_name,ext_json,raw_json,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        site["id"], source_id, site["fingerprint"], site.get("key", ""), site.get("name", "未命名源"),
                        site.get("api", ""), str(site.get("type", 1)), int(bool(site.get("searchable", True))),
                        int(bool(site.get("quickSearch", True))), int(bool(site.get("filterable", False))), site.get("group", "影视"),
                        json.dumps(site.get("ext"), ensure_ascii=False) if site.get("ext") is not None else None,
                        json.dumps(site.get("raw", {}), ensure_ascii=False), now,
                    ),
                )
        return len(site_list)

    def list_sites(self, *, enabled_only: bool = True) -> list[dict[str, Any]]:
        query = "SELECT sites.*, sources.name AS source_name, sources.url AS source_url, sources.enabled AS source_enabled FROM sites JOIN sources ON sources.id = sites.source_id"
        if enabled_only:
            query += " WHERE sources.enabled = 1"
        query += " ORDER BY sites.name COLLATE NOCASE, sites.id"
        with self.connect() as db:
            rows = db.execute(query).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["searchable"] = bool(item["searchable"])
            item["quickSearch"] = bool(item.pop("quick_search"))
            item["filterable"] = bool(item["filterable"])
            item["ext"] = json.loads(item.pop("ext_json")) if item.get("ext_json") else None
            item["raw"] = json.loads(item.pop("raw_json"))
            item["source"] = {"id": item.pop("source_id"), "name": item.pop("source_name"), "url": item.pop("source_url")}
            item.pop("source_enabled", None)
            item.pop("site_key", None)
            result.append(item)
        return result

    def count_sites(self, source_id: str) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM sites WHERE source_id = ?", (source_id,)).fetchone()[0])

    def due_sources(self, now: str | None = None) -> list[dict[str, Any]]:
        now = now or utc_now()
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM sources WHERE enabled = 1 AND (next_sync_at IS NULL OR next_sync_at <= ?) ORDER BY COALESCE(last_synced_at, created_at)",
                (now,),
            ).fetchall()
        return [self._source(row) for row in rows]  # type: ignore[list-item]
