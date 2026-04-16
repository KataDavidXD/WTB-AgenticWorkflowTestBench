"""
Shared helpers and state definitions for Outbox Transaction Consistency tests.

Provides:
- State type definitions for various test scenarios
- Helper functions for state creation
- Node functions for graph definitions
- Mock implementations for isolated testing
- Utility functions for verification

Design Principles:
- DRY: Shared code across test modules
- SOLID: Single responsibility per helper
- Type-Safe: Full type annotations
"""

import operator
import uuid
import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import (
    TypedDict, 
    Annotated, 
    Dict, 
    Any, 
    List, 
    Optional, 
    Callable,
    Tuple,
)
from enum import Enum


# ═══════════════════════════════════════════════════════════════════════════════
# State Definitions
# ═══════════════════════════════════════════════════════════════════════════════


class SimpleState(TypedDict):
    """Simple state for basic tests."""
    messages: Annotated[list, operator.add]
    count: int


class TransactionState(TypedDict):
    """State for transaction consistency tests."""
    messages: Annotated[list, operator.add]
    count: int
    execution_id: str
    checkpoint_ids: Annotated[list, operator.add]
    outbox_event_ids: Annotated[list, operator.add]
    transaction_log: Annotated[list, operator.add]


class FileTrackingState(TypedDict):
    """State for file tracking integration tests."""
    messages: Annotated[list, operator.add]
    count: int
    files_processed: Annotated[list, operator.add]
    commit_ids: Annotated[list, operator.add]
    blob_hashes: Annotated[list, operator.add]


class PauseResumeState(TypedDict):
    """State for pause/resume tests."""
    messages: Annotated[list, operator.add]
    count: int
    execution_id: str
    pause_checkpoint_id: Optional[int]
    resume_checkpoint_id: Optional[int]
    paused_at: Optional[str]
    resumed_at: Optional[str]
    pause_strategy: str


class BranchState(TypedDict):
    """State for branching tests."""
    messages: Annotated[list, operator.add]
    count: int
    path: Annotated[list, operator.add]
    switch: bool
    branch_id: Optional[str]
    parent_checkpoint_id: Optional[int]


class RollbackState(TypedDict):
    """State for rollback tests."""
    messages: Annotated[list, operator.add]
    count: int
    execution_id: str
    checkpoint_ids: Annotated[list, operator.add]
    rollback_target_id: Optional[int]
    rollback_completed: bool
    files_restored: Annotated[list, operator.add]


class BatchTestState(TypedDict):
    """State for batch testing."""
    messages: Annotated[list, operator.add]
    count: int
    batch_test_id: str
    variant_name: str
    variant_config: Dict[str, Any]
    execution_result: Optional[str]


class ParallelExecutionState(TypedDict):
    """State for parallel execution tests."""
    results: Annotated[list, operator.add]
    node_order: Annotated[list, operator.add]
    parallel_count: int
    worker_id: str
    execution_times: Annotated[list, operator.add]


class VenvState(TypedDict):
    """State for venv integration tests."""
    messages: Annotated[list, operator.add]
    count: int
    venv_id: str
    venv_path: str
    packages_installed: Annotated[list, operator.add]
    venv_status: str


class FullIntegrationState(TypedDict):
    """State for full integration tests (Ray + venv + LangGraph + FileTracker)."""
    messages: Annotated[list, operator.add]
    count: int
    execution_id: str
    batch_test_id: Optional[str]
    # LangGraph checkpoints
    checkpoint_ids: Annotated[list, operator.add]
    # FileTracker
    commit_ids: Annotated[list, operator.add]
    files_tracked: Annotated[list, operator.add]
    # Ray
    actor_id: Optional[str]
    worker_id: Optional[str]
    # Venv
    venv_id: Optional[str]
    venv_status: str
    # Transaction tracking
    outbox_events: Annotated[list, operator.add]
    transaction_status: str


# ═══════════════════════════════════════════════════════════════════════════════
# State Factory Functions
# ═══════════════════════════════════════════════════════════════════════════════


def create_simple_state() -> SimpleState:
    """Create initial simple state."""
    return {"messages": [], "count": 0}


