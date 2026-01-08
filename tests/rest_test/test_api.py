from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from httpx import ASGITransport, AsyncClient

from src.api import create_app
from src.config import Settings
from src.db_service.connection import SessionLocal, engine, init_db
from src.db_service.models import EnvOperation

from fake_uv import FakeUVCommandExecutor


@pytest.fixture(scope="module", autouse=True)
def _require_audit_db() -> None:
    try:
        init_db()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"Audit DB not reachable: {e}")


def _settings(tmp_path: Path) -> Settings:
    data_root = tmp_path / "data"
    envs = data_root / "envs"
    cache = data_root / "uv_cache"
    return Settings(
        data_root=data_root,
        envs_base_path=envs,
        uv_cache_dir=cache,
        default_python="3.11",
        execution_timeout_seconds=30,
        cleanup_idle_hours=72,
    )


@pytest.mark.anyio
async def test_create_env_and_list_export_run_sync(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    executor = FakeUVCommandExecutor(settings.envs_base_path, settings.uv_cache_dir)
    app = create_app(settings, executor=executor)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/envs",
            json={"workflow_id": "wf1", "node_id": "node_1", "python_version": "3.11", "packages": ["requests", "numpy>=1.24.0"]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["node_id"] == "node_1"
        assert body["status"] == "created"
        assert "[project]" in body["pyproject_toml"]

        meta_path = settings.envs_base_path / "wf1_node_1" / "metadata.json"
        assert meta_path.exists()
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        assert metadata["node_id"] == "wf1_node_1"

        resp = await client.get("/envs/wf1/node_1/deps")
        assert resp.status_code == 200
        deps = resp.json()
        assert "requests" in deps["dependencies"]
        assert deps["locked_versions"]["requests"] == "0.0.0"

        resp = await client.get("/envs/wf1/node_1/export")
        assert resp.status_code == 200
        exported = resp.json()
        assert "[project]" in exported["pyproject_toml"]
        assert "uv_lock" in exported

        resp = await client.post("/envs/wf1/node_1/sync", json={})
        assert resp.status_code == 200
        assert (settings.envs_base_path / "wf1_node_1" / ".venv").exists()

        resp = await client.post("/envs/wf1/node_1/run", json={"code": "print('x')", "timeout": 5})
        assert resp.status_code == 200
        out = resp.json()
        assert out["stdout"] == "ok\n"


@pytest.mark.anyio
async def test_update_and_delete_deps(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    executor = FakeUVCommandExecutor(settings.envs_base_path, settings.uv_cache_dir)
    app = create_app(settings, executor=executor)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/envs", json={"workflow_id": "wf2", "node_id": "node_2", "packages": ["requests"]})

        resp = await client.put("/envs/wf2/node_2/deps", json={"packages": ["pandas>=2.0.0"]})
        assert resp.status_code == 200

        resp = await client.get("/envs/wf2/node_2/deps")
        assert "pandas>=2.0.0" in resp.json()["dependencies"]

        resp = await client.request("DELETE", "/envs/wf2/node_2/deps", json={"packages": ["pandas"]})
        assert resp.status_code == 200

        resp = await client.get("/envs/wf2/node_2/deps")
        assert all("pandas" not in d for d in resp.json()["dependencies"])


@pytest.mark.anyio
async def test_env_not_found_error(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    executor = FakeUVCommandExecutor(settings.envs_base_path, settings.uv_cache_dir)
    app = create_app(settings, executor=executor)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/envs/wf3/missing/deps")
        assert resp.status_code == 404
        assert resp.json()["code"] == "ENV_NOT_FOUND"


@pytest.mark.anyio
async def test_invalid_packages_validation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    executor = FakeUVCommandExecutor(settings.envs_base_path, settings.uv_cache_dir)
    app = create_app(settings, executor=executor)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/envs", json={"workflow_id": "wf3", "node_id": "node_3", "packages": [""]})
        assert resp.status_code == 400
        assert resp.json()["code"] == "INVALID_PACKAGES"


@pytest.mark.anyio
async def test_cleanup_deletes_idle_envs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    executor = FakeUVCommandExecutor(settings.envs_base_path, settings.uv_cache_dir)
    app = create_app(settings, executor=executor)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/envs", json={"workflow_id": "wf4", "node_id": "node_4"})

        meta_path = settings.envs_base_path / "wf4_node_4" / "metadata.json"
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        metadata["last_used_at"] = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

        resp = await client.post("/envs/cleanup", json={"idle_hours": 1})
        assert resp.status_code == 200
        body = resp.json()
        assert "wf4_node_4" in body["deleted"]
        assert not (settings.envs_base_path / "wf4_node_4").exists()


@pytest.mark.anyio
async def test_audit_record_written_on_create_env(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    executor = FakeUVCommandExecutor(settings.envs_base_path, settings.uv_cache_dir)
    app = create_app(settings, executor=executor)

    workflow_id = f"wf-audit-{uuid.uuid4().hex[:8]}"
    node_id = f"node-audit-{uuid.uuid4().hex[:8]}"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/envs",
            json={"workflow_id": workflow_id, "node_id": node_id, "packages": ["requests"]},
        )
        assert resp.status_code == 200, resp.text

    db = SessionLocal()
    try:
        rec = (
            db.query(EnvOperation)
            .filter(
                EnvOperation.workflow_id == workflow_id,
                EnvOperation.node_id == node_id,
                EnvOperation.operation == "create_env",
                EnvOperation.status == "success",
            )
            .order_by(EnvOperation.created_at.desc())
            .first()
        )
        assert rec is not None
    finally:
        db.query(EnvOperation).filter(
            EnvOperation.workflow_id == workflow_id,
            EnvOperation.node_id == node_id,
        ).delete()
        db.commit()
        db.close()
