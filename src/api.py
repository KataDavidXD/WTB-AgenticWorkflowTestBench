from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterator
from contextlib import contextmanager

from fastapi import FastAPI, Query, Path as APIPath, File, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pathlib import Path

from src.config import Settings, get_settings
from src.exceptions import DBAuditError, EnvLocked, EnvNotFound, InvalidPackages, ServiceError
from src.models import (
    CleanupRequest,
    CleanupResponse,
    CreateEnvRequest,
    CreateEnvResponse,
    DepsResponse,
    EnvStatusResponse,
    ErrorResponse,
    ExportResponse,
    PackagesRequest,
    RunRequest,
    RunResponse,
    SyncResponse,
)
from src.services.dep_manager import DependencyManager
from src.services.env_manager import EnvManager
from src.services.lock_manager import LockManager
from src.services.uv_executor import UVCommandExecutor


def create_app(settings: Settings | None = None, *, executor: UVCommandExecutor | None = None) -> FastAPI:
    """Create a FastAPI application instance."""

    resolved_settings = settings or get_settings()
    resolved_settings.validate_storage_layout()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("env-manager")

    resolved_executor = executor or UVCommandExecutor(
        envs_base_path=resolved_settings.envs_base_path,
        uv_cache_dir=resolved_settings.uv_cache_dir,
    )
    lock_manager = LockManager()
    env_manager = EnvManager(
        envs_base_path=resolved_settings.envs_base_path,
        executor=resolved_executor,
        default_python=resolved_settings.default_python,
        execution_timeout_seconds=resolved_settings.execution_timeout_seconds,
    )
    dep_manager = DependencyManager(resolved_settings.envs_base_path, resolved_executor)

    app = FastAPI(
        title="LangGraph Node Environment Manager",
        version="0.1.0",
        description="UV-based node-level Python virtual environment isolation and dependency management service",
    )

    try:
        from src.db_service.connection import SessionLocal, engine, init_db
        from sqlalchemy import text

        init_db()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        raise DBAuditError(f"Failed to initialize/connect audit database: {e}") from e

    @contextmanager
    def _maybe_env_audit(*, workflow_id: str, node_id: str) -> Iterator[Any | None]:
        from src.db_service.env_audit import EnvAudit
        db = SessionLocal()
        try:
            yield EnvAudit(db=db, workflow_id=workflow_id, node_id=node_id)
        finally:
            db.close()

    @app.exception_handler(ServiceError)
    async def _service_error_handler(_, exc: ServiceError) -> JSONResponse:
        """Handle custom service errors."""
        logger.warning("service_error code=%s message=%s", exc.code, exc.message)
        return JSONResponse(
            status_code=exc.http_status,
            content=ErrorResponse(code=exc.code, message=exc.message).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(_, exc: RequestValidationError) -> JSONResponse:
        """Handle Pydantic validation errors and map them to standard ErrorResponse."""
        message = "Request parameter validation failed"
        if exc.errors():
            message = "; ".join(
                f"{'.'.join(str(x) for x in (err.get('loc') or []))}: {err.get('msg')}"
                for err in exc.errors()
            )
        invalid = InvalidPackages(message)
        logger.warning("validation_error message=%s", invalid.message)
        return JSONResponse(
            status_code=invalid.http_status,
            content=ErrorResponse(code=invalid.code, message=invalid.message).model_dump(),
        )

    @app.get("/envs/{workflow_id}/{node_id}", response_model=EnvStatusResponse)
    async def get_env_status(
        workflow_id: str = APIPath(min_length=1),
        node_id: str = APIPath(min_length=1),
    ) -> EnvStatusResponse:
        """Retrieve the current status of an environment."""
        env_id = f"{workflow_id}_{node_id}"
        logger.info("get_env_status workflow_id=%s node_id=%s", workflow_id, node_id)
        status = env_manager.get_status(env_id)
        return EnvStatusResponse(
            workflow_id=workflow_id,
            node_id=node_id,
            status=status.status,
            env_path=str(status.env_path),
            has_pyproject=status.has_pyproject,
            has_uv_lock=status.has_uv_lock,
            has_venv=status.has_venv,
            metadata=status.metadata,
        )

    async def _resolve_packages(packages: list[str], requirements_file: UploadFile | None) -> list[str]:
        final_packages = list(packages)
        if requirements_file:
            try:
                content = await requirements_file.read()
                content_str = content.decode("utf-8")
                for line in content_str.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        final_packages.append(line)
            except Exception as e:
                raise InvalidPackages(f"Failed to read requirements file: {e}") from e
        return final_packages

    @app.post("/envs", response_model=CreateEnvResponse)
    async def create_env(
        req: CreateEnvRequest,
        requirements_file: UploadFile | None = File(None, description="Requirements file (requirements.txt)")
    ) -> CreateEnvResponse:
        """Create a new environment and optionally install initial dependencies."""
        env_id = f"{req.workflow_id}_{req.node_id}"
        lock = lock_manager.get_lock(env_id)
        if lock.locked():
            raise EnvLocked()
        async with lock:
            with _maybe_env_audit(workflow_id=req.workflow_id, node_id=req.node_id) as audit:
                start_time = audit.start()
                try:
                    logger.info(
                        "create_env workflow_id=%s node_id=%s python=%s",
                        req.workflow_id,
                        req.node_id,
                        req.python_version,
                    )

                    resolved_packages = await _resolve_packages(req.packages, requirements_file)

                    # Perform UV initialization and package installation
                    env_path, version = await env_manager.create_env(
                        env_id, python_version=req.python_version, packages=resolved_packages
                    )
                    # Return the generated pyproject.toml content for verification
                    pyproject = (env_path / "pyproject.toml").read_text(encoding="utf-8")
                    try:
                        audit.success(
                            operation="create_env",
                            start_time=start_time,
                            metadata={
                                "python_version": req.python_version or version,
                                "packages": resolved_packages,
                                "requirements_file": requirements_file.filename if requirements_file else None,
                            },
                        )
                    except Exception as e:
                        raise DBAuditError(f"Failed to audit create_env: {e}") from e
                    return CreateEnvResponse(
                        workflow_id=req.workflow_id,
                        node_id=req.node_id,
                        env_path=str(env_path),
                        python_version=version,
                        status="created",
                        pyproject_toml=pyproject,
                    )
                except Exception as e:
                    try:
                        audit.failed(
                            operation="create_env",
                            start_time=start_time,
                            error=str(e),
                            metadata={
                                "python_version": req.python_version,
                                "packages": req.packages,
                                "requirements_file": requirements_file.filename if requirements_file else None,
                            },
                        )
                    except Exception as audit_exc:
                        raise DBAuditError(f"Failed to audit create_env failure: {audit_exc}") from audit_exc
                    raise

    @app.delete("/envs/{workflow_id}/{node_id}")
    async def delete_env(
        workflow_id: str = APIPath(min_length=1),
        node_id: str = APIPath(min_length=1),
    ) -> dict[str, str]:
        """Delete an environment and clean up its associated lock."""
        env_id = f"{workflow_id}_{node_id}"
        lock = lock_manager.get_lock(env_id)
        if lock.locked():
            raise EnvLocked()
        async with lock:
            with _maybe_env_audit(workflow_id=workflow_id, node_id=node_id) as audit:
                start_time = audit.start()
                try:
                    logger.info("delete_env workflow_id=%s node_id=%s", workflow_id, node_id)
                    env_manager.delete_env(env_id)
                    # Remove the lock from memory to avoid leakage
                    await lock_manager.cleanup_lock(env_id)
                    try:
                        audit.success(operation="delete_env", start_time=start_time)
                    except Exception as e:
                        raise DBAuditError(f"Failed to audit delete_env: {e}") from e
                except Exception as e:
                    try:
                        audit.failed(operation="delete_env", start_time=start_time, error=str(e))
                    except Exception as audit_exc:
                        raise DBAuditError(f"Failed to audit delete_env failure: {audit_exc}") from audit_exc
                    raise
        return {"workflow_id": workflow_id, "node_id": node_id, "status": "deleted"}

    @app.post("/envs/{workflow_id}/{node_id}/deps")
    async def add_deps(
        workflow_id: str = APIPath(min_length=1),
        node_id: str = APIPath(min_length=1),
        req: PackagesRequest = ...,
        requirements_file: UploadFile | None = File(None, description="Requirements file (requirements.txt)")
    ) -> dict[str, str]:
        """Add new dependencies to the environment using 'uv add'."""
        env_id = f"{workflow_id}_{node_id}"
        lock = lock_manager.get_lock(env_id)
        if lock.locked():
            raise EnvLocked()
        async with lock:
            with _maybe_env_audit(workflow_id=workflow_id, node_id=node_id) as audit:
                start_time = audit.start()
                try:
                    if not (resolved_settings.envs_base_path / env_id).exists():
                        raise EnvNotFound()

                    resolved_packages = await _resolve_packages(req.packages, requirements_file)
                    if not resolved_packages:
                        raise InvalidPackages("No packages specified")
                    logger.info(
                        "add_deps workflow_id=%s node_id=%s packages=%s",
                        workflow_id,
                        node_id,
                        resolved_packages,
                    )

                    uv_result = await dep_manager.add(env_id, resolved_packages)
                    # Update the last used timestamp
                    env_manager.touch(env_id)
                    try:
                        audit.success(
                            operation="add_deps",
                            start_time=start_time,
                            stdout=uv_result.stdout,
                            stderr=uv_result.stderr,
                            exit_code=uv_result.exit_code,
                            metadata={
                                "packages": resolved_packages,
                                "requirements_file": requirements_file.filename if requirements_file else None,
                            },
                        )
                    except Exception as e:
                        raise DBAuditError(f"Failed to audit add_deps: {e}") from e
                except Exception as e:
                    try:
                        audit.failed(
                            operation="add_deps",
                            start_time=start_time,
                            error=str(e),
                            metadata={
                                "packages": req.packages,
                                "requirements_file": requirements_file.filename if requirements_file else None,
                            },
                        )
                    except Exception as audit_exc:
                        raise DBAuditError(f"Failed to audit add_deps failure: {audit_exc}") from audit_exc
                    raise
        return {"workflow_id": workflow_id, "node_id": node_id, "status": "added"}

    @app.put("/envs/{workflow_id}/{node_id}/deps")
    async def update_deps(
        workflow_id: str = APIPath(min_length=1),
        node_id: str = APIPath(min_length=1),
        req: PackagesRequest = ...,
        requirements_file: UploadFile | None = File(None, description="Requirements file (requirements.txt)")
    ) -> dict[str, str]:
        """Upgrade existing dependencies using 'uv add --upgrade'."""
        env_id = f"{workflow_id}_{node_id}"
        lock = lock_manager.get_lock(env_id)
        if lock.locked():
            raise EnvLocked()
        async with lock:
            with _maybe_env_audit(workflow_id=workflow_id, node_id=node_id) as audit:
                start_time = audit.start()
                try:
                    if not (resolved_settings.envs_base_path / env_id).exists():
                        raise EnvNotFound()

                    resolved_packages = await _resolve_packages(req.packages, requirements_file)
                    if not resolved_packages:
                        raise InvalidPackages("No packages specified")
                    logger.info(
                        "update_deps workflow_id=%s node_id=%s packages=%s",
                        workflow_id,
                        node_id,
                        resolved_packages,
                    )

                    uv_result = await dep_manager.update(env_id, resolved_packages)
                    env_manager.touch(env_id)
                    try:
                        audit.success(
                            operation="update_deps",
                            start_time=start_time,
                            stdout=uv_result.stdout,
                            stderr=uv_result.stderr,
                            exit_code=uv_result.exit_code,
                            metadata={
                                "packages": resolved_packages,
                                "requirements_file": requirements_file.filename if requirements_file else None,
                            },
                        )
                    except Exception as e:
                        raise DBAuditError(f"Failed to audit update_deps: {e}") from e
                except Exception as e:
                    try:
                        audit.failed(
                            operation="update_deps",
                            start_time=start_time,
                            error=str(e),
                            metadata={
                                "packages": req.packages,
                                "requirements_file": requirements_file.filename if requirements_file else None,
                            },
                        )
                    except Exception as audit_exc:
                        raise DBAuditError(f"Failed to audit update_deps failure: {audit_exc}") from audit_exc
                    raise
        return {"workflow_id": workflow_id, "node_id": node_id, "status": "updated"}

    @app.delete("/envs/{workflow_id}/{node_id}/deps")
    async def delete_deps(
        workflow_id: str = APIPath(min_length=1),
        node_id: str = APIPath(min_length=1),
        req: PackagesRequest = ...,
        requirements_file: UploadFile | None = File(None, description="Requirements file (requirements.txt)")
    ) -> dict[str, str]:
        """Remove dependencies from the environment using 'uv remove'."""
        env_id = f"{workflow_id}_{node_id}"
        lock = lock_manager.get_lock(env_id)
        if lock.locked():
            raise EnvLocked()
        async with lock:
            with _maybe_env_audit(workflow_id=workflow_id, node_id=node_id) as audit:
                start_time = audit.start()
                try:
                    if not (resolved_settings.envs_base_path / env_id).exists():
                        raise EnvNotFound()

                    resolved_packages = await _resolve_packages(req.packages, requirements_file)
                    if not resolved_packages:
                        raise InvalidPackages("No packages specified")
                    logger.info(
                        "delete_deps workflow_id=%s node_id=%s packages=%s",
                        workflow_id,
                        node_id,
                        resolved_packages,
                    )

                    uv_result = await dep_manager.remove(env_id, resolved_packages)
                    env_manager.touch(env_id)
                    try:
                        audit.success(
                            operation="remove_deps",
                            start_time=start_time,
                            stdout=uv_result.stdout,
                            stderr=uv_result.stderr,
                            exit_code=uv_result.exit_code,
                            metadata={
                                "packages": resolved_packages,
                                "requirements_file": requirements_file.filename if requirements_file else None,
                            },
                        )
                    except Exception as e:
                        raise DBAuditError(f"Failed to audit remove_deps: {e}") from e
                except Exception as e:
                    try:
                        audit.failed(
                            operation="remove_deps",
                            start_time=start_time,
                            error=str(e),
                            metadata={
                                "packages": req.packages,
                                "requirements_file": requirements_file.filename if requirements_file else None,
                            },
                        )
                    except Exception as audit_exc:
                        raise DBAuditError(f"Failed to audit remove_deps failure: {audit_exc}") from audit_exc
                    raise
        return {"workflow_id": workflow_id, "node_id": node_id, "status": "removed"}

    @app.get("/envs/{workflow_id}/{node_id}/deps", response_model=DepsResponse)
    async def list_deps(
        workflow_id: str = APIPath(min_length=1),
        node_id: str = APIPath(min_length=1),
    ) -> DepsResponse:
        """List dependencies by reading pyproject.toml and uv.lock."""
        env_id = f"{workflow_id}_{node_id}"
        logger.info("list_deps workflow_id=%s node_id=%s", workflow_id, node_id)
        deps, locked = dep_manager.list_dependencies(env_id)
        env_manager.touch(env_id)
        return DepsResponse(workflow_id=workflow_id, node_id=node_id, dependencies=deps, locked_versions=locked)

    @app.post("/envs/{workflow_id}/{node_id}/sync", response_model=SyncResponse)
    async def sync_env(
        workflow_id: str = APIPath(min_length=1),
        node_id: str = APIPath(min_length=1),
    ) -> SyncResponse:
        """Synchronize the environment from the lock file using 'uv sync'."""
        env_id = f"{workflow_id}_{node_id}"
        lock = lock_manager.get_lock(env_id)
        if lock.locked():
            raise EnvLocked()
        async with lock:
            with _maybe_env_audit(workflow_id=workflow_id, node_id=node_id) as audit:
                start_time = audit.start()
                try:
                    logger.info("sync_env workflow_id=%s node_id=%s", workflow_id, node_id)
                    if not (resolved_settings.envs_base_path / env_id).exists():
                        raise EnvNotFound()
                    uv_result = await env_manager.sync(env_id)
                    env_manager.touch(env_id)
                    try:
                        audit.success(
                            operation="sync",
                            start_time=start_time,
                            stdout=uv_result.stdout,
                            stderr=uv_result.stderr,
                            exit_code=uv_result.exit_code,
                        )
                    except Exception as e:
                        raise DBAuditError(f"Failed to audit sync: {e}") from e
                except Exception as e:
                    try:
                        audit.failed(operation="sync", start_time=start_time, error=str(e))
                    except Exception as audit_exc:
                        raise DBAuditError(f"Failed to audit sync failure: {audit_exc}") from audit_exc
                    raise
        return SyncResponse(workflow_id=workflow_id, node_id=node_id, status="synced")

    @app.post("/envs/{workflow_id}/{node_id}/run", response_model=RunResponse)
    async def run_code(
        workflow_id: str = APIPath(min_length=1),
        node_id: str = APIPath(min_length=1),
        req: RunRequest = ...
    ) -> RunResponse:
        """Execute Python code within the isolated environment using 'uv run'."""
        env_id = f"{workflow_id}_{node_id}"
        lock = lock_manager.get_lock(env_id)
        if lock.locked():
            raise EnvLocked()
        async with lock:
            with _maybe_env_audit(workflow_id=workflow_id, node_id=node_id) as audit:
                start_time = audit.start()
                try:
                    logger.info(
                        "run_code workflow_id=%s node_id=%s timeout=%s",
                        workflow_id,
                        node_id,
                        req.timeout,
                    )
                    result = await env_manager.run(env_id, req.code, timeout_seconds=req.timeout)
                    env_manager.touch(env_id)
                    try:
                        audit.success(
                            operation="run",
                            start_time=start_time,
                            stdout=result.stdout,
                            stderr=result.stderr,
                            exit_code=result.exit_code,
                            metadata={"timeout": req.timeout, "code": req.code},
                        )
                    except Exception as e:
                        raise DBAuditError(f"Failed to audit run: {e}") from e
                except Exception as e:
                    try:
                        audit.failed(
                            operation="run",
                            start_time=start_time,
                            error=str(e),
                            metadata={"timeout": req.timeout, "code": req.code},
                        )
                    except Exception as audit_exc:
                        raise DBAuditError(f"Failed to audit run failure: {audit_exc}") from audit_exc
                    raise
        return RunResponse(workflow_id=workflow_id, node_id=node_id, stdout=result.stdout, stderr=result.stderr, exit_code=result.exit_code)

    @app.get("/envs/{workflow_id}/{node_id}/export", response_model=ExportResponse)
    async def export_env(
        workflow_id: str = APIPath(min_length=1),
        node_id: str = APIPath(min_length=1),
    ) -> ExportResponse:
        """Export environment configuration files (pyproject.toml and uv.lock)."""
        env_id = f"{workflow_id}_{node_id}"
        logger.info("export_env workflow_id=%s node_id=%s", workflow_id, node_id)
        pyproject, uv_lock = env_manager.export_env(env_id)
        env_manager.touch(env_id)
        return ExportResponse(workflow_id=workflow_id, node_id=node_id, pyproject_toml=pyproject, uv_lock=uv_lock)

    @app.post("/envs/cleanup", response_model=CleanupResponse)
    async def cleanup(req: CleanupRequest) -> CleanupResponse:
        """Clean up environments that have not been used for a long time."""

        logger.info("cleanup idle_hours=%s", req.idle_hours)
        with _maybe_env_audit(workflow_id="_", node_id="_") as audit:
            start_time = audit.start()
            try:
                # Use provided idle_hours or fall back to system default
                idle_hours = req.idle_hours or resolved_settings.cleanup_idle_hours
                deleted = env_manager.cleanup(idle_hours=idle_hours)
                try:
                    audit.success(
                        operation="cleanup",
                        start_time=start_time,
                        metadata={"idle_hours": idle_hours, "deleted": deleted},
                    )
                except Exception as e:
                    raise DBAuditError(f"Failed to audit cleanup: {e}") from e
                return CleanupResponse(deleted=deleted, checked_at=datetime.now(timezone.utc))
            except Exception as e:
                try:
                    audit.failed(
                        operation="cleanup",
                        start_time=start_time,
                        error=str(e),
                        metadata={"idle_hours": req.idle_hours},
                    )
                except Exception as audit_exc:
                    raise DBAuditError(f"Failed to audit cleanup failure: {audit_exc}") from audit_exc
                raise

    return app


app = None
