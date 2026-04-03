"""
Pytest fixtures for SDK integration tests.

Provides:
- WTBTestBench instances (in-memory, SQLite, LangGraph)
- Mock and real graph factories
- Ray initialization
- Virtual environment mocks
- File tracking fixtures
- Shared test workflows
"""

import pytest
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass
from unittest.mock import MagicMock, Mock
from datetime import datetime, timezone

# LangGraph imports
try:
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.memory import MemorySaver
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False

# Ray import check
try:
    import ray
    RAY_AVAILABLE = True
except ImportError:
    RAY_AVAILABLE = False

# WTB imports
from wtb.sdk import WTBTestBench, WorkflowProject
from wtb.sdk.workflow_project import (
    FileTrackingConfig,
    EnvironmentConfig,
    ExecutionConfig,
    EnvSpec,
    NodeVariant,
)
from wtb.sdk.test_bench import WTBTestBenchBuilder, RollbackResult, ForkResult
from wtb.application.factories import WTBTestBenchFactory, BatchTestRunnerFactory
from wtb.application.services import ProjectService, VariantService
from wtb.config import WTBConfig, RayConfig, FileTrackingConfig as WTBFileTrackingConfig
from wtb.domain.models import ExecutionStatus
from wtb.domain.models.batch_test import BatchTest, BatchTestStatus, VariantCombination
from wtb.infrastructure.database import InMemoryUnitOfWork
from wtb.infrastructure.adapters import InMemoryStateAdapter


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph Test Graph Definitions
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class SimpleState:
    """Simple state for testing."""
    messages: List[str]
    count: int = 0
    result: Optional[str] = None


def create_simple_state_schema():
    """Create TypedDict for simple state."""
    from typing_extensions import TypedDict
    
    class SimpleStateDict(TypedDict, total=False):
        messages: List[str]
        count: int
        result: Optional[str]
    
    return SimpleStateDict


@pytest.fixture
def simple_state_schema():
    """Simple state schema fixture."""
    return create_simple_state_schema()


@pytest.fixture
def simple_graph_factory():
    """Factory that creates a simple 3-node linear graph."""
    def create_graph():
        if not LANGGRAPH_AVAILABLE:
            pytest.skip("LangGraph not installed")
        
        SimpleStateDict = create_simple_state_schema()
        
        def node_a(state: Dict[str, Any]) -> Dict[str, Any]:
            messages = state.get("messages", [])
            return {"messages": messages + ["A"], "count": state.get("count", 0) + 1}
        
        def node_b(state: Dict[str, Any]) -> Dict[str, Any]:
            messages = state.get("messages", [])
            return {"messages": messages + ["B"], "count": state.get("count", 0) + 1}
        
        def node_c(state: Dict[str, Any]) -> Dict[str, Any]:
            messages = state.get("messages", [])
            return {"messages": messages + ["C"], "count": state.get("count", 0) + 1}
        
        builder = StateGraph(SimpleStateDict)
        builder.add_node("node_a", node_a)
        builder.add_node("node_b", node_b)
        builder.add_node("node_c", node_c)
        builder.add_edge("__start__", "node_a")
        builder.add_edge("node_a", "node_b")
        builder.add_edge("node_b", "node_c")
        builder.add_edge("node_c", END)
        
        # Return uncompiled builder so checkpointer can be added by adapter
        return builder
    
    return create_graph


@pytest.fixture
def branching_graph_factory():
    """Factory that creates a graph with conditional branching."""
    def create_graph():
        if not LANGGRAPH_AVAILABLE:
            pytest.skip("LangGraph not installed")
        
        from typing_extensions import TypedDict
        
        class BranchingState(TypedDict, total=False):
            messages: List[str]
            branch: str
            count: int
        
        def start_node(state: Dict[str, Any]) -> Dict[str, Any]:
            return {"messages": state.get("messages", []) + ["start"], "count": 1}
        
        def branch_a(state: Dict[str, Any]) -> Dict[str, Any]:
            return {"messages": state["messages"] + ["branch_a"], "count": state["count"] + 1}
        
        def branch_b(state: Dict[str, Any]) -> Dict[str, Any]:
            return {"messages": state["messages"] + ["branch_b"], "count": state["count"] + 1}
        
        def end_node(state: Dict[str, Any]) -> Dict[str, Any]:
            return {"messages": state["messages"] + ["end"], "count": state["count"] + 1}
        
        def decide_branch(state: Dict[str, Any]) -> str:
            branch = state.get("branch", "a")
            return f"branch_{branch}"
        
        builder = StateGraph(BranchingState)
        builder.add_node("start", start_node)
        builder.add_node("branch_a", branch_a)
        builder.add_node("branch_b", branch_b)
        builder.add_node("end", end_node)
        
        builder.add_edge("__start__", "start")
        builder.add_conditional_edges("start", decide_branch, {
            "branch_a": "branch_a",
            "branch_b": "branch_b",
        })
        builder.add_edge("branch_a", "end")
        builder.add_edge("branch_b", "end")
        builder.add_edge("end", END)
        
        # Return uncompiled builder so checkpointer can be added by adapter
        return builder
    
    return create_graph


