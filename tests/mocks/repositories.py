"""
Mock Repository Implementations.

These mocks implement the REAL interfaces from wtb.domain.interfaces.repositories,
ensuring that tests written with mocks will also work with real implementations.

Key Design Principles:
1. Type Compatibility: Accept and return real domain types
2. Interface Compliance: Implement all methods from interfaces
3. Thread Safety: Support concurrent access for parallel tests
4. Isolation: Each instance is independent

Usage:
    from tests.mocks import MockOutboxRepository
    from wtb.domain.models.outbox import OutboxEvent, OutboxEventType
    
    repo = MockOutboxRepository()
    event = OutboxEvent.create(
        event_type=OutboxEventType.CHECKPOINT_CREATE,
        aggregate_id="exec-1",
        payload={"key": "value"},
    )
    repo.add(event)  # Works with REAL OutboxEvent type

Updated: 2026-01-28
"""

import threading
import hashlib
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

# Import REAL domain types and interfaces
from wtb.domain.models.outbox import OutboxEvent, OutboxStatus
from wtb.domain.interfaces.repositories import (
    IOutboxRepository,
    IExecutionRepository,
    IWorkflowRepository,
    INodeVariantRepository,
)


class MockOutboxRepository(IOutboxRepository):
    """
    Mock implementation of IOutboxRepository.
    
    Accepts and returns REAL OutboxEvent domain objects.
    Thread-safe for parallel test execution.
    
    Example:
        repo = MockOutboxRepository()
        event = OutboxEvent.create(
            event_type=OutboxEventType.CHECKPOINT_CREATE,
            aggregate_id="exec-1",
            payload={}
        )
        saved = repo.add(event)
        assert saved.id is not None
    """
    
    def __init__(self):
        self._events: Dict[str, OutboxEvent] = {}  # keyed by event_id
        self._events_by_pk: Dict[int, OutboxEvent] = {}  # keyed by id (pk)
        self._lock = threading.Lock()
        self._pk_counter = 0
    
    def add(self, event: OutboxEvent) -> OutboxEvent:
        """Add a new outbox event."""
        with self._lock:
            self._pk_counter += 1
            # Assign database ID if not set
            if event.id is None:
                event.id = self._pk_counter
            
            self._events[event.event_id] = event
            self._events_by_pk[event.id] = event
            return event
    
    def get_by_id(self, event_id: str) -> Optional[OutboxEvent]:
        """Get event by event_id (UUID)."""
        return self._events.get(event_id)
    
    def get_by_pk(self, id: int) -> Optional[OutboxEvent]:
        """Get event by database primary key."""
        return self._events_by_pk.get(id)
    
    def get_pending(self, limit: int = 100) -> List[OutboxEvent]:
        """Get pending events for processing, ordered by created_at."""
        pending = [
            e for e in self._events.values() 
            if e.status == OutboxStatus.PENDING
        ]
        pending.sort(key=lambda e: e.created_at)
        return pending[:limit]
    
    def get_failed_for_retry(self, limit: int = 50) -> List[OutboxEvent]:
        """Get failed events that can be retried."""
        retryable = [
            e for e in self._events.values() 
            if e.status == OutboxStatus.FAILED and e.can_retry()
        ]
        retryable.sort(key=lambda e: e.created_at)
        return retryable[:limit]
    
    def update(self, event: OutboxEvent) -> OutboxEvent:
        """Update event status."""
        with self._lock:
            self._events[event.event_id] = event
            if event.id is not None:
                self._events_by_pk[event.id] = event
            return event
    
    def delete_processed(self, before: datetime, limit: int = 1000) -> int:
        """Delete processed events older than a given time."""
        with self._lock:
            to_delete = [
                e.event_id for e in self._events.values()
                if e.status == OutboxStatus.PROCESSED 
                and e.processed_at 
                and e.processed_at < before
            ][:limit]
            
            for event_id in to_delete:
                event = self._events.pop(event_id, None)
                if event and event.id:
                    self._events_by_pk.pop(event.id, None)
            
            return len(to_delete)
    
    def list_all(self, limit: int = 100) -> List[OutboxEvent]:
        """List all events (for admin/debugging)."""
        events = list(self._events.values())
        events.sort(key=lambda e: e.created_at)
        return events[:limit]
    
    # Additional test helper methods
    def get_processed(self) -> List[OutboxEvent]:
        """Get all processed events (test helper)."""
        return [
            e for e in self._events.values() 
            if e.status == OutboxStatus.PROCESSED
        ]
    
    def get_failed(self) -> List[OutboxEvent]:
        """Get all failed events (test helper)."""
        return [
            e for e in self._events.values() 
            if e.status == OutboxStatus.FAILED
        ]
    
    def clear(self) -> None:
        """Clear all events (test cleanup helper)."""
        with self._lock:
            self._events.clear()
            self._events_by_pk.clear()
            self._pk_counter = 0


