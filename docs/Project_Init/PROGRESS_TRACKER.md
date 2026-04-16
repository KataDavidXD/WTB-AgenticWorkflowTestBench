# WTB Implementation Progress Tracker

**Last Updated:** 2026-03-31  
**Overall Status:** ✅ Phase 1 Complete (v1.7 Released), Ray Batch Rollback v1.8 COMPLETED, Architecture Fix Sprint (Phases 1–7) COMPLETED

---

## Progress Overview

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        IMPLEMENTATION PROGRESS                                   │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Core Architecture         ████████████████████████████████████████  100%       │
│  Domain Layer              ████████████████████████████████████████  100%       │
│  Application Services      ████████████████████████████████████████  100%       │
│  Infrastructure            ████████████████████████████████████████  100%       │
│  API Layer                 ████████████████████████████████████████  100%       │
│  SDK                       ████████████████████████████████████████  100%       │
│  Tests                     ████████████████████████████████████████  100%       │
│  Documentation             ████████████████████████████████████████  100%       │
│                                                                                  │
│  Phase 0 (Domain Cleanup)  ████████████████████████████████████████  100%       │
│  Phase 1 (Batch Unify)     ████████████████████████████████████████  100%       │
│  Phase 2 (Final Cleanup)   ████████████████████████████████████████  100%       │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## ✅ Phase 0: Domain Cleanup - COMPLETED

**Decision:** Remove AgentGit Implementation, Preserve Session Domain Concept  
**Completed:** 2026-01-27

### Completed Tasks

| Task | Status | Notes |
|------|--------|-------|
| Refactor `IStateAdapter` interface | ✅ | Removed AgentGit methods, str IDs |
| Delete `AgentGitStateAdapter` | ✅ | Implementation removed |
| Refactor `LangGraphStateAdapter` | ✅ | Removed ID mapping |
| Refactor `InMemoryStateAdapter` | ✅ | Updated to str IDs |
| Rename `agentgit_session_id` → `session_id` | ✅ | Type: `str` |
| Rename `agentgit_checkpoint_id` → `checkpoint_id` | ✅ | Type: `str` |
| Update `ExecutionController` | ✅ | str IDs, added `fork()` |
| Delete `WTBTestBench.branch()` | ✅ | Was broken |
| Move `fork()` to `ExecutionController` | ✅ | ACID compliance |
| Clean SDK layer | ✅ | Removed `state_adapter` param |
| Create unit tests | ✅ | `tests/test_v16_architecture/` |

### Key Changes Summary

```python
# BEFORE (v1.5):
class IStateAdapter:
    def initialize_session(...) -> int           # AgentGit DB record
    def save_checkpoint(...) -> int              # AgentGit checkpoint ID
    def link_file_commit(...)                    # AgentGit-specific
    def create_branch(...) -> int                # AgentGit session branch

class Execution:
    agentgit_session_id: Optional[int]
    agentgit_checkpoint_id: Optional[int]

class WTBTestBench:
    def __init__(self, ..., state_adapter: IStateAdapter)
    def branch(...) -> BranchResult              # BROKEN

# AFTER (v1.6):
class IStateAdapter:
    def initialize_session(...) -> str           # Returns thread_id
    def save_checkpoint(...) -> str              # Returns UUID string
    # link_file_commit() REMOVED
    # create_branch() REMOVED

class Execution:
    session_id: Optional[str]                    # LangGraph thread_id
    checkpoint_id: Optional[str]                 # LangGraph UUID

class WTBTestBench:
    def __init__(self, ...)                      # NO state_adapter param
    # branch() REMOVED
    def fork(...) -> ForkResult                  # Delegates to controller

class ExecutionController:
    def fork(...) -> Execution                   # NEW: moved from SDK
```

---

## ✅ Phase 1: Unify Batch Execution - COMPLETED (v1.7)

**Version:** v1.7  
**Completed:** 2026-01-27

### Completed Tasks

#### TODO-006: Create ExecutionControllerFactory ✅
**Status:** Completed | **Effort:** Medium

```python
# v1.7 Implementation: ManagedController Pattern

@dataclass
class ManagedController:
    """Controller with managed UoW lifecycle."""
    controller: ExecutionController
    uow: IUnitOfWork
    
    def __enter__(self) -> "ManagedController":
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.uow.commit()
        else:
            self.uow.rollback()
        self.uow.__exit__(exc_type, exc_val, exc_tb)

class ExecutionControllerFactory:
    def create_isolated(self) -> ManagedController:
        """Create isolated controller with its own UoW (ACID Isolation)."""
        uow = UnitOfWorkFactory.create(...)
        uow.__enter__()
        controller = ExecutionController(...)
        return ManagedController(controller=controller, uow=uow)
    
    @classmethod
    def get_factory_callable(cls, config) -> Callable[[], ManagedController]:
        """Get factory callable for batch runners."""
        factory = cls(config)
        return factory.create_isolated
```

**Completed:**
- [x] Created `ManagedController` for proper UoW lifecycle
- [x] Created `ExecutionControllerFactory.create_isolated()` for ACID isolation
- [x] Created `get_factory_callable()` for batch runners
- [x] Added unit tests in `tests/test_v16_architecture/test_controller_factory.py`

---

#### TODO-007: Fix ThreadPoolBatchTestRunner ✅
**Status:** Completed | **Effort:** Medium

**Completed:**
- [x] Injected `ExecutionControllerFactory` via `controller_factory` parameter
- [x] Each thread calls `controller_factory()` for isolated execution
- [x] Removed `_run_workflow_nodes()` placeholder
- [x] Added `_extract_metrics()` for proper metric extraction
- [x] Added integration tests in `tests/test_v16_architecture/test_batch_runner_parity.py`

---

#### TODO-008: Fix RayBatchTestRunner ✅
**Status:** Completed | **Effort:** Medium

**Completed:**
- [x] Updated Actor to use ExecutionController pattern consistently
- [x] Fixed str ID references (`checkpoint_id` instead of `agentgit_checkpoint_id`)
- [x] Added variant info to initial state (like ThreadPoolBatchTestRunner)
- [x] Updated docstrings for ACID compliance documentation

---

## ✅ ISS-006: Outbox Processor Lifecycle - COMPLETED (v1.7)

**Status:** Completed | **Completed:** 2026-01-27

### Implementation

Created `OutboxLifecycleManager` in `wtb/infrastructure/outbox/lifecycle.py`:

| Feature | Implementation |
|---------|----------------|
| Auto-start | `auto_start=True` on init |
| Health endpoint | `get_health()` returns `HealthStatus` |
| Graceful shutdown | `register_signals=True` + `atexit` hooks |
| Callbacks | `on_start`, `on_stop`, `on_error` |
| Context manager | `__enter__` / `__exit__` protocol |

**Tests:** `tests/test_v16_architecture/test_outbox_lifecycle.py`

---

## ✅ Architecture Consolidation (2026-01-27) - COMPLETED

**Issue Reference:** `docs/Project_Init/new_issues.md`

### Issues Resolved

| Issue | Category | Status | Resolution |
|-------|----------|--------|------------|
| CRITICAL-001 | Dual Checkpoint-File Storage | ✅ Fixed | Deleted `CheckpointFileORM`, use `CheckpointFileLinkORM` |
| CRITICAL-002 | Dual Repository Interfaces | ✅ Fixed | Use `ICheckpointFileLinkRepository` only |
| HIGH-001 | Event Base Class Inconsistency | ✅ Fixed | `CheckpointEvent` extends `WTBEvent` |
| HIGH-002 | Deprecated Code Still Exported | ✅ Fixed | Created `_deprecated.py`, added comments |
| MEDIUM-001 | SRP Violation in file_processing.py | ✅ Fixed | Split into package (already done) |

