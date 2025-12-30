from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.exceptions import EnvAlreadyExists, EnvNotFound, ExecutionTimeout
from src.services.project_info import ProjectInfo
from src.services.uv_executor import UVCommandExecutor, UVResult


@dataclass(frozen=True, slots=True)
class EnvStatus:
    status: str
    env_path: Path
    has_pyproject: bool
    has_uv_lock: bool
    has_venv: bool
    metadata: dict[str, Any] | None


class EnvManager:
    """Environmental lifecycle management: creation, deletion, query, export, execution, cleanup."""

    def __init__(
        self,
        envs_base_path: Path,
        executor: UVCommandExecutor,
        *,
        default_python: str,
        execution_timeout_seconds: int,
    ) -> None:
        self.envs_base_path = envs_base_path
        self.executor = executor
        self.default_python = default_python
        self.execution_timeout_seconds = execution_timeout_seconds

    def _project_path(self, node_id: str) -> Path:
        """Get the root directory of an environment."""
        return self.envs_base_path / node_id

    def _metadata_path(self, node_id: str) -> Path:
        """Get the path to the metadata.json file."""
        return self._project_path(node_id) / "metadata.json"

    def _read_metadata(self, node_id: str) -> dict[str, Any] | None:
        """Read environment metadata from disk."""
        path = self._metadata_path(node_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_metadata(self, node_id: str, metadata: dict[str, Any]) -> None:
        """Write environment metadata to disk."""
        path = self._metadata_path(node_id)
        path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    def _touch_last_used(self, node_id: str) -> None:
        """Update the last used timestamp in metadata."""
        metadata = self._read_metadata(node_id) or {}
        metadata["node_id"] = node_id
        metadata["last_used_at"] = datetime.now(timezone.utc).isoformat()
        if "created_at" not in metadata:
            metadata["created_at"] = metadata["last_used_at"]
        self._write_metadata(node_id, metadata)

    def touch(self, node_id: str) -> None:
        """Public method to update activity timestamp."""
        project_path = self._project_path(node_id)
        if not project_path.exists():
            raise EnvNotFound()
        self._touch_last_used(node_id)

    async def create_env(
        self,
        node_id: str,
        *,
        python_version: str | None,
        packages: list[str],
    ) -> tuple[Path, str]:
        """Create a new UV project and optionally install initial packages."""
        project_path = self._project_path(node_id)
        if project_path.exists():
            raise EnvAlreadyExists()

        version = python_version or self.default_python
        await self.executor.init_project(node_id, version)
        self._touch_last_used(node_id)

        if packages:
            mapped_packages = ProjectInfo.get_instance().get_dependency_mapping(packages)
            await self.executor.add_packages(node_id, mapped_packages)
            self._touch_last_used(node_id)

        return project_path, version

    def get_status(self, node_id: str) -> EnvStatus:
        """Check the current status and components of an environment."""
        project_path = self._project_path(node_id)
        if not project_path.exists():
            return EnvStatus(
                status="NOT_EXISTS",
                env_path=project_path,
                has_pyproject=False,
                has_uv_lock=False,
                has_venv=False,
                metadata=None,
            )

        pyproject_path = project_path / "pyproject.toml"
        uv_lock_path = project_path / "uv.lock"
        venv_path = project_path / ".venv"
        metadata = self._read_metadata(node_id)

        return EnvStatus(
            status="ACTIVE",
            env_path=project_path,
            has_pyproject=pyproject_path.exists(),
            has_uv_lock=uv_lock_path.exists(),
            has_venv=venv_path.exists(),
            metadata=metadata,
        )

    def export_env(self, node_id: str) -> tuple[str, str | None]:
        """Export pyproject.toml and uv.lock contents."""
        project_path = self._project_path(node_id)
        if not project_path.exists():
            raise EnvNotFound()
        pyproject_path = project_path / "pyproject.toml"
        if not pyproject_path.exists():
            raise EnvNotFound("pyproject.toml does not exist")
        uv_lock_path = project_path / "uv.lock"

        self._touch_last_used(node_id)
        return (
            pyproject_path.read_text(encoding="utf-8"),
            uv_lock_path.read_text(encoding="utf-8") if uv_lock_path.exists() else None,
        )

    async def sync(self, node_id: str) -> UVResult:
        """Re-sync the environment from uv.lock."""
        project_path = self._project_path(node_id)
        if not project_path.exists():
            raise EnvNotFound()
        result = await self.executor.sync_env(node_id)
        self._touch_last_used(node_id)
        return result

    async def run(self, node_id: str, code: str, *, timeout_seconds: int | None) -> UVResult:
        """Execute Python code within the environment."""
        project_path = self._project_path(node_id)
        if not project_path.exists():
            raise EnvNotFound()

        timeout = timeout_seconds or self.execution_timeout_seconds
        try:
            result = await self.executor.run_code(node_id, code, timeout_seconds=timeout)
        except TimeoutError as e:
            raise ExecutionTimeout() from e

        self._touch_last_used(node_id)
        return result

    def delete_env(self, node_id: str) -> None:
        """Remove the entire environment directory."""
        project_path = self._project_path(node_id)
        if not project_path.exists():
            raise EnvNotFound()
        shutil.rmtree(project_path, ignore_errors=False)

    def cleanup(self, *, idle_hours: int) -> list[str]:
        """Remove environments that haven't been used for a specified duration."""
        deleted: list[str] = []
        if not self.envs_base_path.exists():
            return deleted

        # Calculate the cutoff time for idle environments
        threshold = datetime.now(timezone.utc) - timedelta(hours=idle_hours)
        
        # Iterate through all environment directories
        for child in self.envs_base_path.iterdir():
            if not child.is_dir():
                continue
                
            node_id = child.name
            meta_path = child / "metadata.json"
            if not meta_path.exists():
                continue
                
            try:
                # Load metadata and parse the last used timestamp
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                last_used = datetime.fromisoformat(metadata.get("last_used_at"))
            except Exception:
                # Skip if metadata is corrupted or timestamp is missing
                continue
                
            # Ensure timezone awareness for comparison
            if last_used.tzinfo is None:
                last_used = last_used.replace(tzinfo=timezone.utc)
                
            # Delete the environment if it has been idle longer than the threshold
            if last_used < threshold:
                shutil.rmtree(child, ignore_errors=True)
                deleted.append(node_id)

        return deleted
