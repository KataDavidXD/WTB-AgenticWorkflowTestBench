from __future__ import annotations

import re
from pathlib import Path

from src.services.uv_executor import UVResult


_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+")


def _pkg_name(spec: str) -> str:
    spec = spec.strip()
    match = _NAME_RE.match(spec)
    return match.group(0) if match else spec


class FakeUVCommandExecutor:
    def __init__(self, envs_base_path: Path, uv_cache_dir: Path) -> None:
        self.envs_base_path = envs_base_path
        self.uv_cache_dir = uv_cache_dir

    def _project_path(self, node_id: str) -> Path:
        return self.envs_base_path / node_id

    def _pyproject_path(self, node_id: str) -> Path:
        return self._project_path(node_id) / "pyproject.toml"

    def _uv_lock_path(self, node_id: str) -> Path:
        return self._project_path(node_id) / "uv.lock"

    def _read_deps(self, node_id: str) -> list[str]:
        path = self._pyproject_path(node_id)
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
        lines = [ln.strip() for ln in text.splitlines()]
        deps: list[str] = []
        in_deps = False
        for ln in lines:
            if ln.startswith("dependencies"):
                in_deps = True
                continue
            if in_deps and ln.startswith("]"):
                break
            if in_deps and ln.startswith('"') and ln.endswith('",'):
                deps.append(ln.strip('",'))
        return deps

    def _write_pyproject(self, node_id: str, deps: list[str]) -> None:
        self._project_path(node_id).mkdir(parents=True, exist_ok=True)
        deps_lines = "\n".join(f'    "{d}",' for d in deps)
        content = (
            "[project]\n"
            f'name = "{node_id}"\n'
            'version = "0.1.0"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = [\n"
            f"{deps_lines}\n"
            "]\n"
        )
        self._pyproject_path(node_id).write_text(content, encoding="utf-8")

    def _write_uv_lock(self, node_id: str, deps: list[str]) -> None:
        packages = "\n".join(
            f'[[package]]\nname = "{_pkg_name(d)}"\nversion = "0.0.0"\n' for d in deps
        )
        self._uv_lock_path(node_id).write_text(packages + "\n", encoding="utf-8")

    async def init_project(self, node_id: str, python_version: str) -> UVResult:
        self._write_pyproject(node_id, [])
        self._write_uv_lock(node_id, [])
        return UVResult(stdout="", stderr="", exit_code=0)

    async def add_packages(self, node_id: str, packages: list[str], *, upgrade: bool = False) -> UVResult:
        deps = self._read_deps(node_id)
        for p in packages:
            if p not in deps:
                deps.append(p)
        self._write_pyproject(node_id, deps)
        self._write_uv_lock(node_id, deps)
        return UVResult(stdout="", stderr="", exit_code=0)

    async def remove_packages(self, node_id: str, packages: list[str]) -> UVResult:
        deps = self._read_deps(node_id)
        remove_names = {_pkg_name(p) for p in packages}
        deps = [d for d in deps if _pkg_name(d) not in remove_names]
        self._write_pyproject(node_id, deps)
        self._write_uv_lock(node_id, deps)
        return UVResult(stdout="", stderr="", exit_code=0)

    async def sync_env(self, node_id: str) -> UVResult:
        (self._project_path(node_id) / ".venv").mkdir(parents=True, exist_ok=True)
        return UVResult(stdout="", stderr="", exit_code=0)

    async def run_code(self, node_id: str, code: str, *, timeout_seconds: int) -> UVResult:
        return UVResult(stdout="ok\n", stderr="", exit_code=0)