@pytest.fixture
def pausable_graph_factory():
    """Factory that creates a graph with built-in pause points."""
    def create_graph():
        if not LANGGRAPH_AVAILABLE:
            pytest.skip("LangGraph not installed")
        
        from typing_extensions import TypedDict
        
        class PausableState(TypedDict, total=False):
            messages: List[str]
            paused: bool
            pause_at: Optional[str]
        
        def node_1(state: Dict[str, Any]) -> Dict[str, Any]:
            time.sleep(0.01)  # Small delay
            return {"messages": state.get("messages", []) + ["node_1"]}
        
        def node_2(state: Dict[str, Any]) -> Dict[str, Any]:
            time.sleep(0.01)
            return {"messages": state.get("messages", []) + ["node_2"]}
        
        def node_3(state: Dict[str, Any]) -> Dict[str, Any]:
            time.sleep(0.01)
            return {"messages": state.get("messages", []) + ["node_3"]}
        
        builder = StateGraph(PausableState)
        builder.add_node("node_1", node_1)
        builder.add_node("node_2", node_2)
        builder.add_node("node_3", node_3)
        builder.add_edge("__start__", "node_1")
        builder.add_edge("node_1", "node_2")
        builder.add_edge("node_2", "node_3")
        builder.add_edge("node_3", END)
        
        # Return uncompiled builder so checkpointer can be added by adapter
        return builder
    
    return create_graph


@pytest.fixture
def file_processing_graph_factory(tmp_path):
    """Factory that creates a graph that processes files."""
    def create_graph():
        if not LANGGRAPH_AVAILABLE:
            pytest.skip("LangGraph not installed")
        
        from typing_extensions import TypedDict
        
        class FileState(TypedDict, total=False):
            files_processed: List[str]
            file_count: int
            workspace_path: str
        
        def create_file_node(state: Dict[str, Any]) -> Dict[str, Any]:
            workspace = Path(state.get("workspace_path", str(tmp_path)))
            workspace.mkdir(exist_ok=True)
            
            file_path = workspace / f"file_{uuid.uuid4().hex[:8]}.txt"
            file_path.write_text("Test content")
            
            files = state.get("files_processed", [])
            return {
                "files_processed": files + [str(file_path)],
                "file_count": len(files) + 1,
            }
        
        def modify_file_node(state: Dict[str, Any]) -> Dict[str, Any]:
            files = state.get("files_processed", [])
            for fp in files:
                path = Path(fp)
                if path.exists():
                    content = path.read_text()
                    path.write_text(content + "\nModified")
            return {"files_processed": files}
        
        def finalize_node(state: Dict[str, Any]) -> Dict[str, Any]:
            return {"file_count": state.get("file_count", 0)}
        
        builder = StateGraph(FileState)
        builder.add_node("create_file", create_file_node)
        builder.add_node("modify_file", modify_file_node)
        builder.add_node("finalize", finalize_node)
        builder.add_edge("__start__", "create_file")
        builder.add_edge("create_file", "modify_file")
        builder.add_edge("modify_file", "finalize")
        builder.add_edge("finalize", END)
        
        # Return uncompiled builder so checkpointer can be added by adapter
        return builder
    
    return create_graph


# ═══════════════════════════════════════════════════════════════════════════════
# WTBTestBench Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def wtb_inmemory():
    """Create WTBTestBench with in-memory backend."""
    return WTBTestBenchFactory.create_for_testing()


