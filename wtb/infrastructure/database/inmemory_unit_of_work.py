"""
In-Memory Unit of Work Implementation.

For unit tests and fast iteration - no database I/O.
Provides the same interface as SQLAlchemyUnitOfWork but stores data in memory.

Benefits:
- Fast test execution (no I/O)
- Isolated per test instance (no cleanup needed)
- Same interface as SQLAlchemyUnitOfWork (LSP compliant)
"""

from typing import Any, Dict, List, Optional
from copy import deepcopy
from datetime import datetime

from wtb.domain.interfaces.unit_of_work import IUnitOfWork
from wtb.domain.interfaces.repositories import (
    IWorkflowRepository,
    IExecutionRepository,
    INodeVariantRepository,
    IBatchTestRepository,
    IEvaluationResultRepository,
    INodeBoundaryRepository,
    IOutboxRepository,
    IAuditLogRepository,
)
from wtb.domain.interfaces.file_processing_repository import (
    ICheckpointFileLinkRepository,
    IBlobRepository,
    IFileCommitRepository,
)
from wtb.domain.models.file_processing import (
    FileCommit,
    BlobId,
    CommitId,
)
from wtb.domain.models import (
    TestWorkflow,
    Execution,
    ExecutionStatus,
    NodeVariant,
    NodeBoundary,
    OutboxEvent,
    OutboxStatus,
)
from wtb.domain.models.file_processing import CheckpointFileLink, CommitId
from wtb.domain.models.batch_test import BatchTest, BatchTestStatus
from wtb.domain.models.evaluation import EvaluationResult
from wtb.domain.models.audit import AuditEntry


# ═══════════════════════════════════════════════════════════════════════════════
# In-Memory Repository Implementations
# ═══════════════════════════════════════════════════════════════════════════════


class InMemoryWorkflowRepository(IWorkflowRepository):
    """In-memory workflow repository."""
    
    def __init__(self):
        self._store: Dict[str, TestWorkflow] = {}
    
    def get(self, id: str) -> Optional[TestWorkflow]:
        workflow = self._store.get(id)
        return deepcopy(workflow) if workflow else None
    
    def list(self, limit: int = 100, offset: int = 0) -> List[TestWorkflow]:
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
    
    def find_by_name(self, name: str) -> Optional[TestWorkflow]:
        for w in self._store.values():
            if w.name == name:
                return deepcopy(w)
        return None
    
    def find_by_version(self, name: str, version: str) -> Optional[TestWorkflow]:
        for w in self._store.values():
            if w.name == name and w.version == version:
                return deepcopy(w)
        return None
    
    def list_all(self) -> List[TestWorkflow]:
        """List all workflows without pagination."""
        return [deepcopy(w) for w in self._store.values()]


class InMemoryExecutionRepository(IExecutionRepository):
    """In-memory execution repository."""
    
    def __init__(self):
        self._store: Dict[str, Execution] = {}
    
    def get(self, id: str) -> Optional[Execution]:
        execution = self._store.get(id)
        return deepcopy(execution) if execution else None
    
    def list(self, limit: int = 100, offset: int = 0) -> List[Execution]:
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
    
    def find_by_workflow(self, workflow_id: str) -> List[Execution]:
        return [deepcopy(e) for e in self._store.values() 
                if e.workflow_id == workflow_id]
    
    def find_by_status(self, status: ExecutionStatus) -> List[Execution]:
        return [deepcopy(e) for e in self._store.values() 
                if e.status == status]
    
    def find_running(self) -> List[Execution]:
        return self.find_by_status(ExecutionStatus.RUNNING)


class InMemoryNodeVariantRepository(INodeVariantRepository):
    """In-memory node variant repository."""
    
    def __init__(self):
        self._store: Dict[str, NodeVariant] = {}
    
    def get(self, id: str) -> Optional[NodeVariant]:
        variant = self._store.get(id)
        return deepcopy(variant) if variant else None
    
    def list(self, limit: int = 100, offset: int = 0) -> List[NodeVariant]:
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
    
    def find_by_workflow(self, workflow_id: str) -> List[NodeVariant]:
        return [deepcopy(v) for v in self._store.values() 
                if v.workflow_id == workflow_id]
    
    def find_by_node(self, workflow_id: str, node_id: str) -> List[NodeVariant]:
        return [deepcopy(v) for v in self._store.values() 
                if v.workflow_id == workflow_id and v.original_node_id == node_id]
    
    def find_active(self, workflow_id: str) -> List[NodeVariant]:
        return [deepcopy(v) for v in self._store.values() 
                if v.workflow_id == workflow_id and v.is_active]


