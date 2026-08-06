"""
In-Memory Unit of Work Implementation.

For unit tests and fast iteration - no database I/O.
Provides the same interface as SQLAlchemyUnitOfWork but stores data in memory.

Benefits:
- Fast test execution (no I/O)
- Isolated per test instance (no cleanup needed)
- Same interface as SQLAlchemyUnitOfWork (LSP compliant)
"""

import builtins
from copy import deepcopy
from datetime import datetime
from threading import RLock
from typing import Any

from wtb.domain.interfaces.file_processing_repository import (
    IBlobRepository,
    ICheckpointFileLinkRepository,
    IFileCommitRepository,
)
from wtb.domain.interfaces.repositories import (
    IAuditLogRepository,
    IBatchTestRepository,
    IEvaluationResultRepository,
    IExecutionRepository,
    INodeBoundaryRepository,
    INodeVariantRepository,
    IOutboxRepository,
    IWorkflowRepository,
)
from wtb.domain.interfaces.unit_of_work import IUnitOfWork
from wtb.domain.models import (
    Execution,
    ExecutionStatus,
    NodeBoundary,
    NodeVariant,
    OutboxEvent,
    OutboxStatus,
    TestWorkflow,
)
from wtb.domain.models.audit import AuditEntry
from wtb.domain.models.batch_test import BatchTest, BatchTestStatus
from wtb.domain.models.evaluation import EvaluationResult
from wtb.domain.models.file_processing import (
    BlobId,
    CheckpointFileLink,
    CommitId,
    FileCommit,
)

# ═══════════════════════════════════════════════════════════════════════════════
# In-Memory Repository Implementations
# ═══════════════════════════════════════════════════════════════════════════════


class InMemoryWorkflowRepository(IWorkflowRepository):
    """In-memory workflow repository."""
    
    def __init__(self):
        self._store: dict[str, TestWorkflow] = {}
    
    def get(self, id: str) -> TestWorkflow | None:
        workflow = self._store.get(id)
        return deepcopy(workflow) if workflow else None
    
    def list(self, limit: int = 100, offset: int = 0) -> list[TestWorkflow]:
        workflows = list(self._store.values())[offset:offset + limit]
        return [deepcopy(w) for w in workflows]
    
    def exists(self, id: str) -> bool:
        return id in self._store
    
    def add(self, entity: TestWorkflow) -> TestWorkflow:
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def update(self, entity: TestWorkflow) -> TestWorkflow:
        if entity.id not in self._store:
            raise ValueError(f"Workflow {entity.id} not found")
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._store:
            del self._store[id]
            return True
        return False
    
    def find_by_name(self, name: str) -> TestWorkflow | None:
        for w in self._store.values():
            if w.name == name:
                return deepcopy(w)
        return None
    
    def find_by_version(self, name: str, version: str) -> TestWorkflow | None:
        for w in self._store.values():
            if w.name == name and w.version == version:
                return deepcopy(w)
        return None
    
    def list_all(self) -> builtins.list[TestWorkflow]:
        """List all workflows without pagination."""
        return [deepcopy(w) for w in self._store.values()]


class InMemoryExecutionRepository(IExecutionRepository):
    """In-memory execution repository."""
    
    def __init__(self):
        self._store: dict[str, Execution] = {}
    
    def get(self, id: str) -> Execution | None:
        execution = self._store.get(id)
        return deepcopy(execution) if execution else None
    
    def list(self, limit: int = 100, offset: int = 0) -> list[Execution]:
        executions = list(self._store.values())[offset:offset + limit]
        return [deepcopy(e) for e in executions]
    
    def exists(self, id: str) -> bool:
        return id in self._store
    
    def add(self, entity: Execution) -> Execution:
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def update(self, entity: Execution) -> Execution:
        if entity.id not in self._store:
            raise ValueError(f"Execution {entity.id} not found")
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._store:
            del self._store[id]
            return True
        return False
    
    def find_by_workflow(self, workflow_id: str) -> builtins.list[Execution]:
        return [deepcopy(e) for e in self._store.values() 
                if e.workflow_id == workflow_id]
    
    def find_by_status(self, status: ExecutionStatus) -> builtins.list[Execution]:
        return [deepcopy(e) for e in self._store.values() 
                if e.status == status]
    
    def find_running(self) -> builtins.list[Execution]:
        return self.find_by_status(ExecutionStatus.RUNNING)