def create_transaction_state(execution_id: Optional[str] = None) -> TransactionState:
    """Create initial transaction state."""
    return {
        "messages": [],
        "count": 0,
        "execution_id": execution_id or f"exec-{uuid.uuid4().hex[:8]}",
        "checkpoint_ids": [],
        "outbox_event_ids": [],
        "transaction_log": [],
    }


def create_file_tracking_state() -> FileTrackingState:
    """Create initial file tracking state."""
    return {
        "messages": [],
        "count": 0,
        "files_processed": [],
        "commit_ids": [],
        "blob_hashes": [],
    }


def create_pause_resume_state(
    execution_id: Optional[str] = None,
    pause_strategy: str = "WARM",
) -> PauseResumeState:
    """Create initial pause/resume state."""
    return {
        "messages": [],
        "count": 0,
        "execution_id": execution_id or f"exec-{uuid.uuid4().hex[:8]}",
        "pause_checkpoint_id": None,
        "resume_checkpoint_id": None,
        "paused_at": None,
        "resumed_at": None,
        "pause_strategy": pause_strategy,
    }


def create_branch_state(switch: bool = False) -> BranchState:
    """Create initial branch state."""
    return {
        "messages": [],
        "count": 0,
        "path": [],
        "switch": switch,
        "branch_id": None,
        "parent_checkpoint_id": None,
    }


def create_rollback_state(execution_id: Optional[str] = None) -> RollbackState:
    """Create initial rollback state."""
    return {
        "messages": [],
        "count": 0,
        "execution_id": execution_id or f"exec-{uuid.uuid4().hex[:8]}",
        "checkpoint_ids": [],
        "rollback_target_id": None,
        "rollback_completed": False,
        "files_restored": [],
    }


def create_batch_test_state(
    batch_test_id: Optional[str] = None,
    variant_name: str = "default",
    variant_config: Optional[Dict[str, Any]] = None,
) -> BatchTestState:
    """Create initial batch test state."""
    return {
        "messages": [],
        "count": 0,
        "batch_test_id": batch_test_id or f"bt-{uuid.uuid4().hex[:8]}",
        "variant_name": variant_name,
        "variant_config": variant_config or {},
        "execution_result": None,
    }


def create_parallel_state(worker_id: Optional[str] = None) -> ParallelExecutionState:
    """Create initial parallel execution state."""
    return {
        "results": [],
        "node_order": [],
        "parallel_count": 0,
        "worker_id": worker_id or f"worker-{uuid.uuid4().hex[:4]}",
        "execution_times": [],
    }


def create_venv_state(
    venv_id: Optional[str] = None,
    venv_path: str = "",
) -> VenvState:
    """Create initial venv state."""
    return {
        "messages": [],
        "count": 0,
        "venv_id": venv_id or f"venv-{uuid.uuid4().hex[:8]}",
        "venv_path": venv_path,
        "packages_installed": [],
        "venv_status": "pending",
    }


