"""Domain Models - Entities, Value Objects, and Aggregates."""

from .workflow import (
    WorkflowNode,
    WorkflowEdge,
    TestWorkflow,
    ExecutionState,
    Execution,
    ExecutionStatus,
    NodeVariant,
    InvalidStateTransition,
)

from .node_boundary import NodeBoundary, NodeStatus
# CheckpointFile REMOVED (2026-01-27) - Use CheckpointFileLink from file_processing
from .checkpoint import (
    Checkpoint,
    CheckpointId,
    ExecutionHistory,
    CheckpointNotFoundError,
    InvalidRollbackTargetError,
    ExecutionHistoryError,
)
from .batch_test import (
    BatchTest,
    BatchTestStatus,
    VariantCombination,
    BatchTestResult,
)
from .evaluation import (
    EvaluationResult,
    MetricValue,
    ComparisonResult,
)
from .outbox import (
    OutboxEvent,
    OutboxEventType,
    OutboxStatus,
)
from .audit import AuditEntry
from .integrity import (
    IntegrityIssue,
    IntegrityIssueType,
    IntegritySeverity,
    IntegrityReport,
    RepairAction,
)
from .file_processing import (
    FileCommit,
    FileMemento,
    BlobId,
    CommitId,
    CheckpointFileLink,
    CommitStatus,
    FileProcessingError,
    DuplicateFileError,
    InvalidBlobIdError,
    InvalidCommitIdError,
    CommitAlreadyFinalized,
)
# Backward compatibility alias (2026-01-27)
FileCheckpointLink = CheckpointFileLink
from .workspace import (
    Workspace,
    WorkspaceConfig,
    WorkspaceStrategy,
    LinkMethod,
    LinkResult,
    OrphanWorkspace,
    CleanupReport,
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
