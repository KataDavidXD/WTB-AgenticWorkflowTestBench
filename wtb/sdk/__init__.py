"""
WTB SDK - Workflow Submission and Testing SDK.

Architecture (2026-01-16 Refactor):
    - REUSES domain models (Execution, BatchTest, Checkpoint) - no duplication
    - Facade pattern composing existing domain interfaces

Domain Models (from wtb.domain.models):
    - Execution, ExecutionState, ExecutionStatus
    - Checkpoint, CheckpointId
    - BatchTest, BatchTestResult

SDK Classes:
    - WorkflowProject: Configuration object for workflow submission
    - WTBTestBench: Main entry point (facade)
    - WTBTestBenchBuilder: Builder for DI configuration

Usage:
    from wtb.sdk import WorkflowProject, WTBTestBench
    from wtb.domain.models import ExecutionStatus  # Use domain models directly
    
    project = WorkflowProject(name="my_workflow", graph_factory=create_graph)
    wtb = WTBTestBench.create()
    wtb.register_project(project)
    
    execution = wtb.run(project="my_workflow", initial_state={"key": "value"})
    print(execution.status)  # ExecutionStatus.COMPLETED
"""

from .workflow_project import (
    WorkflowProject,
    FileTrackingConfig,
    EnvironmentConfig,
    ExecutionConfig,
    EnvSpec,
    RayConfig,
    NodeResourceConfig,
    WorkspaceIsolationConfig,
    PauseStrategyConfig,
    NodeVariant,
    WorkflowVariant,
)
from .test_bench import (
    # Main classes
    WTBTestBench,
    WTBTestBenchBuilder,
    # SDK operation results (thin DTOs, not domain models)
    RollbackResult,
    ForkResult,
    BatchRollbackResult,   # NEW v1.8: Batch rollback result
    BatchForkResult,       # NEW v1.8: Batch fork result
    # Deprecated
    ExecutionControllerBuilder,
)

# Re-export coordinator for advanced use (v1.8)
from wtb.application.services.batch_execution_coordinator import (
    BatchExecutionCoordinator,
)
from wtb.domain.interfaces.batch_coordinator import (
    BatchOperationRequest,
    BatchOperationResult,
    OperationType,
)

# Re-export domain models for convenience (single import location)
from wtb.domain.models.workflow import Execution, ExecutionState, ExecutionStatus
from wtb.domain.models.checkpoint import Checkpoint, CheckpointId
from wtb.domain.models.batch_test import BatchTest, BatchTestResult, BatchTestStatus

__all__ = [
    # Main SDK classes
    "WorkflowProject",
    "WTBTestBench",
    "WTBTestBenchBuilder",
    # Configuration
    "FileTrackingConfig",
    "EnvironmentConfig",
    "ExecutionConfig",
    "EnvSpec",
    "RayConfig",
    "NodeResourceConfig",
    "WorkspaceIsolationConfig",
    "PauseStrategyConfig",
    # Variants
    "NodeVariant",
    "WorkflowVariant",
    # Domain models (re-exported for convenience)
    "Execution",
    "ExecutionState",
    "ExecutionStatus",
    "Checkpoint",
    "CheckpointId",
    "BatchTest",
    "BatchTestResult",
    "BatchTestStatus",
    # SDK operation results (thin DTOs)
    "RollbackResult",
    "ForkResult",
    "BatchRollbackResult",   # NEW v1.8
    "BatchForkResult",       # NEW v1.8
    # Batch coordinator (advanced use)
    "BatchExecutionCoordinator",
    "BatchOperationRequest",
    "BatchOperationResult",
    "OperationType",
    # Deprecated
    "ExecutionControllerBuilder",
]
