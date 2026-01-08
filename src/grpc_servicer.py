# src/grpc_servicer.py
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import grpc

from src.grpc_generated import env_manager_pb2 as pb2
from src.grpc_generated import env_manager_pb2_grpc as pb2_grpc
from src.exceptions import (
    DBAuditError,
    EnvLocked,
    EnvNotFound,
    InvalidPackages,
    ServiceError,
)
from src.services.env_manager import EnvManager

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger("grpc-servicer")


def _handle_service_error(context: grpc.aio.ServicerContext, exc: ServiceError) -> None:
    """将 ServiceError 映射到 gRPC 状态码"""
    status_map = {
        400: grpc.StatusCode.INVALID_ARGUMENT,
        404: grpc.StatusCode.NOT_FOUND,
        409: grpc.StatusCode.ALREADY_EXISTS,
        423: grpc.StatusCode.FAILED_PRECONDITION,
        500: grpc.StatusCode.INTERNAL,
        504: grpc.StatusCode.DEADLINE_EXCEEDED,
    }
    code = status_map.get(exc.http_status, grpc.StatusCode.INTERNAL)
    context.set_code(code)
    context.set_details(f"{exc.code}: {exc.message}")


async def _resolve_packages_from_proto(
    packages: list[str],
    requirements_content: bytes | None,
) -> list[str]:
    """从 proto 消息解析包列表"""
    final_packages = []

    for p in packages:
        if p and p.strip():
            if "," in p and " " not in p:
                final_packages.extend([item.strip() for item in p.split(",") if item.strip()])
            else:
                final_packages.append(p.strip())

    if requirements_content:
        try:
            content_str = requirements_content.decode("utf-8")
            for line in content_str.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    final_packages.append(line)
        except Exception as e:
            raise InvalidPackages(f"Failed to read requirements content: {e}") from e

    return final_packages


