from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterator
from contextlib import contextmanager

from fastapi import FastAPI,Depends, Query, Path as APIPath, File, UploadFile, Form
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pathlib import Path

from src.config import Settings, get_settings
from src.exceptions import DBAuditError, EnvLocked, EnvNotFound, InvalidPackages, ServiceError
from src.models import *
from src.services.dep_manager import DependencyManager
from src.services.env_manager import EnvManager
from src.services.lock_manager import LockManager
from src.services.uv_executor import UVCommandExecutor

from src.db_service.models import EnvOperation
from src.db_service.env_audit import EnvAudit
from src.db_service.connection import get_db

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
    def _maybe_env_audit(*, workflow_id: str, node_id: str, version_id: str | None = None) -> Iterator[Any | None]:
        from src.db_service.env_audit import EnvAudit
        db = SessionLocal()
        try:
            yield EnvAudit(db=db, workflow_id=workflow_id, node_id=node_id, version_id=version_id)
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
        version_id: str | None = Query(None),
    ) -> EnvStatusResponse:
        """Retrieve the current status of an environment."""
        env_id = EnvManager.get_env_id(workflow_id, node_id, version_id)
        logger.info(
            "get_env_status workflow_id=%s node_id=%s version_id=%s",
            workflow_id,
            node_id,
            version_id,
        )
        status = env_manager.get_status(env_id)
        return EnvStatusResponse(
            workflow_id=workflow_id,
            node_id=node_id,
            version_id=version_id,
            status=status.status,
            env_path=str(status.env_path),
            has_pyproject=status.has_pyproject,
            has_uv_lock=status.has_uv_lock,
            has_venv=status.has_venv,
            metadata=status.metadata,
        )

    async def _resolve_packages(packages: list[str], requirements_file: UploadFile | None) -> list[str]:
        # Filter out empty or whitespace-only package names from the Form list
        final_packages = []
        for p in packages:
            if p and p.strip():
                # 处理 Swagger UI 可能将数组合并为逗号分隔字符串的情况
                if "," in p and " " not in p: # 只有逗号且没空格，大概率是合并后的数组
                    final_packages.extend([item.strip() for item in p.split(",") if item.strip()])
                else:
                    final_packages.append(p.strip())
        
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
        workflow_id: str = Form(...),
        node_id: str = Form(...),
        version_id: str | None = Form(None),
        python_version: str | None = Form(None),
        packages: list[str] = Form([]),
        requirements_file: UploadFile | None = File(None, description="Requirements file (requirements.txt)")
    ) -> CreateEnvResponse:
        """Create a new environment and optionally install initial dependencies."""
        env_id = EnvManager.get_env_id(workflow_id, node_id, version_id)
        lock = lock_manager.get_lock(env_id)
        if lock.locked():
            raise EnvLocked()
        async with lock:
            with _maybe_env_audit(workflow_id=workflow_id, node_id=node_id, version_id=version_id) as audit:
                start_time = audit.start()
                try:
                    logger.info(
                        "create_env workflow_id=%s node_id=%s version_id=%s python=%s",
                        workflow_id,
                        node_id,
                        version_id,
                        python_version,
                    )

                    resolved_packages = await _resolve_packages(packages, requirements_file)

                    # Perform UV initialization and package installation
                    env_path, version = await env_manager.create_env(
                        env_id, python_version=python_version, packages=resolved_packages
                    )
                    # Return the generated pyproject.toml content for verification
                    pyproject = (env_path / "pyproject.toml").read_text(encoding="utf-8")
                    try:
                        audit.success(
                            operation="create_env",
                            start_time=start_time,
                            metadata={
                                "python_version": python_version or version,
                                "packages": resolved_packages,
                                "requirements_file": requirements_file.filename if requirements_file else None,
                            },
                        )
                    except Exception as e:
                        raise DBAuditError(f"Failed to audit create_env: {e}") from e
                    return CreateEnvResponse(
                        workflow_id=workflow_id,
                        node_id=node_id,
                        version_id=version_id,
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
                                "python_version": python_version,
                                "packages": packages,
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
        version_id: str | None = Query(None),
    ) -> dict[str, str]:
        """Delete an environment and clean up its associated lock."""
        env_id = EnvManager.get_env_id(workflow_id, node_id, version_id)
        lock = lock_manager.get_lock(env_id)
        if lock.locked():
            raise EnvLocked()
        async with lock:
            with _maybe_env_audit(workflow_id=workflow_id, node_id=node_id, version_id=version_id) as audit:
                start_time = audit.start()
                try:
                    logger.info(
                        "delete_env workflow_id=%s node_id=%s version_id=%s",
                        workflow_id,
                        node_id,
                        version_id,
                    )
                    await env_manager.delete_env(env_id)
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
        return {
            "workflow_id": workflow_id,
            "node_id": node_id,
            "version_id": version_id or "",
            "status": "deleted",
        }

    @app.post("/envs/{workflow_id}/{node_id}/deps")
    async def add_deps(
        workflow_id: str = APIPath(min_length=1),
        node_id: str = APIPath(min_length=1),
        version_id: str | None = Query(None),
        packages: list[str] = Form([]),
        requirements_file: UploadFile | None = File(None, description="Requirements file (requirements.txt)")
    ) -> dict[str, str]:
        """Add new dependencies to the environment using 'uv add'."""
        env_id = EnvManager.get_env_id(workflow_id, node_id, version_id)
        lock = lock_manager.get_lock(env_id)
        if lock.locked():
            raise EnvLocked()
        async with lock:
            with _maybe_env_audit(workflow_id=workflow_id, node_id=node_id, version_id=version_id) as audit:
                start_time = audit.start()
                try:
                    if not (resolved_settings.envs_base_path / env_id).exists():
                        raise EnvNotFound()

                    resolved_packages = await _resolve_packages(packages, requirements_file)
                    if not resolved_packages:
                        raise InvalidPackages("No packages specified")
                    logger.info(
                        "add_deps workflow_id=%s node_id=%s version_id=%s packages=%s",
                        workflow_id,
                        node_id,
                        version_id,
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
                                "packages": packages,
                                "requirements_file": requirements_file.filename if requirements_file else None,
                            },
                        )
                    except Exception as audit_exc:
                        raise DBAuditError(f"Failed to audit add_deps failure: {audit_exc}") from audit_exc
                    raise
        return {
            "workflow_id": workflow_id,
            "node_id": node_id,
            "version_id": version_id or "",
            "status": "added",
        }

    @app.put("/envs/{workflow_id}/{node_id}/deps")
    async def update_deps(
        workflow_id: str = APIPath(min_length=1),
        node_id: str = APIPath(min_length=1),
        version_id: str | None = Query(None),
        packages: list[str] = Form([]),
        requirements_file: UploadFile | None = File(None, description="Requirements file (requirements.txt)")
    ) -> dict[str, str]:
        """Upgrade existing dependencies using 'uv add --upgrade'."""
        env_id = EnvManager.get_env_id(workflow_id, node_id, version_id)
        lock = lock_manager.get_lock(env_id)
        if lock.locked():
            raise EnvLocked()
        async with lock:
            with _maybe_env_audit(workflow_id=workflow_id, node_id=node_id, version_id=version_id) as audit:
                start_time = audit.start()
                try:
                    if not (resolved_settings.envs_base_path / env_id).exists():
                        raise EnvNotFound()

                    resolved_packages = await _resolve_packages(packages, requirements_file)
                    if not resolved_packages:
                        raise InvalidPackages("No packages specified")
                    logger.info(
                        "update_deps workflow_id=%s node_id=%s version_id=%s packages=%s",
                        workflow_id,
                        node_id,
                        version_id,
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
                                "packages": packages,
                                "requirements_file": requirements_file.filename if requirements_file else None,
                            },
                        )
                    except Exception as audit_exc:
                        raise DBAuditError(f"Failed to audit update_deps failure: {audit_exc}") from audit_exc
                    raise
        return {
            "workflow_id": workflow_id,
            "node_id": node_id,
            "version_id": version_id or "",
            "status": "updated",
        }

    @app.delete("/envs/{workflow_id}/{node_id}/deps", response_model=SyncResponse)
    async def remove_deps(
        workflow_id: str = APIPath(min_length=1),
        node_id: str = APIPath(min_length=1),
        version_id: str | None = Query(None),
        packages: list[str] = Form([]),
        requirements_file: UploadFile | None = File(None, description="Requirements file (requirements.txt)")
    ) -> dict[str, str]:
        """Remove dependencies from the environment using 'uv remove'."""
        env_id = EnvManager.get_env_id(workflow_id, node_id, version_id)
        lock = lock_manager.get_lock(env_id)
        if lock.locked():
            raise EnvLocked()
        async with lock:
            with _maybe_env_audit(workflow_id=workflow_id, node_id=node_id, version_id=version_id) as audit:
                start_time = audit.start()
                try:
                    if not (resolved_settings.envs_base_path / env_id).exists():
                        raise EnvNotFound()

                    resolved_packages = await _resolve_packages(packages, requirements_file)
                    if not resolved_packages:
                        raise InvalidPackages("No packages specified")
                    logger.info(
                        "delete_deps workflow_id=%s node_id=%s version_id=%s packages=%s",
                        workflow_id,
                        node_id,
                        version_id,
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
                                "packages": packages,
                                "requirements_file": requirements_file.filename if requirements_file else None,
                            },
                        )
                    except Exception as audit_exc:
                        raise DBAuditError(f"Failed to audit remove_deps failure: {audit_exc}") from audit_exc
                    raise
        return {
            "workflow_id": workflow_id,
            "node_id": node_id,
            "version_id": version_id or "",
            "status": "removed",
        }

    @app.get("/envs/{workflow_id}/{node_id}/deps", response_model=DepsResponse)
    async def list_deps(
        workflow_id: str = APIPath(min_length=1),
        node_id: str = APIPath(min_length=1),
        version_id: str | None = Query(None),
    ) -> DepsResponse:
        """List dependencies by reading pyproject.toml and uv.lock."""
        env_id = EnvManager.get_env_id(workflow_id, node_id, version_id)
        logger.info(
            "list_deps workflow_id=%s node_id=%s version_id=%s",
            workflow_id,
            node_id,
            version_id,
        )
        deps, locked = dep_manager.list_dependencies(env_id)
        env_manager.touch(env_id)
        return DepsResponse(
            workflow_id=workflow_id,
            node_id=node_id,
            version_id=version_id,
            dependencies=deps,
            locked_versions=locked,
        )

    @app.post("/envs/{workflow_id}/{node_id}/sync", response_model=SyncResponse)
    async def sync_env(
        workflow_id: str = APIPath(min_length=1),
        node_id: str = APIPath(min_length=1),
        version_id: str | None = Query(None),
    ) -> SyncResponse:
        """Synchronize the environment from the lock file using 'uv sync'."""
        env_id = EnvManager.get_env_id(workflow_id, node_id, version_id)
        lock = lock_manager.get_lock(env_id)
        if lock.locked():
            raise EnvLocked()
        async with lock:
            with _maybe_env_audit(workflow_id=workflow_id, node_id=node_id, version_id=version_id) as audit:
                start_time = audit.start()
                try:
                    logger.info(
                        "sync_env workflow_id=%s node_id=%s version_id=%s",
                        workflow_id,
                        node_id,
                        version_id,
                    )
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
        return SyncResponse(
            workflow_id=workflow_id,
            node_id=node_id,
            version_id=version_id,
            status="synced",
        )

    @app.post("/envs/{workflow_id}/{node_id}/run", response_model=RunResponse)
    async def run_code(
        workflow_id: str = APIPath(min_length=1),
        node_id: str = APIPath(min_length=1),
        request: RunRequest = None,
    ) -> RunResponse:
        """Execute Python code within the isolated environment using 'uv run'."""
        version_id = request.version_id if request else None
        env_id = EnvManager.get_env_id(workflow_id, node_id, version_id)
        lock = lock_manager.get_lock(env_id)
        if lock.locked():
            raise EnvLocked()
        async with lock:
            with _maybe_env_audit(workflow_id=workflow_id, node_id=node_id, version_id=version_id) as audit:
                start_time = audit.start()
                try:
                    logger.info(
                        "run_code workflow_id=%s node_id=%s version_id=%s timeout=%s",
                        workflow_id,
                        node_id,
                        version_id,
                        request.timeout if request else None,
                    )
                    uv_result = await env_manager.run(
                        env_id, request.code, timeout_seconds=request.timeout
                    )
                    env_manager.touch(env_id)
                    try:
                        audit.success(
                            operation="run",
                            start_time=start_time,
                            stdout=uv_result.stdout,
                            stderr=uv_result.stderr,
                            exit_code=uv_result.exit_code,
                            metadata={"timeout": request.timeout, "code": request.code},
                        )
                    except Exception as e:
                        raise DBAuditError(f"Failed to audit run: {e}") from e
                except Exception as e:
                    try:
                        audit.failed(
                            operation="run",
                            start_time=start_time,
                            error=str(e),
                            metadata={"timeout": request.timeout, "code": request.code},
                        )
                    except Exception as audit_exc:
                        raise DBAuditError(f"Failed to audit run failure: {audit_exc}") from audit_exc
                    raise
        return RunResponse(
            workflow_id=workflow_id,
            node_id=node_id,
            version_id=version_id,
            stdout=uv_result.stdout,
            stderr=uv_result.stderr,
            exit_code=uv_result.exit_code,
        )

    @app.get("/envs/{workflow_id}/{node_id}/export", response_model=ExportResponse)
    async def export_env(
        workflow_id: str = APIPath(min_length=1),
        node_id: str = APIPath(min_length=1),
        version_id: str | None = Query(None),
    ) -> ExportResponse:
        """Export the pyproject.toml and uv.lock contents of an environment."""
        env_id = EnvManager.get_env_id(workflow_id, node_id, version_id)
        logger.info(
            "export_env workflow_id=%s node_id=%s version_id=%s",
            workflow_id,
            node_id,
            version_id,
        )
        status = env_manager.get_status(env_id)
        if status.status == "NOT_EXISTS":
            raise EnvNotFound()

        pyproject_path = status.env_path / "pyproject.toml"
        uv_lock_path = status.env_path / "uv.lock"

        pyproject_content = ""
        if pyproject_path.exists():
            pyproject_content = pyproject_path.read_text(encoding="utf-8")

        uv_lock_content = None
        if uv_lock_path.exists():
            uv_lock_content = uv_lock_path.read_text(encoding="utf-8")

        env_manager.touch(env_id)
        return ExportResponse(
            workflow_id=workflow_id,
            node_id=node_id,
            version_id=version_id,
            pyproject_toml=pyproject_content,
            uv_lock=uv_lock_content,
        )

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

    @app.get("/envs-history/{workflow_id}", response_model=EnvOperationListResponse, tags=["Env Audit"])
    async def get_operations(
            workflow_id: str = APIPath(min_length=1),
            node_id: str | None = None,
            status: str | None = Query(None, description="Status: success/failed"),
            limit: int = Query(100, ge=1, le=1000),
            offset: int = Query(0, ge=0),
            db: Session = Depends(get_db),
    ) -> EnvOperationListResponse:
        """查询指定工作流的操作历史"""
        logger.info(
            "get_operations workflow_id=%s node_id=%s status=%s limit=%s offset=%s",
            workflow_id, node_id, status, limit, offset
        )

        audit = EnvAudit(db=db)
        try:
            return audit.query(
                workflow_id=workflow_id,
                node_id=node_id,
                status=status,
                limit=limit,
                offset=offset,
            )
        except Exception as e:
            logger.exception("Failed to query env operations: %s", e)
            raise DBAuditError(f"Failed querying audit: {e}") from e

    @app.delete("/envs-history/{workflow_id}", tags=["Env Audit"])
    async def delete_operations(
            workflow_id: str = APIPath(min_length=1),
            node_id: str | None = None,
            db: Session = Depends(get_db),
    ) -> dict:
        """删除指定工作流的操作记录"""
        logger.info("delete_operations workflow_id=%s node_id=%s", workflow_id, node_id)

        audit = EnvAudit(db=db)
        try:
            result = audit.delete(workflow_id=workflow_id, node_id=node_id)
            if node_id:
                return {
                    "workflow_id": workflow_id,
                    "deleted_count": result["deleted_count"],
                    "node_id": node_id,
                }
            else:
                return {
                    "workflow_id": workflow_id,
                    "deleted_count": result["deleted_count"],
                    "deleted_node_ids": result["deleted_node_ids"],
                }
        except Exception as e:
            logger.exception("Failed to delete env operations: %s", e)
            raise DBAuditError(f"Failed deleting audit records: {e}") from e
    return app


app = create_app()
