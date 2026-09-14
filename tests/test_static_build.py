import asyncio
import json
from pathlib import Path

from scripts.build_static import build
from tv_aggregator.database import Database
from tv_aggregator.fetcher import FetchResult
from tv_aggregator.service import SyncService


def test_static_builder_merges_and_writes_pages_files(tmp_path: Path):
    catalog = tmp_path / "sources.json"
    catalog.write_text(
        json.dumps(
            [
                {"name": "配置一", "url": "https://one.example/config.json"},
                {"name": "配置二", "url": "https://two.example/config.json"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    db = Database(tmp_path / "data.sqlite3")

    async def fake_fetch(source):
        name = "源一" if "one.example" in source["url"] else "源二"
        return FetchResult(text=json.dumps({"sites": [{"key": name, "name": name, "api": "https://provider.example/api/"}]}))

    service = SyncService(db, fetcher=fake_fetch, scheduler_enabled=False)
    status = asyncio.run(build(catalog_path=catalog, db_path=tmp_path / "unused.sqlite3", output_dir=tmp_path / "dist", service=service))

    generated = json.loads((tmp_path / "dist/config.json").read_text(encoding="utf-8"))
    assert status["providerCount"] == 1
    assert generated["sites"][0]["sourceCount"] == 2
    assert (tmp_path / "dist/index.html").exists()
    assert (tmp_path / "dist/.nojekyll").exists()