### Changes Made

| File | Change |
|------|--------|
| `wtb/infrastructure/database/models.py` | DELETED `CheckpointFileORM` |
| `wtb/infrastructure/database/__init__.py` | Updated exports to use `CheckpointFileLinkORM` |
| `wtb/domain/events/checkpoint_events.py` | `CheckpointEvent` now extends `WTBEvent` |
| `wtb/domain/interfaces/_deprecated.py` | NEW: Deprecated interface documentation |
| `wtb/domain/interfaces/__init__.py` | Added deprecation comments |
| `wtb/domain/interfaces/repositories.py` | REMOVED legacy methods from `INodeBoundaryRepository` |
| `wtb/infrastructure/database/inmemory_unit_of_work.py` | REMOVED legacy methods |
| `wtb/infrastructure/database/migrations/004_consolidate_checkpoint_files.sql` | NEW: Migration script |

### New Tests

| Test File | Purpose |
|-----------|---------|
| `tests/test_wtb/test_architecture_consolidation.py` | Unit tests for consolidation |
| `tests/test_wtb/test_migration_integration.py` | Integration tests for migration |

---

## ✅ Phase 2: Final Cleanup - IN PROGRESS

**Target Version:** v1.8  
**Target Date:** 2026-03-01

### Completed Tasks (v1.7)

#### TODO-010: Move _project_to_workflow to ProjectService ✅
**Status:** Completed | **Effort:** Low

**Completed:**
- [x] Created `WorkflowConversionService` in `wtb/application/services/project_service.py`
- [x] Updated SDK to delegate to `WorkflowConversionService`
- [x] Proper layer separation (SDK → Application → Domain)

---

#### TODO-011: Fix UoW Lifecycle in Factories ✅
**Status:** Completed | **Effort:** Medium

**Completed:**
- [x] Created `ManagedController` with proper `__enter__`/`__exit__`
- [x] UoW properly committed/rolled back in context manager
- [x] Added tests in `test_controller_factory.py`

---

### Completed Tasks (v1.8 - 2026-01-28)

#### TODO-014: Fix Infrastructure Bugs & Real Service Integration ✅
**Status:** Completed | **Effort:** High

**Completed:**
- [x] Fixed `idempotency_key` persistence (ORM & Mapper update)
- [x] Fixed Windows blob rename issue (using `os.replace`)
- [x] Fixed Outbox status update persistence (Repository pattern compliance)
- [x] Fixed JSON serialization for Value Objects (enhanced `OutboxMapper`)
- [x] Verified full stack with REAL services (Ray, UV, SQLite) in `test_real_services_integration.py`

---

### Remaining TODO

#### TODO-012: Implement gRPC API
**Status:** Not Started | **Effort:** Medium

**Tasks:**
- [ ] Implement gRPC server from existing protos
- [ ] Add gRPC client to SDK

---

#### TODO-013: Implement UoW File Tracking Integration ✅
**Status:** Completed | **Effort:** Medium

**Completed:**
- [x] Updated `IUnitOfWork` interface with `blobs` and `file_commits`
- [x] Updated `SQLAlchemyUnitOfWork` to implement new properties
- [x] Created integration tests `tests/test_file_processing/integration/test_uow_integration.py`
- [x] Verified ACID compliance for file tracking workflow

---

## ✅ v2.0 Async Architecture - IN PROGRESS

**Target Version:** v2.0  
**Status:** Implementation Started (2026-01-28)  
**Architecture Document:** [ASYNC_ARCHITECTURE_PLAN.md](./ASYNC_ARCHITECTURE_PLAN.md)

### Implementation Status

| Phase | Status | Notes |
|-------|--------|-------|
| Architecture Design | ✅ Complete | `ASYNC_ARCHITECTURE_PLAN.md` v1.0 |
| Code Review | ✅ Complete | 10 issues fixed, 4 suggestions added |
| Document Update | ✅ Complete | v1.1 incorporates all review feedback |
| Phase A: Async Interfaces | ✅ Complete | `IAsyncStateAdapter`, `IAsyncUnitOfWork` |
| Phase B: Async Infrastructure | ✅ Complete | Async repositories, file tracking |
| Phase C: Async Services | ✅ Complete | `AsyncExecutionController`, `AsyncLangGraphStateAdapter` |
| Phase D: Testing | ✅ Complete | Transaction consistency tests (Scenarios A-E) |

### Review Summary (2026-01-27)

| Priority | Issue | Fix |
|----------|-------|-----|
| P0 | `aiofiles.os.path.exists()` doesn't exist | Use `_path_exists()` helper with `aiofiles.os.stat()` |
| P0 | `run_until_complete()` in async context | Lazy graph compilation + `aset_workflow_graph()` |
| P0 | `__aexit__` missing try/finally | Wrapped rollback in try/finally |
| P1 | Streaming error leaves RUNNING state | Added try/except with status update |
| P1 | Cross-DB checkpoint verification | Added `CHECKPOINT_VERIFY` outbox event |
| P1 | Saga compensation error handling | Track and raise `CompensationError` |
| P2 | Dual interface violates ISP | Separate `IStateAdapter` / `IAsyncStateAdapter` |
| P2 | Outbox event ordering | Added `order_by="created_at"` FIFO guarantee |
| P3 | asyncpg for production | Added `[production]` optional dependency group |

### Implementation Details (2026-01-28)

**New Files Created:**

| File | Purpose | Lines |
|------|---------|-------|
| `wtb/infrastructure/adapters/async_langgraph_state_adapter.py` | Async state adapter implementing `IAsyncStateAdapter` | ~400 |
| `wtb/application/services/async_execution_controller.py` | Async execution orchestration with ACID | ~350 |
| `tests/test_file_processing/integration/test_async_transaction_consistency.py` | Transaction consistency tests | ~450 |

**Transaction Consistency Error Scenarios Tested:**

| Scenario | Chinese Description | Solution |
|----------|---------------------|----------|
| A: Non-idempotent Writes | 工具写入不是幂等的，retry 会造成重复记录 | Content-addressable storage (SHA-256) |
| B: Partial Commit | 中途失败留下半套数据；孤儿数据可能被错误读到 | Two-phase write + AsyncBlobOrphanCleaner |
| C: Async Ordering | 异步任务没有写入顺序控制，写入错位混乱 | Outbox pattern with FIFO guarantee |
| D: Stale Reads | 读到过时的写/读在写之前 | Session isolation + explicit commit |
| E: Node Env Isolation | Node级别虚拟环境管理，独立于workflow环境 | GrpcEnvironmentProvider per-node venv |

**SOLID Compliance:**

| Principle | Implementation |
|-----------|----------------|
| SRP | `AsyncExecutionController` orchestrates only, delegates to adapters |
| OCP | New adapters via `IAsyncStateAdapter` interface |
| LSP | All async adapters are interchangeable |
| ISP | Separate `IStateAdapter` and `IAsyncStateAdapter` interfaces |
| DIP | Controller depends on `IAsyncStateAdapter`, not implementations |

**ACID Compliance:**

| Property | Implementation |
|----------|----------------|
| Atomicity | `AsyncSQLAlchemyUnitOfWork` wraps all operations |
| Consistency | SHA-256 hash validation, FK constraints |
| Isolation | Per-session isolation, explicit commit boundaries |
| Durability | SQLite WAL mode, aiofiles fsync |

### Best Practices Added

