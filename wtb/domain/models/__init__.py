"""Domain Models - Entities, Value Objects, and Aggregates."""

from .audit import AuditEntry
from .batch_test import (
    BatchTest,
    BatchTestResult,
    BatchTestStatus,
    VariantCombination,
)

# CheckpointFile REMOVED (2026-01-27) - Use CheckpointFileLink from file_processing
from .checkpoint import (
    Checkpoint,
    CheckpointId,
    CheckpointNotFoundError,
    ExecutionHistory,
    ExecutionHistoryError,
    InvalidRollbackTargetError,
)
from .evaluation import (
    ComparisonResult,
    EvaluationResult,
    MetricValue,
)
from .file_processing import (
    BlobId,
    CheckpointFileLink,
    CommitAlreadyFinalized,
    CommitId,
    CommitStatus,
    DuplicateFileError,
    FileCommit,
    FileMemento,
    FileProcessingError,
    InvalidBlobIdError,
    InvalidCommitIdError,
)
from .integrity import (
    IntegrityIssue,
    IntegrityIssueType,
    IntegrityReport,
    IntegritySeverity,
    RepairAction,
)
from .node_boundary import NodeBoundary, NodeStatus
from .outbox import (
    OutboxEvent,
    OutboxEventType,
    OutboxStatus,
)
from .workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    InvalidStateTransition,
    NodeVariant,
    TestWorkflow,
    WorkflowEdge,
    WorkflowNode,
)

# Backward compatibility alias (2026-01-27)
FileCheckpointLink = CheckpointFileLink
from .workspace import (
    CleanupReport,
    LinkMethod,
    LinkResult,
    OrphanWorkspace,
    Workspace,
    WorkspaceConfig,
    WorkspaceStrategy,
    compute_venv_spec_hash,
)

__all__ = [
    # Workflow models
    "WorkflowNode",
    "WorkflowEdge", 
    "TestWorkflow",
    "ExecutionState",
    "Execution",
    "ExecutionStatus",
    "NodeVariant",
    "InvalidStateTransition",
    # Node boundary
    "NodeBoundary",
    "NodeStatus",
    # Checkpoint-file link (2026-01-27: Consolidated to CheckpointFileLink)
    "CheckpointFileLink",
    # Checkpoint (DDD - 2026-01-15)
    "Checkpoint",
    "CheckpointId",
    "ExecutionHistory",
    "CheckpointNotFoundError",
    "InvalidRollbackTargetError",
    "ExecutionHistoryError",
    # Batch test
    "BatchTest",
    "BatchTestStatus",
    "VariantCombination",
    "BatchTestResult",
    # Evaluation
    "EvaluationResult",
    "MetricValue",
    "ComparisonResult",
    # Outbox Pattern
    "OutboxEvent",
    "OutboxEventType",
    "OutboxStatus",
    # Audit persistence
    "AuditEntry",
    # Integrity Check
    "IntegrityIssue",
    "IntegrityIssueType",
    "IntegritySeverity",
    "IntegrityReport",
    "RepairAction",
    # File Processing (2026-01-15)
    "FileCommit",
    "FileMemento",
    "BlobId",
    "CommitId",
    "FileCheckpointLink",
    "CommitStatus",
    "FileProcessingError",
    "DuplicateFileError",
    "InvalidBlobIdError",
    "InvalidCommitIdError",
    "CommitAlreadyFinalized",
    # Workspace Isolation (2026-01-16)
    "Workspace",
    "WorkspaceConfig",
    "WorkspaceStrategy",
    "LinkMethod",
    "LinkResult",
    "OrphanWorkspace",
    "CleanupReport",
    "compute_venv_spec_hash",
]
