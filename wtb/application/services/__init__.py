"""
Application services - Concrete implementations of domain interfaces.

Architecture Decision (2026-01-15):
═══════════════════════════════════════════════════════════════════════════════

1. ONE ExecutionController + IStateAdapter pattern (Strategy/Adapter)
   - Use ExecutionController with any adapter: InMemory, AgentGit, LangGraph
   - LangGraph capabilities available via extended IStateAdapter interface

2. TWO BatchRunners implementing Strategy Pattern:
   - ThreadPoolBatchTestRunner: Local parallelism
   - RayBatchTestRunner: Distributed parallelism
   - Both can use ANY IStateAdapter (including LangGraphStateAdapter)

3. LangGraphNodeReplacer: Optional utility for native LangGraph development
   - Works directly with LangGraph StateGraph
   - Different domain from NodeReplacer (which works with WTB TestWorkflow)

Usage with LangGraph:
    from wtb.application.services import ExecutionController
    from wtb.infrastructure.adapters import LangGraphStateAdapter, LangGraphConfig
    
    # Create LangGraph-backed controller
    adapter = LangGraphStateAdapter(LangGraphConfig.for_production(db_url))
    controller = ExecutionController(
        execution_repository=exec_repo,
        workflow_repository=workflow_repo,
        state_adapter=adapter,  # LangGraph adapter injected here
    )
    
    # Time-travel available via extended interface:
    if controller.supports_time_travel():
        history = controller.get_checkpoint_history(execution_id)
"""

from .execution_controller import ExecutionController, DefaultNodeExecutor
from .node_replacer import NodeReplacer
from .batch_test_runner import ThreadPoolBatchTestRunner
from .project_service import ProjectService, VariantService, WorkflowConversionService
from .parity_checker import (
    ParityChecker,
    ParityCheckerConfig,
    ParityCheckResult,
    ParityDiscrepancy,
    ParityDiscrepancyType,
)

# Ray imports are conditional
try:
    from .ray_batch_runner import (
        RayBatchTestRunner,
        RayConfig,
        VariantExecutionActor,
        VariantExecutionResult,
    )
    RAY_AVAILABLE = True
except ImportError:
    RayBatchTestRunner = None
    RayConfig = None
    VariantExecutionActor = None
    VariantExecutionResult = None
    RAY_AVAILABLE = False

# Batch Execution Coordinator (v1.8 - 2026-02-05)
try:
    from .batch_execution_coordinator import (
        BatchExecutionCoordinator,
        DefaultExecutionControllerFactory,
    )
    BATCH_COORDINATOR_AVAILABLE = True
except ImportError:
    BatchExecutionCoordinator = None
    DefaultExecutionControllerFactory = None
    BATCH_COORDINATOR_AVAILABLE = False

# LangGraph node replacer (optional utility for native LangGraph development)
try:
    from .langgraph_node_replacer import (
        LangGraphNodeReplacer,
        LangGraphNodeVariant,
        GraphStructure,
        GraphNodeInfo,
        GraphEdgeInfo,
        VariantSet,
        capture_graph_structure,
    )
    LANGGRAPH_NODE_REPLACER_AVAILABLE = True
except ImportError:
    LangGraphNodeReplacer = None
    LangGraphNodeVariant = None
    GraphStructure = None
    GraphNodeInfo = None
    GraphEdgeInfo = None
    VariantSet = None
    capture_graph_structure = None
    LANGGRAPH_NODE_REPLACER_AVAILABLE = False

# Async execution controller (v2.0)
try:
    from .async_execution_controller import (
        AsyncExecutionController,
        AsyncExecutionControllerFactory,
        AsyncExecutionResult,
        AsyncStreamEvent,
    )
    ASYNC_EXECUTION_AVAILABLE = True
except ImportError:
    AsyncExecutionController = None
    AsyncExecutionControllerFactory = None
    AsyncExecutionResult = None
    AsyncStreamEvent = None
    ASYNC_EXECUTION_AVAILABLE = False

# API Services (v2.0 - 2026-01-28)
try:
    from .api_services import (
        ExecutionAPIService,
        AuditAPIService,
        BatchTestAPIService,
        WorkflowAPIService,
        APIServiceFactory,
    )
    API_SERVICES_AVAILABLE = True
except ImportError:
    ExecutionAPIService = None
    AuditAPIService = None
    BatchTestAPIService = None
    WorkflowAPIService = None
    APIServiceFactory = None
    API_SERVICES_AVAILABLE = False

__all__ = [
    # Core services
    "ExecutionController",
    "DefaultNodeExecutor",
    "NodeReplacer",
    "ProjectService",
    "VariantService",
    "WorkflowConversionService",
    # Batch runners (Strategy Pattern)
    "ThreadPoolBatchTestRunner",
    "RayBatchTestRunner",
    "RayConfig",
    "RAY_AVAILABLE",
    # Parity checker
    "ParityChecker",
    "ParityCheckerConfig",
    "ParityCheckResult",
    "ParityDiscrepancy",
    "ParityDiscrepancyType",
    # Ray specific
    "VariantExecutionActor",
    "VariantExecutionResult",
    # Batch Coordinator (v1.8)
    "BatchExecutionCoordinator",
    "DefaultExecutionControllerFactory",
    "BATCH_COORDINATOR_AVAILABLE",
    # LangGraph node replacer (optional)
    "LangGraphNodeReplacer",
    "LangGraphNodeVariant",
    "GraphStructure",
    "GraphNodeInfo",
    "GraphEdgeInfo",
    "VariantSet",
    "capture_graph_structure",
    "LANGGRAPH_NODE_REPLACER_AVAILABLE",
    # Async execution (v2.0)
    "AsyncExecutionController",
    "AsyncExecutionControllerFactory",
    "AsyncExecutionResult",
    "AsyncStreamEvent",
    "ASYNC_EXECUTION_AVAILABLE",
    # API Services (v2.0)
    "ExecutionAPIService",
    "AuditAPIService",
    "BatchTestAPIService",
    "WorkflowAPIService",
    "APIServiceFactory",
    "API_SERVICES_AVAILABLE",
]
