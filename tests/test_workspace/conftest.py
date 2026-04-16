"""
Pytest fixtures for Workspace Isolation Integration Tests.

Provides shared fixtures for testing workspace isolation with:
- Real Ray cluster (local mode)
- LangGraph checkpointing
- Virtual environment (uv_venv_manager)
- FileTracker integration

Design Reference: docs/issues/WORKSPACE_ISOLATION_DESIGN.md
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# Workspace imports
from wtb.domain.models.workspace import (
    Workspace,
    WorkspaceConfig,
    WorkspaceStrategy,
    LinkMethod,
    compute_venv_spec_hash,
)
from wtb.domain.events.workspace_events import (
    WorkspaceCreatedEvent,
    WorkspaceActivatedEvent,
    WorkspaceDeactivatedEvent,
    WorkspaceCleanedUpEvent,
    ForkRequestedEvent,
    ForkCompletedEvent,
)
from wtb.infrastructure.workspace.manager import (
    WorkspaceManager,
    create_file_link,
)

# Event system imports
from wtb.infrastructure.events import (
    WTBEventBus,
    WTBAuditTrail,
    AuditEventListener,
)

# Ray event imports
from wtb.domain.events.ray_events import (
    RayBatchTestStartedEvent,
    RayBatchTestCompletedEvent,
    RayVariantExecutionStartedEvent,
    RayVariantExecutionCompletedEvent,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Temporary Directory Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="function")
def workspace_temp_dir():
    """Create a temporary directory for workspace tests."""
    temp = tempfile.mkdtemp(prefix="wtb_ws_test_")
    yield Path(temp)
    shutil.rmtree(temp, ignore_errors=True)


@pytest.fixture(scope="module")
def module_temp_dir():
    """Module-scoped temp directory for expensive Ray tests."""
    temp = tempfile.mkdtemp(prefix="wtb_ws_module_")
    yield Path(temp)
    shutil.rmtree(temp, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Sample Project Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def sample_project_files(workspace_temp_dir) -> Dict[str, Path]:
    """Create a sample project with various file types."""
    project_dir = workspace_temp_dir / "sample_project"
    project_dir.mkdir(parents=True)
    
    files = {}
    
    # Data files
    data_dir = project_dir / "data"
    data_dir.mkdir()
    
    csv_path = data_dir / "input.csv"
    csv_path.write_text("id,value,category\n1,100,A\n2,200,B\n3,300,A\n")
    files["csv"] = csv_path
    
    json_path = data_dir / "config.json"
    json_path.write_text('{"model": "bm25", "k": 10, "threshold": 0.5}')
    files["json"] = json_path
    
    # Model files
    models_dir = project_dir / "models"
    models_dir.mkdir()
    
    model_path = models_dir / "embeddings.pkl"
    model_path.write_bytes(b"FAKE_MODEL_EMBEDDING_DATA_" * 50)
    files["model"] = model_path
    
    # Output directory (empty initially)
    output_dir = project_dir / "output"
    output_dir.mkdir()
    files["output_dir"] = output_dir
    
    # Source code (for variant testing)
    src_dir = project_dir / "src"
    src_dir.mkdir()
    
    retriever_path = src_dir / "retriever.py"
    retriever_path.write_text('''
def retrieve(query: str, k: int = 10):
    """BM25 retriever implementation."""
    return [f"doc_{i}" for i in range(k)]
''')
    files["retriever"] = retriever_path
    
    files["project_dir"] = project_dir
    files["data_dir"] = data_dir
    files["models_dir"] = models_dir
    files["src_dir"] = src_dir
    
    return files


@pytest.fixture
def large_sample_project(workspace_temp_dir) -> Dict[str, Path]:
    """Create a larger sample project for performance tests."""
    project_dir = workspace_temp_dir / "large_project"
    project_dir.mkdir(parents=True)
    
    files = {}
    
    # Create 100 data files
    data_dir = project_dir / "data"
    data_dir.mkdir()
    
    for i in range(100):
        file_path = data_dir / f"data_{i:03d}.csv"
        content = f"id,value\n" + "\n".join([f"{j},{j*100}" for j in range(100)])
        file_path.write_text(content)
    
    files["data_dir"] = data_dir
    files["data_count"] = 100
    
    # Create large model file (5MB)
    models_dir = project_dir / "models"
    models_dir.mkdir()
    
    large_model = models_dir / "large_model.bin"
    large_model.write_bytes(b"x" * (5 * 1024 * 1024))
    files["large_model"] = large_model
    
    files["project_dir"] = project_dir
    
    return files


# ═══════════════════════════════════════════════════════════════════════════════
# Workspace Configuration Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def workspace_config(workspace_temp_dir) -> WorkspaceConfig:
    """Create standard workspace config for testing."""
    return WorkspaceConfig(
        enabled=True,
        strategy=WorkspaceStrategy.WORKSPACE,
        base_dir=workspace_temp_dir / "workspaces",
        cleanup_on_complete=True,
        preserve_on_failure=True,
        use_hard_links=True,
        orphan_cleanup_grace_hours=0.001,  # Very short for testing
    )


@pytest.fixture
def workspace_config_no_hardlinks(workspace_temp_dir) -> WorkspaceConfig:
    """Create workspace config without hard links (for cross-partition tests)."""
    return WorkspaceConfig(
        enabled=True,
        strategy=WorkspaceStrategy.WORKSPACE,
        base_dir=workspace_temp_dir / "workspaces",
        cleanup_on_complete=True,
        preserve_on_failure=True,
        use_hard_links=False,  # Force copy
    )


@pytest.fixture
def workspace_config_dict(workspace_temp_dir) -> Dict[str, Any]:
    """Create workspace config as dict for Ray transmission."""
    return {
        "enabled": True,
        "strategy": "workspace",
        "base_dir": str(workspace_temp_dir / "workspaces"),
        "cleanup_on_complete": True,
        "preserve_on_failure": True,
        "use_hard_links": True,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Event Bus Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def event_bus() -> WTBEventBus:
    """Create fresh event bus for testing."""
    return WTBEventBus(max_history=1000)


@pytest.fixture
def audit_trail() -> WTBAuditTrail:
    """Create audit trail for testing."""
    return WTBAuditTrail()


@pytest.fixture
def audit_listener(event_bus, audit_trail) -> AuditEventListener:
    """Create audit listener attached to event bus."""
    listener = AuditEventListener(audit_trail)
    listener.attach(event_bus)
    return listener


@pytest.fixture
def event_collector(event_bus) -> Dict[str, List]:
    """Create event collector for workspace events."""
    collector = {
        "workspace_created": [],
        "workspace_activated": [],
        "workspace_deactivated": [],
        "workspace_cleaned_up": [],
        "ray_batch_started": [],
        "ray_batch_completed": [],
        "ray_variant_started": [],
        "ray_variant_completed": [],
        "all": [],
    }
    
    def capture(event):
        collector["all"].append(event)
        if isinstance(event, WorkspaceCreatedEvent):
            collector["workspace_created"].append(event)
        elif isinstance(event, WorkspaceActivatedEvent):
            collector["workspace_activated"].append(event)
        elif isinstance(event, WorkspaceDeactivatedEvent):
            collector["workspace_deactivated"].append(event)
        elif isinstance(event, WorkspaceCleanedUpEvent):
            collector["workspace_cleaned_up"].append(event)
        elif isinstance(event, RayBatchTestStartedEvent):
            collector["ray_batch_started"].append(event)
        elif isinstance(event, RayBatchTestCompletedEvent):
            collector["ray_batch_completed"].append(event)
        elif isinstance(event, RayVariantExecutionStartedEvent):
            collector["ray_variant_started"].append(event)
        elif isinstance(event, RayVariantExecutionCompletedEvent):
            collector["ray_variant_completed"].append(event)
    
    # Subscribe to all relevant events
    event_bus.subscribe(WorkspaceCreatedEvent, capture)
    event_bus.subscribe(WorkspaceActivatedEvent, capture)
    event_bus.subscribe(WorkspaceDeactivatedEvent, capture)
    event_bus.subscribe(WorkspaceCleanedUpEvent, capture)
    event_bus.subscribe(RayBatchTestStartedEvent, capture)
    event_bus.subscribe(RayBatchTestCompletedEvent, capture)
    event_bus.subscribe(RayVariantExecutionStartedEvent, capture)
    event_bus.subscribe(RayVariantExecutionCompletedEvent, capture)
    
    return collector


# ═══════════════════════════════════════════════════════════════════════════════
# Workspace Manager Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def workspace_manager(workspace_config, event_bus) -> WorkspaceManager:
    """Create workspace manager with event bus."""
    manager = WorkspaceManager(
        config=workspace_config,
        event_bus=event_bus,
        session_id=f"test-session-{uuid.uuid4().hex[:8]}",
    )
    
    yield manager
    
    # Cleanup all workspaces
    for ws_id in list(manager._workspaces.keys()):
        try:
            manager.cleanup_workspace(ws_id)
        except Exception:
            pass


@pytest.fixture
def workspace_manager_no_events(workspace_config) -> WorkspaceManager:
    """Create workspace manager without event bus."""
    manager = WorkspaceManager(
        config=workspace_config,
        event_bus=None,
        session_id=f"test-session-{uuid.uuid4().hex[:8]}",
    )
    
    yield manager
    
    # Cleanup
    for ws_id in list(manager._workspaces.keys()):
        try:
            manager.cleanup_workspace(ws_id)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# Ray Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def ray_initialized():
    """Initialize Ray for module (expensive operation)."""
    ray_available = False
    
    try:
        import ray
        
        if not ray.is_initialized():
            # Initialize Ray in local mode
            ray.init(
                num_cpus=4,
                include_dashboard=False,
                ignore_reinit_error=True,
                log_to_driver=False,
            )
        
        ray_available = True
        yield ray_available
        
    except ImportError:
        yield False
    except Exception as e:
        print(f"Ray initialization failed: {e}")
        yield False


@pytest.fixture
def ray_config():
    """Create Ray config for local testing."""
    try:
        from wtb.config import RayConfig
        return RayConfig.for_local_development()
    except ImportError:
        return {"num_cpus": 2, "local_mode": True}


def skip_if_no_ray():
    """Decorator to skip tests if Ray is not available."""
    try:
        import ray
        return pytest.mark.skipif(False, reason="")
    except ImportError:
        return pytest.mark.skip(reason="Ray not installed")


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def memory_checkpointer():
    """Create in-memory LangGraph checkpointer."""
    try:
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()
    except ImportError:
        return MagicMock()


@pytest.fixture
def simple_workflow(memory_checkpointer):
    """Create simple linear workflow for testing."""
    try:
        from langgraph.graph import StateGraph, END
        from typing import TypedDict, Annotated
        from operator import add
        
        class SimpleState(TypedDict):
            messages: Annotated[List[str], add]
            count: int
        
        def node_a(state: SimpleState) -> dict:
            return {"messages": ["A processed"], "count": state["count"] + 1}
        
        def node_b(state: SimpleState) -> dict:
            return {"messages": ["B processed"], "count": state["count"] + 1}
        
        def node_c(state: SimpleState) -> dict:
            return {"messages": ["C processed"], "count": state["count"] + 1}
        
        workflow = StateGraph(SimpleState)
        workflow.add_node("node_a", node_a)
        workflow.add_node("node_b", node_b)
        workflow.add_node("node_c", node_c)
        
        workflow.set_entry_point("node_a")
        workflow.add_edge("node_a", "node_b")
        workflow.add_edge("node_b", "node_c")
        workflow.add_edge("node_c", END)
        
        return workflow.compile(checkpointer=memory_checkpointer)
        
    except ImportError:
        return MagicMock()


@pytest.fixture
def branching_workflow(memory_checkpointer):
    """Create branching workflow for rollback/fork testing."""
    try:
        from langgraph.graph import StateGraph, END
        from typing import TypedDict, Annotated
        from operator import add
        
        class BranchState(TypedDict):
            messages: Annotated[List[str], add]
            count: int
            branch: str
        
        def node_start(state: BranchState) -> dict:
            return {"messages": ["Started"], "count": state["count"] + 1}
        
        def node_branch_a(state: BranchState) -> dict:
            return {"messages": ["Branch A"], "count": state["count"] + 1, "branch": "A"}
        
        def node_branch_b(state: BranchState) -> dict:
            return {"messages": ["Branch B"], "count": state["count"] + 1, "branch": "B"}
        
        def node_end(state: BranchState) -> dict:
            return {"messages": ["Ended"], "count": state["count"] + 1}
        
        def router(state: BranchState) -> str:
            if state["count"] > 2:
                return "branch_a"
            return "branch_b"
        
        workflow = StateGraph(BranchState)
        workflow.add_node("start", node_start)
        workflow.add_node("branch_a", node_branch_a)
        workflow.add_node("branch_b", node_branch_b)
        workflow.add_node("end", node_end)
        
        workflow.set_entry_point("start")
        workflow.add_conditional_edges("start", router)
        workflow.add_edge("branch_a", "end")
        workflow.add_edge("branch_b", "end")
        workflow.add_edge("end", END)
        
        return workflow.compile(checkpointer=memory_checkpointer)
        
    except ImportError:
        return MagicMock()


def skip_if_no_langgraph():
    """Decorator to skip tests if LangGraph is not available."""
    try:
        from langgraph.graph import StateGraph
        return pytest.mark.skipif(False, reason="")
    except ImportError:
        return pytest.mark.skip(reason="LangGraph not installed")


# ═══════════════════════════════════════════════════════════════════════════════
# Venv Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def venv_spec():
    """Create sample venv specification."""
    return {
        "python_version": "3.12",
        "dependencies": ["langchain>=0.1.0", "faiss-cpu>=1.7.0"],
        "requirements_file": None,
    }


@pytest.fixture
def venv_spec_alternate():
    """Create alternate venv specification (different deps)."""
    return {
        "python_version": "3.12",
        "dependencies": ["langchain>=0.2.0", "faiss-gpu>=1.8.0"],
        "requirements_file": None,
    }


@pytest.fixture
def mock_uv_venv_manager():
    """Create mock UV venv manager client."""
    mock = MagicMock()
    mock.create_environment = MagicMock(return_value={
        "env_id": f"env-{uuid.uuid4().hex[:8]}",
        "python_path": "/fake/venv/bin/python",
        "status": "created",
    })
    mock.activate_environment = MagicMock(return_value=True)
    mock.deactivate_environment = MagicMock(return_value=True)
    mock.delete_environment = MagicMock(return_value=True)
    return mock


# ═══════════════════════════════════════════════════════════════════════════════
# FileTracker Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_file_tracker():
    """Create mock file tracker service."""
    mock = MagicMock()
    mock.track_files = MagicMock(return_value={
        "commit_id": f"commit-{uuid.uuid4().hex[:8]}",
        "files_tracked": 5,
        "total_size": 10240,
    })
    mock.track_and_link = MagicMock(return_value={
        "commit_id": f"commit-{uuid.uuid4().hex[:8]}",
        "checkpoint_id": 1,
    })
    mock.restore_from_checkpoint = MagicMock(return_value={
        "files_restored": 5,
        "total_size": 10240,
    })
    mock.restore_to_workspace = MagicMock(return_value={
        "files_restored": 5,
        "workspace_path": "/fake/workspace",
    })
    return mock


@pytest.fixture
def in_memory_file_repos():
    """Create in-memory file processing repositories."""
    from tests.test_file_processing.conftest import (
        InMemoryBlobRepository,
        InMemoryFileCommitRepository,
        InMemoryCheckpointFileLinkRepository,
    )
    
    return {
        "blob_repo": InMemoryBlobRepository(),
        "commit_repo": InMemoryFileCommitRepository(),
        "link_repo": InMemoryCheckpointFileLinkRepository(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Execution Context Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def execution_context():
    """Create standard execution context."""
    execution_id = f"exec-{uuid.uuid4().hex[:8]}"
    return {
        "execution_id": execution_id,
        "workflow_id": f"wf-{uuid.uuid4().hex[:8]}",
        "thread_id": f"thread-{execution_id}",
        "batch_id": f"batch-{uuid.uuid4().hex[:8]}",
    }


@pytest.fixture
def batch_context():
    """Create batch test context with multiple variants."""
    batch_id = f"batch-{uuid.uuid4().hex[:8]}"
    return {
        "batch_id": batch_id,
        "variants": ["bm25", "dense", "hybrid"],
        "workflow_id": f"wf-{uuid.uuid4().hex[:8]}",
        "test_cases": [
            {"query": "test query 1", "expected": "result 1"},
            {"query": "test query 2", "expected": "result 2"},
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════


def create_variant_workspaces(
    manager: WorkspaceManager,
    batch_id: str,
    variants: List[str],
    source_paths: Optional[List[Path]] = None,
) -> List[Workspace]:
    """Helper to create workspaces for multiple variants."""
    workspaces = []
    for variant in variants:
        ws = manager.create_workspace(
            batch_id=batch_id,
            variant_name=variant,
            execution_id=f"exec-{variant}-{uuid.uuid4().hex[:8]}",
            source_paths=source_paths or [],
        )
        workspaces.append(ws)
    return workspaces


def simulate_variant_execution(
    workspace: Workspace,
    variant_name: str,
    output_data: Dict[str, Any],
) -> Path:
    """Helper to simulate variant execution in workspace."""
    original_cwd = Path.cwd()
    
    try:
        workspace.activate()
        
        # Write output file
        output_file = workspace.output_dir / "results.json"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(json.dumps({
            "variant": variant_name,
            "workspace_id": workspace.workspace_id,
            **output_data,
        }))
        
        return output_file
        
    finally:
        workspace.deactivate()
        os.chdir(original_cwd)


@pytest.fixture
def create_variant_workspaces_helper(workspace_manager):
    """Fixture providing variant workspace creation helper."""
    def _create(batch_id: str, variants: List[str], source_paths=None):
        return create_variant_workspaces(
            workspace_manager, batch_id, variants, source_paths
        )
    return _create
