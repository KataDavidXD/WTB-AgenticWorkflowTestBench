from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from src.exceptions import PackageResolutionFailed, UVExecutionError


@dataclass(frozen=True, slots=True)
class UVResult:
    stdout: str
    stderr: str
    exit_code: int


class UVCommandExecutor:
    """Cross-platform UV CLI wrapper, uniformly using uv subcommands."""

    def __init__(self, envs_base_path: Path, uv_cache_dir: Path) -> None:
        self.envs_base_path = envs_base_path
        self.uv_cache_dir = uv_cache_dir
        self.is_windows = sys.platform == "win32"
        self._logger = logging.getLogger(__name__)

    def _get_project_path(self, node_id: str) -> Path:
        """Construct the absolute path to the project directory."""
        return self.envs_base_path / node_id

    async def _run(self, cmd: list[str], timeout_seconds: int | None = None) -> UVResult:
        """Execute a UV command as a subprocess with isolation and timeout."""
        # Inject UV_CACHE_DIR to ensure hardlink-based optimization works
        env = os.environ.copy()
        env["UV_CACHE_DIR"] = str(self.uv_cache_dir)

        self._logger.info("uv_exec cmd=%s", cmd)
        # Spawn subprocess with isolated env and piped streams
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            # Wait for completion or timeout
            stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            # Force kill if timeout occurs
            process.kill()
            await process.wait()
            raise

        # Decode output, replacing invalid characters if necessary
        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        self._logger.info("uv_exit code=%s", process.returncode)
        return UVResult(stdout=stdout, stderr=stderr, exit_code=process.returncode or 0)

    def _raise_for_failure(self, result: UVResult, fallback: str) -> None:
        """Convert UV execution failures into specific domain exceptions."""
        message = result.stderr.strip() or result.stdout.strip() or fallback
        lowered = message.lower()
        # Differentiate between dependency resolution issues and other execution errors
        if "no solution found" in lowered or "resolution" in lowered:
            raise PackageResolutionFailed(message)
        raise UVExecutionError(message)

    async def init_project(self, node_id: str, python_version: str) -> UVResult:
        """Initialize a new UV project with a specific Python version."""
        project_path = self._get_project_path(node_id)
        project_path.mkdir(parents=True, exist_ok=True)
        cmd = ["uv", "init", str(project_path), "--python", python_version, "--no-workspace"]
        result = await self._run(cmd)
        if result.exit_code != 0:
            self._raise_for_failure(result, "uv init failed")
        return result

    async def add_packages(
        self,
        node_id: str,
        packages: list[str],
        *,
        upgrade: bool = False,
    ) -> UVResult:
        """Install new packages into the project environment."""
        project_path = self._get_project_path(node_id)
        cmd = ["uv", "add", "--project", str(project_path), "--no-workspace"]
        if upgrade:
            cmd.append("--upgrade")
        cmd.extend(packages)
        result = await self._run(cmd)
        if result.exit_code != 0:
            self._raise_for_failure(result, "uv add failed")
        return result

    async def remove_packages(self, node_id: str, packages: list[str]) -> UVResult:
        """Remove specified packages from the project."""
        project_path = self._get_project_path(node_id)
        cmd = ["uv", "remove", "--project", str(project_path), "--no-sync", *packages]
        result = await self._run(cmd)
        if result.exit_code != 0:
            self._raise_for_failure(result, "uv remove failed")
        return result

    async def sync_env(self, node_id: str) -> UVResult:
        """Synchronize the virtual environment with the lock file."""
        project_path = self._get_project_path(node_id)
        cmd = ["uv", "sync", "--project", str(project_path)]
        result = await self._run(cmd)
        if result.exit_code != 0:
            self._raise_for_failure(result, "uv sync failed")
        return result

    async def run_code(self, node_id: str, code: str, *, timeout_seconds: int) -> UVResult:
        """Execute arbitrary Python code within the isolated environment."""
        project_path = self._get_project_path(node_id)
        cmd = ["uv", "run", "--project", str(project_path), "python", "-c", code]
        result = await self._run(cmd, timeout_seconds=timeout_seconds)
        return result