class InMemoryNodeVariantRepository(INodeVariantRepository):
    """In-memory node variant repository."""
    
    def __init__(self):
        self._store: dict[str, NodeVariant] = {}
    
    def get(self, id: str) -> NodeVariant | None:
        variant = self._store.get(id)
        return deepcopy(variant) if variant else None
    
    def list(self, limit: int = 100, offset: int = 0) -> list[NodeVariant]:
        variants = list(self._store.values())[offset:offset + limit]
        return [deepcopy(v) for v in variants]
    
    def exists(self, id: str) -> bool:
        return id in self._store
    
    def add(self, entity: NodeVariant) -> NodeVariant:
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def update(self, entity: NodeVariant) -> NodeVariant:
        if entity.id not in self._store:
            raise ValueError(f"NodeVariant {entity.id} not found")
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._store:
            del self._store[id]
            return True
        return False
    
    def find_by_workflow(self, workflow_id: str) -> builtins.list[NodeVariant]:
        return [deepcopy(v) for v in self._store.values() 
                if v.workflow_id == workflow_id]
    
    def find_by_node(self, workflow_id: str, node_id: str) -> builtins.list[NodeVariant]:
        return [deepcopy(v) for v in self._store.values() 
                if v.workflow_id == workflow_id and v.original_node_id == node_id]
    
    def find_active(self, workflow_id: str) -> builtins.list[NodeVariant]:
        return [deepcopy(v) for v in self._store.values() 
                if v.workflow_id == workflow_id and v.is_active]


class InMemoryBatchTestRepository(IBatchTestRepository):
    """In-memory batch test repository."""
    
    def __init__(self):
        self._store: dict[str, BatchTest] = {}
    
    def get(self, id: str) -> BatchTest | None:
        batch_test = self._store.get(id)
        return deepcopy(batch_test) if batch_test else None
    
    def list(self, limit: int = 100, offset: int = 0) -> list[BatchTest]:
        batch_tests = list(self._store.values())[offset:offset + limit]
        return [deepcopy(bt) for bt in batch_tests]
    
    def exists(self, id: str) -> bool:
        return id in self._store
    
    def add(self, entity: BatchTest) -> BatchTest:
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def update(self, entity: BatchTest) -> BatchTest:
        if entity.id not in self._store:
            raise ValueError(f"BatchTest {entity.id} not found")
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._store:
            del self._store[id]
            return True
        return False
    
    def find_by_workflow(self, workflow_id: str) -> builtins.list[BatchTest]:
        return [deepcopy(bt) for bt in self._store.values() 
                if bt.workflow_id == workflow_id]
    
    def find_pending(self) -> builtins.list[BatchTest]:
        return [deepcopy(bt) for bt in self._store.values() 
                if bt.status == BatchTestStatus.PENDING]


