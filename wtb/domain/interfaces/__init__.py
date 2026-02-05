"""
Domain Interfaces - Abstract contracts (Ports) for the domain layer.

PRIMARY Interfaces (Use These):
- ICheckpointStore: Checkpoint persistence (LangGraph-native)
- IExecutionController: Workflow execution lifecycle
- IBatchTestRunner: Batch test orchestration
- IVariantRegistry: Node variant management
- IUnitOfWork: Transaction boundaries

DEPRECATED Interfaces (Legacy, Backward Compat Only - 2026-01-27):
- IStateAdapter: AgentGit-centric, replaced by ICheckpointStore
  See _deprecated.py for migration guide.
"""

from .execution_controller import IExecutionController
from .node_replacer import INodeReplacer, IVariantRegistry, INodeSwapper

# PRIMARY: Checkpoint persistence (LangGraph-native)
from .checkpoint_store import (
    ICheckpointStore,
    ICheckpointStoreFactory,
)

# DEPRECATED (2026-01-27): AgentGit-centric state adapter
# These are kept for backward compatibility only.
# New code should use ICheckpointStore instead.
from .state_adapter import (
    IStateAdapter,  # DEPRECATED - use ICheckpointStore
    CheckpointTrigger,  # Still valid - enum for checkpoint triggers
    CheckpointInfo,  # Still valid - lightweight checkpoint info
    NodeBoundaryInfo,  # Still valid - node boundary tracking
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
    IAuditLogRepository,
    INodeBoundaryRepository,
    # ICheckpointFileRepository REMOVED (2026-01-27) - Use ICheckpointFileLinkRepository
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
from .batch_runner import (
    IBatchTestRunner,
    IEnvironmentProvider,
    BatchRunnerStatus,
    BatchRunnerProgress,
    BatchRunnerError,
    BatchRunnerConfigError,
    BatchRunnerExecutionError,
)
# Batch Coordinator (v1.8 - 2026-02-05)
from .batch_coordinator import (
    IBatchExecutionCoordinator,
    IExecutionControllerFactory,
    OperationType,
    BatchOperationRequest,
    BatchOperationResult,
)
from .file_tracking import (
    IFileTrackingService,
    IFileTrackingServiceFactory,
    TrackedFile,
    FileTrackingResult,
    FileRestoreResult,
    FileTrackingLink,
    FileTrackingError,
    FileNotFoundError as FileTrackingFileNotFoundError,
    CommitNotFoundError,
    CheckpointLinkError,
)
from .file_processing_repository import (
    IBlobRepository,
    IFileCommitRepository,
    ICheckpointFileLinkRepository,
    IFileProcessingUnitOfWork,
)

# API Service Interfaces (v2.0 - 2026-01-28)
from .api_services import (
    IExecutionAPIService,
    IAuditAPIService,
    IBatchTestAPIService,
    IWorkflowAPIService,
    ExecutionDTO,
    ControlResultDTO,
    RollbackResultDTO,
    CheckpointDTO,
    PaginatedResultDTO,
    AuditEventDTO,
    AuditSummaryDTO,
    BatchTestDTO,
    BatchTestProgressDTO,
    WorkflowDTO,
)

__all__ = [
    # ═══════════════════════════════════════════════════════════════════
    # PRIMARY INTERFACES (Use These)
    # ═══════════════════════════════════════════════════════════════════
    
    # Checkpoint Store (PRIMARY - LangGraph-native, DDD-compliant)
    "ICheckpointStore",
    "ICheckpointStoreFactory",
    
    # Execution Controller
    "IExecutionController",
    
    # Node Replacer / Variant Registry
    "INodeReplacer",
    "IVariantRegistry",
    "INodeSwapper",
    
    # ═══════════════════════════════════════════════════════════════════
    # DEPRECATED INTERFACES (Backward Compat Only)
    # ═══════════════════════════════════════════════════════════════════
    
    # State Adapter (DEPRECATED - AgentGit-centric, use ICheckpointStore)
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
    "IAuditLogRepository",
    "INodeBoundaryRepository",
    # ICheckpointFileRepository REMOVED (2026-01-27) - Use ICheckpointFileLinkRepository
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
    # Batch Runner
    "IBatchTestRunner",
    "IEnvironmentProvider",
    "BatchRunnerStatus",
    "BatchRunnerProgress",
    "BatchRunnerError",
    "BatchRunnerConfigError",
    "BatchRunnerExecutionError",
    # Batch Coordinator (v1.8)
    "IBatchExecutionCoordinator",
    "IExecutionControllerFactory",
    "OperationType",
    "BatchOperationRequest",
    "BatchOperationResult",
    # File Tracking (2026-01-15, renamed 2026-01-16)
    "IFileTrackingService",
    "IFileTrackingServiceFactory",
    "TrackedFile",
    "FileTrackingResult",
    "FileRestoreResult",
    "FileTrackingLink",  # Renamed from CheckpointFileLink to avoid confusion with domain model
    "FileTrackingError",
    "FileTrackingFileNotFoundError",
    "CommitNotFoundError",
    "CheckpointLinkError",
    # File Processing Repository (2026-01-15)
    "IBlobRepository",
    "IFileCommitRepository",
    "ICheckpointFileLinkRepository",
    "IFileProcessingUnitOfWork",
    # API Service Interfaces (v2.0 - 2026-01-28)
    "IExecutionAPIService",
    "IAuditAPIService",
    "IBatchTestAPIService",
    "IWorkflowAPIService",
    "ExecutionDTO",
    "ControlResultDTO",
    "RollbackResultDTO",
    "CheckpointDTO",
    "PaginatedResultDTO",
    "AuditEventDTO",
    "AuditSummaryDTO",
    "BatchTestDTO",
    "BatchTestProgressDTO",
    "WorkflowDTO",
]
