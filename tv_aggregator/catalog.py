"""Declarative source catalog shared by the API and GitHub Actions builder."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .database import Database


def source_id(url: str) -> str:
    return "src_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def load_catalog(path: str | Path) -> list[dict[str, Any]]:
    catalog_path = Path(path)
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("sources.json 顶层必须是数组")
    result: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"sources.json 第 {index} 项必须是对象")
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        parsed = urlsplit(url)
        if not name:
            raise ValueError(f"sources.json 第 {index} 项缺少 name")
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"sources.json 第 {index} 项不是有效的 http/https 地址")
        if url in seen_urls:
            raise ValueError(f"sources.json 中地址重复: {url}")
        seen_urls.add(url)
        headers = item.get("headers") or {}
        if not isinstance(headers, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in headers.items()):
            raise ValueError(f"sources.json 第 {index} 项 headers 必须是字符串键值对象")
        interval = int(item.get("interval_minutes", 360))
        if interval < 1 or interval > 10080:
            raise ValueError(f"sources.json 第 {index} 项 interval_minutes 超出 1..10080")
        result.append(
            {
                "id": source_id(url),
                "name": name,
                "url": url,
                "enabled": bool(item.get("enabled", True)),
                "interval_minutes": interval,
                "headers": headers,
            }
        )
    return result


def reconcile_catalog(db: Database, catalog: list[dict[str, Any]], *, prune: bool = False) -> None:
    """Create/update catalog entries and optionally remove entries no longer declared."""

    desired_ids = {item["id"] for item in catalog}
    for item in catalog:
        existing = db.get_source(item["id"]) or db.get_source_by_url(item["url"])
        if existing is None:
            db.create_source(item)
            continue
        db.update_source(
            existing["id"],
            {
                "name": item["name"],
                "url": item["url"],
                "enabled": item["enabled"],
                "interval_minutes": item["interval_minutes"],
                "headers": item["headers"],
            },
        )
    if prune:
        for existing in db.list_sources():
            if existing["id"] not in desired_ids:
                db.delete_source(existing["id"])