class InMemoryEvaluationResultRepository(IEvaluationResultRepository):
    """In-memory evaluation result repository."""
    
    def __init__(self):
        self._store: dict[str, EvaluationResult] = {}
    
    def get(self, id: str) -> EvaluationResult | None:
        result = self._store.get(id)
        return deepcopy(result) if result else None
    
    def list(self, limit: int = 100, offset: int = 0) -> list[EvaluationResult]:
        results = list(self._store.values())[offset:offset + limit]
        return [deepcopy(r) for r in results]
    
    def exists(self, id: str) -> bool:
        return id in self._store
    
    def add(self, entity: EvaluationResult) -> EvaluationResult:
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def update(self, entity: EvaluationResult) -> EvaluationResult:
        if entity.id not in self._store:
            raise ValueError(f"EvaluationResult {entity.id} not found")
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._store:
            del self._store[id]
            return True
        return False
    
    def find_by_execution(self, execution_id: str) -> builtins.list[EvaluationResult]:
        return [deepcopy(r) for r in self._store.values() 
                if r.execution_id == execution_id]
    
    def find_by_evaluator(self, evaluator_name: str) -> builtins.list[EvaluationResult]:
        return [deepcopy(r) for r in self._store.values() 
                if r.evaluator_name == evaluator_name]


class InMemoryNodeBoundaryRepository(INodeBoundaryRepository):
    """
    In-memory node boundary repository.
    
    Updated 2026-01-15 for DDD compliance:
    - Changed from internal_session_id to execution_id
    """
    
    def __init__(self):
        self._store: dict[int, NodeBoundary] = {}
        self._next_id: int = 1
    
    def get(self, id: str) -> NodeBoundary | None:
        boundary = self._store.get(int(id))
        return deepcopy(boundary) if boundary else None
    
    def list(self, limit: int = 100, offset: int = 0) -> list[NodeBoundary]:
        boundaries = list(self._store.values())[offset:offset + limit]
        return [deepcopy(b) for b in boundaries]
    
    def exists(self, id: str) -> bool:
        return int(id) in self._store
    
    def add(self, entity: NodeBoundary) -> NodeBoundary:
        entity.id = self._next_id
        self._next_id += 1
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def update(self, entity: NodeBoundary) -> NodeBoundary:
        if entity.id not in self._store:
            raise ValueError(f"NodeBoundary {entity.id} not found")
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def delete(self, id: str) -> bool:
        int_id = int(id)
        if int_id in self._store:
            del self._store[int_id]
            return True
        return False
    
    # New DDD-compliant methods (2026-01-15)
    def find_by_execution(self, execution_id: str) -> builtins.list[NodeBoundary]:
        """Find all boundaries for an execution."""
        return [deepcopy(b) for b in self._store.values() 
                if b.execution_id == execution_id]
    
    def find_by_execution_and_node(self, execution_id: str, node_id: str) -> NodeBoundary | None:
        """Find boundary for a specific node in an execution."""
        for b in self._store.values():
            if b.execution_id == execution_id and b.node_id == node_id:
                return deepcopy(b)
        return None
    
    def find_completed_by_execution(self, execution_id: str) -> builtins.list[NodeBoundary]:
        """Find completed boundaries for an execution."""
        return [deepcopy(b) for b in self._store.values()
                if b.execution_id == execution_id 
                and b.node_status == 'completed']
    
    # Legacy methods REMOVED (2026-01-27):
    # - find_by_session() → Use find_by_execution()
    # - find_by_node() → Use find_by_execution_and_node()
    # - find_completed() → Use find_completed_by_execution()


class InMemoryCheckpointFileLinkRepository(ICheckpointFileLinkRepository):
    """In-memory checkpoint file link repository (2026-01-27 consolidated)."""
    
    def __init__(self):
        self._store: dict[int, CheckpointFileLink] = {}
    
    def add(self, link: CheckpointFileLink) -> None:
        """Add a checkpoint-file link (upsert behavior)."""
        self._store[link.checkpoint_id] = deepcopy(link)
    
    def get_by_checkpoint(self, checkpoint_id: str) -> CheckpointFileLink | None:
        """Get link by checkpoint ID."""
        link = self._store.get(checkpoint_id)
        return deepcopy(link) if link else None
    
    def get_by_commit(self, commit_id: CommitId) -> list[CheckpointFileLink]:
        """Get all links for a commit."""
        return [deepcopy(link) for link in self._store.values() 
                if link.commit_id.value == commit_id.value]
    
    def delete_by_checkpoint(self, checkpoint_id: str) -> bool:
        """Delete link by checkpoint ID."""
        if checkpoint_id in self._store:
            del self._store[checkpoint_id]
            return True
        return False
    
    def list_all(self, limit: int = 10000) -> list[CheckpointFileLink]:
        """List all checkpoint file links."""
        links = list(self._store.values())
        return [deepcopy(link) for link in links[:limit]]
    
    def delete_by_commit(self, commit_id: CommitId) -> int:
        """Delete all links for a commit."""
        to_delete = [
            cp_id for cp_id, link in self._store.items()
            if link.commit_id.value == commit_id.value
        ]
        for cp_id in to_delete:
            del self._store[cp_id]
        return len(to_delete)
    
    def list_all(self, limit: int = 100) -> list[CheckpointFileLink]:
        """List all links (extension method, not in interface)."""
        links = list(self._store.values())
        return [deepcopy(link) for link in links[:limit]]