class InMemoryBatchTestRepository(IBatchTestRepository):
    """In-memory batch test repository."""
    
    def __init__(self):
        self._store: Dict[str, BatchTest] = {}
    
    def get(self, id: str) -> Optional[BatchTest]:
        batch_test = self._store.get(id)
        return deepcopy(batch_test) if batch_test else None
    
    def list(self, limit: int = 100, offset: int = 0) -> List[BatchTest]:
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
    
    def find_by_workflow(self, workflow_id: str) -> List[BatchTest]:
        return [deepcopy(bt) for bt in self._store.values() 
                if bt.workflow_id == workflow_id]
    
    def find_pending(self) -> List[BatchTest]:
        return [deepcopy(bt) for bt in self._store.values() 
                if bt.status == BatchTestStatus.PENDING]


class InMemoryEvaluationResultRepository(IEvaluationResultRepository):
    """In-memory evaluation result repository."""
    
    def __init__(self):
        self._store: Dict[str, EvaluationResult] = {}
    
    def get(self, id: str) -> Optional[EvaluationResult]:
        result = self._store.get(id)
        return deepcopy(result) if result else None
    
    def list(self, limit: int = 100, offset: int = 0) -> List[EvaluationResult]:
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
    
    def find_by_execution(self, execution_id: str) -> List[EvaluationResult]:
        return [deepcopy(r) for r in self._store.values() 
                if r.execution_id == execution_id]
    
    def find_by_evaluator(self, evaluator_name: str) -> List[EvaluationResult]:
        return [deepcopy(r) for r in self._store.values() 
                if r.evaluator_name == evaluator_name]


class InMemoryNodeBoundaryRepository(INodeBoundaryRepository):
    """
    In-memory node boundary repository.
    
    Updated 2026-01-15 for DDD compliance:
    - Changed from internal_session_id to execution_id
    """
    
    def __init__(self):
        self._store: Dict[int, NodeBoundary] = {}
        self._next_id: int = 1
    
    def get(self, id: str) -> Optional[NodeBoundary]:
        boundary = self._store.get(int(id))
        return deepcopy(boundary) if boundary else None
    
    def list(self, limit: int = 100, offset: int = 0) -> List[NodeBoundary]:
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
    def find_by_execution(self, execution_id: str) -> List[NodeBoundary]:
        """Find all boundaries for an execution."""
        return [deepcopy(b) for b in self._store.values() 
                if b.execution_id == execution_id]
    
    def find_by_execution_and_node(self, execution_id: str, node_id: str) -> Optional[NodeBoundary]:
        """Find boundary for a specific node in an execution."""
        for b in self._store.values():
            if b.execution_id == execution_id and b.node_id == node_id:
                return deepcopy(b)
        return None
    
    def find_completed_by_execution(self, execution_id: str) -> List[NodeBoundary]:
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
        self._store: Dict[int, CheckpointFileLink] = {}
    
    def add(self, link: CheckpointFileLink) -> None:
        """Add a checkpoint-file link (upsert behavior)."""
        self._store[link.checkpoint_id] = deepcopy(link)
    
    def get_by_checkpoint(self, checkpoint_id: str) -> Optional[CheckpointFileLink]:
        """Get link by checkpoint ID."""
        link = self._store.get(checkpoint_id)
        return deepcopy(link) if link else None
    
    def get_by_commit(self, commit_id: CommitId) -> List[CheckpointFileLink]:
        """Get all links for a commit."""
        return [deepcopy(link) for link in self._store.values() 
                if link.commit_id.value == commit_id.value]
    
    def delete_by_checkpoint(self, checkpoint_id: str) -> bool:
        """Delete link by checkpoint ID."""
        if checkpoint_id in self._store:
            del self._store[checkpoint_id]
            return True
        return False
    
    def list_all(self, limit: int = 10000) -> List[CheckpointFileLink]:
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
    
    def list_all(self, limit: int = 100) -> List[CheckpointFileLink]:
        """List all links (extension method, not in interface)."""
        links = list(self._store.values())
        return [deepcopy(link) for link in links[:limit]]


