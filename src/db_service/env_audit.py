"""
High-level helper for recording environment operations.

This module provides a minimal, non-intrusive way to persist
environment-related actions into the database.

One call = one row in env_operations table.
"""

from __future__ import annotations

import time
from typing import Any
from sqlalchemy.orm import Session
from src.db_service.env_repository import EnvOperationRepository
from src.db_service.models import EnvOperation
from src.models import *

class EnvAudit:
    """Helper class to record and query env operations."""

    def __init__(
        self,
        *,
        db: Session,
        workflow_id: str,
        node_id: str,
        version_id: str | None = None,
    ) -> None:
        self._db = db
        self._repo = EnvOperationRepository(db)
        self._workflow_id = workflow_id
        self._node_id = node_id
        self._version_id = version_id

    # ========================================================================
    # Timing helpers
    # ========================================================================

    @staticmethod
    def start() -> float:
        return time.time()

    @staticmethod
    def _duration_ms(start_time: float) -> float:
        return time.time() - start_time

    # ------------------------------------------------------------------
    # Record helpers
    # ------------------------------------------------------------------

    def success(
        self,
        *,
        operation: str,
        start_time: float,
        stdout: str | None = None,
        stderr: str | None = None,
        exit_code: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._repo.create(
            workflow_id=self._workflow_id,
            node_id=self._node_id,
            version_id=self._version_id,
            operation=operation,
            status="success",
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_ms=self._duration_ms(start_time),
            metadata=metadata,
        )

    def failed(
        self,
        *,
        operation: str,
        start_time: float,
        error: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        meta: dict[str, Any] = dict(metadata or {})
        meta["error"] = error
        self._repo.create(
            workflow_id=self._workflow_id,
            node_id=self._node_id,
            version_id=self._version_id,
            operation=operation,
            status="failed",
            stderr=error,
            duration_ms=self._duration_ms(start_time),
            metadata=meta,
        )

    # ========================================================================
    # Query Methods
    # ========================================================================

    def query(
            self,
            *,
            workflow_id: str | None = None,
            node_id: str | None = None,
            status: str | None = None,
            limit: int | None = None,
            offset: int | None = None,
    ) -> EnvOperationListResponse:
        """
        通用查询方法，所有参数均为可选。

        Args:
            workflow_id: 工作流ID
            node_id: 节点ID
            operation: 操作类型 (create_env/add_deps/run/sync/cleanup)
            status: 状态 (success/failed)
            limit: 返回数量限制
            offset: 分页偏移
        """
        query = self._db.query(EnvOperation)

        if workflow_id:
            query = query.filter(EnvOperation.workflow_id == workflow_id)
        if node_id:
            query = query.filter(EnvOperation.node_id == node_id)
        if status:
            query = query.filter(EnvOperation.status == status)

        # 获取总数（分页前）
        total = query.count()

        # 排序
        query = query.order_by(EnvOperation.created_at.desc())


        items = query.all()

        return EnvOperationListResponse(
            total=total,
            items=[self._to_response(item) for item in items],
        )

    # ========================================================================
    # Delete Methods
    # ========================================================================

    def delete(
            self,
            *,
            workflow_id: str | None = None,
            node_id: str | None = None,
    ) -> dict:
        """
        通用删除方法。

        Args:
            workflow_id: 工作流ID（可选）
            node_id: 节点ID（可选）

        Returns:
            删除信息：deleted_count, deleted_node_ids
        """
        query = self._db.query(EnvOperation)

        if workflow_id:
            query = query.filter(EnvOperation.workflow_id == workflow_id)
        if node_id:
            query = query.filter(EnvOperation.node_id == node_id)

        # 先查询要删除的记录，获取 node_id 列表
        records = query.all()
        deleted_node_ids = list(set(record.node_id for record in records))
        deleted_count = len(records)

        # 再执行删除
        if records:
            query.delete(synchronize_session=False)
            self._db.commit()

        return {
            "deleted_count": deleted_count,
            "deleted_node_ids": deleted_node_ids,
        }

    # ========================================================================
    # Helper Methods
    # ========================================================================

    @staticmethod
    def _to_response(record: EnvOperation) -> EnvOperationResponse:
        return EnvOperationResponse(
            id=record.id,
            workflow_id=record.workflow_id,
            node_id=record.node_id,
            operation=record.operation,
            status=record.status,
            stdout=record.stdout,
            stderr=record.stderr,
            exit_code=record.exit_code,
            duration_ms=record.duration_ms,
            metadata=record.operation_metadata,
            created_at=record.created_at,
        )