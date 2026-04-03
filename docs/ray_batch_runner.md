# Ray Batch Runner: Rollback & Fork Coordination

> **Status**: COMPLETED - Phase 1 (Coordinator) + Phase 2 (SDK) Merged  
> **Priority**: High  
> **Estimated Effort**: 2-3 days  
> **Last Updated**: 2026-02-05
> **Implemented By**: BatchExecutionCoordinator v1.8 + SDK Integration

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Current Architecture Analysis](#2-current-architecture-analysis)
3. [Adopted Design: BatchExecutionCoordinator](#3-adopted-design-batchexecutioncoordinator)
4. [Implementation TODOs](#4-implementation-todos)
5. [Interface Definitions](#5-interface-definitions)
6. [Testing Requirements](#6-testing-requirements)

---

## 1. Problem Statement

### 1.1 Missing Functionality

After `RayBatchTestRunner.run_batch_test()` completes, there is **no convenient way** to:

1. Rollback a specific variant to a checkpoint (restore state + files)
2. Fork a variant execution for exploration
3. Access `file_commit_id` or `checkpoint_ids` from batch results

### 1.2 Information Loss in BatchTestResult

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    Current: Information Loss During Conversion                  │
│                                                                                 │
│  Ray Actor Returns:                    BatchTestResult Stores:                  │
│  ┌─────────────────────────┐           ┌─────────────────────────┐             │
│  │ execution_id      ✓    │    →      │ execution_id      ✓    │             │
│  │ combination_name  ✓    │    →      │ combination_name  ✓    │             │
│  │ success           ✓    │    →      │ success           ✓    │             │
│  │ duration_ms       ✓    │    →      │ duration_ms       ✓    │             │
│  │ metrics           ✓    │    →      │ metrics           ✓    │             │
│  │ checkpoint_count  ✓    │    →      │ ❌ LOST                 │             │
│  │ file_commit_id    ✓    │    →      │ ❌ LOST                 │             │
│  │ checkpoint_ids    ✓    │    →      │ ❌ LOST                 │             │
│  └─────────────────────────┘           └─────────────────────────┘             │
│                                                                                 │
│  Code Location: ray_batch_runner.py lines 1404-1412                            │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 1.3 No Rollback in IBatchTestRunner

```python
class IBatchTestRunner(ABC):
    def run_batch_test(self, batch_test: BatchTest) -> BatchTest: ...
    def get_status(self, batch_test_id: str) -> BatchRunnerStatus: ...
    def get_progress(self, batch_test_id: str) -> BatchRunnerProgress: ...
    def cancel(self, batch_test_id: str) -> bool: ...
    def shutdown(self) -> None: ...
    # ❌ NO rollback_variant() method
    # ❌ NO fork_variant() method
```

---

## 2. Current Architecture Analysis

### 2.1 Execution Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        RayBatchTestRunner.run_batch_test()                      │
│                                                                                 │
│  ┌─────────────────┐                                                            │
│  │   BatchTest     │  variant_combinations: [Config_A, Config_B, Config_C]      │
│  │   (Input)       │  initial_state: {...}                                      │
│  └────────┬────────┘                                                            │
│           │                                                                     │
│           ▼                                                                     │
│  ┌─────────────────────────────────────────────────────────────────┐            │
│  │              1. Initialize Ray Actor Pool                       │            │
│  │   ray.util.ActorPool([actor_0, actor_1, actor_2, ...])          │            │
│  └────────┬────────────────────────────────────────────────────────┘            │
│           │                                                                     │
│           ▼                                                                     │
│  ┌─────────────────────────────────────────────────────────────────┐            │
│  │              2. Distribute Variants to Actors                   │            │
│  │                                                                 │            │
│  │   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │            │
│  │   │ Actor 0      │  │ Actor 1      │  │ Actor 2      │          │            │
│  │   │ execute_     │  │ execute_     │  │ execute_     │          │            │
│  │   │ variant()    │  │ variant()    │  │ variant()    │          │            │
│  │   │              │  │              │  │              │          │            │
│  │   │ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │          │            │
│  │   │ │UoW       │ │  │ │UoW       │ │  │ │UoW       │ │ ◄─ ACID  │            │
│  │   │ │StateAdpt │ │  │ │StateAdpt │ │  │ │StateAdpt │ │          │            │
│  │   │ │ExecCtrl  │ │  │ │ExecCtrl  │ │  │ │ExecCtrl  │ │          │            │
│  │   │ │FileTrack │ │  │ │FileTrack │ │  │ │FileTrack │ │          │            │
│  │   │ │Workspace │ │  │ │Workspace │ │  │ │Workspace │ │          │            │
│  │   │ └──────────┘ │  │ └──────────┘ │  │ └──────────┘ │          │            │
│  │   └──────────────┘  └──────────────┘  └──────────────┘          │            │
│  └────────┬────────────────────────────────────────────────────────┘            │
│           │                                                                     │
│           ▼                                                                     │
│  ┌─────────────────────────────────────────────────────────────────┐            │
│  │              3. Collect Results (ray.wait + backpressure)       │            │
│  │                                                                 │            │
│  │   result_dict = {                                               │            │
│  │       execution_id: "uuid-xxx",                                 │            │
│  │       combination_name: "Config_A",                             │            │
│  │       success: True,                                            │            │
│  │       duration_ms: 1234,                                        │            │
│  │       metrics: {overall_score: 0.85},                           │            │
│  │       checkpoint_count: 5,         ◄── Available here           │            │
│  │       files_tracked: 3,                                         │            │
│  │       file_commit_id: "ft-xxx",    ◄── Available here           │            │
│  │   }                                                             │            │
│  └────────┬────────────────────────────────────────────────────────┘            │
│           │                                                                     │
│           ▼                                                                     │
│  ┌─────────────────────────────────────────────────────────────────┐            │
│  │              4. Convert to BatchTestResult (INFO LOSS!)         │            │
│  │                                                                 │            │
│  │   BatchTestResult = {                                           │            │
│  │       combination_name, execution_id, success, metrics,         │            │
│  │       overall_score, duration_ms, error_message                 │            │
│  │       # ❌ Missing: checkpoint_ids, file_commit_id              │            │
│  │   }                                                             │            │
│  └────────┬────────────────────────────────────────────────────────┘            │
│           │                                                                     │
│           ▼                                                                     │
│  ┌─────────────────┐                                                            │
│  │   BatchTest     │  results: [BatchTestResult, ...]                           │
│  │   (Output)      │  comparison_matrix: {...}                                  │
│  └─────────────────┘                                                            │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Current Rollback Behavior

`ExecutionController.rollback()` restores files from **state**, not from FileTracker:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                ExecutionController.rollback(execution_id, checkpoint_id)        │
│                                                                                 │
│  Step 1: Validate & Get Execution                                               │
│  ────────────────────────────────────────────────────────────────────────────   │
│  execution = self._get_execution(execution_id)                                  │
│  if not execution.can_rollback(): raise ValueError(...)                         │
│                                                                                 │
│  Step 2: State Rollback (via StateAdapter)                                      │
│  ────────────────────────────────────────────────────────────────────────────   │
│  restored_state = self._state_adapter.rollback(checkpoint_id)                   │
│  # Loads checkpoint from SQLite/PostgreSQL checkpointer                         │
│                                                                                 │
│  Step 3: File Restore (from _output_files in STATE)                             │
│  ────────────────────────────────────────────────────────────────────────────   │
│  if self._file_tracking and self._output_dir:                                   │
│      output_files_data = restored_state.get("_output_files")                    │
│      for filename, content in output_files_data.items():                        │
│          file_path.write_text(content)  # ⚠️ From state, NOT FileTracker        │
│                                                                                 │
│  Step 4: Update Execution                                                       │
│  ────────────────────────────────────────────────────────────────────────────   │
│  execution.state = restored_state                                               │
│  execution.status = ExecutionStatus.PAUSED                                      │
│  uow.commit()                                                                   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 2.3 Two File Restore Mechanisms

| Method | Data Source | Location | Use Case |
|--------|-------------|----------|----------|
| From State | `restored_state["_output_files"]` | `execution_controller.py:527-562` | Current rollback |
| From FileTracker | `file_commit_id` | `IFileTrackingService.restore_commit()` | Independent restore |

**Issue**: These are not coordinated - need unified approach.

---

## 3. Adopted Design: BatchExecutionCoordinator

### 3.1 Design Principles

| Principle | Implementation |
|-----------|----------------|
| **SRP** | Coordinator only orchestrates; delegates to ExecutionController |
| **OCP** | New operation types via OperationType enum |
| **DIP** | All dependencies via interfaces (IUnitOfWorkFactory, etc.) |
| **ACID** | Each operation in single UoW transaction |

### 3.2 Rollback vs Fork Semantics

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        Rollback vs Fork Semantics                               │
│                                                                                 │
│  ┌─────────────────────────────────────┐  ┌─────────────────────────────────┐  │
│  │          ROLLBACK (Destructive)     │  │          FORK (Non-Destructive) │  │
│  │                                     │  │                                 │  │
│  │  Execution A                        │  │  Execution A (unchanged)        │  │
│  │  ┌─────┐                            │  │  ┌─────┐                        │  │
│  │  │ CP1 │──────┐                     │  │  │ CP1 │──────┐                 │  │
│  │  └─────┘      │                     │  │  └─────┘      │                 │  │
│  │       │       │                     │  │       │       │                 │  │
│  │       ▼       │                     │  │       ▼       │                 │  │
│  │  ┌─────┐      │ rollback            │  │  ┌─────┐      │ fork            │  │
│  │  │ CP2 │◄─────┘ to CP2              │  │  │ CP2 │──────┼─────────┐       │  │
│  │  └─────┘                            │  │  └─────┘      │         │       │  │
│  │       │                             │  │       │       │         ▼       │  │
│  │       ▼  ❌ Overwritten             │  │       ▼       │    Execution B   │  │
│  │  ┌─────┐                            │  │  ┌─────┐      │    (NEW)        │  │
│  │  │ CP3 │  (lost)                    │  │  │ CP3 │      │    ┌─────┐      │  │
│  │  └─────┘                            │  │  └─────┘      │    │ CP2'│      │  │
│  │                                     │  │               │    └─────┘      │  │
│  │  Status: PAUSED                     │  │  A: unchanged │    Status: PENDING│ │
│  │                                     │  │  B: new       │                 │  │
│  └─────────────────────────────────────┘  └─────────────────────────────────┘  │
│                                                                                 │
│  Rollback: "Go back in time and redo"                                          │
│  Fork:     "Keep original + explore branch"                                    │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 3.3 Graph Requirement Matrix

| Operation | Graph Required | Reason |
|-----------|----------------|--------|
| `rollback()` | ❌ No | Only restores state, execution becomes PAUSED |
| `fork()` | ❌ No | Only creates new execution in PENDING state |
| `run()` after rollback | ✅ Yes | Needs workflow graph to continue execution |
| `run()` after fork | ✅ Yes | Needs graph to run new execution |
| State inspection | ❌ No | Read-only operation |

**Conclusion**: Graph should be **optional**, only required for `*_and_run()` operations.

### 3.4 Transaction Architecture (CRITICAL)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    Refined Transaction Architecture                             │
│                                                                                 │
│  Why separate file restore from UoW transaction?                                │
│  ─────────────────────────────────────────────────                              │
│  - FileTracker may use different database (PostgreSQL vs SQLite)                │
│  - Cannot guarantee atomic commit across heterogeneous databases                │
│  - Outbox pattern handles retry if file restore fails                           │
│                                                                                 │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                    Operation Flow: rollback()                           │   │
│  │                                                                         │   │
│  │  ┌───────────────────────────────────────────────────────────────┐     │   │
│  │  │  Phase 1: UoW Transaction (State + Metadata)                  │     │   │
│  │  │                                                               │     │   │
│  │  │  1. controller.rollback(exec_id, cp_id)                       │     │   │
│  │  │     └─► StateAdapter loads checkpoint                         │     │   │
│  │  │     └─► Execution.state = restored_state                      │     │   │
│  │  │     └─► Execution.status = PAUSED                             │     │   │
│  │  │                                                               │     │   │
│  │  │  2. outbox.add(ROLLBACK_PERFORMED, {                          │     │   │
│  │  │         execution_id, checkpoint_id, file_commit_id           │     │   │
│  │  │     })                                                        │     │   │
│  │  │     └─► Audit event queued (same transaction)                 │     │   │
│  │  │                                                               │     │   │
│  │  │  3. uow.commit()                                              │     │   │
│  │  │     └─► ACID durability guaranteed                            │     │   │
│  │  │                                                               │     │   │
│  │  └───────────────────────────────────────────────────────────────┘     │   │
│  │                              │                                         │   │
│  │                              ▼                                         │   │
│  │  ┌───────────────────────────────────────────────────────────────┐     │   │
│  │  │  Phase 2: Post-Commit File Restore (Best-Effort)              │     │   │
│  │  │                                                               │     │   │
│  │  │  4. file_tracking.restore_commit(file_commit_id)              │     │   │
│  │  │     └─► Restore files from blob storage                       │     │   │
│  │  │     └─► If fails: logged, retryable via outbox processor      │     │   │
│  │  │                                                               │     │   │
│  │  └───────────────────────────────────────────────────────────────┘     │   │
│  │                                                                         │   │
│  │  Failure Handling:                                                      │   │
│  │  - Phase 1 fails → entire operation rolled back, no side effects        │   │
│  │  - Phase 2 fails → state is correct, files retryable via outbox         │   │
│  │                                                                         │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Implementation TODOs

### TODO 1: Fix BatchTestResult Information Loss

**File**: `wtb/domain/models/batch_test.py`  
**Priority**: High  
**Estimated Time**: 30 minutes

```python
# BEFORE (current)
@dataclass
class BatchTestResult:
    combination_name: str
    execution_id: str
    success: bool
    duration_ms: int = 0
    metrics: Dict[str, float] = field(default_factory=dict)
    overall_score: float = 0.0
    error_message: Optional[str] = None

# AFTER (add these fields)
@dataclass
class BatchTestResult:
    combination_name: str
    execution_id: str
    success: bool
    duration_ms: int = 0
    metrics: Dict[str, float] = field(default_factory=dict)
    overall_score: float = 0.0
    error_message: Optional[str] = None
    # NEW: Rollback support fields
    file_commit_id: Optional[str] = None      # FileTracker commit ID
    checkpoint_count: int = 0                  # Number of checkpoints
    last_checkpoint_id: Optional[str] = None   # Most recent checkpoint ID
```

**Also update**: `ray_batch_runner.py` lines 1404-1412 to pass these fields.

---

### TODO 2: Create Interface Definitions

**File**: `wtb/domain/interfaces/batch_coordinator.py` (NEW)  
**Priority**: High  
**Estimated Time**: 1 hour

```python
"""
Batch Execution Coordinator Interfaces.

Provides abstractions for coordinating rollback/fork operations
across batch test results.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from enum import Enum

from wtb.domain.models.workflow import Execution


class OperationType(Enum):
    """Operation type for batch coordination."""
    ROLLBACK = "rollback"           # Destructive rollback
    FORK = "fork"                   # Non-destructive fork
    ROLLBACK_AND_RUN = "rollback_run"  # Rollback + continue execution
    FORK_AND_RUN = "fork_run"       # Fork + run new execution


@dataclass
class BatchOperationRequest:
    """Request for a batch operation."""
    execution_id: str
    checkpoint_id: str
    operation: OperationType = OperationType.ROLLBACK
    new_state: Optional[Dict[str, Any]] = None  # For fork: merge into checkpoint state
    graph: Optional[Any] = None  # Required for *_AND_RUN operations


@dataclass
class BatchOperationResult:
    """Result of a batch operation."""
    execution_id: str
    checkpoint_id: str
    operation: OperationType
    success: bool
    result_execution: Optional[Execution] = None
    new_execution_id: Optional[str] = None  # For fork: new execution ID
    files_restored: int = 0
    error: Optional[str] = None


class IExecutionControllerFactory(ABC):
    """Factory for creating ExecutionController instances."""
    
    @abstractmethod
    def create(
        self,
        uow: "IUnitOfWork",
        state_adapter: "IStateAdapter",
        file_tracking_service: Optional["IFileTrackingService"] = None,
    ) -> "IExecutionController":
        """Create ExecutionController with injected dependencies."""
        pass


class IBatchExecutionCoordinator(ABC):
    """
    Interface for batch execution coordination.
    
    Responsibilities:
    - Coordinate rollback/fork operations
    - Manage transaction boundaries
    - Handle file restore with outbox pattern
    """
    
    @abstractmethod
    def rollback(
        self,
        execution_id: str,
        checkpoint_id: str,
    ) -> Execution:
        """
        Rollback execution to checkpoint (destructive).
        
        Args:
            execution_id: Execution to rollback
            checkpoint_id: Target checkpoint
            
        Returns:
            Execution in PAUSED state
        """
        pass
    
    @abstractmethod
    def fork(
        self,
        execution_id: str,
        checkpoint_id: str,
        new_state: Optional[Dict[str, Any]] = None,
    ) -> Execution:
        """
        Fork execution from checkpoint (non-destructive).
        
        Args:
            execution_id: Source execution
            checkpoint_id: Checkpoint to fork from
            new_state: Optional state to merge
            
        Returns:
            NEW Execution in PENDING state
        """
        pass
    
    @abstractmethod
    def rollback_and_run(
        self,
        execution_id: str,
        checkpoint_id: str,
        graph: Any,
    ) -> Execution:
        """Rollback and continue execution (requires graph)."""
        pass
    
    @abstractmethod
    def fork_and_run(
        self,
        execution_id: str,
        checkpoint_id: str,
        graph: Any,
        new_state: Optional[Dict[str, Any]] = None,
    ) -> Execution:
        """Fork and run new execution (requires graph)."""
        pass
    
    @abstractmethod
    def batch_operate(
        self,
        requests: List[BatchOperationRequest],
        stop_on_error: bool = False,
    ) -> List[BatchOperationResult]:
        """Execute batch operations."""
        pass
```

---

### TODO 3: Implement BatchExecutionCoordinator

**File**: `wtb/application/services/batch_execution_coordinator.py` (NEW)  
**Priority**: High  
**Estimated Time**: 2-3 hours

```python
"""
Batch Execution Coordinator Implementation.

Design Principles:
- SRP: Coordinator orchestrates, delegates to ExecutionController
- OCP: Extensible via OperationType enum
- DIP: All dependencies via interfaces
- ACID: Each operation in single UoW transaction + post-commit file restore
"""

import logging
from typing import List, Optional, Dict, Any, Callable

from wtb.domain.interfaces.batch_coordinator import (
    IBatchExecutionCoordinator,
    IExecutionControllerFactory,
    OperationType,
    BatchOperationRequest,
    BatchOperationResult,
)
from wtb.domain.interfaces.unit_of_work import IUnitOfWork
from wtb.domain.interfaces.state_adapter import IStateAdapter
from wtb.domain.interfaces.file_tracking import IFileTrackingService
from wtb.domain.models.workflow import Execution
from wtb.domain.models.outbox import OutboxEvent, OutboxEventType

logger = logging.getLogger(__name__)


class BatchExecutionCoordinator(IBatchExecutionCoordinator):
    """
    Coordinates batch rollback/fork operations.
    
    Transaction Architecture:
    - Phase 1: State changes + outbox event in single UoW transaction
    - Phase 2: File restore post-commit (best-effort, retryable)
    
    Usage:
        coordinator = BatchExecutionCoordinator(
            uow_factory=uow_factory,
            controller_factory=controller_factory,
            state_adapter=shared_state_adapter,
            file_tracking=file_tracking_service,
        )
        
        # Single rollback
        result = coordinator.rollback(exec_id, checkpoint_id)
        
        # Batch fork
        results = coordinator.batch_operate([
            BatchOperationRequest(exec1, cp1, OperationType.FORK),
            BatchOperationRequest(exec2, cp2, OperationType.FORK),
        ])
    """
    
    def __init__(
        self,
        uow_factory: Callable[[], IUnitOfWork],
        controller_factory: IExecutionControllerFactory,
        state_adapter: IStateAdapter,
        file_tracking: Optional[IFileTrackingService] = None,
    ):
        """
        Initialize coordinator with dependencies.
        
        Args:
            uow_factory: Factory function creating IUnitOfWork instances
            controller_factory: Factory for creating ExecutionController
            state_adapter: Shared StateAdapter (reused across operations)
            file_tracking: Optional FileTrackingService for file restore
        """
        self._uow_factory = uow_factory
        self._controller_factory = controller_factory
        self._state_adapter = state_adapter
        self._file_tracking = file_tracking
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Single Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    def rollback(
        self,
        execution_id: str,
        checkpoint_id: str,
    ) -> Execution:
        """
        Rollback execution to checkpoint (destructive).
        
        Transaction Flow:
        1. [UoW] controller.rollback() - restore state
        2. [UoW] outbox.add(ROLLBACK_PERFORMED) - queue audit event
        3. [UoW] commit() - ACID durability
        4. [Post] file_tracking.restore_commit() - best-effort file restore
        
        Cost: ~10-20ms (StateAdapter reused)
        """
        file_commit_id: Optional[str] = None
        
        # Phase 1: UoW Transaction
        with self._uow_factory() as uow:
            controller = self._controller_factory.create(
                uow=uow,
                state_adapter=self._state_adapter,
                file_tracking_service=self._file_tracking,
            )
            
            execution = controller.rollback(execution_id, checkpoint_id)
            
            # Get file_commit_id from execution state if available
            file_commit_id = execution.state.workflow_variables.get(
                "_file_tracking_result", {}
            ).get("commit_id")
            
            # Emit audit event via outbox
            outbox_event = OutboxEvent.create(
                event_type=OutboxEventType.ROLLBACK_PERFORMED,
                aggregate_id=execution_id,
                payload={
                    "execution_id": execution_id,
                    "checkpoint_id": checkpoint_id,
                    "file_commit_id": file_commit_id,
                },
            )
            uow.outbox.add(outbox_event)
            uow.commit()
        
        # Phase 2: Post-commit file restore (best-effort)
        if file_commit_id and self._file_tracking:
            try:
                result = self._file_tracking.restore_commit(file_commit_id)
                logger.info(
                    f"Restored {result.files_restored} files for "
                    f"rollback {execution_id} -> {checkpoint_id}"
                )
            except Exception as e:
                # Log but don't fail - outbox processor will retry
                logger.warning(
                    f"File restore failed (will retry via outbox): {e}"
                )
        
        return execution
    
    def fork(
        self,
        execution_id: str,
        checkpoint_id: str,
        new_state: Optional[Dict[str, Any]] = None,
    ) -> Execution:
        """
        Fork execution from checkpoint (non-destructive).
        
        Creates new execution with PENDING status.
        Original execution is unchanged.
        
        Cost: ~10-20ms
        """
        with self._uow_factory() as uow:
            controller = self._controller_factory.create(
                uow=uow,
                state_adapter=self._state_adapter,
                file_tracking_service=self._file_tracking,
            )
            
            forked = controller.fork(execution_id, checkpoint_id, new_state)
            
            # Emit audit event
            outbox_event = OutboxEvent.create(
                event_type=OutboxEventType.EXECUTION_FORKED,
                aggregate_id=forked.id,
                payload={
                    "source_execution_id": execution_id,
                    "fork_execution_id": forked.id,
                    "source_checkpoint_id": checkpoint_id,
                },
            )
            uow.outbox.add(outbox_event)
            uow.commit()
        
        return forked
    
    def rollback_and_run(
        self,
        execution_id: str,
        checkpoint_id: str,
        graph: Any,
    ) -> Execution:
        """
        Rollback and continue execution (atomic).
        
        Both operations in same UoW transaction for atomicity.
        """
        file_commit_id: Optional[str] = None
        
        with self._uow_factory() as uow:
            controller = self._controller_factory.create(
                uow=uow,
                state_adapter=self._state_adapter,
                file_tracking_service=self._file_tracking,
            )
            
            # Rollback state
            execution = controller.rollback(execution_id, checkpoint_id)
            file_commit_id = execution.state.workflow_variables.get(
                "_file_tracking_result", {}
            ).get("commit_id")
            
            # Continue execution
            execution = controller.run(execution_id, graph=graph)
            
            # Single audit event for compound operation
            outbox_event = OutboxEvent.create(
                event_type=OutboxEventType.ROLLBACK_PERFORMED,
                aggregate_id=execution_id,
                payload={
                    "execution_id": execution_id,
                    "checkpoint_id": checkpoint_id,
                    "continued": True,
                    "final_status": execution.status.value,
                },
            )
            uow.outbox.add(outbox_event)
            uow.commit()
        
        # Post-commit file restore
        if file_commit_id and self._file_tracking:
            try:
                self._file_tracking.restore_commit(file_commit_id)
            except Exception as e:
                logger.warning(f"File restore failed: {e}")
        
        return execution
    
    def fork_and_run(
        self,
        execution_id: str,
        checkpoint_id: str,
        graph: Any,
        new_state: Optional[Dict[str, Any]] = None,
    ) -> Execution:
        """Fork and run new execution (atomic)."""
        with self._uow_factory() as uow:
            controller = self._controller_factory.create(
                uow=uow,
                state_adapter=self._state_adapter,
                file_tracking_service=self._file_tracking,
            )
            
            forked = controller.fork(execution_id, checkpoint_id, new_state)
            result = controller.run(forked.id, graph=graph)
            
            outbox_event = OutboxEvent.create(
                event_type=OutboxEventType.EXECUTION_FORKED,
                aggregate_id=forked.id,
                payload={
                    "source_execution_id": execution_id,
                    "fork_execution_id": forked.id,
                    "ran_immediately": True,
                    "final_status": result.status.value,
                },
            )
            uow.outbox.add(outbox_event)
            uow.commit()
        
        return result
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Batch Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    def batch_operate(
        self,
        requests: List[BatchOperationRequest],
        stop_on_error: bool = False,
    ) -> List[BatchOperationResult]:
        """
        Execute batch operations.
        
        Each request is processed in its own transaction.
        StateAdapter is reused across all operations for efficiency.
        
        Args:
            requests: List of operation requests
            stop_on_error: If True, stop on first error
            
        Returns:
            List of results (same order as requests)
        """
        results: List[BatchOperationResult] = []
        
        for req in requests:
            try:
                if req.operation == OperationType.ROLLBACK:
                    execution = self.rollback(req.execution_id, req.checkpoint_id)
                    results.append(BatchOperationResult(
                        execution_id=req.execution_id,
                        checkpoint_id=req.checkpoint_id,
                        operation=req.operation,
                        success=True,
                        result_execution=execution,
                    ))
                    
                elif req.operation == OperationType.FORK:
                    execution = self.fork(
                        req.execution_id, req.checkpoint_id, req.new_state
                    )
                    results.append(BatchOperationResult(
                        execution_id=req.execution_id,
                        checkpoint_id=req.checkpoint_id,
                        operation=req.operation,
                        success=True,
                        result_execution=execution,
                        new_execution_id=execution.id,
                    ))
                    
                elif req.operation == OperationType.ROLLBACK_AND_RUN:
                    if not req.graph:
                        raise ValueError("Graph required for ROLLBACK_AND_RUN")
                    execution = self.rollback_and_run(
                        req.execution_id, req.checkpoint_id, req.graph
                    )
                    results.append(BatchOperationResult(
                        execution_id=req.execution_id,
                        checkpoint_id=req.checkpoint_id,
                        operation=req.operation,
                        success=True,
                        result_execution=execution,
                    ))
                    
                elif req.operation == OperationType.FORK_AND_RUN:
                    if not req.graph:
                        raise ValueError("Graph required for FORK_AND_RUN")
                    execution = self.fork_and_run(
                        req.execution_id, req.checkpoint_id, req.graph, req.new_state
                    )
                    results.append(BatchOperationResult(
                        execution_id=req.execution_id,
                        checkpoint_id=req.checkpoint_id,
                        operation=req.operation,
                        success=True,
                        result_execution=execution,
                        new_execution_id=execution.id,
                    ))
                    
            except Exception as e:
                logger.error(f"Batch operation failed for {req.execution_id}: {e}")
                results.append(BatchOperationResult(
                    execution_id=req.execution_id,
                    checkpoint_id=req.checkpoint_id,
                    operation=req.operation,
                    success=False,
                    error=str(e),
                ))
                if stop_on_error:
                    break
        
        return results
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Convenience Methods
    # ═══════════════════════════════════════════════════════════════════════════
    
    def batch_rollback(
        self,
        items: List[tuple],  # [(exec_id, checkpoint_id), ...]
    ) -> List[BatchOperationResult]:
        """Convenience: batch rollback multiple executions."""
        requests = [
            BatchOperationRequest(
                execution_id=exec_id,
                checkpoint_id=cp_id,
                operation=OperationType.ROLLBACK,
            )
            for exec_id, cp_id in items
        ]
        return self.batch_operate(requests)
    
    def batch_fork(
        self,
        items: List[tuple],  # [(exec_id, checkpoint_id, new_state?), ...]
    ) -> List[BatchOperationResult]:
        """Convenience: batch fork multiple executions."""
        requests = []
        for item in items:
            exec_id, cp_id = item[0], item[1]
            new_state = item[2] if len(item) > 2 else None
            requests.append(BatchOperationRequest(
                execution_id=exec_id,
                checkpoint_id=cp_id,
                operation=OperationType.FORK,
                new_state=new_state,
            ))
        return self.batch_operate(requests)
```

---

### TODO 4: Add Factory Method to RayBatchTestRunner

**File**: `wtb/application/services/ray_batch_runner.py`  
**Priority**: Medium  
**Estimated Time**: 30 minutes

Add method to create coordinator that reuses runner's configuration:

```python
class RayBatchTestRunner(IBatchTestRunner):
    # ... existing code ...
    
    def create_rollback_coordinator(self) -> "BatchExecutionCoordinator":
        """
        Create BatchExecutionCoordinator reusing this runner's configuration.
        
        Usage:
            runner = RayBatchTestRunner(config, ...)
            result = runner.run_batch_test(batch_test)
            
            # After batch completes, rollback specific variants
            coordinator = runner.create_rollback_coordinator()
            coordinator.rollback(result.results[0].execution_id, checkpoint_id)
        """
        from wtb.application.services.batch_execution_coordinator import (
            BatchExecutionCoordinator,
        )
        from wtb.application.factories import ExecutionControllerFactory
        
        return BatchExecutionCoordinator(
            uow_factory=self._create_uow,
            controller_factory=ExecutionControllerFactory(),
            state_adapter=self._create_shared_state_adapter(),
            file_tracking=self._create_file_tracking_service(),
        )
    
    def _create_shared_state_adapter(self) -> "IStateAdapter":
        """Create StateAdapter with same config as actors."""
        from wtb.infrastructure.adapters.langgraph_state_adapter import (
            LangGraphStateAdapter,
            LangGraphConfig,
        )
        return LangGraphStateAdapter(LangGraphConfig.for_development())
    
    def _create_file_tracking_service(self) -> Optional["IFileTrackingService"]:
        """Create FileTrackingService if configured."""
        if not self._file_tracking_enabled:
            return None
        
        from wtb.infrastructure.file_tracking import FileTrackerService
        from wtb.infrastructure.file_tracking.config import FileTrackingConfig
        
        config = FileTrackingConfig.from_dict(self._filetracker_config)
        return FileTrackerService(config)
    
    def _create_uow(self) -> "IUnitOfWork":
        """Create UnitOfWork instance."""
        from wtb.infrastructure.database import UnitOfWorkFactory
        return UnitOfWorkFactory.create(
            mode="sqlalchemy" if "://" in self._wtb_db_url else "inmemory",
            db_url=self._wtb_db_url,
        )
```

---

### TODO 5: Update Exports and __init__.py

**Files**:
- `wtb/domain/interfaces/__init__.py`
- `wtb/application/services/__init__.py`

Add exports for new interfaces and implementations.

---

## 5. Interface Definitions

### 5.1 New Outbox Event Type

**File**: `wtb/domain/models/outbox.py`

```python
class OutboxEventType(Enum):
    # ... existing types ...
    EXECUTION_FORKED = "execution_forked"  # ADD THIS
```

---

## 6. Testing Requirements

### 6.1 Unit Tests

**File**: `tests/unit/application/test_batch_execution_coordinator.py`

```python
class TestBatchExecutionCoordinator:
    """Unit tests for BatchExecutionCoordinator."""
    
    def test_rollback_restores_state_and_emits_event(self):
        """Rollback should restore state and emit outbox event."""
        pass
    
    def test_rollback_calls_file_restore_post_commit(self):
        """File restore should happen after UoW commit."""
        pass
    
    def test_fork_creates_new_execution(self):
        """Fork should create new execution without modifying original."""
        pass
    
    def test_rollback_and_run_atomic(self):
        """Compound operation should be atomic."""
        pass
    
    def test_batch_operate_continues_on_error_by_default(self):
        """Batch should continue processing on individual failures."""
        pass
    
    def test_batch_operate_stops_on_error_when_requested(self):
        """Batch should stop on first error when stop_on_error=True."""
        pass
```

### 6.2 Integration Tests

**File**: `tests/integration/test_ray_batch_rollback.py`

```python
class TestRayBatchRollback:
    """Integration tests for Ray batch + rollback coordination."""
    
    def test_batch_result_contains_file_commit_id(self):
        """BatchTestResult should include file_commit_id."""
        pass
    
    def test_coordinator_from_runner_shares_config(self):
        """Coordinator created from runner should use same config."""
        pass
    
    def test_rollback_variant_restores_files(self):
        """Rolling back a variant should restore its files."""
        pass
```

---

## 7. SDK Integration

### 7.1 Design Principle

The SDK should provide a **simple, unified API** for batch rollback operations without exposing internal complexity.

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           SDK Layer Integration                                 │
│                                                                                 │
│  User Code                           SDK (WTBTestBench)                         │
│  ──────────                          ─────────────────                          │
│                                                                                 │
│  # Run batch test                    ┌─────────────────────────────────────┐   │
│  batch = wtb.run_batch_test(...)     │  WTBTestBench                       │   │
│                                      │  ├── run_batch_test()               │   │
│  # Get coordinator (lazy)            │  ├── get_batch_coordinator() ◄──NEW │   │
│  coord = wtb.get_batch_coordinator() │  ├── rollback_batch_result() ◄──NEW │   │
│                                      │  └── fork_batch_result()    ◄──NEW │   │
│  # Rollback variant                  └────────────────┬────────────────────┘   │
│  coord.rollback(                                      │                         │
│      batch.results[0].execution_id,                   ▼                         │
│      checkpoint_id                   ┌─────────────────────────────────────┐   │
│  )                                   │  BatchExecutionCoordinator          │   │
│                                      │  (Application Layer)                │   │
│  # OR use convenience method         │                                     │   │
│  wtb.rollback_batch_result(          │  Manages:                           │   │
│      batch.results[0],               │  - Transaction boundaries           │   │
│      checkpoint_id                   │  - File restore coordination        │   │
│  )                                   │  - Outbox event emission            │   │
│                                      └─────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### TODO 6: Add SDK Methods to WTBTestBench

**File**: `wtb/sdk/test_bench.py`  
**Priority**: High  
**Estimated Time**: 1 hour  
**Depends On**: TODO 1 (BatchTestResult must have `last_checkpoint_id` field)

```python
class WTBTestBench:
    """Main entry point for WTB SDK."""
    
    def __init__(self, ...):
        # ... existing code ...
        self._batch_coordinator: Optional["BatchExecutionCoordinator"] = None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Batch Rollback/Fork - NEW SECTION
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_batch_coordinator(self) -> "BatchExecutionCoordinator":
        """
        Get or create BatchExecutionCoordinator for rollback/fork operations.
        
        Lazily initializes coordinator on first call.
        Reuses same coordinator instance for efficiency (StateAdapter reuse).
        
        Usage:
            batch = wtb.run_batch_test(...)
            coordinator = wtb.get_batch_coordinator()
            coordinator.rollback(batch.results[0].execution_id, checkpoint_id)
        
        Returns:
            BatchExecutionCoordinator instance
        """
        if self._batch_coordinator is None:
            self._batch_coordinator = self._create_batch_coordinator()
        return self._batch_coordinator
    
    def rollback_batch_result(
        self,
        result: "BatchTestResult",
        checkpoint_id: Optional[str] = None,  # Defaults to result.last_checkpoint_id
    ) -> "BatchRollbackResult":
        """
        Convenience: Rollback a batch test result to a checkpoint.
        
        Args:
            result: BatchTestResult from run_batch_test()
            checkpoint_id: Checkpoint ID to rollback to.
                          Defaults to result.last_checkpoint_id if not provided.
            
        Returns:
            BatchRollbackResult with execution and file restore status
            
        Raises:
            ValueError: If result has no execution_id or no checkpoint available
            
        Example:
            # Rollback to last checkpoint (most common)
            wtb.rollback_batch_result(batch.results[0])
            
            # Rollback to specific checkpoint
            wtb.rollback_batch_result(batch.results[0], checkpoint_id="abc-123")
        """
        if not result.execution_id:
            raise ValueError("BatchTestResult has no execution_id")
        
        # Use provided checkpoint_id or default to last_checkpoint_id
        cp_id = checkpoint_id or result.last_checkpoint_id
        if not cp_id:
            raise ValueError(
                "No checkpoint_id provided and result has no last_checkpoint_id. "
                "Use get_batch_result_checkpoints() to list available checkpoints."
            )
        
        coordinator = self.get_batch_coordinator()
        execution = coordinator.rollback(result.execution_id, cp_id)
        
        return BatchRollbackResult(
            execution_id=result.execution_id,
            checkpoint_id=cp_id,
            success=True,
            execution=execution,
        )
    
    def fork_batch_result(
        self,
        result: "BatchTestResult",
        checkpoint_id: Optional[str] = None,  # Defaults to result.last_checkpoint_id
        new_state: Optional[Dict[str, Any]] = None,
    ) -> "BatchForkResult":
        """
        Convenience: Fork a batch test result from a checkpoint.
        
        Creates a new execution starting from the checkpoint state.
        Original execution is unchanged.
        
        Args:
            result: BatchTestResult from run_batch_test()
            checkpoint_id: Checkpoint ID to fork from.
                          Defaults to result.last_checkpoint_id if not provided.
            new_state: Optional state to merge with checkpoint state
            
        Returns:
            BatchForkResult with new execution details
            
        Example:
            # Fork from last checkpoint
            fork = wtb.fork_batch_result(batch.results[0])
            
            # Fork from specific checkpoint with modified state
            fork = wtb.fork_batch_result(
                batch.results[0], 
                checkpoint_id="abc-123",
                new_state={"temperature": 0.5}
            )
        """
        if not result.execution_id:
            raise ValueError("BatchTestResult has no execution_id")
        
        # Use provided checkpoint_id or default to last_checkpoint_id
        cp_id = checkpoint_id or result.last_checkpoint_id
        if not cp_id:
            raise ValueError(
                "No checkpoint_id provided and result has no last_checkpoint_id. "
                "Use get_batch_result_checkpoints() to list available checkpoints."
            )
        
        coordinator = self.get_batch_coordinator()
        forked = coordinator.fork(result.execution_id, cp_id, new_state)
        
        return BatchForkResult(
            source_execution_id=result.execution_id,
            fork_execution_id=forked.id,
            checkpoint_id=cp_id,
            execution=forked,
        )
    
    def get_batch_result_checkpoints(
        self,
        result: "BatchTestResult",
    ) -> List["Checkpoint"]:
        """
        Get checkpoints for a batch test result.
        
        Convenience method to list available checkpoints for rollback/fork.
        
        Args:
            result: BatchTestResult from run_batch_test()
            
        Returns:
            List of Checkpoint objects
        """
        if not result.execution_id:
            return []
        return self.get_checkpoints(result.execution_id)
    
    def _create_batch_coordinator(self) -> "BatchExecutionCoordinator":
        """Create BatchExecutionCoordinator with current configuration."""
        from wtb.application.services.batch_execution_coordinator import (
            BatchExecutionCoordinator,
        )
        from wtb.application.factories import ExecutionControllerFactory
        
        # Get config from batch_runner if available
        if self._batch_runner and hasattr(self._batch_runner, 'create_rollback_coordinator'):
            return self._batch_runner.create_rollback_coordinator()
        
        # Fallback: create with default config
        factory = ExecutionControllerFactory()
        return BatchExecutionCoordinator(
            uow_factory=factory._create_uow,
            controller_factory=factory,
            state_adapter=factory._create_state_adapter(),
        )
```

### TODO 7: Add SDK Result DTOs

**File**: `wtb/sdk/test_bench.py`  
**Priority**: Medium  
**Estimated Time**: 15 minutes

```python
@dataclass
class BatchRollbackResult:
    """Result of rolling back a batch test result."""
    execution_id: str
    checkpoint_id: str
    success: bool
    execution: Optional[Execution] = None
    files_restored: int = 0
    error: Optional[str] = None


@dataclass
class BatchForkResult:
    """Result of forking a batch test result."""
    source_execution_id: str
    fork_execution_id: str
    checkpoint_id: str
    execution: Optional[Execution] = None
    error: Optional[str] = None
```

### TODO 8: Update SDK Exports

**File**: `wtb/sdk/__init__.py`  
**Priority**: Low  
**Estimated Time**: 10 minutes

```python
from .test_bench import (
    # Main classes
    WTBTestBench,
    WTBTestBenchBuilder,
    # SDK operation results
    RollbackResult,
    ForkResult,
    BatchRollbackResult,   # NEW
    BatchForkResult,       # NEW
    # Deprecated
    ExecutionControllerBuilder,
)

# Re-export coordinator for advanced use
from wtb.application.services.batch_execution_coordinator import (
    BatchExecutionCoordinator,
    BatchOperationRequest,
    BatchOperationResult,
    OperationType,
)

__all__ = [
    # ... existing exports ...
    # NEW: Batch rollback/fork
    "BatchRollbackResult",
    "BatchForkResult",
    "BatchExecutionCoordinator",
    "BatchOperationRequest",
    "BatchOperationResult",
    "OperationType",
]
```

### 7.2 Usage Examples

#### Simplest Rollback (uses last_checkpoint_id)

```python
from wtb.sdk import WTBTestBench

# Create and run batch test
wtb = WTBTestBench.create(mode="development", enable_file_tracking=True)
wtb.register_project(my_project)

batch = wtb.run_batch_test(
    project="my_workflow",
    variant_matrix=[{"model": "gpt-4"}, {"model": "gpt-3.5"}],
    test_cases=[{"question": "What is 2+2?"}],
)

# Find the variant you want to rollback
variant_result = batch.results[0]  # First variant

# Rollback to last checkpoint (most common use case)
# No checkpoint_id needed - defaults to result.last_checkpoint_id
rollback = wtb.rollback_batch_result(variant_result)
print(f"Rollback success: {rollback.success}")
```

#### Rollback to Specific Checkpoint

```python
# Get available checkpoints
checkpoints = wtb.get_batch_result_checkpoints(variant_result)
print(f"Available checkpoints: {[cp.id for cp in checkpoints]}")

# Rollback to specific checkpoint (e.g., checkpoint 2)
rollback = wtb.rollback_batch_result(variant_result, checkpoints[2].id.value)
print(f"Rolled back to: {rollback.checkpoint_id}")
```

#### Fork for Exploration

```python
# Fork from last checkpoint (simple)
fork = wtb.fork_batch_result(variant_result)

# Fork from specific checkpoint with modified state
fork = wtb.fork_batch_result(
    variant_result,
    checkpoint_id=checkpoints[2].id.value,
    new_state={"temperature": 0.5}  # Try different temperature
)

# Run the forked execution
forked_execution = wtb.resume(fork.fork_execution_id)
```

#### Advanced: Direct Coordinator Access

```python
from wtb.sdk import WTBTestBench, BatchOperationRequest, OperationType

wtb = WTBTestBench.create(mode="development")
batch = wtb.run_batch_test(...)

# Get coordinator for batch operations
coordinator = wtb.get_batch_coordinator()

# Batch rollback multiple variants
results = coordinator.batch_operate([
    BatchOperationRequest(
        execution_id=batch.results[0].execution_id,
        checkpoint_id=batch.results[0].last_checkpoint_id,
        operation=OperationType.ROLLBACK,
    ),
    BatchOperationRequest(
        execution_id=batch.results[1].execution_id,
        checkpoint_id=batch.results[1].last_checkpoint_id,
        operation=OperationType.FORK,
        new_state={"debug": True},
    ),
])

for result in results:
    print(f"{result.operation.value}: {result.success}")
```

---

## Summary Checklist

### Implementation Order (Dependencies)

```
┌─────────────────────────────────────────────────────────────────┐
│  Phase 1: Domain & Infrastructure (Independent)                 │
│  ───────────────────────────────────────────────────────────    │
│  [1] BatchTestResult ──┬──► [6] SDK Methods                     │
│  [2] Interfaces       ──┤                                        │
│  [3] Coordinator ─────┬┤                                         │
│  [4] Ray Factory ─────┘│                                         │
│  [5] Outbox Event ─────┘                                         │
│                                                                  │
│  Phase 2: SDK (Depends on Phase 1)                              │
│  ───────────────────────────────────────────────────────────    │
│  [6] SDK Methods ──► Requires [1] for last_checkpoint_id         │
│  [7] SDK DTOs                                                    │
│  [8] SDK Exports                                                 │
│                                                                  │
│  Phase 3: Testing (Depends on Phase 1 & 2)                      │
│  ───────────────────────────────────────────────────────────    │
│  [10] Unit Tests                                                 │
│  [11] Integration Tests                                          │
│  [12] SDK Tests                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Task List

| # | Task | File | Status | Priority | Depends | Est. |
|---|------|------|--------|----------|---------|------|
| 1 | Fix BatchTestResult info loss | `domain/models/batch_test.py` | ✅ COMPLETED | High | - | 30m |
| 2 | Create interface definitions | `domain/interfaces/batch_coordinator.py` | ✅ COMPLETED | High | - | 1h |
| 3 | Implement BatchExecutionCoordinator | `application/services/batch_execution_coordinator.py` | ✅ COMPLETED | High | 2 | 2-3h |
| 4 | Add factory method to RayBatchTestRunner | `application/services/ray_batch_runner.py` | ✅ COMPLETED | Medium | 3 | 30m |
| 5 | Add EXECUTION_FORKED event type | `domain/models/outbox.py` | ✅ COMPLETED | Medium | - | 10m |
| 6 | **Add SDK methods to WTBTestBench** | `sdk/test_bench.py` | ✅ COMPLETED | **High** | **1,3** | **1h** |
| 7 | **Add SDK result DTOs** | `sdk/test_bench.py` | ✅ COMPLETED | **Medium** | - | **15m** |
| 8 | **Update SDK exports** | `sdk/__init__.py` | ✅ COMPLETED | **Low** | 6,7 | **10m** |
| 9 | Update Application exports | `application/services/__init__.py` | ✅ COMPLETED | Low | 3 | 10m |
| 10 | Unit tests | `tests/unit/application/test_batch_execution_coordinator.py` | ✅ COMPLETED | High | 1-5 | 1-2h |
| 11 | Integration tests | `tests/integration/test_ray_batch_rollback.py` | ✅ COMPLETED | Medium | 1-9 | 1h |
| 12 | **SDK integration tests** | `tests/test_sdk/test_sdk_batch_rollback.py` | ✅ COMPLETED | **Medium** | 6-8 | **1h** |
| 13 | **Add BatchCoordinatorFactory** | `application/factories.py` | ✅ COMPLETED | **High** | 3 | **30m** |

**Total Estimated Time**: 8-10 hours  
**Actual Completion**: 2026-02-05  
**Test Coverage**: 58 tests (all passing with n=4 parallel execution)

### Implementation Summary

**Phase 1: Core Coordinator (COMPLETED)**
- ✅ `BatchTestResult` extended with `file_commit_id`, `checkpoint_count`, `last_checkpoint_id`
- ✅ `IBatchExecutionCoordinator` interface with rollback/fork operations
- ✅ `BatchExecutionCoordinator` implementation with two-phase transaction architecture
- ✅ `RayBatchTestRunner.create_rollback_coordinator()` factory method
- ✅ `EXECUTION_FORKED` outbox event type

**Phase 2: SDK Integration (COMPLETED)**
- ✅ `BatchRollbackResult` and `BatchForkResult` DTOs
- ✅ `WTBTestBench.get_batch_coordinator()` lazy accessor
- ✅ `WTBTestBench.rollback_batch_result()` convenience method
- ✅ `WTBTestBench.fork_batch_result()` convenience method
- ✅ `WTBTestBench.get_batch_result_checkpoints()` helper method
- ✅ `BatchCoordinatorFactory` for proper layer separation (DIP compliance)
- ✅ SDK exports updated in `wtb/sdk/__init__.py`

**Phase 3: Testing (COMPLETED)**
- ✅ Unit tests: 18 tests for `BatchExecutionCoordinator`
- ✅ Integration tests: 12 tests for Ray batch + coordinator
- ✅ SDK tests: 28 tests for SDK convenience methods
- ✅ All tests passing with parallel execution (n=4)

### Key Architectural Decisions

1. **Two-Phase Transaction Architecture**: State changes + outbox event in UoW (Phase 1), file restore post-commit (Phase 2)
2. **Layer Separation**: SDK delegates to `BatchCoordinatorFactory` (Application layer), never imports infrastructure directly
3. **Rollback vs Fork Semantics**: Rollback is destructive (overwrites future checkpoints), Fork is non-destructive (creates new execution)
4. **Graph Requirement**: Only `*_and_run()` operations require a graph; basic rollback/fork only need execution_id and checkpoint_id

---

## References

- Original design discussion: This document
- ExecutionController: `wtb/application/services/execution_controller.py`
- RayBatchTestRunner: `wtb/application/services/ray_batch_runner.py`
- FileTrackerService: `wtb/infrastructure/file_tracking/filetracker_service.py`
- OutboxProcessor: `wtb/infrastructure/outbox/processor.py`
