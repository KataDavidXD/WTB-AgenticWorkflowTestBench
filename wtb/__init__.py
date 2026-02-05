"""
Workflow Test Bench (WTB) - Testing Framework for Agent Git v0.2

A DDD-based orchestration layer for workflow testing with:
- Execution Controller: Manages workflow lifecycle (run/pause/resume/stop)
- Node Replacer: A/B testing via node variant substitution
- State Adapters: Pluggable persistence (AgentGit, InMemory, LangGraph)
- Evaluation Engine: Metrics and scoring
- External API: REST, WebSocket, gRPC
- SDK: WorkflowProject, WTBTestBench

Architecture follows:
- SOLID principles
- Domain-Driven Design
- Repository + Unit of Work pattern
- Interface-based abstractions

API Usage:
    from wtb.api import create_app
    app = create_app()
    
SDK Usage:
    from wtb.sdk import WorkflowProject, WTBTestBench
    
    project = WorkflowProject(name="my_workflow", graph_factory=create_graph)
    wtb = WTBTestBench()
    wtb.register_project(project)
    result = wtb.run(project="my_workflow", initial_state={})
"""

__version__ = "0.2.0"

# Domain Models
from wtb.domain.models import (
    TestWorkflow,
    WorkflowNode,
    WorkflowEdge,
    Execution,
    ExecutionState,
    ExecutionStatus,
    NodeVariant,
    NodeBoundary,
    CheckpointFileLink,  # 2026-01-27: Renamed from CheckpointFile
    BatchTest,
    BatchTestStatus,
    VariantCombination,
    BatchTestResult,
    EvaluationResult,
    MetricValue,
)

# Domain Interfaces
from wtb.domain.interfaces import (
    IExecutionController,
    INodeReplacer,
    IStateAdapter,
    IUnitOfWork,
    CheckpointTrigger,
    CheckpointInfo,
    NodeBoundaryInfo,
)

# Application Services
from wtb.application import (
    ExecutionController,
    NodeReplacer,
)

# Infrastructure
from wtb.infrastructure import (
    SQLAlchemyUnitOfWork,
    InMemoryStateAdapter,
)

# SDK (lazy import to avoid circular dependencies)
def _get_sdk():
    """Lazy import SDK components."""
    from wtb.sdk import (
        WorkflowProject,
        WTBTestBench,
        FileTrackingConfig,
        EnvironmentConfig,
        ExecutionConfig,
    )
    return {
        "WorkflowProject": WorkflowProject,
        "WTBTestBench": WTBTestBench,
        "FileTrackingConfig": FileTrackingConfig,
        "EnvironmentConfig": EnvironmentConfig,
        "ExecutionConfig": ExecutionConfig,
    }

# API (lazy import)
def _get_api():
    """Lazy import API components."""
    from wtb.api import create_app, get_app
    return {
        "create_app": create_app,
        "get_app": get_app,
    }

__all__ = [
    # Version
    "__version__",
    # Domain Models
    "TestWorkflow",
    "WorkflowNode",
    "WorkflowEdge",
    "Execution",
    "ExecutionState",
    "ExecutionStatus",
    "NodeVariant",
    "NodeBoundary",
    "CheckpointFileLink",  # 2026-01-27: Renamed from CheckpointFile
    "BatchTest",
    "BatchTestStatus",
    "VariantCombination",
    "BatchTestResult",
    "EvaluationResult",
    "MetricValue",
    # Domain Interfaces
    "IExecutionController",
    "INodeReplacer",
    "IStateAdapter",
    "IUnitOfWork",
    "CheckpointTrigger",
    "CheckpointInfo",
    "NodeBoundaryInfo",
    # Application Services
    "ExecutionController",
    "NodeReplacer",
    # Infrastructure
    "SQLAlchemyUnitOfWork",
    "InMemoryStateAdapter",
]