def create_full_integration_state(
    execution_id: Optional[str] = None,
    batch_test_id: Optional[str] = None,
) -> FullIntegrationState:
    """Create initial full integration state."""
    return {
        "messages": [],
        "count": 0,
        "execution_id": execution_id or f"exec-{uuid.uuid4().hex[:8]}",
        "batch_test_id": batch_test_id,
        "checkpoint_ids": [],
        "commit_ids": [],
        "files_tracked": [],
        "actor_id": None,
        "worker_id": None,
        "venv_id": None,
        "venv_status": "pending",
        "outbox_events": [],
        "transaction_status": "pending",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Node Functions
# ═══════════════════════════════════════════════════════════════════════════════


def node_a(state: SimpleState) -> Dict[str, Any]:
    """Simple node A."""
    return {"messages": ["A"], "count": state["count"] + 1}


def node_b(state: SimpleState) -> Dict[str, Any]:
    """Simple node B."""
    return {"messages": ["B"], "count": state["count"] + 1}


def node_c(state: SimpleState) -> Dict[str, Any]:
    """Simple node C."""
    return {"messages": ["C"], "count": state["count"] + 1}


def node_d(state: SimpleState) -> Dict[str, Any]:
    """Simple node D (alternate branch)."""
    return {"messages": ["D"], "count": state["count"] + 1}


def failing_node(state: SimpleState) -> Dict[str, Any]:
    """Node that always fails."""
    raise ValueError("Intentional failure for testing")


def transaction_node(state: TransactionState) -> Dict[str, Any]:
    """Node that tracks transaction info."""
    import uuid
    event_id = str(uuid.uuid4())[:8]
    return {
        "messages": ["TX"],
        "count": state["count"] + 1,
        "outbox_event_ids": [event_id],
        "transaction_log": [f"tx-{datetime.now().isoformat()}"],
    }


def file_processing_node(state: FileTrackingState) -> Dict[str, Any]:
    """Node that processes files and creates commits."""
    file_id = uuid.uuid4().hex[:8]
    blob_hash = hashlib.sha256(f"content-{file_id}".encode()).hexdigest()
    commit_id = f"commit-{uuid.uuid4().hex[:8]}"
    
    return {
        "messages": [f"Processed {file_id}"],
        "count": state["count"] + 1,
        "files_processed": [f"file_{file_id}.txt"],
        "commit_ids": [commit_id],
        "blob_hashes": [blob_hash],
    }


def pauseable_node(state: PauseResumeState, pause_condition: bool = False) -> Dict[str, Any]:
    """Node that can be paused."""
    if pause_condition:
        return {
            "messages": ["PAUSING"],
            "count": state["count"] + 1,
            "paused_at": datetime.now().isoformat(),
        }
    return {
        "messages": ["RUNNING"],
        "count": state["count"] + 1,
    }


def branch_node_a(state: BranchState) -> Dict[str, Any]:
    """Branching node A."""
    return {
        "messages": ["A"],
        "count": state["count"] + 1,
        "path": ["node_a"],
    }


def branch_node_b(state: BranchState) -> Dict[str, Any]:
    """Branching node B."""
    return {
        "messages": ["B"],
        "count": state["count"] + 1,
        "path": ["node_b"],
    }


def branch_node_c(state: BranchState) -> Dict[str, Any]:
    """Branching node C."""
    return {
        "messages": ["C"],
        "count": state["count"] + 1,
        "path": ["node_c"],
    }


def branch_node_d(state: BranchState) -> Dict[str, Any]:
    """Branching node D (alternate path)."""
    return {
        "messages": ["D"],
        "count": state["count"] + 1,
        "path": ["node_d"],
    }


def venv_setup_node(state: VenvState) -> Dict[str, Any]:
    """Node that simulates venv setup."""
    return {
        "messages": ["venv_setup"],
        "count": state["count"] + 1,
        "venv_status": "created",
    }


def venv_install_node(state: VenvState) -> Dict[str, Any]:
    """Node that simulates package installation."""
    return {
        "messages": ["packages_installed"],
        "count": state["count"] + 1,
        "packages_installed": ["numpy", "pandas"],
        "venv_status": "ready",
    }


def parallel_worker_node(state: ParallelExecutionState) -> Dict[str, Any]:
    """Node for parallel execution."""
    import time
    start = time.time()
    time.sleep(0.01)  # Simulate work
    duration = time.time() - start
    
    return {
        "results": [{"worker": state["worker_id"], "value": state["parallel_count"] + 1}],
        "node_order": [state["worker_id"]],
        "parallel_count": state["parallel_count"] + 1,
        "execution_times": [duration],
    }


def aggregator_node(state: ParallelExecutionState) -> Dict[str, Any]:
    """Node that aggregates parallel results."""
    total = sum(r.get("value", 0) for r in state["results"])
    return {
        "results": [{"aggregated": True, "total": total}],
        "node_order": ["aggregator"],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Routing Functions
# ═══════════════════════════════════════════════════════════════════════════════


def route_by_switch(state: BranchState) -> str:
    """Route based on switch flag."""
    from langgraph.graph import END
    if state.get("switch", False):
        return "node_d"
    return "node_c"


def route_by_count(state: Dict[str, Any]) -> str:
    """Route based on count value."""
    from langgraph.graph import END
    if state["count"] >= 3:
        return END
    return "node_b"


def route_by_pause(state: PauseResumeState) -> str:
    """Route based on pause status."""
    from langgraph.graph import END
    if state.get("paused_at"):
        return END  # Pause execution
    return "continue"


# ═══════════════════════════════════════════════════════════════════════════════
# Mock Implementations
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class MockMemento:
    """Mock file memento."""
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
    """Mock file commit."""
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


@dataclass
class MockCheckpoint:
    """Mock LangGraph checkpoint."""
    checkpoint_id: int
    thread_id: str
    step: int
    state: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class MockOutboxEvent:
    """Mock outbox event for testing."""
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    created_at: datetime = field(default_factory=datetime.now)
    processed_at: Optional[datetime] = None
    
    def mark_processed(self):
        self.status = "processed"
        self.processed_at = datetime.now()
    
    def mark_failed(self, error: str):
        self.status = "failed"


@dataclass
class MockActor:
    """Mock Ray actor."""
    actor_id: str
    execution_id: str
    workspace_id: str
    state: str = "active"
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class MockVenv:
    """Mock virtual environment."""
    venv_id: str
    venv_path: str
    packages: List[str] = field(default_factory=list)
    status: str = "created"
    python_version: str = "3.12"


class MockCommitRepository:
    """Mock FileTracker commit repository."""
    
    def __init__(self):
        self._commits: Dict[str, MockCommit] = {}
        self._lock = threading.Lock()
    
    def add_commit(
        self, 
        commit_id: str, 
        mementos: Optional[List[MockMemento]] = None,
        execution_id: Optional[str] = None,
    ) -> MockCommit:
        with self._lock:
            commit = MockCommit(
                commit_id=commit_id,
                mementos=mementos or [],
                execution_id=execution_id,
            )
            self._commits[commit_id] = commit
            return commit
    
    def get_by_id(self, commit_id: str) -> Optional[MockCommit]:
        return self._commits.get(commit_id)
    
    def find_by_id(self, commit_id: str) -> Optional[MockCommit]:
        return self.get_by_id(commit_id)
    
    def list_by_execution(self, execution_id: str) -> List[MockCommit]:
        return [c for c in self._commits.values() if c.execution_id == execution_id]
    
    def save(self, commit: MockCommit) -> MockCommit:
        with self._lock:
            self._commits[commit.commit_id] = commit
            return commit
    
    def delete(self, commit_id: str) -> bool:
        with self._lock:
            if commit_id in self._commits:
                del self._commits[commit_id]
                return True
            return False


class MockBlobRepository:
    """Mock FileTracker blob repository."""
    
    def __init__(self):
        self._blobs: Dict[str, bytes] = {}
        self._lock = threading.Lock()
    
    def add_blob(self, content_hash: str, content: bytes = b"test") -> str:
        with self._lock:
            self._blobs[content_hash] = content
            return content_hash
    
    def save(self, content: bytes) -> str:
        content_hash = hashlib.sha256(content).hexdigest()
        return self.add_blob(content_hash, content)
    
    def exists(self, content_hash: str) -> bool:
        if hasattr(content_hash, 'value'):
            content_hash = content_hash.value
        return content_hash in self._blobs
    
    def get(self, content_hash: str) -> Optional[bytes]:
        if hasattr(content_hash, 'value'):
            content_hash = content_hash.value
        return self._blobs.get(content_hash)


class MockCheckpointRepository:
    """Mock checkpoint repository."""
    
    def __init__(self):
        self._checkpoints: Dict[int, MockCheckpoint] = {}
        self._lock = threading.Lock()
    
    def add_checkpoint(
        self, 
        checkpoint_id: int, 
        thread_id: str = "test",
        step: int = 0,
        state: Optional[Dict[str, Any]] = None,
    ) -> MockCheckpoint:
        with self._lock:
            checkpoint = MockCheckpoint(
                checkpoint_id=checkpoint_id,
                thread_id=thread_id,
                step=step,
                state=state or {},
            )
            self._checkpoints[checkpoint_id] = checkpoint
            return checkpoint
    
    def get_by_id(self, checkpoint_id: int) -> Optional[MockCheckpoint]:
        return self._checkpoints.get(checkpoint_id)
    
    def list_by_thread(self, thread_id: str) -> List[MockCheckpoint]:
        return sorted(
            [c for c in self._checkpoints.values() if c.thread_id == thread_id],
            key=lambda c: c.step,
        )


class MockOutboxRepository:
    """Mock outbox repository."""
    
    def __init__(self):
        self._events: Dict[str, MockOutboxEvent] = {}
        self._lock = threading.Lock()
        self._id_counter = 0
    
    def add(self, event: MockOutboxEvent) -> MockOutboxEvent:
        with self._lock:
            self._id_counter += 1
            if not event.event_id:
                event.event_id = str(self._id_counter)
            self._events[event.event_id] = event
            return event
    
    def get_by_id(self, event_id: str) -> Optional[MockOutboxEvent]:
        return self._events.get(event_id)
    
    def get_pending(self) -> List[MockOutboxEvent]:
        return [e for e in self._events.values() if e.status == "pending"]
    
    def get_processed(self) -> List[MockOutboxEvent]:
        return [e for e in self._events.values() if e.status == "processed"]
    
    def get_failed(self) -> List[MockOutboxEvent]:
        return [e for e in self._events.values() if e.status == "failed"]
    
    def update(self, event: MockOutboxEvent) -> MockOutboxEvent:
        with self._lock:
            self._events[event.event_id] = event
            return event
    
    def list_all(self) -> List[MockOutboxEvent]:
        return list(self._events.values())


class MockActorPool:
    """Mock Ray actor pool."""
    
    def __init__(self, pool_size: int = 4):
        self._actors: Dict[str, MockActor] = {}
        self._pool_size = pool_size
        self._lock = threading.Lock()
    
    def create_actor(
        self, 
        actor_id: str, 
        execution_id: str, 
        workspace_id: str,
    ) -> MockActor:
        with self._lock:
            actor = MockActor(
                actor_id=actor_id,
                execution_id=execution_id,
                workspace_id=workspace_id,
            )
            self._actors[actor_id] = actor
            return actor
    
    def get_actor(self, actor_id: str) -> Optional[MockActor]:
        return self._actors.get(actor_id)
    
    def pause_actor(self, actor_id: str) -> bool:
        actor = self._actors.get(actor_id)
        if actor:
            actor.state = "paused"
            return True
        return False
    
    def resume_actor(self, actor_id: str) -> bool:
        actor = self._actors.get(actor_id)
        if actor:
            actor.state = "active"
            return True
        return False
    
    def kill_actor(self, actor_id: str) -> bool:
        with self._lock:
            if actor_id in self._actors:
                self._actors[actor_id].state = "killed"
                return True
            return False
    
    def list_active(self) -> List[MockActor]:
        return [a for a in self._actors.values() if a.state == "active"]


class MockVenvManager:
    """Mock virtual environment manager."""
    
    def __init__(self):
        self._venvs: Dict[str, MockVenv] = {}
        self._lock = threading.Lock()
    
    def create_venv(
        self, 
        venv_id: str, 
        venv_path: str,
        python_version: str = "3.12",
    ) -> MockVenv:
        with self._lock:
            venv = MockVenv(
                venv_id=venv_id,
                venv_path=venv_path,
                python_version=python_version,
            )
            self._venvs[venv_id] = venv
            return venv
    
    def install_packages(self, venv_id: str, packages: List[str]) -> bool:
        venv = self._venvs.get(venv_id)
        if venv:
            venv.packages.extend(packages)
            venv.status = "ready"
            return True
        return False
    
    def get_venv(self, venv_id: str) -> Optional[MockVenv]:
        return self._venvs.get(venv_id)
    
    def delete_venv(self, venv_id: str) -> bool:
        with self._lock:
            if venv_id in self._venvs:
                del self._venvs[venv_id]
                return True
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# Verification Utilities
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class VerificationResult:
    """Result of a verification check."""
    success: bool
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


def verify_outbox_consistency(
    outbox_repo: MockOutboxRepository,
    expected_event_count: int,
    expected_processed: int = 0,
) -> VerificationResult:
    """Verify outbox consistency."""
    all_events = outbox_repo.list_all()
    processed = outbox_repo.get_processed()
    
    if len(all_events) != expected_event_count:
        return VerificationResult(
            success=False,
            message=f"Event count mismatch: expected {expected_event_count}, got {len(all_events)}",
            details={"actual_count": len(all_events)},
        )
    
    if len(processed) != expected_processed:
        return VerificationResult(
            success=False,
            message=f"Processed count mismatch: expected {expected_processed}, got {len(processed)}",
            details={"actual_processed": len(processed)},
        )
    
    return VerificationResult(
        success=True,
        message="Outbox consistency verified",
        details={"event_count": len(all_events), "processed_count": len(processed)},
    )


def verify_checkpoint_file_link(
    checkpoint_id: int,
    commit_id: str,
    checkpoint_repo: MockCheckpointRepository,
    commit_repo: MockCommitRepository,
) -> VerificationResult:
    """Verify checkpoint-file link consistency."""
    checkpoint = checkpoint_repo.get_by_id(checkpoint_id)
    commit = commit_repo.get_by_id(commit_id)
    
    if not checkpoint:
        return VerificationResult(
            success=False,
            message=f"Checkpoint {checkpoint_id} not found",
        )
    
    if not commit:
        return VerificationResult(
            success=False,
            message=f"Commit {commit_id} not found",
        )
    
    return VerificationResult(
        success=True,
        message="Checkpoint-file link verified",
        details={"checkpoint_id": checkpoint_id, "commit_id": commit_id},
    )


def verify_transaction_atomicity(
    operations: List[Tuple[str, bool]],
) -> VerificationResult:
    """Verify all operations succeeded or all failed (atomicity)."""
    successes = [op for op in operations if op[1]]
    failures = [op for op in operations if not op[1]]
    
    if len(successes) > 0 and len(failures) > 0:
        return VerificationResult(
            success=False,
            message="Atomicity violation: partial success",
            details={"successes": successes, "failures": failures},
        )
    
    return VerificationResult(
        success=True,
        message="Atomicity verified",
        details={"all_succeeded": len(failures) == 0},
    )


def verify_state_consistency(
    states: List[Dict[str, Any]],
    invariant: Callable[[Dict[str, Any]], bool],
) -> VerificationResult:
    """Verify state invariant holds for all states."""
    violations = []
    for i, state in enumerate(states):
        if not invariant(state):
            violations.append(i)
    
    if violations:
        return VerificationResult(
            success=False,
            message=f"State invariant violated at indices: {violations}",
            details={"violation_indices": violations},
        )
    
    return VerificationResult(
        success=True,
        message="State consistency verified",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Test Data Generators
# ═══════════════════════════════════════════════════════════════════════════════


def generate_test_files(count: int = 3) -> List[Tuple[str, bytes]]:
    """Generate test file data."""
    files = []
    for i in range(count):
        name = f"test_file_{i}.txt"
        content = f"Test content for file {i}\nLine 2\nLine 3".encode()
        files.append((name, content))
    return files


def generate_test_commits(
    commit_repo: MockCommitRepository,
    count: int = 3,
    files_per_commit: int = 2,
    execution_id: Optional[str] = None,
) -> List[MockCommit]:
    """Generate test commits with mementos."""
    commits = []
    for i in range(count):
        mementos = []
        for j in range(files_per_commit):
            mementos.append(MockMemento(
                file_path=f"file_{i}_{j}.txt",
                file_hash=hashlib.sha256(f"content-{i}-{j}".encode()).hexdigest(),
            ))
        
        commit = commit_repo.add_commit(
            commit_id=f"commit-{uuid.uuid4().hex[:8]}",
            mementos=mementos,
            execution_id=execution_id,
        )
        commits.append(commit)
    
    return commits


def generate_batch_test_variants(
    count: int = 5,
    base_config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Generate batch test variant configurations."""
    base = base_config or {"learning_rate": 0.01}
    variants = []
    
    for i in range(count):
        variant = {
            "variant_name": f"variant_{i}",
            "variant_config": {
                **base,
                "seed": i,
                "batch_size": 16 * (i + 1),
            },
        }
        variants.append(variant)
    
    return variants
