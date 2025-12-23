"""Domain Interfaces - Abstract contracts (Ports) for the domain layer."""

from .execution_controller import IExecutionController
from .node_replacer import INodeReplacer, IVariantRegistry, INodeSwapper
from .state_adapter import (
    IStateAdapter,
    CheckpointTrigger,
    CheckpointInfo,
    NodeBoundaryInfo,
)
from .repositories import (
    IRepository,
    IReadRepository,
    IWriteRepository,
    IWorkflowRepository,
    IExecutionRepository,
    INodeVariantRepository,
    IBatchTestRepository,
    IEvaluationResultRepository,
    INodeBoundaryRepository,
    ICheckpointFileRepository,
    IOutboxRepository,
)
from .unit_of_work import IUnitOfWork
from .node_executor import (
    INodeExecutor,
    INodeExecutorRegistry,
    NodeExecutionResult,
)
from .evaluator import (
    IEvaluator,
    IEvaluatorRegistry,
    IEvaluationEngine,
    EvaluationMetric,
    EvaluationScore,
)

__all__ = [
    # Execution Controller
    "IExecutionController",
    # Node Replacer
    "INodeReplacer",
    "IVariantRegistry",
    "INodeSwapper",
    # State Adapter
    "IStateAdapter",
    "CheckpointTrigger",
    "CheckpointInfo",
    "NodeBoundaryInfo",
    # Repositories
    "IRepository",
    "IReadRepository",
    "IWriteRepository",
    "IWorkflowRepository",
    "IExecutionRepository",
    "INodeVariantRepository",
    "IBatchTestRepository",
    "IEvaluationResultRepository",
    "INodeBoundaryRepository",
    "ICheckpointFileRepository",
    "IOutboxRepository",
    # Unit of Work
    "IUnitOfWork",
    # Node Executor
    "INodeExecutor",
    "INodeExecutorRegistry",
    "NodeExecutionResult",
    # Evaluator
    "IEvaluator",
    "IEvaluatorRegistry",
    "IEvaluationEngine",
    "EvaluationMetric",
    "EvaluationScore",
]
