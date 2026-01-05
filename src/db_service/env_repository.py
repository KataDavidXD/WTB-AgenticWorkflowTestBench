"""Repository for environment operation records."""

from typing import Any
from sqlalchemy.orm import Session
from src.db_service.models import EnvOperation


class EnvOperationRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        workflow_id: str,
        node_id: str,
        version_id: str | None = None,
        operation: str,
        status: str,
        stdout: str | None = None,
        stderr: str | None = None,
        exit_code: int | None = None,
        duration_ms: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EnvOperation:
        record = EnvOperation(
            workflow_id=workflow_id,
            node_id=node_id,
            version_id=version_id,
            operation=operation,
            status=status,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_ms=duration_ms,
            operation_metadata=metadata,
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def get_by_workflow_id(self, workflow_id: str) -> list[EnvOperation]:
        return (
            self.db.query(EnvOperation)
            .filter(EnvOperation.workflow_id == workflow_id)
            .order_by(EnvOperation.created_at.desc())
            .all()
        )

    def get_by_node_id(self, node_id: str) -> list[EnvOperation]:
        return (
            self.db.query(EnvOperation)
            .filter(EnvOperation.node_id == node_id)
            .order_by(EnvOperation.created_at.desc())
            .all()
        )

    def get_by_workflow_node_version(
        self, workflow_id: str, node_id: str, version_id: str | None = None
    ) -> list[EnvOperation]:
        query = self.db.query(EnvOperation).filter(
            EnvOperation.workflow_id == workflow_id,
            EnvOperation.node_id == node_id,
        )
        if version_id:
            query = query.filter(EnvOperation.version_id == version_id)
        else:
            query = query.filter(EnvOperation.version_id.is_(None))

        return query.order_by(EnvOperation.created_at.desc()).all()

    def get_failed_operations(
        self, workflow_id: str | None = None
    ) -> list[EnvOperation]:
        query = self.db.query(EnvOperation).filter(EnvOperation.status == "failed")
        if workflow_id:
            query = query.filter(EnvOperation.workflow_id == workflow_id)
        return query.order_by(EnvOperation.created_at.desc()).all()

    def delete_by_workflow_id(self, workflow_id: str) -> int:
        count = (
            self.db.query(EnvOperation)
            .filter(EnvOperation.workflow_id == workflow_id)
            .delete()
        )
        self.db.commit()
        return count

    def delete_by_workflow_and_node(self, workflow_id: str, node_id: str) -> int:
        count = (
            self.db.query(EnvOperation)
            .filter(
                EnvOperation.workflow_id == workflow_id,
                EnvOperation.node_id == node_id,
            )
            .delete()
        )
        self.db.commit()
        return count