class InMemoryOutboxRepository(IOutboxRepository):
    """In-memory outbox repository for testing."""
    
    def __init__(self):
        self._store: dict[int, OutboxEvent] = {}
        self._next_id: int = 1
    
    def add(self, event: OutboxEvent) -> OutboxEvent:
        event.id = self._next_id
        self._next_id += 1
        self._store[event.id] = deepcopy(event)
        return event
    
    def get_by_id(self, event_id: str) -> OutboxEvent | None:
        for event in self._store.values():
            if event.event_id == event_id:
                return deepcopy(event)
        return None
    
    def get_by_pk(self, id: int) -> OutboxEvent | None:
        event = self._store.get(id)
        return deepcopy(event) if event else None
    
    def get_pending(self, limit: int = 100) -> list[OutboxEvent]:
        pending = [
            deepcopy(e) for e in self._store.values() 
            if e.status == OutboxStatus.PENDING
        ]
        # Sort by created_at
        pending.sort(key=lambda e: e.created_at)
        return pending[:limit]
    
    def get_failed_for_retry(self, limit: int = 50) -> list[OutboxEvent]:
        retryable = [
            deepcopy(e) for e in self._store.values()
            if e.status == OutboxStatus.FAILED and e.can_retry()
        ]
        retryable.sort(key=lambda e: e.created_at)
        return retryable[:limit]
    
    def update(self, event: OutboxEvent) -> OutboxEvent:
        if event.id not in self._store:
            raise ValueError(f"OutboxEvent with id {event.id} not found")
        self._store[event.id] = deepcopy(event)
        return event
    
    def delete_processed(self, before: datetime, limit: int = 1000) -> int:
        to_delete = []
        for id, event in self._store.items():
            if (event.status == OutboxStatus.PROCESSED 
                and event.processed_at 
                and event.processed_at < before):
                to_delete.append(id)
                if len(to_delete) >= limit:
                    break
        
        for id in to_delete:
            del self._store[id]
        return len(to_delete)
    
    def list_all(self, limit: int = 100) -> list[OutboxEvent]:
        events = list(self._store.values())
        events.sort(key=lambda e: e.created_at, reverse=True)
        return [deepcopy(e) for e in events[:limit]]


class InMemoryAuditLogRepository(IAuditLogRepository):
    """In-memory audit log repository."""
    
    def __init__(self):
        self._store: list[AuditEntry] = []
    
    def get(self, id: str) -> AuditEntry | None:
        # Not typically used for logs, but implemented for interface
        return None
    
    def list(self, limit: int = 100, offset: int = 0) -> list[AuditEntry]:
        return [deepcopy(e) for e in self._store[offset:offset + limit]]
    
    def exists(self, id: str) -> bool:
        return False
    
    def add(self, entity: AuditEntry) -> AuditEntry:
        self._store.append(deepcopy(entity))
        return entity
    
    def update(self, entity: AuditEntry) -> AuditEntry:
        raise NotImplementedError("Audit logs are immutable")
    
    def delete(self, id: str) -> bool:
        raise NotImplementedError("Audit logs are immutable")
    
    def append_logs(self, execution_id: str, logs: builtins.list[AuditEntry]) -> None:
        for log in logs:
            if not log.execution_id:
                log.execution_id = execution_id
            self._store.append(deepcopy(log))
    
    def find_by_execution(self, execution_id: str) -> builtins.list[AuditEntry]:
        return [
            deepcopy(e) for e in self._store 
            if e.execution_id == execution_id
        ]


