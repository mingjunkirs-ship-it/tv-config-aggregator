"""Unified TVBox/影视仓 configuration API.

Run with: ``uvicorn app:app --reload``.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tv_aggregator.catalog import load_catalog, reconcile_catalog, source_id
from tv_aggregator.database import Database
from tv_aggregator.service import SyncService

CATALOG_PATH = Path(__file__).with_name("sources.json")


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2048)
    enabled: bool = True
    interval_minutes: int = Field(default=360, ge=1, le=10080)
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("名称不能为空")
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("只支持 http/https 配置地址")
        return value


class SourceUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    enabled: bool | None = None
    interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    headers: dict[str, str] | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("只支持 http/https 配置地址")
        return value


def _source_view(db: Database, source: dict[str, Any]) -> dict[str, Any]:
    result = dict(source)
    result["siteCount"] = db.count_sites(source["id"])
    result.pop("headers", None)
    return result


def create_app(*, db_path: str | Path | None = None, seed_defaults: bool = True, start_scheduler: bool = True) -> FastAPI:
    configured_path = db_path or os.getenv("TV_AGGREGATOR_DB", "data/tv.sqlite3")
    db = Database(configured_path)
    db.initialize()
    if seed_defaults:
        reconcile_catalog(db, load_catalog(CATALOG_PATH))
    sync_service = SyncService(db, scheduler_enabled=start_scheduler)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await sync_service.start()
        yield
        await sync_service.stop()

    application = FastAPI(title="TV Config Aggregator", version="1.0.0", lifespan=lifespan)
    application.state.db = db
    application.state.sync_service = sync_service
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.strip() for origin in os.getenv("TV_AGGREGATOR_CORS", "*").split(",") if origin.strip()],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.get("/", tags=["system"])
    async def root() -> dict[str, Any]:
        return {"name": "TV Config Aggregator", "version": "1.0.0", "config": "/api/v1/config.json", "docs": "/docs"}

    @application.get("/healthz", tags=["system"])
    async def healthz(request: Request) -> dict[str, Any]:
        sources = request.app.state.db.list_sources()
        return {"status": "ok", "sources": len(sources), "enabledSources": sum(source["enabled"] for source in sources)}

    @application.get("/api/v1/sources", tags=["sources"])
    async def list_sources(request: Request) -> dict[str, Any]:
        source_db = request.app.state.db
        sources = source_db.list_sources()
        return {"items": [_source_view(source_db, source) for source in sources], "total": len(sources)}

    @application.post("/api/v1/sources", status_code=status.HTTP_201_CREATED, tags=["sources"])
    async def add_source(payload: SourceCreate, request: Request) -> dict[str, Any]:
        source_db: Database = request.app.state.db
        if source_db.get_source_by_url(payload.url):
            raise HTTPException(status_code=409, detail="该配置地址已经存在")
        source = source_db.create_source({"id": source_id(payload.url), **payload.model_dump()})
        return _source_view(source_db, source)

    @application.get("/api/v1/sources/{source_id}", tags=["sources"])
    async def get_source(source_id: str, request: Request) -> dict[str, Any]:
        source_db: Database = request.app.state.db
        source = source_db.get_source(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="配置源不存在")
        return _source_view(source_db, source)

    @application.patch("/api/v1/sources/{source_id}", tags=["sources"])
    async def edit_source(source_id: str, payload: SourceUpdate, request: Request) -> dict[str, Any]:
        source_db: Database = request.app.state.db
        if source_db.get_source(source_id) is None:
            raise HTTPException(status_code=404, detail="配置源不存在")
        values = payload.model_dump(exclude_unset=True)
        if "url" in values:
            duplicate = source_db.get_source_by_url(values["url"])
            if duplicate and duplicate["id"] != source_id:
                raise HTTPException(status_code=409, detail="该配置地址已经存在")
        source = source_db.update_source(source_id, values)
        return _source_view(source_db, source)  # type: ignore[arg-type]

    @application.delete("/api/v1/sources/{source_id}", tags=["sources"])
    async def remove_source(source_id: str, request: Request) -> dict[str, Any]:
        if not request.app.state.db.delete_source(source_id):
            raise HTTPException(status_code=404, detail="配置源不存在")
        return {"deleted": True, "sourceId": source_id}

    @application.post("/api/v1/sources/{source_id}/sync", tags=["sync"])
    async def sync_one(source_id: str, request: Request, force: bool = Query(default=False)) -> dict[str, Any]:
        if request.app.state.db.get_source(source_id) is None:
            raise HTTPException(status_code=404, detail="配置源不存在")
        result = await request.app.state.sync_service.sync_source(source_id, force=force)
        return result

    @application.post("/api/v1/sync", tags=["sync"])
    async def sync_all(request: Request) -> dict[str, Any]:
        results = await request.app.state.sync_service.sync_due()
        return {"items": results, "total": len(results)}

    @application.get("/api/v1/providers", tags=["catalog"])
    async def providers(
        request: Request,
        q: str = Query(default="", max_length=200),
        group: str = Query(default="", max_length=80),
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        config = request.app.state.sync_service.merged_config(keyword=q, group=group)
        return {"items": config["sites"][offset : offset + limit], "total": len(config["sites"]), "updatedAt": config["updatedAt"]}

    @application.get("/api/v1/config", tags=["catalog"])
    @application.get("/api/v1/config.json", tags=["catalog"])
    async def config(request: Request, q: str = Query(default="", max_length=200), group: str = Query(default="", max_length=80)) -> dict[str, Any]:
        return request.app.state.sync_service.merged_config(keyword=q, group=group)

    return application


app = create_app()