- ✅ Connection pool management (`AsyncSQLAlchemyUnitOfWork._engine_pool`)
- ✅ Async health checks (`check_async_health()`)
- ✅ Structured logging (`@log_async_operation` decorator)
- ✅ Typed AsyncContextManager factory (`get_async_uow()`)

### Implementation Roadmap

| Phase | Timeframe | Focus |
|-------|-----------|-------|
| A | Week 1-2 | Async interfaces (`IAsyncStateAdapter`, `IAsyncUnitOfWork`) |
| B | Week 3-4 | Async infrastructure (DB, Files, Adapters) |
| C | Week 5-6 | Async application services (`AsyncExecutionController`) |
| D | Week 7-8 | Integration, testing, documentation |

---

## Component Status

### 1. Domain Layer (`wtb/domain/`) - ✅ 100%

| Component | Status | Notes |
|-----------|--------|-------|
| **Models** | ✅ Done | `Execution` uses str IDs |
| **Events** | ✅ Done | 70+ event types |
| **Interfaces** | ✅ Done | `IStateAdapter` cleaned (v1.6) |

### 2. Application Layer (`wtb/application/`) - ✅ 100%

| Service | Status | Notes |
|---------|--------|-------|
| `ExecutionController` | ✅ | str IDs, has `fork()` |
| `RayBatchRunner` | ✅ | ACID compliant (Actor isolation) |
| `BatchTestRunner` | ✅ | Uses ExecutionControllerFactory |

### 3. Infrastructure Layer (`wtb/infrastructure/`) - ✅ 100%

| Component | Status | Notes |
|-----------|--------|-------|
| `LangGraphStateAdapter` | ✅ | PRIMARY, str IDs native |
| `InMemoryStateAdapter` | ✅ | str IDs |
| `AgentGitStateAdapter` | ❌ DELETED | Removed in v1.6 |

### 4. SDK Layer (`wtb/sdk/`) - ✅ 100%

| Component | Status | Notes |
|-----------|--------|-------|
| `TestBench` | ✅ | Clean, no `state_adapter` |
| `branch()` | ❌ DELETED | Removed in v1.6 |
| `fork()` | ✅ | Delegates to controller |

### 5. Test Coverage

| Test Category | Status | Notes |
|---------------|--------|-------|
| `test_v16_architecture/` | ✅ NEW | str IDs, fork tests |
| `test_wtb/` | ✅ | ~85% coverage |
| `test_langgraph/` | ✅ | ~90% coverage |

---

## Architecture Compliance (Post-v1.6)

### SOLID Principles

| Principle | Status | Evidence |
|-----------|--------|----------|
| **S**ingle Responsibility | ✅ | `IStateAdapter` focused |
| **O**pen/Closed | ✅ | New adapters via interface |
| **L**iskov Substitution | ✅ | All str IDs |
| **I**nterface Segregation | ✅ | ~15 methods (was 20+) |
| **D**ependency Inversion | ✅ | SDK uses only App services |

### ACID Compliance

| Property | Status | Notes |
|----------|--------|-------|
| **A**tomicity | ✅ | UoW pattern |
| **C**onsistency | ✅ | Unified str types |
| **I**solation | ✅ | ManagedController & Actor Isolation |
| **D**urability | ✅ | SQLite/PostgreSQL |

---

## Version Roadmap

| Version | Focus | Target | Status |
|---------|-------|--------|--------|
| v1.5 | Initial release | 2026-01-20 | ✅ Released |
| **v1.6** | **Phase 0: Domain Cleanup** | 2026-01-27 | **✅ COMPLETED** |
| **v1.7** | **Phase 1: Batch Unify, ISS-006** | 2026-01-27 | **✅ COMPLETED** |
| **v1.8** | **Ray Batch Rollback Coordination** | 2026-02-05 | **✅ COMPLETED** |
| v1.9 | Phase 2: gRPC API | 2026-03-01 | Planned |
| **v2.0** | **Full Async Architecture** | 2026-01-28 | **🔄 IN PROGRESS** |

### v2.0 Async Architecture (PROPOSED)

**Reference:** [ASYNC_ARCHITECTURE_PLAN.md](./ASYNC_ARCHITECTURE_PLAN.md)

| Phase | Description | Duration |
|-------|-------------|----------|
| Phase A | Async Interfaces (IAsyncStateAdapter, IAsyncUnitOfWork) | Week 1-2 |
| Phase B | Async Infrastructure (DB, Files, LangGraph) | Week 3-4 |
| Phase C | Async Application Services (AsyncExecutionController) | Week 5-6 |
| Phase D | Integration, Migration, Documentation | Week 7-8 |

**Key Features:**
- Non-blocking I/O across all layers
- Native LangGraph async (ainvoke, astream)
- Async file tracking (aiofiles)
- Async SQLAlchemy 2.0 (aiosqlite, asyncpg)
- Backward compatibility via ExecutionControllerCompat

---

## File Changes Summary (v1.6)

### Modified Files

| File | Change |
|------|--------|
| `wtb/domain/interfaces/state_adapter.py` | Refactored: str IDs, removed AgentGit methods |
| `wtb/domain/models/workflow.py` | Renamed: `session_id`, `checkpoint_id` (str) |
| `wtb/infrastructure/adapters/inmemory_state_adapter.py` | Updated: str IDs |
| `wtb/infrastructure/adapters/langgraph_state_adapter.py` | Refactored: removed ID mapping |
| `wtb/infrastructure/adapters/__init__.py` | Removed AgentGitStateAdapter export |
| `wtb/application/services/execution_controller.py` | Added `fork()`, str IDs |
| `wtb/application/factories.py` | Removed state_adapter from WTBTestBench |
| `wtb/sdk/test_bench.py` | Removed `branch()`, `state_adapter` param |

### Deleted Files

| File | Reason |
|------|--------|
| `wtb/infrastructure/adapters/agentgit_state_adapter.py` | AgentGit implementation removed |

### New Files

| File | Purpose |
|------|---------|
| `tests/test_v16_architecture/__init__.py` | Test package for v1.6 |
| `tests/test_v16_architecture/test_string_ids.py` | str ID compliance tests |
| `tests/test_v16_architecture/test_execution_controller_fork.py` | fork() tests |

---

## File Changes Summary (v1.7)

### Modified Files

| File | Change |
|------|--------|
| `wtb/application/factories.py` | Added `ManagedController`, `ExecutionControllerFactory.create_isolated()`, `get_factory_callable()` |
| `wtb/application/services/batch_test_runner.py` | Refactored to use `controller_factory`, removed placeholder `_run_workflow_nodes()` |
| `wtb/application/services/ray_batch_runner.py` | Fixed str ID refs, updated to use ExecutionController pattern |
| `wtb/application/services/project_service.py` | Added `WorkflowConversionService` |
| `wtb/application/services/__init__.py` | Exported `WorkflowConversionService` |
| `wtb/sdk/test_bench.py` | Delegates `_project_to_workflow` to `WorkflowConversionService` |
| `wtb/infrastructure/outbox/__init__.py` | Exported lifecycle components |

### New Files

| File | Purpose |
|------|---------|
| `wtb/infrastructure/outbox/lifecycle.py` | `OutboxLifecycleManager` for ISS-006 |
| `tests/test_v16_architecture/test_controller_factory.py` | Tests for `ManagedController`, factory isolation |
| `tests/test_v16_architecture/test_batch_runner_parity.py` | Tests for batch runner ACID compliance |
| `tests/test_v16_architecture/test_outbox_lifecycle.py` | Tests for `OutboxLifecycleManager` |

---

## ✅ v2.1: SDK & API Communication Layer Improvements - COMPLETED

**Version:** v2.1  
**Completed:** 2026-01-28

### Overview

