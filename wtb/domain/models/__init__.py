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

from .node_boundary import NodeBoundary
from .checkpoint_file import CheckpointFile
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
from .integrity import (
    IntegrityIssue,
    IntegrityIssueType,
    IntegritySeverity,
    IntegrityReport,
    RepairAction,
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
    # Checkpoint-file link
    "CheckpointFile",
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
    # Integrity Check
    "IntegrityIssue",
    "IntegrityIssueType",
    "IntegritySeverity",
    "IntegrityReport",
    "RepairAction",
]