@dataclass
class MockCheckpoint:
    """
    Mock checkpoint for testing.
    
    Note: This is NOT a domain object - it's a simplified mock
    for LangGraph checkpoint testing. For real checkpoint testing,
    use the actual LangGraph checkpointer.
    """
    checkpoint_id: int
    thread_id: str
    step: int
    state: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


class MockCheckpointRepository:
    """
    Mock checkpoint repository for testing.
    
    Note: LangGraph uses its own checkpointer interface.
    This mock is for testing checkpoint-related logic,
    not for replacing the actual LangGraph checkpointer.
    """
    
    def __init__(self):
        self._checkpoints: Dict[int, MockCheckpoint] = {}
        self._lock = threading.Lock()
    
    def add_checkpoint(
        self, 
        checkpoint_id: int, 
        thread_id: str = "test",
        step: int = 0,
        state: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MockCheckpoint:
        """Add a checkpoint."""
        with self._lock:
            checkpoint = MockCheckpoint(
                checkpoint_id=checkpoint_id,
                thread_id=thread_id,
                step=step,
                state=state or {},
                metadata=metadata or {},
            )
            self._checkpoints[checkpoint_id] = checkpoint
            return checkpoint
    
    def get_by_id(self, checkpoint_id: int) -> Optional[MockCheckpoint]:
        """Get checkpoint by ID."""
        return self._checkpoints.get(checkpoint_id)
    
    def list_by_thread(self, thread_id: str) -> List[MockCheckpoint]:
        """List checkpoints by thread ID, ordered by step."""
        return sorted(
            [c for c in self._checkpoints.values() if c.thread_id == thread_id],
            key=lambda c: c.step,
        )
    
    def list_all(self) -> List[MockCheckpoint]:
        """List all checkpoints."""
        return list(self._checkpoints.values())
    
    def clear(self) -> None:
        """Clear all checkpoints."""
        with self._lock:
            self._checkpoints.clear()


@dataclass
class MockMemento:
    """
    Mock file memento.
    
    Represents a file snapshot for testing file tracking.
    """
    file_path: str
    file_hash: str
    file_size: int = 1024
    
    @property
    def _file_path(self) -> str:
        return self.file_path
    
    @property
    def _file_hash(self) -> str:
        return self.file_hash
    
    @property
    def _file_size(self) -> int:
        return self.file_size


@dataclass
class MockCommit:
    """
    Mock file commit.
    
    Represents a commit of file changes for testing file tracking.
    """
    commit_id: str
    mementos: List[MockMemento] = field(default_factory=list)
    execution_id: Optional[str] = None
    message: str = "test commit"
    created_at: datetime = field(default_factory=datetime.now)
    
    @property
    def _mementos(self) -> List[MockMemento]:
        return self.mementos
    
    @property
    def file_count(self) -> int:
        return len(self.mementos)


class MockCommitRepository:
    """
    Mock FileTracker commit repository.
    
    Thread-safe for parallel test execution.
    """
    
    def __init__(self):
        self._commits: Dict[str, MockCommit] = {}
        self._lock = threading.Lock()
    
    def add_commit(
        self, 
        commit_id: str, 
        mementos: Optional[List[MockMemento]] = None,
        execution_id: Optional[str] = None,
        message: str = "test commit",
    ) -> MockCommit:
        """Add a commit."""
        with self._lock:
            commit = MockCommit(
                commit_id=commit_id,
                mementos=mementos or [],
                execution_id=execution_id,
                message=message,
            )
            self._commits[commit_id] = commit
            return commit
    
    def get_by_id(self, commit_id: str) -> Optional[MockCommit]:
        """Get commit by ID."""
        return self._commits.get(commit_id)
    
    def find_by_id(self, commit_id: str) -> Optional[MockCommit]:
        """Alias for get_by_id (compatibility)."""
        return self.get_by_id(commit_id)
    
    def list_by_execution(self, execution_id: str) -> List[MockCommit]:
        """List commits by execution ID."""
        return [c for c in self._commits.values() if c.execution_id == execution_id]
    
    def save(self, commit: MockCommit) -> MockCommit:
        """Save a commit."""
        with self._lock:
            self._commits[commit.commit_id] = commit
            return commit
    
    def delete(self, commit_id: str) -> bool:
        """Delete a commit."""
        with self._lock:
            if commit_id in self._commits:
                del self._commits[commit_id]
                return True
            return False
    
    def list_all(self) -> List[MockCommit]:
        """List all commits."""
        return list(self._commits.values())
    
    def clear(self) -> None:
        """Clear all commits."""
        with self._lock:
            self._commits.clear()


class MockBlobRepository:
    """
    Mock FileTracker blob repository.
    
    Thread-safe for parallel test execution.
    """
    
    def __init__(self):
        self._blobs: Dict[str, bytes] = {}
        self._lock = threading.Lock()
    
    def add_blob(self, content_hash: str, content: bytes = b"test") -> str:
        """Add a blob by hash."""
        with self._lock:
            self._blobs[content_hash] = content
            return content_hash
    
    def save(self, content: bytes) -> str:
        """Save content and return its hash."""
        content_hash = hashlib.sha256(content).hexdigest()
        return self.add_blob(content_hash, content)
    
    def exists(self, content_hash: str) -> bool:
        """Check if blob exists."""
        # Handle wrapped types
        if hasattr(content_hash, 'value'):
            content_hash = content_hash.value
        return content_hash in self._blobs
    
    def get(self, content_hash: str) -> Optional[bytes]:
        """Get blob content."""
        if hasattr(content_hash, 'value'):
            content_hash = content_hash.value
        return self._blobs.get(content_hash)
    
    def delete(self, content_hash: str) -> bool:
        """Delete a blob."""
        with self._lock:
            if content_hash in self._blobs:
                del self._blobs[content_hash]
                return True
            return False
    
    def list_all(self) -> List[str]:
        """List all blob hashes."""
        return list(self._blobs.keys())
    
    def clear(self) -> None:
        """Clear all blobs."""
        with self._lock:
            self._blobs.clear()


class MockExecutionRepository(IExecutionRepository):
    """
    Mock implementation of IExecutionRepository.
    
    Note: For full interface compliance, implements all required methods.
    """
    
    def __init__(self):
        self._executions: Dict[str, Any] = {}
        self._lock = threading.Lock()
    
    def get(self, id: str) -> Optional[Any]:
        """Get execution by ID."""
        return self._executions.get(id)
    
    def list(self, limit: int = 100, offset: int = 0) -> List[Any]:
        """List executions with pagination."""
        all_executions = list(self._executions.values())
        return all_executions[offset:offset + limit]
    
    def exists(self, id: str) -> bool:
        """Check if execution exists."""
        return id in self._executions
    
    def add(self, entity: Any) -> Any:
        """Add an execution."""
        with self._lock:
            self._executions[entity.id] = entity
            return entity
    
    def update(self, entity: Any) -> Any:
        """Update an execution."""
        with self._lock:
            self._executions[entity.id] = entity
            return entity
    
    def delete(self, id: str) -> bool:
        """Delete an execution."""
        with self._lock:
            if id in self._executions:
                del self._executions[id]
                return True
            return False
    
    def find_by_workflow(self, workflow_id: str) -> List[Any]:
        """Find executions by workflow ID."""
        return [
            e for e in self._executions.values() 
            if hasattr(e, 'workflow_id') and e.workflow_id == workflow_id
        ]
    
    def find_by_status(self, status: Any) -> List[Any]:
        """Find executions by status."""
        return [
            e for e in self._executions.values() 
            if hasattr(e, 'status') and e.status == status
        ]
    
    def find_running(self) -> List[Any]:
        """Find running executions."""
        from wtb.domain.models.workflow import ExecutionStatus
        return self.find_by_status(ExecutionStatus.RUNNING)
    
    def clear(self) -> None:
        """Clear all executions."""
        with self._lock:
            self._executions.clear()


class MockWorkflowRepository(IWorkflowRepository):
    """
    Mock implementation of IWorkflowRepository.
    """
    
    def __init__(self):
        self._workflows: Dict[str, Any] = {}
        self._lock = threading.Lock()
    
    def get(self, id: str) -> Optional[Any]:
        """Get workflow by ID."""
        return self._workflows.get(id)
    
    def list(self, limit: int = 100, offset: int = 0) -> List[Any]:
        """List workflows with pagination."""
        all_workflows = list(self._workflows.values())
        return all_workflows[offset:offset + limit]
    
    def exists(self, id: str) -> bool:
        """Check if workflow exists."""
        return id in self._workflows
    
    def add(self, entity: Any) -> Any:
        """Add a workflow."""
        with self._lock:
            self._workflows[entity.id] = entity
            return entity
    
    def update(self, entity: Any) -> Any:
        """Update a workflow."""
        with self._lock:
            self._workflows[entity.id] = entity
            return entity
    
    def delete(self, id: str) -> bool:
        """Delete a workflow."""
        with self._lock:
            if id in self._workflows:
                del self._workflows[id]
                return True
            return False
    
    def find_by_name(self, name: str) -> Optional[Any]:
        """Find workflow by name."""
        for wf in self._workflows.values():
            if hasattr(wf, 'name') and wf.name == name:
                return wf
        return None
    
    def find_by_version(self, name: str, version: str) -> Optional[Any]:
        """Find workflow by name and version."""
        for wf in self._workflows.values():
            if (hasattr(wf, 'name') and wf.name == name and 
                hasattr(wf, 'version') and wf.version == version):
                return wf
        return None
    
    def list_all(self) -> List[Any]:
        """List all workflows."""
        return list(self._workflows.values())
    
    def clear(self) -> None:
        """Clear all workflows."""
        with self._lock:
            self._workflows.clear()


class MockNodeVariantRepository(INodeVariantRepository):
    """
    Mock implementation of INodeVariantRepository.
    """
    
    def __init__(self):
        self._variants: Dict[str, Any] = {}
        self._lock = threading.Lock()
    
    def get(self, id: str) -> Optional[Any]:
        """Get variant by ID."""
        return self._variants.get(id)
    
    def list(self, limit: int = 100, offset: int = 0) -> List[Any]:
        """List variants with pagination."""
        all_variants = list(self._variants.values())
        return all_variants[offset:offset + limit]
    
    def exists(self, id: str) -> bool:
        """Check if variant exists."""
        return id in self._variants
    
    def add(self, entity: Any) -> Any:
        """Add a variant."""
        with self._lock:
            self._variants[entity.id] = entity
            return entity
    
    def update(self, entity: Any) -> Any:
        """Update a variant."""
        with self._lock:
            self._variants[entity.id] = entity
            return entity
    
    def delete(self, id: str) -> bool:
        """Delete a variant."""
        with self._lock:
            if id in self._variants:
                del self._variants[id]
                return True
            return False
    
    def find_by_workflow(self, workflow_id: str) -> List[Any]:
        """Find variants by workflow ID."""
        return [
            v for v in self._variants.values() 
            if hasattr(v, 'workflow_id') and v.workflow_id == workflow_id
        ]
    
    def find_by_node(self, workflow_id: str, node_id: str) -> List[Any]:
        """Find variants by workflow and node ID."""
        return [
            v for v in self._variants.values() 
            if (hasattr(v, 'workflow_id') and v.workflow_id == workflow_id and
                hasattr(v, 'node_id') and v.node_id == node_id)
        ]
    
    def find_active(self, workflow_id: str) -> List[Any]:
        """Find active variants by workflow ID."""
        return [
            v for v in self._variants.values() 
            if (hasattr(v, 'workflow_id') and v.workflow_id == workflow_id and
                hasattr(v, 'is_active') and v.is_active)
        ]
    
    def clear(self) -> None:
        """Clear all variants."""
        with self._lock:
            self._variants.clear()