Comprehensive refactoring of SDK and REST/gRPC communication layers to ensure:
- Proper SOLID compliance with interface-based design
- ACID transaction consistency across all API operations
- Outbox pattern for cross-system event ordering

### Completed Tasks

| Task | Status | Notes |
|------|--------|-------|
| Create IExecutionAPIService interface | ✅ | SOLID ISP compliance |
| Create IAuditAPIService interface | ✅ | Separated audit concerns |
| Create IBatchTestAPIService interface | ✅ | Batch test abstraction |
| Create IWorkflowAPIService interface | ✅ | Workflow CRUD abstraction |
| Implement ExecutionAPIService | ✅ | ACID-compliant with UoW |
| Implement AuditAPIService | ✅ | Read-only audit access |
| Implement BatchTestAPIService | ✅ | Outbox event creation |
| Implement WorkflowAPIService | ✅ | Transaction boundaries |
| Update REST dependencies for DI | ✅ | Proper interface injection |
| Implement gRPC WTBServicer | ✅ | Transaction support |
| Fix checkpoint_id type (int → string) | ✅ | UUID strings throughout |
| Add backward-compatible aliases | ✅ | Legacy method support |
| Create API service unit tests | ✅ | 23 tests |
| Create transaction consistency tests | ✅ | 14 tests (ACID scenarios) |
| Add missing OutboxEventType values | ✅ | API events supported |

### Architecture Changes

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    API LAYER ARCHITECTURE (v2.1)                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   REST Endpoints                 gRPC Servicer                              │
│        │                              │                                     │
│        ▼                              ▼                                     │
│   ┌──────────────────────────────────────────────────────┐                 │
│   │           IExecutionAPIService (Abstraction)         │                 │
│   │           IAuditAPIService                           │                 │
│   │           IBatchTestAPIService                       │                 │
│   └──────────────────────────────────────────────────────┘                 │
│                          │                                                  │
│                          ▼                                                  │
│   ┌──────────────────────────────────────────────────────┐                 │
│   │         ExecutionAPIService (Concrete)               │                 │
│   │         ├── IUnitOfWork (Transaction boundary)       │                 │
│   │         ├── IExecutionController (Domain ops)        │                 │
│   │         └── Outbox (Event ordering)                  │                 │
│   └──────────────────────────────────────────────────────┘                 │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### New Files

| File | Purpose |
|------|---------|
| `wtb/domain/interfaces/api_services.py` | API service interfaces (DIP) |
| `wtb/application/services/api_services.py` | Concrete implementations |
| `wtb/api/grpc/servicer.py` | gRPC servicer with transactions |
| `tests/test_api/test_api_services_unit.py` | Unit tests (23 tests) |
| `tests/test_api/test_api_transaction_consistency.py` | ACID tests (14 tests) |

### Modified Files

| File | Change |
|------|--------|
| `wtb/api/rest/dependencies.py` | DI with new API services, legacy fallback |
| `wtb/api/grpc/__init__.py` | Export servicer components |
| `wtb/domain/models/outbox.py` | Added API event types |
| `wtb/domain/interfaces/__init__.py` | Export API interfaces |
| `wtb/application/services/__init__.py` | Export API services |

### SOLID Compliance Summary

| Principle | Implementation |
|-----------|----------------|
| **S**RP | Each API service handles one concern |
| **O**CP | New services via interface implementation |
| **L**SP | All services interchangeable via interfaces |
| **I**SP | Separate interfaces: Execution, Audit, BatchTest, Workflow |
| **D**IP | REST/gRPC depend on abstractions, not implementations |

### ACID Compliance Summary

| Property | Implementation |
|----------|----------------|
| **A**tomicity | All operations wrapped in Unit of Work |
| **C**onsistency | Validation before commit, type-safe DTOs |
| **I**solation | Per-request UoW instances |
| **D**urability | Outbox events persist before response |

---

## Code Quality Audit (2026-01-27)

### Issues Fixed

| Issue | Category | Status | Location |
|-------|----------|--------|----------|
| `BranchResult` imported but doesn't exist | Code Error | ✅ Fixed | `wtb/sdk/__init__.py` |
| `IWorkflowRepository` missing `list_all()` | ISP Gap | ✅ Fixed | Interface + implementations |
| Duplicate `_create_state_adapter()` code | DRY Violation | ✅ Fixed | `WTBTestBenchFactory` now delegates |

### Changes Made

| File | Change |
|------|--------|
| `wtb/sdk/__init__.py` | Removed `BranchResult` import/export (dead code from v1.6 branch() removal) |
| `wtb/domain/interfaces/repositories.py` | Added `list_all()` to `IWorkflowRepository` interface |
| `wtb/infrastructure/database/repositories/base.py` | Added `list_all()` to `BaseRepository` |
| `wtb/infrastructure/database/inmemory_unit_of_work.py` | Added `list_all()` to `InMemoryWorkflowRepository` |
| `wtb/infrastructure/database/repositories/workflow_repository.py` | Added `list_all()` override |
| `wtb/application/factories.py` | `WTBTestBenchFactory._create_state_adapter()` now delegates to `ExecutionControllerFactory._create_state_adapter()` (DRY) |

### Architecture Observations

**SOLID Compliance:**
- ✅ SRP: Services have clear responsibilities
- ✅ OCP: New adapters via interface implementations
- ✅ LSP: All adapters work interchangeably (str IDs)
- ✅ ISP: Interfaces are focused (~15 methods in IStateAdapter)
- ✅ DIP: SDK depends on Application services, not Infrastructure

**ACID Compliance:**
- ✅ Atomicity: UoW pattern with explicit commit()
- ✅ Consistency: Unified str ID types across layers
- ✅ Isolation: ManagedController provides isolated UoW per execution
- ✅ Durability: SQLite/PostgreSQL persistence

**Deprecation Note:**
- `IStateAdapter` is marked DEPRECATED but still heavily used
- `ICheckpointStore` is the "new" primary interface but not yet integrated
- This is an architectural decision for gradual migration (not a bug)

---

## Architecture Review (2026-01-28)

**Reference:** [ARCHITECTURE_REVIEW_2026_01_28.md](./ARCHITECTURE_REVIEW_2026_01_28.md)

### Summary of Findings

| Category | Status | Critical | High | Medium | Low |
|----------|--------|----------|------|--------|-----|
| File System Duplicate Logic | ⚠️ | 0 | 2 | 1 | 0 |
| Checkpoint Entry/Exit Consistency | ⚠️ | 1 | 1 | 0 | 0 |
| Async/API Services Quality | ✅ | 0 | 0 | 2 | 1 |
| Outbox Pattern Effectiveness | ✅ | 0 | 0 | 1 | 1 |
| SOLID Compliance | ✅ | 0 | 0 | 1 | 2 |
| ACID/Transaction Consistency | ✅ | 0 | 1 | 0 | 0 |

### Critical Fixes Applied (2026-01-28)

| Issue | Fix | File |
|-------|-----|------|
| CP-002 | Fixed repository mapping to return str IDs | `node_boundary_repository.py` |
| FS-002 | Created shared OutboxMapper | `mappers/outbox_mapper.py` |
| - | Created migration for deprecated ORM fields | `migrations/005_node_boundary_cleanup.sql` |
| - | Added architecture consistency tests | `test_architecture/test_node_boundary_consistency.py` |

### Remaining Action Items

- [ ] CP-001: Run migration to remove deprecated ORM columns
- [x] FS-001: Extract `BlobStorageCore` for shared logic ✅ (2026-01-28)
- [x] API-001: Fix `list_executions()` to query repository ✅ (2026-01-28)
- [ ] ACID-001: Full async migration (documented limitation)