@pytest.fixture
def wtb_sqlite(tmp_path):
    """Create WTBTestBench with SQLite backend."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    return WTBTestBenchFactory.create_for_development(data_dir)


@pytest.fixture
def wtb_langgraph():
    """Create WTBTestBench with LangGraph checkpointer."""
    if not LANGGRAPH_AVAILABLE:
        pytest.skip("LangGraph not installed")
    
    # Create fresh instance each time to ensure test isolation
    from wtb.infrastructure.adapters.langgraph_state_adapter import (
        LangGraphStateAdapter,
        LangGraphConfig,
        CheckpointerType,
    )
    from wtb.infrastructure.database import InMemoryUnitOfWork
    from wtb.application.factories import ExecutionControllerFactory, NodeReplacerFactory
    from wtb.application.services import ProjectService, VariantService
    from wtb.sdk.test_bench import WTBTestBench
    
    # Create all dependencies fresh
    uow = InMemoryUnitOfWork()
    lg_config = LangGraphConfig(checkpointer_type=CheckpointerType.MEMORY)
    state_adapter = LangGraphStateAdapter(lg_config)
    
    exec_ctrl = ExecutionControllerFactory.create_with_dependencies(
        uow=uow,
        state_adapter=state_adapter,
    )
    variant_registry = NodeReplacerFactory.create_with_dependencies(uow)
    project_service = ProjectService(uow)
    variant_service = VariantService(uow, variant_registry)
    
    return WTBTestBench(
        project_service=project_service,
        variant_service=variant_service,
        execution_controller=exec_ctrl,
        batch_runner=None,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# WorkflowProject Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def simple_project(simple_graph_factory):
    """Create a simple WorkflowProject."""
    return WorkflowProject(
        name=f"simple_workflow_{uuid.uuid4().hex[:8]}",
        graph_factory=simple_graph_factory,
        description="Simple 3-node linear workflow for testing",
    )


@pytest.fixture
def branching_project(branching_graph_factory):
    """Create a branching WorkflowProject."""
    return WorkflowProject(
        name=f"branching_workflow_{uuid.uuid4().hex[:8]}",
        graph_factory=branching_graph_factory,
        description="Workflow with conditional branching",
    )


@pytest.fixture
def pausable_project(pausable_graph_factory):
    """Create a pausable WorkflowProject."""
    return WorkflowProject(
        name=f"pausable_workflow_{uuid.uuid4().hex[:8]}",
        graph_factory=pausable_graph_factory,
        description="Workflow with pause points",
    )


@pytest.fixture
def file_processing_project(file_processing_graph_factory, tmp_path):
    """Create a file processing WorkflowProject."""
    project = WorkflowProject(
        name=f"file_workflow_{uuid.uuid4().hex[:8]}",
        graph_factory=file_processing_graph_factory,
        description="Workflow that creates/modifies files",
        file_tracking=FileTrackingConfig(
            enabled=True,
            tracked_paths=[str(tmp_path)],
        ),
    )
    return project


@pytest.fixture
def project_with_variants(simple_graph_factory):
    """Create a project with registered node variants."""
    project = WorkflowProject(
        name=f"variant_workflow_{uuid.uuid4().hex[:8]}",
        graph_factory=simple_graph_factory,
        description="Workflow with node variants",
    )
    
    # Register variants for node_b
    def variant_b_fast(state: Dict[str, Any]) -> Dict[str, Any]:
        return {"messages": state.get("messages", []) + ["B_FAST"], "count": state.get("count", 0) + 1}
    
    def variant_b_slow(state: Dict[str, Any]) -> Dict[str, Any]:
        time.sleep(0.05)
        return {"messages": state.get("messages", []) + ["B_SLOW"], "count": state.get("count", 0) + 1}
    
    project.register_variant("node_b", "fast", variant_b_fast, "Fast variant")
    project.register_variant("node_b", "slow", variant_b_slow, "Slow variant")
    
    return project


# ═══════════════════════════════════════════════════════════════════════════════
# Ray Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def ray_initialized():
    """Initialize Ray for integration tests (module scope)."""
    if not RAY_AVAILABLE:
        pytest.skip("Ray not installed")
    
    if not ray.is_initialized():
        try:
            # Try to connect to existing cluster first (no resource specification)
            ray.init(address="auto", ignore_reinit_error=True, log_to_driver=False)
        except ConnectionError:
            # No existing cluster, start a local one with resources
            ray.init(
                num_cpus=2,
                ignore_reinit_error=True,
                log_to_driver=False,
            )
    
    yield ray
    
    # Don't shutdown - other tests might use it


@pytest.fixture
def ray_config():
    """Get Ray testing configuration."""
    return RayConfig.for_testing()


@pytest.fixture
def ray_batch_runner(tmp_path):
    """Create Ray batch test runner."""
    if not RAY_AVAILABLE:
        pytest.skip("Ray not installed")
    
    from wtb.application.services.ray_batch_runner import RayBatchTestRunner
    
    config = RayConfig.for_testing()
    runner = RayBatchTestRunner(
        config=config,
        agentgit_db_url=str(tmp_path / "agentgit.db"),
        wtb_db_url=f"sqlite:///{tmp_path / 'wtb.db'}",
    )
    
    yield runner
    
    runner.shutdown()


# ═══════════════════════════════════════════════════════════════════════════════
# Virtual Environment Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class MockVenvSpec:
    """Mock venv specification."""
    env_id: str
    python_version: str = "3.12"
    packages: List[str] = None
    
    def __post_init__(self):
        self.packages = self.packages or []


@dataclass
class MockVenvStatus:
    """Mock venv status."""
    env_id: str
    status: str = "ready"
    python_path: Optional[str] = None
    error: Optional[str] = None


class MockVenvProvider:
    """Mock virtual environment provider."""
    
    def __init__(self):
        self._environments: Dict[str, MockVenvStatus] = {}
        self._specs: Dict[str, MockVenvSpec] = {}
    
    def create_environment(self, spec: MockVenvSpec) -> str:
        """Create a new environment."""
        self._specs[spec.env_id] = spec
        self._environments[spec.env_id] = MockVenvStatus(
            env_id=spec.env_id,
            status="ready",
            python_path=f"/mock/envs/{spec.env_id}/bin/python",
        )
        return spec.env_id
    
    def get_status(self, env_id: str) -> Optional[MockVenvStatus]:
        """Get environment status."""
        return self._environments.get(env_id)
    
    def destroy_environment(self, env_id: str) -> bool:
        """Destroy an environment."""
        if env_id in self._environments:
            self._environments[env_id].status = "destroyed"
            return True
        return False
    
    def list_environments(self) -> List[str]:
        """List all environments."""
        return list(self._environments.keys())


@pytest.fixture
def mock_venv_provider():
    """Create mock venv provider."""
    return MockVenvProvider()


@pytest.fixture
def venv_project(simple_graph_factory):
    """Create project with environment configuration."""
    project = WorkflowProject(
        name=f"venv_workflow_{uuid.uuid4().hex[:8]}",
        graph_factory=simple_graph_factory,
        description="Workflow with venv configuration",
        environment=EnvironmentConfig(
            granularity="workflow",
            use_current_env=False,
            default_env=EnvSpec(
                python_version="3.12",
                dependencies=["langgraph>=0.2"],
            ),
        ),
    )
    return project


# ═══════════════════════════════════════════════════════════════════════════════
# File Tracking Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def temp_workspace(tmp_path):
    """Create temporary workspace directory."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture
