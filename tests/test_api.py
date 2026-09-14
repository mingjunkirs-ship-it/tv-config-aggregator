from pathlib import Path

from fastapi.testclient import TestClient

from app import create_app


def test_source_lifecycle_and_config_output(tmp_path: Path):
    client = TestClient(create_app(db_path=tmp_path / "tv.sqlite3", seed_defaults=False, start_scheduler=False))
    created = client.post("/api/v1/sources", json={"name": "demo", "url": "https://example.com/config.json"})
    assert created.status_code == 201
    source_id = created.json()["id"]
    assert client.get("/api/v1/sources").json()["total"] == 1
    assert client.patch(f"/api/v1/sources/{source_id}", json={"enabled": False}).json()["enabled"] is False
    assert client.delete(f"/api/v1/sources/{source_id}").json()["deleted"] is True


def test_defaults_are_seeded_once(tmp_path: Path):
    application = create_app(db_path=tmp_path / "tv.sqlite3", seed_defaults=True, start_scheduler=False)
    client = TestClient(application)
    assert client.get("/api/v1/sources").json()["total"] == 17
    # Recreating the app must not duplicate the default entries.
    application = create_app(db_path=tmp_path / "tv.sqlite3", seed_defaults=True, start_scheduler=False)
    assert TestClient(application).get("/api/v1/sources").json()["total"] == 17