---

## ✅ Transaction Consistency Examples - COMPLETED (2026-01-29)

### Overview

Added comprehensive examples demonstrating how WTB handles transaction consistency issues.

**Location:** `examples/transaction_consistency/`

### Completed Scenarios

| Scenario | Description | Status |
|----------|-------------|--------|
| A: Non-idempotent Writes | Retry causes duplicates → Checkpoint idempotency | ✅ |
| B: Partial Commit | Orphan data on failure → Unit of Work pattern | ✅ |
| C: Async Side Effects | Out-of-order writes → Ordered node execution | ✅ |
| D: Stale Reads | Reading outdated data → Snapshot isolation | ✅ |
| E: Node Venv Management | Dependency conflicts → UV venv isolation | ✅ |

### Implementation Summary

| Component | Files | Lines | Status |
|-----------|-------|-------|--------|
| Base Classes | `scenarios/base.py` | ~500 | ✅ |
| Scenario A | `scenario_a_non_idempotent.py` | ~450 | ✅ |
| Scenario B | `scenario_b_partial_commit.py` | ~400 | ✅ |
| Scenario C | `scenario_c_async_side_effects.py` | ~450 | ✅ |
| Scenario D | `scenario_d_stale_reads.py` | ~400 | ✅ |
| Scenario E | `scenario_e_node_venv.py` | ~400 | ✅ |
| Runner | `run_scenarios.py` | ~200 | ✅ |
| Unit Tests | `tests/test_scenarios.py` | ~350 | ✅ |
| Integration Tests | `tests/test_integration.py` | ~300 | ✅ |
| Documentation | `README.md` | ~250 | ✅ |

### Design Patterns Used

- **Template Method**: `ScenarioBase.run()` defines algorithm skeleton
- **Strategy**: `EnvironmentConfig` for different isolation strategies
- **Factory**: Graph factories create LangGraph workflows
- **Repository**: `SimulatedDataStore` for testing

### SOLID Compliance

- **SRP**: Each scenario handles one issue
- **OCP**: Extend via inheritance, not modification
- **LSP**: All scenarios substitute `ScenarioBase`
- **ISP**: Focused interfaces per concern
- **DIP**: Depend on `WTBTestBench` abstraction

---

## ✅ Ray Batch Rollback Coordination - COMPLETED (2026-02-05)

### Overview

`BatchExecutionCoordinator` v1.8 implemented to enable rollback/fork operations on batch test results.

**Design Document:** `docs/ray_batch_runner.md` (Status: COMPLETED)

### Problem Statement (RESOLVED)

After `RayBatchTestRunner.run_batch_test()` completes:
- ✅ Rollback specific variant via `coordinator.rollback()`
- ✅ `BatchTestResult` now includes `file_commit_id`, `checkpoint_count`, `last_checkpoint_id`
- ✅ `RayBatchTestRunner.create_rollback_coordinator()` factory method added

### Adopted Design

| Aspect | Decision | Rationale |
|--------|----------|-----------|
| Pattern | `BatchExecutionCoordinator` | SRP: Separate orchestration from batch execution |
| DIP | `IUnitOfWorkFactory`, `IExecutionControllerFactory` | Interface-based dependency injection |
| ACID | Two-phase operation | State in UoW, file restore post-commit |
| Audit | Outbox events | `ROLLBACK_PERFORMED`, `EXECUTION_FORKED` |

### Transaction Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Phase 1: UoW Transaction (State + Metadata)                    │
│  1. controller.rollback() ──► restore state                     │
│  2. outbox.add(ROLLBACK_PERFORMED) ──► queue audit event        │
│  3. uow.commit() ──► ACID durability                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Phase 2: Post-Commit File Restore (Best-effort)                │
│  4. file_tracking.restore_commit() ──► restore files            │
│     (logged, retryable via outbox if fails)                     │
└─────────────────────────────────────────────────────────────────┘
```

### Implementation Status - ALL COMPLETED

| # | Task | File | Status |
|---|------|------|--------|
| 1 | Fix BatchTestResult info loss | `domain/models/batch_test.py` | ✅ |
| 2 | Create interface definitions | `domain/interfaces/batch_coordinator.py` | ✅ |
| 3 | Implement BatchExecutionCoordinator | `application/services/batch_execution_coordinator.py` | ✅ |
| 4 | Add factory method to RayBatchTestRunner | `application/services/ray_batch_runner.py` | ✅ |
| 5 | Add EXECUTION_FORKED event type | `domain/models/outbox.py` | ✅ |
| 6 | Update exports | `__init__.py` files | ✅ |
| 7 | Unit tests | `tests/unit/application/test_batch_execution_coordinator.py` | ✅ |
| 8 | Integration tests | `tests/integration/test_ray_batch_rollback.py` | ✅ |

### New Files Created

| File | Purpose | Lines |
|------|---------|-------|
| `wtb/domain/interfaces/batch_coordinator.py` | Interface definitions | ~350 |
| `wtb/application/services/batch_execution_coordinator.py` | Implementation | ~580 |
| `tests/unit/application/test_batch_execution_coordinator.py` | Unit tests | ~450 |
| `tests/integration/test_ray_batch_rollback.py` | Integration tests | ~350 |

### Modified Files

| File | Change |
|------|--------|
| `wtb/domain/models/batch_test.py` | Added rollback fields to BatchTestResult |
| `wtb/domain/models/outbox.py` | Added EXECUTION_FORKED event type |
| `wtb/application/services/ray_batch_runner.py` | Added create_rollback_coordinator() factory |
| `wtb/domain/interfaces/__init__.py` | Exported batch coordinator interfaces |
| `wtb/application/services/__init__.py` | Exported BatchExecutionCoordinator |

### Usage Example

```python
# Run batch test
runner = RayBatchTestRunner(config, ...)
result = runner.run_batch_test(batch_test)

# After batch completes, rollback specific variant
coordinator = runner.create_rollback_coordinator()
execution = coordinator.rollback(
    execution_id=result.results[0].execution_id,
    checkpoint_id=result.results[0].last_checkpoint_id,
)

# Or fork for A/B exploration
forked = coordinator.fork(
    execution_id=result.results[0].execution_id,
    checkpoint_id=result.results[0].last_checkpoint_id,
    new_state={"temperature": 0.7},
)
```

### Key Decisions (Documented in ARCHITECTURE.md §13.6, §13.7, §13.8)

1. **Two-Phase Transaction**: State changes + outbox in UoW (Phase 1), file restore post-commit (Phase 2)
2. **Rollback vs Fork Semantics**: Rollback is destructive, Fork is non-destructive
3. **Graph Requirement**: Only `*_and_run()` operations require a graph
4. **SDK Layer Separation**: SDK delegates to Application factories, never imports infrastructure directly

### SDK Integration (v1.8)

| Component | Purpose | File |
|-----------|---------|------|
| `BatchRollbackResult` | SDK DTO for rollback operations | `sdk/test_bench.py` |
| `BatchForkResult` | SDK DTO for fork operations | `sdk/test_bench.py` |
| `BatchCoordinatorFactory` | Application factory for coordinator creation | `application/factories.py` |
| `get_batch_coordinator()` | SDK lazy coordinator accessor | `sdk/test_bench.py` |
| `rollback_batch_result()` | SDK convenience method | `sdk/test_bench.py` |
| `fork_batch_result()` | SDK convenience method | `sdk/test_bench.py` |

**Layer Separation (DIP Compliance):**
```
SDK Layer (WTBTestBench)
    │
    ├── batch_runner available? ──► batch_runner.create_rollback_coordinator()
    │
    └── fallback ──► BatchCoordinatorFactory.create_default() [Application Layer]
                          │
                          └── UnitOfWorkFactory, StateAdapter [Infrastructure]