class InMemoryBlobRepository(IBlobRepository):
    """In-memory blob repository for testing."""
    
    def __init__(self):
        self._store: dict[str, bytes] = {}
    
    def save(self, content: bytes) -> BlobId:
        blob_id = BlobId.from_content(content)
        self._store[blob_id.value] = content
        return blob_id
    
    def get(self, blob_id: BlobId) -> bytes | None:
        return self._store.get(blob_id.value)
    
    def exists(self, blob_id: BlobId) -> bool:
        return blob_id.value in self._store
    
    def delete(self, blob_id: BlobId) -> bool:
        if blob_id.value in self._store:
            del self._store[blob_id.value]
            return True
        return False
    
    def restore_to_file(self, blob_id: BlobId, output_path: str) -> None:
        content = self._store.get(blob_id.value)
        if content is None:
            raise FileNotFoundError(f"Blob not found: {blob_id.value}")
        import os
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(content)
    
    def get_stats(self) -> dict[str, Any]:
        total = sum(len(v) for v in self._store.values())
        return {
            "blob_count": len(self._store),
            "total_size_bytes": total,
            "total_size_mb": round(total / (1024 * 1024), 2),
        }


class InMemoryFileCommitRepository(IFileCommitRepository):
    """In-memory file commit repository for testing."""
    
    def __init__(self):
        self._store: dict[str, FileCommit] = {}
    
    def save(self, commit: FileCommit) -> None:
        self._store[str(commit.id)] = deepcopy(commit)
    
    def get_by_id(self, commit_id: CommitId) -> FileCommit | None:
        c = self._store.get(str(commit_id))
        return deepcopy(c) if c else None
    
    def get_by_id_without_mementos(self, commit_id: CommitId) -> FileCommit | None:
        return self.get_by_id(commit_id)
    
    def get_all(self, limit: int = 100, offset: int = 0) -> list[FileCommit]:
        commits = sorted(
            self._store.values(),
            key=lambda c: c.timestamp if hasattr(c, "timestamp") else "",
            reverse=True,
        )
        return [deepcopy(c) for c in commits[offset:offset + limit]]
    
    def get_by_execution_id(self, execution_id: str) -> list[FileCommit]:
        return [
            deepcopy(c) for c in self._store.values()
            if getattr(c, "execution_id", None) == execution_id
        ]
    
    def get_by_checkpoint_id(self, checkpoint_id: str) -> FileCommit | None:
        for c in self._store.values():
            if getattr(c, "checkpoint_id", None) == checkpoint_id:
                return deepcopy(c)
        return None
    
    def delete(self, commit_id: CommitId) -> bool:
        key = str(commit_id)
        if key in self._store:
            del self._store[key]
            return True
        return False
    
    def count(self) -> int:
        return len(self._store)


# ═══════════════════════════════════════════════════════════════════════════════
# In-Memory Unit of Work
# ═══════════════════════════════════════════════════════════════════════════════


