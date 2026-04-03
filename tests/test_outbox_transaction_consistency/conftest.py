"""
Pytest fixtures for Outbox Transaction Consistency tests.

REFACTORED (2026-01-27): Uses REAL services instead of mocks:
- SQLite-based WTBTestBench with real checkpoint persistence
- Real OutboxProcessor with transaction verification
- Real FileTracker integration (when available)
- Real LangGraph checkpointer

Design Principles:
- DIP: Fixtures provide real implementations via dependency injection
- ISP: Separate fixtures for each concern
- ACID: Tests verify real transaction consistency

Run with: pytest tests/test_outbox_transaction_consistency/ -v
"""

import pytest
import tempfile
import uuid
import shutil
from typing import List, Dict, Any, Optional, Generator
from datetime import datetime
from pathlib import Path

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

# WTB SDK imports
from wtb.sdk import (
    WTBTestBench,
    WorkflowProject,
    FileTrackingConfig,
    EnvironmentConfig,
    ExecutionConfig,
    EnvSpec,
)
from wtb.application.factories import WTBTestBenchFactory

# WTB Infrastructure imports
from wtb.infrastructure.outbox import (
    OutboxProcessor,
    OutboxLifecycleManager,
    create_managed_processor,
)
from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork
from wtb.infrastructure.events import WTBEventBus, WTBAuditTrail