```

**Test Coverage:**
- Unit tests: `tests/test_sdk/test_sdk_batch_rollback.py` (28 tests)
- Integration tests: `tests/integration/test_ray_batch_rollback.py` (12 tests)
- Coordinator tests: `tests/unit/application/test_batch_execution_coordinator.py` (18 tests)

---

## 🔄 BioRAG Benchmark Analysis - IN PROGRESS (2026-02-02)

### Overview

Analysis of MedRAG and RAG-Gym benchmarks for conversion to LangGraph-style workflows with transaction consistency.

**Location:** `examples/bio_rag_benchmarks/`

### Projects Analyzed

| Project | Description | Workflow Style |
|---------|-------------|----------------|
| MedRAG | Medical QA RAG toolkit | Single/Multi-round retrieval |
| RAG-Gym | MDP-based agentic RAG | Gym-style step() loop |

### Key Findings

#### Architecture Comparison

| Aspect | MedRAG | RAG-Gym | WTB |
|--------|--------|---------|-----|
| State Management | In-memory variables | Immutable State objects | LangGraph Checkpointing |
| Error Handling | Try-except with .error files | Error string in Action | Checkpoint rollback |
| Caching | File-based (id2text.json) | Per-episode qa_cache | Venv Cache + Outbox |
| Transaction | None | None | Unit of Work + Outbox |
| Environment | Monolithic (PyTorch + Java) | Monolithic | Provider-based isolation |

#### Failure Scenario Analysis

| Scenario | MedRAG Risk | RAG-Gym Risk | WTB Solution |
|----------|------------|--------------|--------------|
| A: Non-idempotent Writes | Medium (no idempotency keys) | Low (inference only) | Outbox Pattern |
| B: Partial Commit | High (non-atomic file saves) | Low (in-memory) | Unit of Work |
| C: Async Side Effects | N/A (synchronous) | N/A (synchronous) | Outbox Processor |
| D: Stale Reads | Medium (no cache invalidation) | Low (context invalidation) | Session Isolation |
| E: Heterogeneous Dependencies | High (requires PyTorch + Java) | High (PyTorch + Java) | Environment Providers |
| F: Version Drift | Medium (pinned versions) | Medium (pinned versions) | VenvCacheManager |

### Conversion Plan: RAG → LangGraph

#### Phase 1: State Schema (Completed in README)

```python
class RAGState(TypedDict):
    question: str
    history: Annotated[list[dict], add_messages]
    answer: str | None
    terminated: bool
    truncated: bool
    iteration: int
    qa_cache: dict[str, str]
```

#### Phase 2: Node Definitions

| Node | RAG-Gym Equivalent | Purpose |
|------|--------------------|---------|
| `agent_node` | `agent.generate_action()` | Generate query/answer |
| `retrieve_node` | `env.step()` retrieval path | Execute retrieval |
| `score_node` | `agent.score()` | PRM action selection |
| `terminal_check` | `is_terminal()` | Route to END |

#### Phase 3: Graph Construction

- Entry: `__start__` → `agent_node`
- Conditional: `agent_node` → `retrieve_node` OR `END`
- Loop: `retrieve_node` → `agent_node`

#### Phase 4: Transaction Safety Integration

- Wrap with `IUnitOfWork` for ACID compliance
- Use `IStateAdapter` for checkpoint persistence
- Add `Outbox` events for cross-system consistency

### TODO

- [ ] Create `examples/bio_rag_benchmarks/langgraph_conversion/` implementation
- [ ] Add unit tests for converted workflow
- [ ] Add integration tests with WTB transaction patterns
- [ ] Benchmark performance comparison

### Documentation Updates

| File | Change |
|------|--------|
| `medrag_benchmark/README.md` | Added workflow analysis + failure scenarios |
| `RAG-Gym/README.md` | Added workflow analysis + WTB comparison |

---

## Sequential Execution Fixes (v1.9 - 2026-03-26)

### Summary

Fixed 10 identified flaws across sequential execution (controller, adapter, outbox), Ray/ThreadPool batch paths, and factory wiring. Adopted colleague review feedback for full SOLID/ACID compliance.

### Flaws Fixed

| Flaw | Severity | Description | Fix |
|------|----------|-------------|-----|
| 1+2 | CRITICAL | Run routing drops graph on resume | Capability-based routing: `graph provided?` -> `adapter.has_graph()?` -> `node_executor` |
| 1 (resume) | CRITICAL | `_run_with_langgraph` re-executes from scratch on resume | Distinct PAUSED vs PENDING branching; `adapter.execute(None)` for resume |
| 3 | MEDIUM | Double `initialize_session()` + unnecessary `hasattr` | Removed redundant call; removed `hasattr` guards for `IStateAdapter` methods |
| 4 | MEDIUM | Rollback bypasses domain model | Uses `Execution.restore_from_checkpoint()` with dict->ExecutionState normalization |
| 4a | MEDIUM | Setup error handling gap | `set_workflow_graph()` moved to `run()` before dispatching |
| 5+6 | MEDIUM | Factory always passes `batch_runner=None` | Wired `ThreadPoolBatchTestRunner` + outbox decorator into all factory methods |
| 7 | HIGH | No outbox events from SDK path | `OutboxExecutionControllerDecorator` (OCP-compliant, no SRP violation) |
| 8 | LOW | Missing `on_variant_execution_started` | Added event emission at variant submission in Ray runner |
| 9 | HIGH | ThreadPool never passes graph | Added graph factory import (mirrors Ray pattern) + rollback metadata |
| 10 | HIGH | Ray coordinator checkpoint DB mismatch | Store actor checkpoint DB path in `execution.metadata["checkpoint_db_path"]` |

### Files Modified

- `wtb/application/services/execution_controller.py` -- Routing, resume, rollback, fork fixes
- `wtb/infrastructure/adapters/langgraph_state_adapter.py` -- `execute()` accepts `Optional[Dict]` for resume
- `wtb/domain/models/outbox.py` -- Added `EXECUTION_STARTED`, `EXECUTION_COMPLETED`, `EXECUTION_FAILED` event types
- `wtb/application/services/outbox_controller_decorator.py` -- **New**: Decorator for outbox events
- `wtb/application/factories.py` -- Wired decorator + batch runner into all factory methods
- `wtb/application/services/batch_test_runner.py` -- Graph factory import + rollback metadata
- `wtb/application/services/ray_batch_runner.py` -- Checkpoint DB in metadata + started event

### Tests Added

- `tests/unit/test_execution_controller.py` -- 12 unit tests (routing, resume, rollback, negative cases)
- `tests/unit/test_outbox_decorator.py` -- 11 unit tests (all lifecycle events, error resilience)
- `tests/integration/test_sequential_execution.py` -- 9 integration tests (full LangGraph flow, rollback, fork, outbox, negative cases)

### SOLID/ACID Compliance

- **LSP**: Pure capability-based routing, no `isinstance` checks
- **SRP**: Outbox via decorator, not mixed into controller
- **OCP**: Decorator adds behavior without modifying controller
- **ACID**: Resume uses existing checkpoint (not re-execution); rollback uses domain model

---

## Resource Management & Integration Test Fixes (v1.9.1 - 2026-03-26)

### Summary

System-level fixes for resource lifecycle management (SQLite connection leaks on Windows), missing abstract method implementations across CAS file tracking services, and Docker service configuration. Added comprehensive real integration tests for UV/venv and CAS.

### System Bugs Fixed

| Bug | Severity | Root Cause | Fix |
|-----|----------|------------|-----|
| `PermissionError` on Windows temp cleanup | HIGH | `SqliteFileTrackingService` never closed SQLite connections | Added `close()` + context manager (`__enter__`/`__exit__`) |
| `PermissionError` on Windows temp cleanup | HIGH | `LangGraphStateAdapter` never closed `SqliteSaver` connections | Added `close()` using checkpointer's `__exit__` or `.conn.close()` |
| `PermissionError` on Windows temp cleanup | HIGH | `BatchExecutionCoordinator` never delegated close to adapter | Added `close()` delegating to `_state_adapter.close()` |
| `TypeError: Can't instantiate MockFileTrackingService` | MEDIUM | `get_files_at_checkpoint` not implemented | Implemented abstract method |
| `TypeError: Can't instantiate FileTrackerService` | MEDIUM | `get_files_at_checkpoint` not implemented (PostgreSQL CAS) | Implemented abstract method |
| `RayFileTrackerService.get_files_at_checkpoint` missing | MEDIUM | Not delegated to inner service | Implemented delegation method |
| Docker `uv_venv_manager` crash | HIGH | Wrong volume mount (`./src` empty) + wrong module path (`src.api`) | Fixed to `./uv_venv_manager` mount + `uv_venv_manager.api:app` |

