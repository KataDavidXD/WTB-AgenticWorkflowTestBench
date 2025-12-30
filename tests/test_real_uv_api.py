import threading
import json
import uuid
import socket
import time
from pathlib import Path

import pytest
import httpx
import uvicorn
from httpx import AsyncClient

from src.api import create_app
from src.config import Settings


def _settings(tmp_path: Path) -> Settings:
    data_root = tmp_path / "data"
    envs = data_root / "envs"
    cache = data_root / "uv_cache"
    return Settings(
        data_root=data_root,
        envs_base_path=envs,
        uv_cache_dir=cache,
        default_python="3.11",
        execution_timeout_seconds=60,
        cleanup_idle_hours=72,
    )


class ServerThread(threading.Thread):
    def __init__(self, app, host, port):
        super().__init__(daemon=True)
        # 显式禁用 websockets 以避免第三方库 (uvicorn/websockets) 的弃用警告
        self.server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="error", ws="none")
        )

    def run(self):
        self.server.run()

    def stop(self):
        self.server.should_exit = True


def get_free_port():
    """Get a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture
def server_url(tmp_path: Path):
    """Start a real uvicorn server in a thread and yield the base URL."""
    settings = _settings(tmp_path)
    app = create_app(settings)
    host = "127.0.0.1"
    port = get_free_port()
    url = f"http://{host}:{port}"

    server = ServerThread(app, host, port)
    server.start()

    # Wait for the server to be ready
    max_retries = 50
    ready = False
    for _ in range(max_retries):
        try:
            with socket.create_connection((host, port), timeout=0.1):
                ready = True
                break
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)

    if not ready:
        server.stop()
        pytest.fail("Server failed to start")

    yield url

    # Shutdown the server
    server.stop()
    server.join(timeout=5)


@pytest.mark.anyio
async def test_full_api_flow_with_real_uv(server_url: str, tmp_path: Path) -> None:
    workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
    node_id = f"node-{uuid.uuid4().hex[:8]}"
    
    async with AsyncClient(base_url=server_url, timeout=60.0) as client:
        # 1. Create Environment
        resp = await client.post(
            "/envs",
            json={
                "workflow_id": workflow_id,
                "node_id": node_id,
                "python_version": "3.11",
                "packages": ["toml"],
            },
        )
        assert resp.status_code == 200, resp.text
        created = resp.json()
        assert created["workflow_id"] == workflow_id
        assert created["node_id"] == node_id
        assert created["status"] == "created"
        assert "pyproject_toml" in created

        # 2. Get Status
        resp = await client.get(f"/envs/{node_id}?workflow_id={workflow_id}")
        assert resp.status_code == 200, resp.text
        status = resp.json()
        assert status["workflow_id"] == workflow_id
        assert status["node_id"] == node_id
        assert status["status"] == "ACTIVE"
        assert status["has_pyproject"] is True
        assert status["has_uv_lock"] is True
        assert status["has_venv"] is True

        # 3. List Dependencies
        resp = await client.get(f"/envs/{node_id}/deps?workflow_id={workflow_id}")
        assert resp.status_code == 200, resp.text
        deps = resp.json()
        assert deps["node_id"] == node_id
        assert any("toml" in d for d in deps["dependencies"])

        # 4. Export
        resp = await client.get(f"/envs/{node_id}/export?workflow_id={workflow_id}")
        assert resp.status_code == 200, resp.text
        exported = resp.json()
        assert exported["node_id"] == node_id
        assert "toml" in exported["pyproject_toml"]

        # 5. Run Code
        resp = await client.post(
            f"/envs/{node_id}/run",
            json={
                "workflow_id": workflow_id,
                "code": "import toml; print('toml version:', toml.__version__)",
            },
        )
        assert resp.status_code == 200, resp.text
        run_data = resp.json()
        assert run_data["node_id"] == node_id
        assert run_data["exit_code"] == 0
        assert "toml version:" in run_data["stdout"]

        # 6. Add Deps via requirements file
        req_file = tmp_path / "extra_reqs.txt"
        req_file.write_text("pyyaml\n", encoding="utf-8")
        
        resp = await client.post(
            f"/envs/{node_id}/deps",
            json={
                "workflow_id": workflow_id,
                "requirements_file": str(req_file),
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "added"

        # Verify added
        resp = await client.get(f"/envs/{node_id}/deps?workflow_id={workflow_id}")
        deps = resp.json()
        assert any("pyyaml" in d for d in deps["dependencies"])

        # 7. Update Deps
        resp = await client.put(
            f"/envs/{node_id}/deps",
            json={"workflow_id": workflow_id, "packages": ["pyyaml"]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "updated"

        # 8. Delete Deps
        resp = await client.request(
            "DELETE",
            f"/envs/{node_id}/deps",
            json={"workflow_id": workflow_id, "packages": ["pyyaml"]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "removed"

        # 9. Sync
        resp = await client.post(f"/envs/{node_id}/sync?workflow_id={workflow_id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "synced"

        # 10. Cleanup
        # To ensure the env is actually deleted, we manually backdate its metadata
        settings = _settings(tmp_path)
        env_id = f"{workflow_id}_{node_id}"
        meta_path = settings.envs_base_path / env_id / "metadata.json"
        metadata = json.loads(meta_path.read_text())
        from datetime import datetime, timedelta, timezone
        metadata["last_used_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        meta_path.write_text(json.dumps(metadata))

        resp = await client.post("/envs/cleanup", json={"idle_hours": 1})
        assert resp.status_code == 200, resp.text
        cleanup = resp.json()
        assert env_id in cleanup["deleted"]

        # 11. Final status check (should be NOT_EXISTS)
        resp = await client.get(f"/envs/{node_id}?workflow_id={workflow_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "NOT_EXISTS"


@pytest.mark.anyio
async def test_concurrent_creation_simulation_real_uv(server_url: str, tmp_path: Path) -> None:
    workflow_id = f"wf-concurrent"
    node_id = f"node-concurrent"
    env_id = f"{workflow_id}_{node_id}"
    
    # Pre-create directory to simulate race condition
    settings = _settings(tmp_path)
    env_path = settings.envs_base_path / env_id
    env_path.mkdir(parents=True)
    
    # Initialize a valid pyproject.toml manually
    (env_path / "pyproject.toml").write_text(
        """[project]
name = "test-project"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []
""", 
        encoding="utf-8"
    )

    async with AsyncClient(base_url=server_url, timeout=60.0) as client:
        resp = await client.post(
            "/envs",
            json={
                "workflow_id": workflow_id,
                "node_id": node_id,
                "packages": ["toml"],
            },
        )
        assert resp.status_code == 200, resp.text
        
        pyproject = (env_path / "pyproject.toml").read_text(encoding="utf-8")
        assert "toml" in pyproject