class InMemoryOutboxRepository(IOutboxRepository):
    """In-memory outbox repository for testing."""
    
    def __init__(self):
        self._store: Dict[int, OutboxEvent] = {}
        self._next_id: int = 1
    
    def add(self, event: OutboxEvent) -> OutboxEvent:
        event.id = self._next_id
        self._next_id += 1
        self._store[event.id] = deepcopy(event)
        return event
    
    def get_by_id(self, event_id: str) -> Optional[OutboxEvent]:
        for event in self._store.values():
            if event.event_id == event_id:
                return deepcopy(event)
        return None
    
    def get_by_pk(self, id: int) -> Optional[OutboxEvent]:
        event = self._store.get(id)
        return deepcopy(event) if event else None
    
    def get_pending(self, limit: int = 100) -> List[OutboxEvent]:
        pending = [
            deepcopy(e) for e in self._store.values() 
            if e.status == OutboxStatus.PENDING
        ]
        # Sort by created_at
        pending.sort(key=lambda e: e.created_at)
        return pending[:limit]
    
    def get_failed_for_retry(self, limit: int = 50) -> List[OutboxEvent]:
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
    
    def list_all(self, limit: int = 100) -> List[OutboxEvent]:
        events = list(self._store.values())
        events.sort(key=lambda e: e.created_at, reverse=True)
        return [deepcopy(e) for e in events[:limit]]


class InMemoryAuditLogRepository(IAuditLogRepository):
    """In-memory audit log repository."""
    
    def __init__(self):
        self._store: List[AuditEntry] = []
    
    def get(self, id: str) -> Optional[AuditEntry]:
        # Not typically used for logs, but implemented for interface
        return None
    
    def list(self, limit: int = 100, offset: int = 0) -> List[AuditEntry]:
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
    
    def append_logs(self, execution_id: str, logs: List[AuditEntry]) -> None:
        for log in logs:
            if not log.execution_id:
                log.execution_id = execution_id
            self._store.append(deepcopy(log))
    
    def find_by_execution(self, execution_id: str) -> List[AuditEntry]:
        return [
            deepcopy(e) for e in self._store 
            if e.execution_id == execution_id
        ]


class InMemoryBlobRepository(IBlobRepository):
    """In-memory blob repository for testing."""
    
    def __init__(self):
        self._store: Dict[str, bytes] = {}
    
    def save(self, content: bytes) -> BlobId:
        import hashlib
        hash_hex = hashlib.sha256(content).hexdigest()
        blob_id = BlobId(hash=hash_hex)
        self._store[hash_hex] = content
        return blob_id
    
    def get(self, blob_id: BlobId) -> Optional[bytes]:
        return self._store.get(blob_id.hash)
    
    def exists(self, blob_id: BlobId) -> bool:
        return blob_id.hash in self._store
    
    def delete(self, blob_id: BlobId) -> bool:
        if blob_id.hash in self._store:
            del self._store[blob_id.hash]
            return True
        return False
    
    def restore_to_file(self, blob_id: BlobId, output_path: str) -> None:
        content = self._store.get(blob_id.hash)
        if content is None:
            raise FileNotFoundError(f"Blob not found: {blob_id.hash}")
        import os
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(content)
    
    def get_stats(self) -> Dict[str, Any]:
        total = sum(len(v) for v in self._store.values())
        return {
            "blob_count": len(self._store),
            "total_size_bytes": total,
            "total_size_mb": round(total / (1024 * 1024), 2),
        }


class InMemoryFileCommitRepository(IFileCommitRepository):
    """In-memory file commit repository for testing."""
    
    def __init__(self):
        self._store: Dict[str, FileCommit] = {}
    
    def save(self, commit: FileCommit) -> None:
        self._store[str(commit.id)] = deepcopy(commit)
    
    def get_by_id(self, commit_id: CommitId) -> Optional[FileCommit]:
        c = self._store.get(str(commit_id))
        return deepcopy(c) if c else None
    
    def get_by_id_without_mementos(self, commit_id: CommitId) -> Optional[FileCommit]:
        return self.get_by_id(commit_id)
    
    def get_all(self, limit: int = 100, offset: int = 0) -> List[FileCommit]:
        commits = sorted(
            self._store.values(),
            key=lambda c: c.timestamp if hasattr(c, "timestamp") else "",
            reverse=True,
        )
        return [deepcopy(c) for c in commits[offset:offset + limit]]
    
    def get_by_execution_id(self, execution_id: str) -> List[FileCommit]:
        return [
            deepcopy(c) for c in self._store.values()
            if getattr(c, "execution_id", None) == execution_id
        ]
    
    def get_by_checkpoint_id(self, checkpoint_id: str) -> Optional[FileCommit]:
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
            uow.commit()  # No-op, data already in memory
    
    Note: For true isolation between tests, create a new InMemoryUnitOfWork
    instance for each test.
    """
    
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
    
    def __enter__(self) -> "InMemoryUnitOfWork":
        self._in_transaction = True
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self._in_transaction = False
        # No rollback needed - in-memory doesn't have pending state
    
    def commit(self):
        """No-op for in-memory - data is already persisted in dicts."""
        pass
    
    def rollback(self):
        """No-op for in-memory - create new UoW instance for isolation."""
        pass
    
    def reset(self):
        """Reset all repositories (for testing)."""
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