### Resource Management Pattern (close())

```
IStateAdapter.close() [default no-op]
    │
    ├── LangGraphStateAdapter.close()
    │   └── Strategy: __exit__ (MemorySaver) → .conn.close() (SqliteSaver)
    │
    ├── InMemoryStateAdapter.close() [inherited no-op]
    │
    └── BatchExecutionCoordinator.close()
        └── Delegates to self._state_adapter.close()
```

**SOLID Compliance of close() pattern:**
- **SRP**: Each class manages its own resource lifecycle only
- **OCP**: Default no-op on interface; subclasses override as needed
- **LSP**: All adapters substitutable; close() always callable
- **ISP**: Single method, no forced implementation
- **DIP**: Coordinator calls close() via IStateAdapter interface, not concrete class

### Architectural Review & Refinements (2026-03-26)

| Issue Found | Category | Fix |
|-------------|----------|-----|
| `LangGraphStateAdapter.close()` used `hasattr` probing on checkpointer | ISP/DIP violation | Replaced with ordered strategy: `__exit__` protocol first, then `.conn` (public API) |
| `BatchExecutionCoordinator.close()` used `hasattr(adapter, 'close')` | Redundant after interface change | Removed; `close()` is on `IStateAdapter` interface |

### Integration Tests Added

| Test File | Tests | Scope |
|-----------|-------|-------|
| `tests/integration/test_sequential_langgraph.py` | 10 | Sequential LangGraph: run, rollback, resume, fork, batch |
| `tests/integration/test_sequential_node_executor.py` | 9 | Sequential node executor: run, rollback, fork, batch |
| `tests/integration/test_ray_batch.py` | 10 | Ray batch: parallel execution, rollback, fork |
| `tests/integration/test_acid_outbox.py` | 10 | ACID/outbox: transaction atomicity, event ordering |
| `tests/integration/test_uv_venv_integration.py` | 34 | Real UV/venv: gRPC service, cache, workspace provisioning |
| `tests/integration/test_cas_file_tracking.py` | 24 | CAS: all 3 backends (Mock, SQLite, PostgreSQL delegation) |

### Requirements

- Created `requirements.txt` with all pinned versions (`==`) from `.venv` (uv-managed)

---

## Architecture Fix Sprint (Phase 1–7)

**Status: COMPLETED**

### Phase 1: Critical Runtime Errors (8 fixes)

- C3: `OutboxProcessor._get_session()` replaced with UoW pattern
- C4: Added audit-only handlers for `RAY_EVENT`, `FILE_CLEANUP_COMPLETED`, etc.
- C5: `AsyncLangGraphStateAdapter` uses `connection_string` instead of `db_path`
- C6: `InMemoryUnitOfWork` now includes `blobs` and `file_commits` repositories
- C7: Fixed `checkpoint_files` → `checkpoint_file_links` property name
- C10: `run_batch_test` processes all test cases, not just last
- C12: `load_checkpoint` extracts `workflow_variables` correctly
- C13: `_create_ray_actor` raises `NotImplementedError` instead of returning `None`

### Phase 2: ACID / Transaction Integrity (5 fixes)

- C2: Outbox events in same transaction via deferred commit
- H8: `BatchExecutionCoordinator` uses deferred commit to prevent double-commit
- H10: Ray batch runner no longer mutates `execution.id` after persist
- H13 / H14 / H15: State transition guards on `Execution`, `BatchTest`, `NodeBoundary`

### Phase 3: Type Safety

- Systemic `int` → `str` migration for `checkpoint_id` across domain, events, interfaces, infrastructure

### Phase 4: Security

- C8: `eval()` replaced with safe AST-based evaluator
- C9: SDK builder preserves custom `batch_runner` through factory delegation

### Phase 5: Thread Safety

- H5: `threading.Lock` added to `LangGraphStateAdapter` session tracking
- H6: `ContextVar` for `AsyncExecutionController._current_execution`
- H9: `threading.Lock` for `RayBatchTestRunner._running_tests`

### Phase 6: Architecture Compliance

- C14: Domain audit type (`AuditEntry`) replaces infrastructure import
- H2 / H3: Factory lifecycle warnings for UoW leaks
- H11: API service in-memory dicts marked as cache with lock
- H17: `importlib` moved from domain model to application `graph_loader`
- H18: Null safety guard in `acreate_fork`
- H22: Conditional edge placeholder replaced with `NotImplementedError`

### Phase 7: Medium Priority

- M9: `NodeStatus` enum replaces raw strings
- M12: `Execution.reset_for_retry` preserves `node_boundaries`
- M19: `LangGraphStateAdapter.close()` for checkpoint store cleanup
- M20: SQLAlchemy engine caching via LRU

---

### Examples Refactor (2026-04-01)

All four demo directories refactored for SDK-only usage and cross-platform support:

- `examples/quick_start`: Domain model imports migrated to `wtb.sdk`, bare excepts fixed, ANSI color safety, cross-platform docs
- `examples/ray_batch_demo`: Full SDK-only rewrite -- removed `RayBatchTestRunner`, `SQLAlchemyUnitOfWork`, ORM queries; uses `WTBTestBench.run_batch_test()`, `rollback_batch_result()`, `fork_batch_result()`, `get_batch_coordinator()`
- `examples/transaction_consistency`: Bug fixes (verify_order_node, checkpoint comments), unused imports removed, tautological asserts fixed, encoding fix
- `examples/wtb_presentation`: Stale script references fixed, private API (`_project_cache`) removed, unused imports, cross-platform docs

### Ray Mode Validation & Fixes (2026-04-01)

Three critical bugs fixed in Ray batch execution path to make all demos fully functional and paper-publish ready:

| Bug | Root Cause | Fix | File |
|-----|------------|-----|------|
| Execution ID mismatch | `_run_workflow_execution` returned pre-generated ID instead of DB-assigned ID | Propagate `execution.id` from `controller.create_execution()` through result dict | `ray_batch_runner.py` |
| "Graph not set" on rollback/fork | `rollback_batch_result()` / `fork_batch_result()` did not pass graph to coordinator | Added `_resolve_graph_for_result()` helper, auto-resolves from project cache | `sdk/test_bench.py` |
| Checkpoints invisible to main process | Ray actors stored checkpoints in per-actor SQLite DB, main process queried different DB | Unified to single shared `wtb_checkpoints.db` (LangGraph partitions by thread_id) | `ray_batch_runner.py` |