class InMemoryUnitOfWork(IUnitOfWork):
    """
    In-memory Unit of Work for testing.
    
    Benefits:
    - No database I/O (extremely fast tests)
    - Isolated per test instance (no cleanup needed)
    - Same interface as SQLAlchemyUnitOfWork (LSP compliant)
    
    Usage:
        uow = InMemoryUnitOfWork()
        with uow:
            uow.workflows.add(workflow)
            uow.executions.add(execution)
            uow.commit()  # Establish a new rollback baseline
    
    Note: For true isolation between tests, create a new InMemoryUnitOfWork
    instance for each test.
    """

    _REPOSITORY_NAMES = (
        "workflows",
        "executions",
        "variants",
        "batch_tests",
        "evaluation_results",
        "node_boundaries",
        "checkpoint_file_links",
        "outbox",
        "audit_logs",
        "blobs",
        "file_commits",
    )

    def __init__(self):
        # Initialize all repositories
        self.workflows: IWorkflowRepository = InMemoryWorkflowRepository()
        self.executions: IExecutionRepository = InMemoryExecutionRepository()
        self.variants: INodeVariantRepository = InMemoryNodeVariantRepository()
        self.batch_tests: IBatchTestRepository = InMemoryBatchTestRepository()
        self.evaluation_results: IEvaluationResultRepository = InMemoryEvaluationResultRepository()
        self.node_boundaries: INodeBoundaryRepository = InMemoryNodeBoundaryRepository()
        self.checkpoint_file_links: ICheckpointFileLinkRepository = InMemoryCheckpointFileLinkRepository()
        self.outbox: IOutboxRepository = InMemoryOutboxRepository()
        self.audit_logs: IAuditLogRepository = InMemoryAuditLogRepository()
        self.blobs: IBlobRepository = InMemoryBlobRepository()
        self.file_commits: IFileCommitRepository = InMemoryFileCommitRepository()
        
        self._in_transaction = False
        self._transaction_lock = RLock()
        self._transaction_depth = 0
        self._committed_state = self._capture_repository_state()

    def _capture_repository_state(self) -> dict[str, Any]:
        """Copy the atomically committed state of every repository."""
        return {
            name: deepcopy(getattr(getattr(self, name), "_store"))
            for name in self._REPOSITORY_NAMES
        }

    def _restore_repository_state(self, snapshot: dict[str, Any]) -> None:
        """Restore contents while preserving injected repository objects."""
        for name in self._REPOSITORY_NAMES:
            repository = getattr(self, name)
            repository._store = deepcopy(snapshot[name])

    def __enter__(self) -> "InMemoryUnitOfWork":
        # Repository mutations are immediate, so the lock must span the whole
        # transaction. Locking commit/rollback alone can snapshot another
        # thread's uncommitted rows.
        self._transaction_lock.acquire()
        self._transaction_depth += 1
        self._in_transaction = True
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._transaction_depth <= 0:
            self._in_transaction = False
            return False

        try:
            if exc_type is not None or self._transaction_depth == 1:
                self.rollback()
        finally:
            self._transaction_depth -= 1
            self._in_transaction = self._transaction_depth > 0
            self._transaction_lock.release()
        return False
    
    def commit(self):
        """Atomically advance the rollback baseline to current repository state."""
        with self._transaction_lock:
            self._committed_state = self._capture_repository_state()
    
    def rollback(self):
        """Restore all repositories to the last successful commit."""
        with self._transaction_lock:
            self._restore_repository_state(self._committed_state)
    
    def reset(self):
        """Atomically reset repositories without crossing a transaction."""
        with self._transaction_lock:
            self.workflows = InMemoryWorkflowRepository()
            self.executions = InMemoryExecutionRepository()
            self.variants = InMemoryNodeVariantRepository()
            self.batch_tests = InMemoryBatchTestRepository()
            self.evaluation_results = InMemoryEvaluationResultRepository()
            self.node_boundaries = InMemoryNodeBoundaryRepository()
            self.checkpoint_file_links = InMemoryCheckpointFileLinkRepository()
            self.outbox = InMemoryOutboxRepository()
            self.audit_logs = InMemoryAuditLogRepository()
            self.blobs = InMemoryBlobRepository()
            self.file_commits = InMemoryFileCommitRepository()
            self._committed_state = self._capture_repository_state()
            if self._transaction_depth == 0:
                self._in_transaction = False