# Import helpers
from tests.test_outbox_transaction_consistency.helpers import (
    # States
    SimpleState,
    TransactionState,
    FileTrackingState,
    PauseResumeState,
    BranchState,
    RollbackState,
    BatchTestState,
    ParallelExecutionState,
    VenvState,
    FullIntegrationState,
    # State factories
    create_simple_state,
    create_transaction_state,
    create_file_tracking_state,
    create_pause_resume_state,
    create_branch_state,
    create_rollback_state,
    create_batch_test_state,
    create_parallel_state,
    create_venv_state,
    create_full_integration_state,
    # Node functions
    node_a,
    node_b,
    node_c,
    node_d,
    failing_node,
    transaction_node,
    file_processing_node,
    branch_node_a,
    branch_node_b,
    branch_node_c,
    branch_node_d,
    venv_setup_node,
    venv_install_node,
    parallel_worker_node,
    aggregator_node,
    # Routing
    route_by_switch,
    route_by_count,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Real Database Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def temp_data_dir(tmp_path) -> Generator[Path, None, None]:
    """Create temporary data directory for real SQLite databases."""
    data_dir = tmp_path / "wtb_test_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    yield data_dir
    # Cleanup handled by tmp_path


@pytest.fixture
def wtb_db_url(temp_data_dir) -> str:
    """Create real WTB SQLite database URL."""
    db_path = temp_data_dir / "wtb.db"
    return f"sqlite:///{db_path}"


@pytest.fixture
def checkpoint_db_path(temp_data_dir) -> Path:
    """Create path for SQLite checkpoint database."""
    return temp_data_dir / "checkpoints.db"


# ═══════════════════════════════════════════════════════════════════════════════
# Real WTB TestBench Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def wtb_bench_development(temp_data_dir) -> WTBTestBench:
    """
    Create WTBTestBench with real SQLite persistence.
    
    Uses 'development' mode for:
    - SQLite checkpoint persistence
    - Real UnitOfWork transactions
    - File tracking support (optional)
    """
    return WTBTestBench.create(
        mode="development",
        data_dir=str(temp_data_dir),
        enable_file_tracking=False,  # Can be enabled for specific tests
    )


@pytest.fixture
def wtb_bench_with_file_tracking(temp_data_dir) -> WTBTestBench:
    """
    Create WTBTestBench with real SQLite and file tracking enabled.
    """
    return WTBTestBench.create(
        mode="development",
        data_dir=str(temp_data_dir),
        enable_file_tracking=True,
    )


@pytest.fixture
def wtb_bench_testing() -> WTBTestBench:
    """
    Create WTBTestBench with in-memory backend for fast tests.
    
    Uses 'testing' mode for:
    - In-memory checkpointer
    - Fast execution
    - No disk I/O
    """
    return WTBTestBenchFactory.create_for_testing()


# ═══════════════════════════════════════════════════════════════════════════════
# Real Outbox Processor Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def outbox_processor_db_url(temp_data_dir) -> str:
    """Create separate database URL for outbox processor to avoid conflicts."""
    db_path = temp_data_dir / "outbox_processor.db"
    return f"sqlite:///{db_path}"


@pytest.fixture
def outbox_processor(outbox_processor_db_url) -> Generator[OutboxProcessor, None, None]:
    """
    Create real OutboxProcessor with SQLite backend.
    
    Uses separate database to avoid conflicts with wtb_bench fixtures.
    
    The processor will:
    - Poll wtb_outbox table for pending events
    - Execute verification handlers
    - Track verification statistics
    """
    # Initialize the database first
    with SQLAlchemyUnitOfWork(outbox_processor_db_url) as uow:
        uow.commit()
    
    processor = OutboxProcessor(
        wtb_db_url=outbox_processor_db_url,
        poll_interval_seconds=0.1,  # Fast polling for tests
        batch_size=10,
        strict_verification=False,  # Don't fail on missing repos
    )
    yield processor
    # Cleanup - ensure processor is stopped
    if processor.is_running():
        processor.stop(timeout=2.0)


@pytest.fixture
def lifecycle_manager_db_url(temp_data_dir) -> str:
    """Create separate database URL for lifecycle manager to avoid conflicts."""
    db_path = temp_data_dir / "lifecycle_manager.db"
    return f"sqlite:///{db_path}"


@pytest.fixture
def outbox_lifecycle_manager(lifecycle_manager_db_url) -> Generator[OutboxLifecycleManager, None, None]:
    """
    Create real OutboxLifecycleManager with managed lifecycle.
    
    Uses separate database to avoid conflicts.
    """
    # Initialize the database first
    with SQLAlchemyUnitOfWork(lifecycle_manager_db_url) as uow:
        uow.commit()
    
    manager = OutboxLifecycleManager(
        wtb_db_url=lifecycle_manager_db_url,
        poll_interval_seconds=0.1,
        auto_start=False,  # Start manually in tests
        register_signals=False,  # Don't register signal handlers in tests
    )
    yield manager
    # Cleanup
    manager.shutdown(timeout=2.0)


@pytest.fixture
def running_outbox_processor(outbox_processor) -> Generator[OutboxProcessor, None, None]:
    """
    Create and start a real OutboxProcessor.
    """
    outbox_processor.start()
    yield outbox_processor
    outbox_processor.stop(timeout=2.0)


# ═══════════════════════════════════════════════════════════════════════════════
# Real Unit of Work Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def unit_of_work(wtb_db_url) -> Generator[SQLAlchemyUnitOfWork, None, None]:
    """
    Create real SQLAlchemy Unit of Work for transaction testing.
    """
    with SQLAlchemyUnitOfWork(wtb_db_url) as uow:
        yield uow


# ═══════════════════════════════════════════════════════════════════════════════
# Real Repository Fixtures (for Integration Tests)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def real_outbox_repository(unit_of_work):
    """
    Provide REAL outbox repository from the Unit of Work.
    
    Use this for integration tests that need actual database persistence.
    """
    return unit_of_work.outbox


@pytest.fixture
def real_checkpoint_repository(unit_of_work):
    """
    Provide REAL checkpoint repository from the Unit of Work.
    
    Use this for integration tests that need actual database persistence.
    """
    return unit_of_work.checkpoints


# ═══════════════════════════════════════════════════════════════════════════════
# Mock Repository Fixtures (for Unit Tests)
# 
# ARCHITECTURE NOTE (2026-01-28):
# Refactored to use centralized mock infrastructure from tests.mocks.
# Mocks now implement REAL interfaces and accept REAL domain types.
# See: CONSOLIDATED_ISSUES.md - ISSUE-TEST-001 (RESOLVED)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def outbox_repository():
    """
    Mock outbox repository for unit tests.
    
    Uses centralized MockOutboxRepository which:
    - Implements IOutboxRepository interface
    - Accepts REAL OutboxEvent domain objects
    - Thread-safe for parallel tests
    
    For integration tests with real database, use real_outbox_repository.
    """
    from tests.mocks import MockOutboxRepository
    return MockOutboxRepository()


@pytest.fixture
def checkpoint_repository():
    """
    Mock checkpoint repository for unit tests.
    
    Uses centralized MockCheckpointRepository.
    For integration tests, use real_checkpoint_repository.
    """
    from tests.mocks import MockCheckpointRepository
    return MockCheckpointRepository()


@pytest.fixture
def commit_repository():
    """
    Mock file commit repository for unit tests.
    
    Uses centralized MockCommitRepository.
    """
    from tests.mocks import MockCommitRepository
    return MockCommitRepository()


@pytest.fixture
def blob_repository():
    """
    Mock blob repository for unit tests.
    
    Uses centralized MockBlobRepository.
    """
    from tests.mocks import MockBlobRepository
    return MockBlobRepository()


@pytest.fixture
def ray_event_bridge(outbox_repository):
    """
    Mock Ray event bridge for unit tests.
    
    Uses centralized MockRayEventBridge which:
    - Creates REAL OutboxEvent instances
    - Properly integrates with outbox repository
    
    See: CONSOLIDATED_ISSUES.md - ISSUE-RAY-002 (RESOLVED)
    """
    from tests.mocks import MockRayEventBridge
    return MockRayEventBridge(outbox_repository)


@pytest.fixture
def mock_actor_pool():
    """
    Mock Ray actor pool for unit tests.
    
    Uses centralized MockActorPool.
    """
    from tests.mocks import MockActorPool
    return MockActorPool(pool_size=4)


# Fixture alias for tests that use 'actor_pool' name
# Resolves ISSUE-MOCK-002: Fixture Name Misalignment
@pytest.fixture
def actor_pool(mock_actor_pool):
    """
    Alias for mock_actor_pool fixture.
    
    Provides compatibility for tests that use 'actor_pool' name.
    """
    return mock_actor_pool


@pytest.fixture
def mock_venv_manager():
    """
    Mock virtual environment manager for unit tests.
    
    Uses centralized MockVenvManager.
    """
    from tests.mocks import MockVenvManager
    return MockVenvManager()


# Fixture alias for tests that use 'venv_manager' name
# Resolves ISSUE-MOCK-002: Fixture Name Misalignment
@pytest.fixture
def venv_manager(mock_venv_manager):
    """
    Alias for mock_venv_manager fixture.
    
    Provides compatibility for tests that use 'venv_manager' name.
    """
    return mock_venv_manager


@pytest.fixture
def full_integration_setup(
    outbox_repository,
    checkpoint_repository,
    commit_repository,
    blob_repository,
    mock_actor_pool,
    mock_venv_manager,
):
    """
    Full integration setup providing all mock repositories.
    
    This fixture provides a complete mock environment for
    cross-system integration tests. Each repository is independent
    and properly isolated.
    
    Note: All repositories now use centralized mocks that accept
    REAL domain types, ensuring test-production compatibility.
    
    Returns:
        dict: Dictionary with all repository fixtures
    """
    return {
        "outbox_repository": outbox_repository,
        "checkpoint_repository": checkpoint_repository,
        "commit_repository": commit_repository,
        "blob_repository": blob_repository,
        "actor_pool": mock_actor_pool,
        "venv_manager": mock_venv_manager,
    }


@pytest.fixture
def batch_test_variants():
    """
    Generate batch test variants for testing.
    
    Uses centralized test data generator.
    """
    from tests.mocks import generate_batch_test_variants
    return generate_batch_test_variants(count=5)


# ═══════════════════════════════════════════════════════════════════════════════
# Real Integration Setup (NO MOCKS - Real Services Only)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def real_full_integration_setup(
    unit_of_work,
    temp_data_dir,
):
    """
    Full integration setup using REAL services - NO MOCKS.
    
    Uses:
    - Real SQLite database via Unit of Work
    - Real outbox repository from UoW
    - Real checkpoint repository from UoW
    - Real file commit repository from UoW
    - Real blob repository from UoW
    - Real temporary file storage
    
    For true integration testing with actual persistence.
    """
    # Create real blob storage directory
    blob_dir = temp_data_dir / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)
    
    return {
        "outbox_repository": unit_of_work.outbox,
        "checkpoint_repository": unit_of_work.checkpoints,
        "commit_repository": unit_of_work.file_commits,
        "blob_repository": unit_of_work.blobs,
        "unit_of_work": unit_of_work,
        "temp_data_dir": temp_data_dir,
        "blob_dir": blob_dir,
    }


