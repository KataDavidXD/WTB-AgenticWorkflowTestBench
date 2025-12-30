from __future__ import annotations

import os
import uuid

import httpx
import pytest


@pytest.fixture(scope="module")
def base_url() -> str:
    return os.getenv("INTEGRATION_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


@pytest.fixture(scope="module")
def client(base_url: str) -> httpx.Client:
    timeout = httpx.Timeout(connect=5.0, read=300.0, write=300.0, pool=5.0)
    with httpx.Client(base_url=base_url, timeout=timeout) as c:
        try:
            resp = c.get("/openapi.json")
        except Exception as e:
            pytest.skip(f"API not reachable at {base_url}: {e}")
        if resp.status_code >= 400:
            pytest.skip(f"API not reachable at {base_url}, status={resp.status_code}")
        yield c


@pytest.fixture(scope="module")
def node_id() -> str:
    return f"integration-{uuid.uuid4().hex[:12]}"


def test_full_api_flow(client: httpx.Client, node_id: str) -> None:
    try:
        resp = client.post("/envs", json={"node_id": node_id, "packages": ["requests"]})
        assert resp.status_code == 200, resp.text
        created = resp.json()
        assert created["node_id"] == node_id
        assert created["status"] == "created"
        assert "pyproject_toml" in created

        resp = client.get(f"/envs/{node_id}")
        assert resp.status_code == 200, resp.text
        status = resp.json()
        assert status["node_id"] == node_id
        assert status["status"] == "ACTIVE"
        assert status["has_pyproject"] is True
        assert status["has_uv_lock"] is True
        assert status["has_venv"] is True

        resp = client.get(f"/envs/{node_id}/deps")
        assert resp.status_code == 200, resp.text
        deps = resp.json()
        assert deps["node_id"] == node_id
        assert any("requests" in d for d in deps["dependencies"])

        resp = client.get(f"/envs/{node_id}/export")
        assert resp.status_code == 200, resp.text
        exported = resp.json()
        assert exported["node_id"] == node_id
        assert "requests" in exported["pyproject_toml"]

        resp = client.post(
            f"/envs/{node_id}/run",
            json={"code": "import requests; print('Requests version:', requests.__version__)"},
        )
        assert resp.status_code == 200, resp.text
        run_data = resp.json()
        assert run_data["node_id"] == node_id
        assert run_data["exit_code"] == 0
        assert "Requests version:" in run_data["stdout"]

        resp = client.post(f"/envs/{node_id}/deps", json={"packages": ["rich"]})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "added"

        resp = client.get(f"/envs/{node_id}/deps")
        assert resp.status_code == 200, resp.text
        deps = resp.json()
        assert any("rich" in d for d in deps["dependencies"])

        resp = client.put(f"/envs/{node_id}/deps", json={"packages": ["rich"]})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "updated"

        resp = client.request("DELETE", f"/envs/{node_id}/deps", json={"packages": ["rich"]})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "removed"

        resp = client.post(f"/envs/{node_id}/sync")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "synced"

        resp = client.post("/envs/cleanup", json={"idle_hours": 999999})
        assert resp.status_code == 200, resp.text
        cleanup = resp.json()
        assert isinstance(cleanup["deleted"], list)
        assert "checked_at" in cleanup
    finally:
        client.delete(f"/envs/{node_id}")


def test_dependency_inheritance(client: httpx.Client, node_id: str) -> None:
    try:
        # Create env with a package that IS in the main pyproject.toml
        # 'httpx' is in pyproject.toml as httpx>=0.28.1
        resp = client.post("/envs", json={"node_id": node_id, "packages": ["httpx"]})
        assert resp.status_code == 200, resp.text
        created = resp.json()
        
        # Check if the pyproject.toml content reflects the constraint
        pyproject_content = created["pyproject_toml"]
        assert "httpx>=0.28.1" in pyproject_content or 'httpx = ">=0.28.1"' in pyproject_content
        
    finally:
        client.delete(f"/envs/{node_id}")


def test_deleted_env_behaviour(client: httpx.Client, node_id: str) -> None:
    resp = client.delete(f"/envs/{node_id}")
    assert resp.status_code in (200, 404), resp.text

    resp = client.get(f"/envs/{node_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "NOT_EXISTS"

    resp = client.get(f"/envs/{node_id}/export")
    assert resp.status_code == 404, resp.text
    err = resp.json()
    assert err["code"] == "ENV_NOT_FOUND"