class EnvManagerServicer(pb2_grpc.EnvManagerServiceServicer):
    """gRPC Servicer - 复用 FastAPI 中的服务实例"""

    def __init__(self, app: FastAPI):
        self.app = app

    # =========================================================================
    # 从 app.state 获取共享的服务实例
    # =========================================================================
    @property
    def settings(self):
        return self.app.state.settings

    @property
    def env_manager(self):
        return self.app.state.env_manager

    @property
    def dep_manager(self):
        return self.app.state.dep_manager

    @property
    def lock_manager(self):
        return self.app.state.lock_manager

    @asynccontextmanager
    async def _audit_context(
        self, workflow_id: str, node_id: str, version_id: str | None = None
    ):
        """从 app.state 获取 audit factory"""
        audit_factory = getattr(self.app.state, "audit_factory", None)
        if audit_factory:
            with audit_factory(
                workflow_id=workflow_id, node_id=node_id, version_id=version_id
            ) as audit:
                yield audit
        else:
            yield None

    # =========================================================================
    # GetEnvStatus
    # =========================================================================
    async def GetEnvStatus(
        self,
        request: pb2.GetEnvStatusRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb2.EnvStatusResponse:
        try:
            version_id = request.version_id if request.version_id else None
            env_id = EnvManager.get_env_id(request.workflow_id, request.node_id, version_id)

            logger.info(
                "grpc:get_env_status workflow_id=%s node_id=%s version_id=%s",
                request.workflow_id, request.node_id, version_id,
            )

            status = self.env_manager.get_status(env_id)

            return pb2.EnvStatusResponse(
                workflow_id=request.workflow_id,
                node_id=request.node_id,
                version_id=request.version_id,
                status=status.status,
                env_path=str(status.env_path),
                has_pyproject=status.has_pyproject,
                has_uv_lock=status.has_uv_lock,
                has_venv=status.has_venv,
                metadata_json=json.dumps(status.metadata) if status.metadata else "{}",
            )
        except ServiceError as e:
            _handle_service_error(context, e)
            raise
        except Exception as e:
            logger.exception("GetEnvStatus failed: %s", e)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            raise

    # =========================================================================
    # CreateEnv
    # =========================================================================
    async def CreateEnv(
        self,
        request: pb2.CreateEnvRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb2.CreateEnvResponse:
        version_id = request.version_id if request.version_id else None
        env_id = EnvManager.get_env_id(request.workflow_id, request.node_id, version_id)
        lock = self.lock_manager.get_lock(env_id)

        if lock.locked():
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details("Environment is locked")
            raise EnvLocked()

        async with lock:
            async with self._audit_context(
                request.workflow_id, request.node_id, version_id
            ) as audit:
                start_time = audit.start() if audit else datetime.now(timezone.utc)
                try:
                    logger.info(
                        "grpc:create_env workflow_id=%s node_id=%s version_id=%s python=%s",
                        request.workflow_id, request.node_id, version_id, request.python_version,
                    )

                    resolved_packages = await _resolve_packages_from_proto(
                        list(request.packages),
                        request.requirements_file_content if request.requirements_file_content else None,
                    )

                    python_version = request.python_version if request.python_version else None
                    env_path, version = await self.env_manager.create_env(
                        env_id, python_version=python_version, packages=resolved_packages,
                    )

                    pyproject = (env_path / "pyproject.toml").read_text(encoding="utf-8")

                    if audit:
                        audit.success(
                            operation="create_env",
                            start_time=start_time,
                            metadata={
                                "python_version": version,
                                "packages": resolved_packages,
                                "source": "grpc",
                            },
                        )

                    return pb2.CreateEnvResponse(
                        workflow_id=request.workflow_id,
                        node_id=request.node_id,
                        version_id=request.version_id,
                        env_path=str(env_path),
                        python_version=version,
                        status="created",
                        pyproject_toml=pyproject,
                    )
                except ServiceError as e:
                    if audit:
                        audit.failed(operation="create_env", start_time=start_time, error=str(e))
                    _handle_service_error(context, e)
                    raise
                except Exception as e:
                    if audit:
                        audit.failed(operation="create_env", start_time=start_time, error=str(e))
                    logger.exception("CreateEnv failed: %s", e)
                    context.set_code(grpc.StatusCode.INTERNAL)
                    context.set_details(str(e))
                    raise

    # =========================================================================
    # DeleteEnv
    # =========================================================================
    async def DeleteEnv(
        self,
        request: pb2.DeleteEnvRequest,
        context: grpc.aio.ServicerContext,
    ) -> pb2.DeleteEnvResponse:
        version_id = request.version_id if request.version_id else None
        env_id = EnvManager.get_env_id(request.workflow_id, request.node_id, version_id)
        lock = self.lock_manager.get_lock(env_id)

        if lock.locked():
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details("Environment is locked")
            raise EnvLocked()

        async with lock:
            async with self._audit_context(
                request.workflow_id, request.node_id, version_id
            ) as audit:
                start_time = audit.start() if audit else datetime.now(timezone.utc)
                try:
                    logger.info(
                        "grpc:delete_env workflow_id=%s node_id=%s version_id=%s",
                        request.workflow_id, request.node_id, version_id,
                    )

                    await self.env_manager.delete_env(env_id)
                    await self.lock_manager.cleanup_lock(env_id)

                    if audit:
                        audit.success(operation="delete_env", start_time=start_time)

                    return pb2.DeleteEnvResponse(
                        workflow_id=request.workflow_id,
                        node_id=request.node_id,
                        version_id=request.version_id,
                        status="deleted",
                    )
                except ServiceError as e:
                    if audit:
                        audit.failed(operation="delete_env", start_time=start_time, error=str(e))
                    _handle_service_error(context, e)
                    raise
                except Exception as e:
                    if audit:
                        audit.failed(operation="delete_env", start_time=start_time, error=str(e))
                    logger.exception("DeleteEnv failed: %s", e)
                    context.set_code(grpc.StatusCode.INTERNAL)
                    context.set_details(str(e))
                    raise

    # =========================================================================
    # AddDeps / UpdateDeps / RemoveDeps
    # =========================================================================
    async def AddDeps(
        self, request: pb2.DepsRequest, context: grpc.aio.ServicerContext
    ) -> pb2.DepsOperationResponse:
        return await self._deps_operation(request, context, "add")

    async def UpdateDeps(
        self, request: pb2.DepsRequest, context: grpc.aio.ServicerContext
    ) -> pb2.DepsOperationResponse:
        return await self._deps_operation(request, context, "update")

    async def RemoveDeps(
        self, request: pb2.DepsRequest, context: grpc.aio.ServicerContext
    ) -> pb2.DepsOperationResponse:
        return await self._deps_operation(request, context, "remove")

    async def _deps_operation(
        self,
        request: pb2.DepsRequest,
        context: grpc.aio.ServicerContext,
        operation: str,
    ) -> pb2.DepsOperationResponse:
        version_id = request.version_id if request.version_id else None
        env_id = EnvManager.get_env_id(request.workflow_id, request.node_id, version_id)
        lock = self.lock_manager.get_lock(env_id)

        if lock.locked():
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details("Environment is locked")
            raise EnvLocked()

        async with lock:
            async with self._audit_context(
                request.workflow_id, request.node_id, version_id
            ) as audit:
                start_time = audit.start() if audit else datetime.now(timezone.utc)
                try:
                    if not (self.settings.envs_base_path / env_id).exists():
                        raise EnvNotFound()

                    resolved_packages = await _resolve_packages_from_proto(
                        list(request.packages),
                        request.requirements_file_content if request.requirements_file_content else None,
                    )

                    if not resolved_packages:
                        raise InvalidPackages("No packages specified")

                    logger.info(
                        "grpc:%s_deps workflow_id=%s node_id=%s packages=%s",
                        operation, request.workflow_id, request.node_id, resolved_packages,
                    )

                    if operation == "add":
                        uv_result = await self.dep_manager.add(env_id, resolved_packages)
                        status = "added"
                    elif operation == "update":
                        uv_result = await self.dep_manager.update(env_id, resolved_packages)
                        status = "updated"
                    else:
                        uv_result = await self.dep_manager.remove(env_id, resolved_packages)
                        status = "removed"

                    self.env_manager.touch(env_id)

                    if audit:
                        audit.success(
                            operation=f"{operation}_deps",
                            start_time=start_time,
                            stdout=uv_result.stdout,
                            stderr=uv_result.stderr,
                            exit_code=uv_result.exit_code,
                            metadata={"packages": resolved_packages, "source": "grpc"},
                        )

                    return pb2.DepsOperationResponse(
                        workflow_id=request.workflow_id,
                        node_id=request.node_id,
                        version_id=request.version_id,
                        status=status,
                        stdout=uv_result.stdout,
                        stderr=uv_result.stderr,
                        exit_code=uv_result.exit_code,
                    )
                except ServiceError as e:
                    if audit:
                        audit.failed(
                            operation=f"{operation}_deps", start_time=start_time, error=str(e)
                        )
                    _handle_service_error(context, e)
                    raise
                except Exception as e:
                    if audit:
                        audit.failed(
                            operation=f"{operation}_deps", start_time=start_time, error=str(e)
                        )
                    logger.exception("%sDeps failed: %s", operation.capitalize(), e)
                    context.set_code(grpc.StatusCode.INTERNAL)
                    context.set_details(str(e))
                    raise

    # =========================================================================
    # ListDeps
    # =========================================================================
    async def ListDeps(
        self, request: pb2.ListDepsRequest, context: grpc.aio.ServicerContext
    ) -> pb2.ListDepsResponse:
        try:
            version_id = request.version_id if request.version_id else None
            env_id = EnvManager.get_env_id(request.workflow_id, request.node_id, version_id)

            logger.info(
                "grpc:list_deps workflow_id=%s node_id=%s",
                request.workflow_id, request.node_id,
            )

            deps, locked = self.dep_manager.list_dependencies(env_id)
            self.env_manager.touch(env_id)

            return pb2.ListDepsResponse(
                workflow_id=request.workflow_id,
                node_id=request.node_id,
                version_id=request.version_id,
                dependencies=deps,
                locked_versions=locked,
            )
        except ServiceError as e:
            _handle_service_error(context, e)
            raise
        except Exception as e:
            logger.exception("ListDeps failed: %s", e)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            raise

    # =========================================================================
    # SyncEnv
    # =========================================================================
    async def SyncEnv(
        self, request: pb2.SyncEnvRequest, context: grpc.aio.ServicerContext
    ) -> pb2.SyncEnvResponse:
        version_id = request.version_id if request.version_id else None
        env_id = EnvManager.get_env_id(request.workflow_id, request.node_id, version_id)
        lock = self.lock_manager.get_lock(env_id)

        if lock.locked():
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details("Environment is locked")
            raise EnvLocked()

        async with lock:
            async with self._audit_context(
                request.workflow_id, request.node_id, version_id
            ) as audit:
                start_time = audit.start() if audit else datetime.now(timezone.utc)
                try:
                    logger.info(
                        "grpc:sync_env workflow_id=%s node_id=%s",
                        request.workflow_id, request.node_id,
                    )

                    if not (self.settings.envs_base_path / env_id).exists():
                        raise EnvNotFound()

                    uv_result = await self.env_manager.sync(env_id)
                    self.env_manager.touch(env_id)

                    if audit:
                        audit.success(
                            operation="sync",
                            start_time=start_time,
                            stdout=uv_result.stdout,
                            stderr=uv_result.stderr,
                            exit_code=uv_result.exit_code,
                        )

                    return pb2.SyncEnvResponse(
                        workflow_id=request.workflow_id,
                        node_id=request.node_id,
                        version_id=request.version_id,
                        status="synced",
                        stdout=uv_result.stdout,
                        stderr=uv_result.stderr,
                        exit_code=uv_result.exit_code,
                    )
                except ServiceError as e:
                    if audit:
                        audit.failed(operation="sync", start_time=start_time, error=str(e))
                    _handle_service_error(context, e)
                    raise
                except Exception as e:
                    if audit:
                        audit.failed(operation="sync", start_time=start_time, error=str(e))
                    logger.exception("SyncEnv failed: %s", e)
                    context.set_code(grpc.StatusCode.INTERNAL)
                    context.set_details(str(e))
                    raise

    # =========================================================================
    # RunCode
    # =========================================================================
    async def RunCode(
        self, request: pb2.RunCodeRequest, context: grpc.aio.ServicerContext
    ) -> pb2.RunCodeResponse:
        version_id = request.version_id if request.version_id else None
        env_id = EnvManager.get_env_id(request.workflow_id, request.node_id, version_id)
        lock = self.lock_manager.get_lock(env_id)

        if lock.locked():
            context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
            context.set_details("Environment is locked")
            raise EnvLocked()

        async with lock:
            async with self._audit_context(
                request.workflow_id, request.node_id, version_id
            ) as audit:
                start_time = audit.start() if audit else datetime.now(timezone.utc)
                try:
                    timeout = request.timeout if request.timeout > 0 else None
                    logger.info(
                        "grpc:run_code workflow_id=%s node_id=%s timeout=%s",
                        request.workflow_id, request.node_id, timeout,
                    )

                    uv_result = await self.env_manager.run(
                        env_id, request.code, timeout_seconds=timeout
                    )
                    self.env_manager.touch(env_id)

                    if audit:
                        audit.success(
                            operation="run",
                            start_time=start_time,
                            stdout=uv_result.stdout,
                            stderr=uv_result.stderr,
                            exit_code=uv_result.exit_code,
                            metadata={"timeout": timeout, "source": "grpc"},
                        )

                    return pb2.RunCodeResponse(
                        workflow_id=request.workflow_id,
                        node_id=request.node_id,
                        version_id=request.version_id,
                        stdout=uv_result.stdout,
                        stderr=uv_result.stderr,
                        exit_code=uv_result.exit_code,
                    )
                except ServiceError as e:
                    if audit:
                        audit.failed(operation="run", start_time=start_time, error=str(e))
                    _handle_service_error(context, e)
                    raise
                except Exception as e:
                    if audit:
                        audit.failed(operation="run", start_time=start_time, error=str(e))
                    logger.exception("RunCode failed: %s", e)
                    context.set_code(grpc.StatusCode.INTERNAL)
                    context.set_details(str(e))
                    raise

    # =========================================================================
    # ExportEnv
    # =========================================================================
    async def ExportEnv(
        self, request: pb2.ExportEnvRequest, context: grpc.aio.ServicerContext
    ) -> pb2.ExportEnvResponse:
        try:
            version_id = request.version_id if request.version_id else None
            env_id = EnvManager.get_env_id(request.workflow_id, request.node_id, version_id)

            logger.info(
                "grpc:export_env workflow_id=%s node_id=%s",
                request.workflow_id, request.node_id,
            )

            status = self.env_manager.get_status(env_id)
            if status.status == "NOT_EXISTS":
                raise EnvNotFound()

            pyproject_content = ""
            uv_lock_content = ""

            pyproject_path = status.env_path / "pyproject.toml"
            if pyproject_path.exists():
                pyproject_content = pyproject_path.read_text(encoding="utf-8")

            uv_lock_path = status.env_path / "uv.lock"
            if uv_lock_path.exists():
                uv_lock_content = uv_lock_path.read_text(encoding="utf-8")

            self.env_manager.touch(env_id)

            return pb2.ExportEnvResponse(
                workflow_id=request.workflow_id,
                node_id=request.node_id,
                version_id=request.version_id,
                pyproject_toml=pyproject_content,
                uv_lock=uv_lock_content,
            )
        except ServiceError as e:
            _handle_service_error(context, e)
            raise
        except Exception as e:
            logger.exception("ExportEnv failed: %s", e)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            raise

    # =========================================================================
    # Cleanup
    # =========================================================================
    async def Cleanup(
        self, request: pb2.CleanupRequest, context: grpc.aio.ServicerContext
    ) -> pb2.CleanupResponse:
        async with self._audit_context(workflow_id="_", node_id="_") as audit:
            start_time = audit.start() if audit else datetime.now(timezone.utc)
            try:
                idle_hours = (
                    request.idle_hours
                    if request.idle_hours > 0
                    else self.settings.cleanup_idle_hours
                )
                logger.info("grpc:cleanup idle_hours=%s", idle_hours)

                deleted = self.env_manager.cleanup(idle_hours=idle_hours)

                if audit:
                    audit.success(
                        operation="cleanup",
                        start_time=start_time,
                        metadata={"idle_hours": idle_hours, "deleted": deleted},
                    )

                return pb2.CleanupResponse(
                    deleted=deleted,
                    checked_at_unix=int(datetime.now(timezone.utc).timestamp()),
                )
            except Exception as e:
                if audit:
                    audit.failed(operation="cleanup", start_time=start_time, error=str(e))
                logger.exception("Cleanup failed: %s", e)
                context.set_code(grpc.StatusCode.INTERNAL)
                context.set_details(str(e))
                raise

    # =========================================================================
    # GetOperations
    # =========================================================================
    async def GetOperations(
        self, request: pb2.GetOperationsRequest, context: grpc.aio.ServicerContext
    ) -> pb2.OperationListResponse:
        try:
            from src.db_service.connection import SessionLocal
            from src.db_service.env_audit import EnvAudit

            logger.info(
                "grpc:get_operations workflow_id=%s node_id=%s",
                request.workflow_id, request.node_id,
            )

            db = SessionLocal()
            try:
                audit = EnvAudit(db=db)
                result = audit.query(
                    workflow_id=request.workflow_id,
                    node_id=request.node_id if request.node_id else None,
                    status=request.status if request.status else None,
                    limit=request.limit if request.limit > 0 else 100,
                    offset=request.offset if request.offset >= 0 else 0,
                )

                items = []
                for item in result.items:
                    items.append(
                        pb2.OperationRecord(
                            id=item.id,
                            workflow_id=item.workflow_id,
                            node_id=item.node_id,
                            version_id=item.version_id or "",
                            operation=item.operation,
                            status=item.status,
                            started_at_unix=int(item.started_at.timestamp()),
                            finished_at_unix=int(item.finished_at.timestamp()),
                            duration_ms=item.duration_ms,
                            stdout=item.stdout or "",
                            stderr=item.stderr or "",
                            exit_code=item.exit_code or 0,
                            error=item.error or "",
                            metadata_json=json.dumps(item.metadata) if item.metadata else "{}",
                        )
                    )

                return pb2.OperationListResponse(
                    total=result.total,
                    limit=result.limit,
                    offset=result.offset,
                    items=items,
                )
            finally:
                db.close()
        except Exception as e:
            logger.exception("GetOperations failed: %s", e)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            raise

    # =========================================================================
    # DeleteOperations
    # =========================================================================
    async def DeleteOperations(
        self, request: pb2.DeleteOperationsRequest, context: grpc.aio.ServicerContext
    ) -> pb2.DeleteOperationsResponse:
        try:
            from src.db_service.connection import SessionLocal
            from src.db_service.env_audit import EnvAudit

            logger.info(
                "grpc:delete_operations workflow_id=%s node_id=%s",
                request.workflow_id, request.node_id,
            )

            db = SessionLocal()
            try:
                audit = EnvAudit(db=db)
                result = audit.delete(
                    workflow_id=request.workflow_id,
                    node_id=request.node_id if request.node_id else None,
                )

                return pb2.DeleteOperationsResponse(
                    workflow_id=request.workflow_id,
                    deleted_count=result["deleted_count"],
                    node_id=request.node_id or "",
                    deleted_node_ids=result.get("deleted_node_ids", []),
                )
            finally:
                db.close()
        except Exception as e:
            logger.exception("DeleteOperations failed: %s", e)
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            raise