@pytest.fixture
def real_services_setup(
    wtb_bench_development,
    outbox_processor,
    temp_data_dir,
):
    """
    Setup with real running services for end-to-end testing.
    
    Uses:
    - Real WTBTestBench with SQLite backend
    - Real OutboxProcessor (not started by default)
    - Real temporary file storage
    
    Start the outbox processor with: setup["outbox_processor"].start()
    """
    return {
        "test_bench": wtb_bench_development,
        "outbox_processor": outbox_processor,
        "temp_data_dir": temp_data_dir,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph Graph Definition Fixtures (Same as before)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def simple_graph_def() -> StateGraph:
    """Create simple linear graph: A -> B -> C -> END."""
    workflow = StateGraph(SimpleState)
    
    workflow.add_node("node_a", node_a)
    workflow.add_node("node_b", node_b)
    workflow.add_node("node_c", node_c)
    
    workflow.set_entry_point("node_a")
    workflow.add_edge("node_a", "node_b")
    workflow.add_edge("node_b", "node_c")
    workflow.add_edge("node_c", END)
    
    return workflow


@pytest.fixture
def transaction_graph_def() -> StateGraph:
    """Create graph with transaction tracking."""
    workflow = StateGraph(TransactionState)
    
    workflow.add_node("node_a", lambda s: {
        "messages": ["A"],
        "count": s["count"] + 1,
        "transaction_log": [f"start-{datetime.now().isoformat()}"],
    })
    workflow.add_node("node_b", transaction_node)
    workflow.add_node("node_c", lambda s: {
        "messages": ["C"],
        "count": s["count"] + 1,
        "transaction_log": [f"end-{datetime.now().isoformat()}"],
    })
    
    workflow.set_entry_point("node_a")
    workflow.add_edge("node_a", "node_b")
    workflow.add_edge("node_b", "node_c")
    workflow.add_edge("node_c", END)
    
    return workflow


@pytest.fixture
def file_tracking_graph_def() -> StateGraph:
    """Create graph for file tracking tests."""
    workflow = StateGraph(FileTrackingState)
    
    workflow.add_node("ingest", lambda s: {
        "messages": ["Ingested"],
        "count": s["count"] + 1,
        "files_processed": ["input.csv"],
    })
    workflow.add_node("process", file_processing_node)
    workflow.add_node("save", lambda s: {
        "messages": ["Saved"],
        "count": s["count"] + 1,
        "commit_ids": [f"commit_{len(s.get('files_processed', []))}"],
    })
    
    workflow.set_entry_point("ingest")
    workflow.add_edge("ingest", "process")
    workflow.add_edge("process", "save")
    workflow.add_edge("save", END)
    
    return workflow


@pytest.fixture
def branching_graph_def() -> StateGraph:
    """Create branching graph: A -> B -> (C or D) -> END."""
    workflow = StateGraph(BranchState)
    
    workflow.add_node("node_a", branch_node_a)
    workflow.add_node("node_b", branch_node_b)
    workflow.add_node("node_c", branch_node_c)
    workflow.add_node("node_d", branch_node_d)
    
    workflow.set_entry_point("node_a")
    workflow.add_edge("node_a", "node_b")
    workflow.add_conditional_edges("node_b", route_by_switch)
    workflow.add_edge("node_c", END)
    workflow.add_edge("node_d", END)
    
    return workflow


@pytest.fixture
def pause_resume_graph_def() -> StateGraph:
    """Create graph for pause/resume tests."""
    workflow = StateGraph(PauseResumeState)
    
    workflow.add_node("start", lambda s: {
        "messages": ["Started"],
        "count": s["count"] + 1,
    })
    workflow.add_node("process", lambda s: {
        "messages": ["Processing"],
        "count": s["count"] + 1,
    })
    workflow.add_node("checkpoint", lambda s: {
        "messages": ["Checkpointed"],
        "count": s["count"] + 1,
        "pause_checkpoint_id": s["count"] + 1,
    })
    workflow.add_node("complete", lambda s: {
        "messages": ["Completed"],
        "count": s["count"] + 1,
    })
    
    workflow.set_entry_point("start")
    workflow.add_edge("start", "process")
    workflow.add_edge("process", "checkpoint")
    workflow.add_edge("checkpoint", "complete")
    workflow.add_edge("complete", END)
    
    return workflow


@pytest.fixture
def venv_graph_def() -> StateGraph:
    """Create graph for venv integration tests."""
    workflow = StateGraph(VenvState)
    
    workflow.add_node("setup", venv_setup_node)
    workflow.add_node("install", venv_install_node)
    workflow.add_node("execute", lambda s: {
        "messages": ["Executed"],
        "count": s["count"] + 1,
        "venv_status": "executed",
    })
    
    workflow.set_entry_point("setup")
    workflow.add_edge("setup", "install")
    workflow.add_edge("install", "execute")
    workflow.add_edge("execute", END)
    
    return workflow


@pytest.fixture
def failing_graph_def() -> StateGraph:
    """Create graph with a failing node."""
    workflow = StateGraph(SimpleState)
    
    workflow.add_node("node_a", node_a)
    workflow.add_node("failing_node", failing_node)
    workflow.add_node("node_b", node_b)
    
    workflow.set_entry_point("node_a")
    workflow.add_edge("node_a", "failing_node")
    workflow.add_edge("failing_node", "node_b")
    workflow.add_edge("node_b", END)
    
    return workflow


@pytest.fixture
def batch_test_graph_def() -> StateGraph:
    """Create graph for batch testing."""
    workflow = StateGraph(BatchTestState)
    
    workflow.add_node("setup", lambda s: {
        "messages": ["Setup"],
        "count": s["count"] + 1,
    })
    workflow.add_node("execute", lambda s: {
        "messages": ["Executed"],
        "count": s["count"] + 1,
        "execution_result": f"result_{s['variant_name']}",
    })
    workflow.add_node("evaluate", lambda s: {
        "messages": ["Evaluated"],
        "count": s["count"] + 1,
    })
    
    workflow.set_entry_point("setup")
    workflow.add_edge("setup", "execute")
    workflow.add_edge("execute", "evaluate")
    workflow.add_edge("evaluate", END)
    
    return workflow


# ═══════════════════════════════════════════════════════════════════════════════
# Checkpointer Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def memory_checkpointer() -> MemorySaver:
    """Create in-memory checkpointer."""
    return MemorySaver()


@pytest.fixture
def sqlite_checkpointer(checkpoint_db_path) -> Generator[SqliteSaver, None, None]:
    """Create real SQLite checkpointer."""
    # SqliteSaver from langgraph.checkpoint.sqlite
    conn_string = f"sqlite:///{checkpoint_db_path}"
    with SqliteSaver.from_conn_string(conn_string) as saver:
        yield saver


# ═══════════════════════════════════════════════════════════════════════════════
# Compiled Graph Fixtures with Real Checkpointer
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def simple_compiled_graph(simple_graph_def, memory_checkpointer):
    """Create compiled simple graph with checkpointer."""
    return simple_graph_def.compile(checkpointer=memory_checkpointer)


@pytest.fixture
def transaction_compiled_graph(transaction_graph_def, memory_checkpointer):
    """Create compiled transaction graph with checkpointer."""
    return transaction_graph_def.compile(checkpointer=memory_checkpointer)


@pytest.fixture
def file_tracking_compiled_graph(file_tracking_graph_def, memory_checkpointer):
    """Create compiled file tracking graph with checkpointer."""
    return file_tracking_graph_def.compile(checkpointer=memory_checkpointer)


@pytest.fixture
def branching_compiled_graph(branching_graph_def, memory_checkpointer):
    """Create compiled branching graph with checkpointer."""
    return branching_graph_def.compile(checkpointer=memory_checkpointer)


@pytest.fixture
def pause_resume_compiled_graph(pause_resume_graph_def, memory_checkpointer):
    """Create compiled pause/resume graph with checkpointer."""
    return pause_resume_graph_def.compile(checkpointer=memory_checkpointer)


@pytest.fixture
def venv_compiled_graph(venv_graph_def, memory_checkpointer):
    """Create compiled venv graph with checkpointer."""
    return venv_graph_def.compile(checkpointer=memory_checkpointer)


@pytest.fixture
def batch_test_compiled_graph(batch_test_graph_def, memory_checkpointer):
    """Create compiled batch test graph with checkpointer."""
    return batch_test_graph_def.compile(checkpointer=memory_checkpointer)


# ═══════════════════════════════════════════════════════════════════════════════
# WorkflowProject Fixtures for SDK Integration
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def simple_project(simple_graph_def) -> WorkflowProject:
    """Create simple WorkflowProject for SDK tests."""
    return WorkflowProject(
        name="simple_test_project",
        graph_factory=lambda: simple_graph_def,
        description="Simple test workflow for outbox testing",
    )


@pytest.fixture
def transaction_project(transaction_graph_def) -> WorkflowProject:
    """Create transaction-aware WorkflowProject."""
    return WorkflowProject(
        name="transaction_test_project",
        graph_factory=lambda: transaction_graph_def,
        description="Transaction consistency test workflow",
    )


@pytest.fixture
def file_tracking_project(file_tracking_graph_def, temp_data_dir) -> WorkflowProject:
    """Create WorkflowProject with file tracking enabled."""
    tracked_dir = temp_data_dir / "tracked_files"
    tracked_dir.mkdir(parents=True, exist_ok=True)
    
    return WorkflowProject(
        name="file_tracking_test_project",
        graph_factory=lambda: file_tracking_graph_def,
        description="File tracking integration test workflow",
        file_tracking=FileTrackingConfig(
            enabled=True,
            tracked_paths=[str(tracked_dir)],
            auto_commit=True,
            commit_on="checkpoint",
        ),
    )


@pytest.fixture
def pause_resume_project(pause_resume_graph_def) -> WorkflowProject:
    """Create WorkflowProject for pause/resume tests."""
    return WorkflowProject(
        name="pause_resume_test_project",
        graph_factory=lambda: pause_resume_graph_def,
        description="Pause/resume test workflow",
    )


@pytest.fixture
def branching_project(branching_graph_def) -> WorkflowProject:
    """Create WorkflowProject for branching/fork tests."""
    return WorkflowProject(
        name="branching_test_project",
        graph_factory=lambda: branching_graph_def,
        description="Branching/forking test workflow",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Event System Fixtures (Real Implementations)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def event_bus():
    """Create real WTB event bus."""
    return WTBEventBus(max_history=1000)


@pytest.fixture
def audit_trail():
    """Create real audit trail."""
    return WTBAuditTrail()


# ═══════════════════════════════════════════════════════════════════════════════
# Execution Context Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def execution_context():
    """Create standard execution context."""
    execution_id = f"exec-{uuid.uuid4().hex[:8]}"
    thread_id = f"wtb-{execution_id}"
    batch_test_id = f"bt-{uuid.uuid4().hex[:8]}"
    
    return {
        "execution_id": execution_id,
        "thread_id": thread_id,
        "batch_test_id": batch_test_id,
        "config": {"configurable": {"thread_id": thread_id}},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Test Data Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def test_files(tmp_path) -> Dict[str, Dict[str, Any]]:
    """Create temporary test files."""
    files = {}
    test_data = [
        ("data.csv", b"id,name,value\n1,Alice,100\n2,Bob,200"),
        ("model.pkl", b"PICKLE_BINARY_MODEL_DATA_PLACEHOLDER"),
        ("config.json", b'{"setting": "value", "enabled": true}'),
    ]
    
    for name, content in test_data:
        path = tmp_path / name
        path.write_bytes(content)
        files[name] = {
            "path": str(path),
            "content": content,
            "size": len(content),
        }
    
    return files


# ═══════════════════════════════════════════════════════════════════════════════
# Cleanup Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Cleanup after each test."""
    yield
    # Any cleanup logic here
    import gc
    gc.collect()
