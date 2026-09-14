"""Fetch all configured feeds and generate files suitable for GitHub Pages."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
from pathlib import Path
from typing import Any

from tv_aggregator.catalog import load_catalog, reconcile_catalog
from tv_aggregator.database import Database
from tv_aggregator.service import SyncService


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def render_index(config: dict[str, Any], results: list[dict[str, Any]]) -> str:
    ok_count = sum(result["status"] in {"ok", "not_modified"} for result in results)
    error_count = sum(result["status"] == "error" for result in results)
    updated_at = html.escape(str(config.get("updatedAt") or "尚未更新"))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>影视配置聚合</title>
  <style>body{{max-width:760px;margin:4rem auto;padding:0 1.2rem;font:16px/1.7 system-ui;color:#1f2937}}code,a{{color:#2563eb}}.card{{padding:1.5rem;border:1px solid #e5e7eb;border-radius:14px}}small{{color:#6b7280}}</style>
</head>
<body><div class="card">
  <h1>影视配置聚合</h1>
  <p>已合并 <strong>{int(config['meta']['providerCount'])}</strong> 个影视源。本轮成功 {ok_count} 个配置接口，失败 {error_count} 个。</p>
  <p><a href="config.json">统一配置 config.json</a> · <a href="providers.json">源列表 providers.json</a> · <a href="status.json">更新状态 status.json</a></p>
  <small>最后更新：{updated_at}（页面由 GitHub Actions 自动生成）</small>
</div></body></html>
"""


async def build(
    *,
    catalog_path: Path,
    db_path: Path,
    output_dir: Path,
    service: SyncService | None = None,
    allow_empty: bool = False,
) -> dict[str, Any]:
    db = service.db if service else Database(db_path)
    db.initialize()
    catalog = load_catalog(catalog_path)
    reconcile_catalog(db, catalog, prune=True)
    sync_service = service or SyncService(db, scheduler_enabled=False)

    sources = [source for source in db.list_sources() if source["enabled"]]
    semaphore = asyncio.Semaphore(4)

    async def sync(source: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await sync_service.sync_source(source["id"])

    results = list(await asyncio.gather(*(sync(source) for source in sources)))
    source_by_id = {source["id"]: source for source in sources}
    for result in results:
        source = source_by_id.get(result.get("sourceId"), {})
        result["sourceName"] = source.get("name")
        result["sourceUrl"] = source.get("url")
        result["cachedSiteCount"] = result.get("siteCount", 0)
    config = sync_service.merged_config()
    if not config["sites"] and not allow_empty:
        raise RuntimeError("所有配置接口均不可用，且缓存中没有上一次成功结果；停止发布空配置")

    output_dir.mkdir(parents=True, exist_ok=True)
    public_sources = [
        {
            "id": source["id"],
            "name": source["name"],
            "url": source["url"],
            "enabled": source["enabled"],
            "status": source["status"],
            "siteCount": db.count_sites(source["id"]),
            "lastSyncedAt": source["last_synced_at"],
            "lastError": source["last_error"],
        }
        for source in db.list_sources()
    ]
    status = {
        "updatedAt": config["updatedAt"],
        "providerCount": config["meta"]["providerCount"],
        "sourceCount": len(sources),
        "successfulSources": sum(result["status"] in {"ok", "not_modified"} for result in results),
        "failedSources": sum(result["status"] == "error" for result in results),
        "results": results,
    }
    write_json(output_dir / "config.json", config)
    write_json(output_dir / "providers.json", {"items": config["sites"], "total": len(config["sites"]), "updatedAt": config["updatedAt"]})
    write_json(output_dir / "sources.json", {"items": public_sources, "total": len(public_sources)})
    write_json(output_dir / "status.json", status)
    (output_dir / "index.html").write_text(render_index(config, results), encoding="utf-8")
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    return status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=Path("sources.json"))
    parser.add_argument("--db", type=Path, default=Path("data/tv.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--allow-empty", action="store_true", help="Allow publishing an empty configuration (mainly for testing)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    status = asyncio.run(build(catalog_path=args.catalog, db_path=args.db, output_dir=args.output, allow_empty=args.allow_empty))
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