def temp_files(temp_workspace):
    """Create temporary test files."""
    files = {}
    for i in range(3):
        name = f"test_file_{i}.txt"
        path = temp_workspace / name
        content = f"Test content {i}\n" * 10
        content_bytes = content.encode("utf-8")
        # Write in binary mode to ensure consistent line endings across platforms
        path.write_bytes(content_bytes)
        files[name] = {
            "path": str(path),
            "content": content_bytes,
            "size": len(content_bytes),
        }
    return files


@pytest.fixture
def file_tracking_config(temp_workspace):
    """Create file tracking configuration."""
    return WTBFileTrackingConfig.for_development(
        storage_path=str(temp_workspace / "file_storage"),
        wtb_db_url=f"sqlite:///{temp_workspace / 'wtb.db'}",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Batch Testing Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def sample_variant_matrix():
    """Sample variant configuration matrix."""
    return [
        {"node_b": "default"},
        {"node_b": "fast"},
        {"node_b": "slow"},
    ]


@pytest.fixture
def sample_test_cases():
    """Sample test cases for batch testing."""
    return [
        {"messages": [], "count": 0},
        {"messages": ["init"], "count": 10},
        {"messages": ["start", "middle"], "count": 100},
    ]


@pytest.fixture
def sample_batch_test(simple_project):
    """Create sample batch test."""
    return BatchTest(
        id=f"batch_{uuid.uuid4().hex[:8]}",
        name="Sample Batch Test",
        workflow_id=simple_project.id,
        variant_combinations=[
            VariantCombination(
                name="Config A",
                variants={"node_b": "fast"},
                metadata={"description": "Fast variant"},
            ),
            VariantCombination(
                name="Config B",
                variants={"node_b": "slow"},
                metadata={"description": "Slow variant"},
            ),
        ],
        initial_state={"messages": [], "count": 0},
        parallel_count=2,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════


def create_initial_state() -> Dict[str, Any]:
    """Create initial state for simple graph."""
    return {"messages": [], "count": 0}


def create_branching_state(branch: str = "a") -> Dict[str, Any]:
    """Create initial state for branching graph."""
    return {"messages": [], "branch": branch, "count": 0}


def create_file_state(workspace_path: str) -> Dict[str, Any]:
    """Create initial state for file processing graph."""
    return {"files_processed": [], "file_count": 0, "workspace_path": workspace_path}


@pytest.fixture
def initial_state():
    """Initial state fixture."""
    return create_initial_state()


@pytest.fixture
def branching_state():
    """Branching state fixture."""
    return create_branching_state()


# Export helpers for other test modules
__all__ = [
    "create_initial_state",
    "create_branching_state", 
    "create_file_state",
    "MockVenvProvider",
    "MockVenvSpec",
    "MockVenvStatus",
    "LANGGRAPH_AVAILABLE",
    "RAY_AVAILABLE",
]
