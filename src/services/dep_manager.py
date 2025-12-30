from __future__ import annotations

import tomllib
from pathlib import Path

from src.exceptions import EnvNotFound
from src.services.project_info import ProjectInfo
from src.services.uv_executor import UVCommandExecutor, UVResult


class DependencyManager:
    """Manage environment dependencies using UV."""

    def __init__(self, envs_base_path: Path, executor: UVCommandExecutor) -> None:
        self.envs_base_path = envs_base_path
        self.executor = executor

    def _project_path(self, node_id: str) -> Path:
        """Get project root directory."""
        return self.envs_base_path / node_id

    def _pyproject_path(self, node_id: str) -> Path:
        """Get path to pyproject.toml."""
        return self._project_path(node_id) / "pyproject.toml"

    def _uv_lock_path(self, node_id: str) -> Path:
        """Get path to uv.lock."""
        return self._project_path(node_id) / "uv.lock"

    def list_dependencies(self, node_id: str) -> tuple[list[str], dict[str, str]]:
        """List declared dependencies and their locked versions."""
        project_path = self._project_path(node_id)
        if not project_path.exists():
            raise EnvNotFound()

        deps: list[str] = []
        pyproject_path = self._pyproject_path(node_id)
        if pyproject_path.exists():
            data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
            deps = list(data.get("project", {}).get("dependencies", []) or [])

        locked_versions: dict[str, str] = {}
        uv_lock_path = self._uv_lock_path(node_id)
        if uv_lock_path.exists():
            try:
                lock_data = tomllib.loads(uv_lock_path.read_text(encoding="utf-8"))
                for pkg in lock_data.get("package", []) or []:
                    name = pkg.get("name")
                    version = pkg.get("version")
                    if isinstance(name, str) and isinstance(version, str):
                        locked_versions[name] = version
            except Exception:
                locked_versions = {}

        return deps, locked_versions

    async def add(self, node_id: str, packages: list[str]) -> UVResult:
        """Add new packages to the environment."""
        mapped_packages = ProjectInfo.get_instance().get_dependency_mapping(packages)
        return await self.executor.add_packages(node_id, mapped_packages)

    async def update(self, node_id: str, packages: list[str]) -> UVResult:
        """Upgrade existing packages."""
        return await self.executor.add_packages(node_id, packages, upgrade=True)

    async def remove(self, node_id: str, packages: list[str]) -> UVResult:
        """Remove packages from the environment."""
        return await self.executor.remove_packages(node_id, packages)