**Validation Results:**
- `examples/ray_batch_demo/run_demo.py`: 9/9 variants, rollback OK, fork OK, 9/9 batch ops, verification OK
- `examples/ray_batch_demo/test_demo.py`: 10/10 tests pass (including 3 Ray-specific tests)
- `tests/unit/`: 49/49 pass
- `tests/integration/`: 114/114 pass
- `examples/transaction_consistency/tests/`: 55/55 pass

### Full System Review and Refactor (2026-04-02)

Comprehensive system-wide review covering non-batch modes, thread safety, ACID compliance, and all examples. 22 issues fixed across 5 severity categories:

**CRITICAL (Data Integrity):**
- LangGraph session not set in `_run_with_langgraph` -- checkpoints could go to wrong execution
- `fork()` did not use `LangGraphStateAdapter.create_fork()` -- forked executions had no checkpoint history
- `rollback_and_run()` missing `set_deferred_commit(True)` -- multiple intermediate commits
- gRPC `invalidate_environment` double-pop bug -- remote environments leaked
- Venv spec hash mismatch across three incompatible computations -- cache misses

**HIGH (Thread Safety / Consistency):**
- `_current_workflow` not thread-safe in ThreadPoolBatchTestRunner -- concurrent races
- CAS blob writes not atomic (shutil.copy2 -> temp+os.replace) -- truncated blobs on crash
- `track_and_link` two-phase non-atomicity -- orphan commits possible
- gRPC channel leak on Ray shutdown + no threading lock on `_environments`

**MEDIUM (Cleanup / Correctness):**
- LangGraph adapter: duplicate `supports_graph_execution`, dead `_session_lock`, stale `_node_boundaries`
- Checkpoint ID type mismatch (INTEGER -> TEXT in SQLite schema)
- Venv rollback compatibility check wired into coordinator
- ManagedController redundant commit fixed via deferred commit pattern

**Factory / SDK:**
- Duplicate outbox_repo/wrapped_ctrl/batch_runner blocks removed from factories
- `_run_batch_sequential` refactored: graph built per variant, all BatchTestResult fields populated

**Examples:**
- Scenario B `verify_order_node` data access bug fixed
- SimulatedDataStore enhanced with transactional atomicity
- `install_checker.py` SDK E2E test added, unused asyncio removed
- `wtb_presentation` unused imports removed, README updated, project config consolidated
- Ray batch demo fixture teardown with `ray.shutdown()`

**Tests:** 49 new tests (15 unit + 17 integration + 17 example-level), all passing.

### Demo & System Improvements (2026-04-03)

Runtime bugs discovered and fixed while running all 5 demo modes end-to-end:

**Bug Fixes:**
- `unregister_workflow` cascade failure: Deleting a workflow left orphan executions with NOT NULL FK violation. Fix: delete associated executions before removing the workflow (`project_service.py`).
- Batch runner UNIQUE constraint: Isolated per-thread UoW sessions blindly INSERT'd the workflow into the shared SQLite DB. Fix: check `exists()` before `add()` (`batch_test_runner.py`).
- Fork "closed database" crash: `LangGraphStateAdapter.create_fork()` created a temporary adapter sharing the parent's checkpointer; when GC'd, its `__del__` closed the shared SQLite connection. Fix: added `_owns_checkpointer` flag; only the owning adapter closes the connection (`langgraph_state_adapter.py`).
- Batch runner SQL noise: Isolated sessions inherited `echo=True` from development config. Fix: force `echo=False` in `create_isolated()` (`factories.py`).

**Demo Results (all passing):**
- Demo 1 (Basic Execution): COMPLETED
- Demo 2 (Rollback): SUCCESS
- Demo 3 (Forking): 2/2 forks created
- Demo 4 (Batch Testing): 4/4 variants SUCCESS
- Demo 5 (Pause/Resume): COMPLETED

**Test Results:** 205 tests passing (81 unit/example + 124 integration, excluding Ray infrastructure tests).

---

### Resource Lifecycle & System Hardening (2026-04-03, Session 2)

**Problem:** Frequent OOM / memory bloat in long demo runs. Batch mode rollback/fork operations failing with "Execution not found" or "Checkpoint not found".

**Root Causes Identified:**
1. `WTBTestBench` had no `close()` / context manager - UoW sessions, checkpointer connections, and thread pools leaked for the lifetime of the process.
2. `unregister_workflow` only deleted executions, missing `node_variants`, `batch_tests`, and `evaluation_results` FK children.
3. `create_fork()` called `LangGraphStateAdapter.__init__` then immediately discarded the freshly-created checkpointer connection.
4. Batch runner's `exists()+add()+commit()` pattern had a TOCTOU race and broke ACID Atomicity by committing inside a deferred-commit boundary.
5. `WTBConfig.for_development()` set `state_adapter_mode="agentgit"` but `_create_state_adapter()` only recognized `"langgraph"`, so batch workers got `InMemoryStateAdapter` — no checkpoints were persisted.
6. LangGraph execution path never captured `execution.checkpoint_id`, making batch results' `last_checkpoint_id` always `None`.
7. Bench's ORM session was stale after batch execution (couldn't see rows committed by worker sessions).
8. `ThreadPoolBatchTestRunner` lacked `create_rollback_coordinator()`, so coordinator used wrong DB.

**Fixes Applied (8 changes across 8 files):**

| # | File | Fix | SOLID/ACID |
|---|------|-----|------------|
| 1 | `wtb/sdk/test_bench.py` | Added `close()`, `__enter__`/`__exit__`, `__del__` for full resource teardown + `_expire_session()` after batch | SRP, RAII |
| 2 | `wtb/application/services/project_service.py` | Full FK cascade: eval_results -> executions -> variants -> batch_tests -> workflow | ACID Consistency |
| 3 | `wtb/infrastructure/adapters/langgraph_state_adapter.py` | `create_fork()` uses `object.__new__()` to avoid leaked checkpointer connection | SRP |
| 4 | `wtb/application/services/batch_test_runner.py` | `try/except` replaces `exists()+add()+commit()` + added `create_rollback_coordinator()` with config | ACID Isolation/Atomicity |
| 5 | `wtb/config.py` | `WTBConfig.for_development()` changed `state_adapter_mode` from `"agentgit"` to `"langgraph"` | Consistency |
| 6 | `wtb/application/services/execution_controller.py` | `_run_with_langgraph` now captures `checkpoint_id` from last LangGraph snapshot | Durability |
| 7 | `wtb/application/factories.py` | `BatchTestRunnerFactory.create_threadpool()` passes config to runner | DIP |
| 8 | `examples/wtb_presentation/scripts/run_demo.py` | All 5 demos call `bench.close()` | RAII |

**Demo Results (all passing):**
- Presentation Demos 1-5: All PASS with proper cleanup
- Ray Batch Demo (thread-pool mode): 9/9 batch, rollback verified, fork verified, 9/9 batch ops OK

**Test Results:** 205 tests passing (81 unit/example + 124 integration).

---

## Related Documents

| Document | Description |
|----------|-------------|
| [ARCHITECTURE_REVIEW_2026_01_28.md](./ARCHITECTURE_REVIEW_2026_01_28.md) | Latest architecture review |
| [ARCHITECTURE_ISSUES.md](./ARCHITECTURE_ISSUES.md) | Consolidated issues analysis |
| [ARCHITECTURE_STRUCTURE.md](./ARCHITECTURE_STRUCTURE.md) | Code structure |
| [INDEX.md](./INDEX.md) | Documentation hub |
