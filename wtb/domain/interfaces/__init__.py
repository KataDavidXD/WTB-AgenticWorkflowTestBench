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

# API Service Interfaces (v2.0 - 2026-01-28)
from .api_services import (
    AuditEventDTO,
    AuditSummaryDTO,
    BatchTestDTO,
    BatchTestProgressDTO,
    CheckpointDTO,
    ControlResultDTO,
    ExecutionDTO,
    IAuditAPIService,
    IBatchTestAPIService,
    IExecutionAPIService,
    IWorkflowAPIService,
    PaginatedResultDTO,
    RollbackResultDTO,
    WorkflowDTO,
)

# Batch Coordinator (v1.8 - 2026-02-05)
from .batch_coordinator import (
    BatchOperationRequest,
    BatchOperationResult,
    IBatchExecutionCoordinator,
    IExecutionControllerFactory,
    OperationType,
)
from .batch_runner import (
    BatchRunnerConfigError,
    BatchRunnerError,
    BatchRunnerExecutionError,
    BatchRunnerProgress,
    BatchRunnerStatus,
    IBatchTestRunner,
    IEnvironmentProvider,
)

# PRIMARY: Checkpoint persistence (LangGraph-native)
from .checkpoint_store import (
    ICheckpointStore,
    ICheckpointStoreFactory,
)
from .evaluator import (
    EvaluationMetric,
    EvaluationScore,
    IEvaluationEngine,
    IEvaluator,
    IEvaluatorRegistry,
)
from .execution_controller import IExecutionController
from .file_processing_repository import (
    IBlobRepository,
    ICheckpointFileLinkRepository,
    IFileCommitRepository,
    IFileProcessingUnitOfWork,
)
from .file_tracking import (
    CheckpointLinkError,
    CommitNotFoundError,
    FileRestoreResult,
    FileTrackingError,
    FileTrackingLink,
    FileTrackingResult,
    IFileTrackingService,
    IFileTrackingServiceFactory,
    TrackedFile,
)
from .file_tracking import (
    FileNotFoundError as FileTrackingFileNotFoundError,
)
from .node_executor import (
    INodeExecutor,
    INodeExecutorRegistry,
    NodeExecutionResult,
)
from .node_replacer import INodeReplacer, INodeSwapper, IVariantRegistry
from .repositories import (
    IAuditLogRepository,
    IBatchTestRepository,
    IEvaluationResultRepository,
    IExecutionRepository,
    INodeBoundaryRepository,
    INodeVariantRepository,
    # ICheckpointFileRepository REMOVED (2026-01-27) - Use ICheckpointFileLinkRepository
    IOutboxRepository,
    IReadRepository,
    IRepository,
    IWorkflowRepository,
    IWriteRepository,
)

# DEPRECATED (2026-01-27): AgentGit-centric state adapter
# These are kept for backward compatibility only.
# New code should use ICheckpointStore instead.
from .state_adapter import (
    CheckpointInfo,  # Still valid - lightweight checkpoint info
    CheckpointTrigger,  # Still valid - enum for checkpoint triggers
    IStateAdapter,  # DEPRECATED - use ICheckpointStore
    NodeBoundaryInfo,  # Still valid - node boundary tracking
)
from .unit_of_work import IUnitOfWork

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
