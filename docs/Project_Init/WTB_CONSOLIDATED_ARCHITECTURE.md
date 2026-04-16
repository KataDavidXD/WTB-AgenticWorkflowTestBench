# WTB Consolidated Architecture Document

**Version:** 2.1
**Last Updated:** 2026-01-28
**Status:** Active
**Consolidates:** ARCHITECTURE_STRUCTURE.md, ASYNC_ARCHITECTURE_PLAN.md, ASYNC_FILE_TRACKING_SUMMARY.md

---

## Executive Summary

This document consolidates the WTB (Workflow Test Bench) architecture into a single reference, covering:

- Layer architecture and directory structure
- Sync and async implementations
- SOLID and ACID compliance
- File tracking and transaction patterns

---

## 1. Layer Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              WTB LAYER ARCHITECTURE                              │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│   ┌───────────────────────────────────────────────────────────────────────────┐ │
│   │                           API LAYER (wtb/api/)                             │ │
│   │  REST routes, WebSocket handlers, gRPC protos                              │ │
│   │  Entry points for external consumers                                        │ │
│   └───────────────────────────────────────────────────────────────────────────┘ │
│                                      │                                           │
│                                      ▼                                           │
│   ┌───────────────────────────────────────────────────────────────────────────┐ │
│   │                      APPLICATION LAYER (wtb/application/)                  │ │
│   │  ExecutionController, BatchTestRunner, NodeReplacer                        │ │
│   │  Orchestration, use cases, transaction boundaries                          │ │
│   └───────────────────────────────────────────────────────────────────────────┘ │
│                                      │                                           │
│                                      ▼                                           │
│   ┌───────────────────────────────────────────────────────────────────────────┐ │
│   │                        DOMAIN LAYER (wtb/domain/)                          │ │
│   │  Models (Execution, Checkpoint, Workflow), Events, Interfaces              │ │
│   │  Business logic, domain rules, abstractions                                │ │
│   └───────────────────────────────────────────────────────────────────────────┘ │
│                                      │                                           │
│                                      ▼                                           │
│   ┌───────────────────────────────────────────────────────────────────────────┐ │
│   │                   INFRASTRUCTURE LAYER (wtb/infrastructure/)               │ │
│   │  Adapters, Repositories, Event Bus, File Tracking, Outbox                  │ │
│   │  Concrete implementations, external system integration                     │ │
│   └───────────────────────────────────────────────────────────────────────────┘ │
│                                      │                                           │
│                                      ▼                                           │
│   ┌───────────────────────────────────────────────────────────────────────────┐ │
│   │                        EXTERNAL SYSTEMS                                    │ │
│   │  SQLite/PostgreSQL, LangGraph, Ray, FileTracker                           │ │
│   └───────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Directory Structure

### 2.1 Complete Structure

```
wtb/
├── api/                              # API Layer
│   ├── grpc/                         # gRPC definitions
│   │   ├── servicer.py               # WTBServicer implementation
│   │   └── protos/wtb_service.proto
│   ├── rest/                         # REST API (FastAPI)
│   │   ├── app.py                    # FastAPI application
│   │   ├── dependencies.py           # DI (API services)
│   │   ├── models.py                 # Pydantic models
│   │   └── routes/                   # Route handlers
│   └── websocket/handlers.py         # Real-time streaming
│
├── application/                      # Application Layer
│   ├── factories.py                  # Service factories
│   ├── validators.py                 # Input validation (NEW 2026-01-28)
│   └── services/
│       ├── api_services.py           # API service implementations
│       ├── async_execution_controller.py  # Async orchestration
│       ├── execution_controller.py   # Sync orchestration
│       ├── batch_test_runner.py      # Sequential batch
│       └── ray_batch_runner.py       # Parallel batch
│
├── domain/                           # Domain Layer
│   ├── events/                       # Domain events (70+ types)
│   ├── interfaces/                   # Abstractions (Ports)
│   │   ├── api_services.py           # API service interfaces
│   │   ├── async_repositories.py     # Async repository interfaces
│   │   ├── repositories.py           # Sync repository interfaces
│   │   └── unit_of_work.py           # UoW interface
│   └── models/                       # Domain entities
│       ├── checkpoint.py
│       ├── execution.py
│       ├── file_processing.py
│       ├── outbox.py
│       └── workflow.py
│
├── infrastructure/                   # Infrastructure Layer
│   ├── adapters/                     # State adapters
│   │   ├── async_langgraph_state_adapter.py
│   │   ├── langgraph_state_adapter.py
│   │   └── inmemory_state_adapter.py
│   ├── database/
│   │   ├── mappers/                  # Shared ORM↔Domain mappers
│   │   │   ├── blob_storage_core.py  # Blob logic (DRY)
│   │   │   └── outbox_mapper.py      # Outbox conversion (DRY)
│   │   ├── repositories/             # Sync repositories
│   │   ├── async_repositories/       # Async repositories
│   │   └── unit_of_work.py           # SQLAlchemy UoW
│   ├── file_tracking/                # File state management
│   ├── outbox/                       # Outbox pattern processor
│   └── events/wtb_event_bus.py       # Event pub/sub
│
└── sdk/                              # SDK Layer
    ├── test_bench.py                 # High-level API
    └── workflow_project.py           # Configuration
```

---

## 3. Sync/Async Architecture

### 3.1 Current Status

| Layer                | Sync | Async | Notes                     |
| -------------------- | ---- | ----- | ------------------------- |
| API (FastAPI)        | -    | ✅    | Native async              |
| Application Services | ✅   | ✅    | Both available            |
| Domain Interfaces    | ✅   | ✅    | Separate interfaces (ISP) |
| Infrastructure/DB    | ✅   | ✅    | SQLAlchemy 2.0            |
| File Tracking        | ✅   | ✅    | aiofiles for async        |
| LangGraph Adapter    | ✅   | ✅    | ainvoke/astream           |

### 3.2 Interface Strategy (ISP Compliant)

```python
# GOOD: Separate interfaces for sync and async
class IStateAdapter(ABC):
    """Sync state adapter interface."""
    @abstractmethod
    def execute(self, state: Dict) -> Dict:
        pass

class IAsyncStateAdapter(ABC):
    """Async state adapter interface."""
    @abstractmethod
    async def execute(self, state: Dict) -> Dict:
        pass
```

### 3.3 Async Execution Flow

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                     ASYNC EXECUTION FLOW                                         │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│   API Route              ExecutionService       ExecutionController (ASYNC)     │
│        │                       │                        │                        │
│        │  await service.run()  │                        │                        │
│        │──────────────────────>│                        │                        │
│        │                       │  await controller      │                        │
│        │                       │    .arun()             │                        │
│        │                       │  ──────────────────────>│                        │
│        │                       │                        │                        │
│        │ ◀─── Event Loop Free  │      ┌─────────────────┤                        │
│        │      to handle other  │      │ await state_    │                        │
│        │      requests! ◀────  │      │   adapter       │                        │
│        │                       │      │   .aexecute()   │                        │
│        │                       │      │                 │                        │
│        │                       │      │ (yields control)│                        │
│        │                       │      └─────────────────┤                        │
│        │                       │                        │                        │
│        │                       │◀──────────────────────│                        │
│        │◀──────────────────────│                        │                        │
│                                                                                  │
│   BENEFIT: Event loop handles multiple concurrent workflows!                    │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. File Tracking Architecture

### 4.1 Blob Storage (Content-Addressable)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    BLOB STORAGE ARCHITECTURE                                     │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│   Application Layer                                                              │
│   ┌───────────────────────────────────────────────────────────────────────┐     │
│   │  with UnitOfWork() as uow:                                            │     │
│   │      blob_id = uow.blobs.save(content)  # Step 1                      │     │
│   │      commit.add_memento(path, blob_id)  # Step 2                      │     │
│   │      uow.file_commits.save(commit)      # Step 3                      │     │
│   │      uow.commit()                       # Step 4                      │     │
│   └───────────────────────────────────────────────────────────────────────┘     │
│                          │                                                       │
│                          ▼                                                       │
│   Storage Layer                                                                  │
│   ┌───────────────────────────────────────────────────────────────────────┐     │
│   │  Content-Addressable Storage (Git-like)                               │     │
│   │                                                                       │     │
│   │  data/blobs/objects/                                                  │     │
│   │  ├── ab/                                                              │     │
│   │  │   └── cdef1234567890...  (SHA-256 hash)                           │     │
│   │  ├── cd/                                                              │     │
│   │  │   └── ef5678901234...                                              │     │
│   │  └── ...                                                              │     │
│   └───────────────────────────────────────────────────────────────────────┘     │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 4.2 Two-Phase Write Pattern

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    TWO-PHASE WRITE PATTERN                                       │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│   Phase 1: File Write                                                           │
│   ────────────────────────────                                                  │
│   blob_id = uow.blobs.save(content)                                             │
│   │                                                                              │
│   │  Internally:                                                                │
│   │  1. Write to temp file                                                      │
│   │  2. Atomic rename to final path                                            │
│   │  3. session.add(FileBlobORM(...))  # Not yet committed                     │
│   ▼                                                                              │
│                                                                                  │
│   Phase 2: Commit                                                               │
│   ────────────────────────────                                                  │
│   uow.commit()                                                                  │
│   │                                                                              │
│   │  If commit FAILS:                                                           │
│   │  → Orphaned blob on filesystem                                             │
│   │  → AsyncBlobOrphanCleaner handles cleanup                                  │
│   ▼                                                                              │
│                                                                                  │
│   Result: Either BOTH (file + DB record) or NEITHER                            │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. SOLID Principles Implementation

### 5.1 Single Responsibility (SRP)

| Component                 | Responsibility                   | Status |
| ------------------------- | -------------------------------- | ------ |
| `ExecutionController`   | Workflow lifecycle orchestration | ✅     |
| `LangGraphStateAdapter` | LangGraph ↔ WTB translation     | ✅     |
| `SQLAlchemyUnitOfWork`  | Transaction boundaries           | ✅     |
| `BlobStorageCore`       | Blob path/hash computation       | ✅     |
| `OutboxMapper`          | Outbox ORM↔Domain conversion    | ✅     |

### 5.2 Open/Closed (OCP)

Extension points via interfaces:

```
IStateAdapter                     ICheckpointStore
├── InMemoryStateAdapter          ├── InMemoryCheckpointStore
├── LangGraphStateAdapter  ◄──    ├── LangGraphCheckpointStore  ◄── PRIMARY
└── AsyncLangGraphStateAdapter    └── SqliteCheckpointStore

IBlobRepository                   IOutboxRepository
├── SQLAlchemyBlobRepository      ├── SQLAlchemyOutboxRepository
├── AsyncSQLAlchemyBlobRepository ├── AsyncOutboxRepository
└── InMemoryBlobRepository        └── InMemoryOutboxRepository
```

### 5.3 Interface Segregation (ISP)

```
IRepository<T>
├── IReadRepository<T>      # get, list, find
└── IWriteRepository<T>     # add, update, delete

IStateAdapter               # Sync operations
IAsyncStateAdapter          # Async operations (separate!)

API Service Interfaces
├── IExecutionAPIService    # Execution lifecycle
├── IAuditAPIService        # Audit queries
├── IBatchTestAPIService    # Batch test management
└── IWorkflowAPIService     # Workflow CRUD
```

### 5.4 Dependency Inversion (DIP)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     DEPENDENCY DIRECTION                                 │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   Application Layer                  Infrastructure Layer                │
│   ─────────────────                  ────────────────────                │
│                                                                          │
│   ExecutionController ─────────────► IStateAdapter (abstraction)        │
│           ▲                                  │                           │
│           │                                  ▼                           │
│           │                          LangGraphStateAdapter               │
│           │                          InMemoryStateAdapter                │
│                                                                          │
│   Both depend on abstractions, not concretions ✅                       │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 6. ACID Compliance

### 6.1 Transaction Boundaries (Unit of Work)

```python
class SQLAlchemyUnitOfWork(IUnitOfWork):
    """
    ACID Compliance:
    - Atomicity: All changes committed or rolled back together
    - Consistency: Validation before commit
    - Isolation: Session-level isolation
    - Durability: Persistent storage on commit
    """
  
    # Repositories managed:
    workflows: IWorkflowRepository
    executions: IExecutionRepository
    variants: INodeVariantRepository
    blobs: IBlobRepository
    file_commits: IFileCommitRepository
    checkpoint_file_links: ICheckpointFileLinkRepository
    outbox: IOutboxRepository
```

### 6.2 Outbox Pattern for Cross-DB Consistency

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        OUTBOX PATTERN FLOW                               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   1. Business Transaction                                                │
│   ────────────────────────                                               │
│   BEGIN TX                                                               │
│   ├── INSERT execution (wtb.db)                                          │
│   ├── INSERT outbox_event (wtb.db)   ◄── Same transaction               │
│   COMMIT TX                                                              │
│                                                                          │
│   2. Background Processor                                                │
│   ────────────────────────                                               │
│   OutboxProcessor.process_pending()                                      │
│   ├── Read PENDING events (FIFO order)                                   │
│   ├── Verify checkpoint exists (LangGraph)                               │
│   ├── Verify file commit exists (FileTracker)                            │
│   ├── Mark PROCESSED or FAILED                                           │
│   └── Retry on failure (max 5)                                           │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

### 6.3 ACID Properties Summary

| Property              | Implementation                            | Status |
| --------------------- | ----------------------------------------- | ------ |
| **Atomicity**   | UoW commit/rollback, two-phase blob write | ✅     |
| **Consistency** | SHA-256 hash validation, FK constraints   | ✅     |
| **Isolation**   | Per-session isolation, unique temp files  | ✅     |
| **Durability**  | SQLite WAL, atomic file rename, fsync     | ✅     |

---

## 7. DRY Compliance (2026-01-28 Update)

### 7.1 Shared Mappers

To eliminate ~500 lines of duplicated code between sync/async repositories:

| Mapper                       | Purpose                       | Used By                           |
| ---------------------------- | ----------------------------- | --------------------------------- |
| `BlobStorageCore`          | Blob path/hash computation    | Sync & Async BlobRepository       |
| `OutboxMapper`             | Outbox ORM↔Domain conversion | Sync & Async OutboxRepository     |
| `FileCommitMapper`         | FileCommit ORM↔Domain        | Sync & Async FileCommitRepository |
| `CheckpointFileLinkMapper` | Link ORM↔Domain              | Sync & Async LinkRepository       |

### 7.2 BlobStorageCore Usage

```python
# Both sync and async repositories use:
from wtb.infrastructure.database.mappers import BlobStorageCore

# Path computation
storage_path = BlobStorageCore.compute_storage_path(blob_id, objects_path)
dir_path = BlobStorageCore.compute_directory_path(blob_id, objects_path)
temp_path = BlobStorageCore.compute_temp_path(storage_path, unique_suffix)

# ORM creation
orm_dict = BlobStorageCore.create_orm_dict(blob_id, storage_location, size)

# Stats
stats = BlobStorageCore.compute_stats(count, total_size)
```

---

## 8. API Service Architecture

### 8.1 Interface-Based Design

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    API SERVICE ARCHITECTURE                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   REST Endpoints                 gRPC Servicer                              │
│        │                              │                                     │
│        └──────────────┬───────────────┘                                     │
│                       ▼                                                     │
│   ┌──────────────────────────────────────────────────────────────────────┐ │
│   │           API Service Interfaces (Abstractions)                       │ │
│   │  ┌─────────────────────┐  ┌─────────────────────┐                    │ │
│   │  │ IExecutionAPIService│  │ IAuditAPIService    │                    │ │
│   │  │ - list_executions() │  │ - query_events()    │                    │ │
│   │  │ - pause_execution() │  │ - get_summary()     │                    │ │
│   │  │ - resume_execution()│  └─────────────────────┘                    │ │
│   │  │ - rollback_exec()   │                                              │ │
│   │  └─────────────────────┘                                              │ │
│   └──────────────────────────────────────────────────────────────────────┘ │
│                       │                                                     │
│                       ▼                                                     │
│   ┌──────────────────────────────────────────────────────────────────────┐ │
│   │         Concrete Implementations                                      │ │
│   │  ExecutionAPIService, AuditAPIService, BatchTestAPIService           │ │
│   │  Dependencies: IUnitOfWork, IExecutionController, OutboxRepository   │ │
│   └──────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 8.2 Transaction Pattern

```python
# All API operations use this pattern:
async def pause_execution(self, execution_id: str) -> ControlResultDTO:
    try:
        with self._uow:  # Transaction boundary
            # Domain operation
            execution = self._controller.pause(execution_id)
          
            # Outbox event (same transaction)
            outbox_event = OutboxEvent.create(
                event_type=OutboxEventType.EXECUTION_PAUSED,
                aggregate_id=execution_id,
                payload={...},
            )
            self._uow.outbox.add(outbox_event)
            self._uow.commit()  # ATOMIC
          
            return ControlResultDTO(success=True, ...)
    except Exception as e:
        return ControlResultDTO(success=False, error=str(e))
```

---

## 9. Test Structure

```
tests/
├── test_architecture/              # Architecture compliance tests
│   ├── test_dry_compliance.py      # DRY principle tests
│   ├── test_acid_compliance.py     # ACID property tests
│   └── test_node_boundary_consistency.py
├── test_api/                       # API tests
│   ├── test_api_services_unit.py
│   └── test_api_transaction_consistency.py
├── test_file_processing/           # File tracking tests
│   ├── integration/
│   └── unit/
├── test_langgraph/                 # LangGraph integration
├── test_outbox_transaction_consistency/  # Outbox ACID tests
├── test_sdk/                       # SDK tests
└── test_wtb/                       # Core WTB tests
```

---

## 10. Key Decisions

| Decision                             | Date       | Rationale                              |
| ------------------------------------ | ---------- | -------------------------------------- |
| Adopt SQLAlchemy for file tracking   | 2026-01-27 | Unified DB management, ACID compliance |
| Separate sync/async interfaces (ISP) | 2026-01-27 | No forced dual implementation          |
| Create shared mappers (DRY)          | 2026-01-28 | Eliminate 500+ lines duplication       |
| Use outbox pattern for cross-DB      | 2026-01-27 | Eventual consistency guarantee         |
| Content-addressable blob storage     | 2026-01-27 | Automatic deduplication                |
| Centralized Input Validation         | 2026-01-28 | DRY validation logic (`validators.py`) |
| Client-Provided Idempotency Keys     | 2026-01-28 | Ensure safe retries for API calls      |
| Robust JSON Serialization            | 2026-01-28 | Handle Value Objects in API payloads   |
| Transaction Consistency Scenarios    | 2026-01-29 | SDK examples demonstrating ACID patterns |

---

## 11. Transaction Consistency Scenarios (NEW 2026-01-29)

### 11.1 Overview

The `examples/transaction_consistency/` package provides real-world scenarios demonstrating how WTB handles common consistency issues in agent systems.

### 11.2 Scenarios

| Scenario | Issue | WTB Solution |
|----------|-------|--------------|
| **A: Non-idempotent Writes** | Retry creates duplicates | Checkpoint-based idempotency, execution tracking |
| **B: Partial Commit** | Orphan data on failure | Unit of Work pattern, atomic transactions |
| **C: Async Side Effects** | Out-of-order writes | Ordered execution, node boundaries |
| **D: Stale Reads** | Reading outdated data | Snapshot isolation, state versioning |
| **E: Node Venv Management** | Dependency conflicts | Node-level venv isolation via UV manager |

### 11.3 Architecture Diagram

```
┌───────────────────────────────────────────────────────────────────────────┐
│                    TRANSACTION CONSISTENCY SCENARIOS                       │
├───────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│   ┌──────────────────────────────────────────────────────────────────┐   │
│   │                     ScenarioBase (Abstract)                       │   │
│   │  Template Method Pattern                                          │   │
│   │  - run() : Template method                                        │   │
│   │  - demonstrate_problem_without_wtb() : Abstract                   │   │
│   │  - demonstrate_solution_with_wtb() : Abstract                     │   │
│   └──────────────────────────────────────────────────────────────────┘   │
│                              ▲                                            │
│         ┌────────────────────┼────────────────────┐                      │
│         │                    │                    │                      │
│   ┌─────┴─────┐        ┌─────┴─────┐        ┌─────┴─────┐               │
│   │ ScenarioA │        │ ScenarioB │        │ ScenarioC │ ...           │
│   │ Idempotent│        │ Partial   │        │ Async     │               │
│   └───────────┘        └───────────┘        └───────────┘               │
│                                                                            │
│   ┌──────────────────────────────────────────────────────────────────┐   │
│   │                   SimulatedDataStore                              │   │
│   │  In-memory store for testing: records, chunks, indexes            │   │
│   └──────────────────────────────────────────────────────────────────┘   │
│                                                                            │
│   ┌──────────────────────────────────────────────────────────────────┐   │
│   │                     ScenarioResult                                │   │
│   │  - issues_without_wtb: List[ConsistencyIssue]                    │   │
│   │  - issues_with_wtb: List[ConsistencyIssue]                       │   │
│   │  - resolution_evidence: Dict[str, Any]                            │   │
│   └──────────────────────────────────────────────────────────────────┘   │
│                                                                            │
└───────────────────────────────────────────────────────────────────────────┘
```

### 11.4 SOLID Compliance

| Principle | Implementation |
|-----------|----------------|
| **SRP** | Each scenario handles one consistency issue |
| **OCP** | Scenarios extend ScenarioBase without modification |
| **LSP** | All scenarios are interchangeable via base class |
| **ISP** | Focused interfaces for each concern |
| **DIP** | Depend on WTBTestBench abstraction |

### 11.5 Usage

```bash
# Run all scenarios
python -m examples.transaction_consistency.run_scenarios

# Run specific scenario
python -m examples.transaction_consistency.run_scenarios --scenario=A

# Run tests
pytest examples/transaction_consistency/tests/ -v
```

---

## Related Documents in docs_archive\old_archive\1-28

| Document                                                                                         | Description             |
| ------------------------------------------------------------------------------------------------ | ----------------------- |
| [FILE_TRACKING_ARCHITECTURE_DECISION.md](../file_processing/FILE_TRACKING_ARCHITECTURE_DECISION.md) | File tracking ADR       |
| [ARCHITECTURE_REVIEW_2026_01_28.md](./ARCHITECTURE_REVIEW_2026_01_28.md)                            | Latest review           |
| [CONSOLIDATED_ISSUES.md](./CONSOLIDATED_ISSUES.md)                                                  | All issues in one place |
| [PROGRESS_TRACKER.md](./PROGRESS_TRACKER.md)                                                        | Implementation progress |

---

**Document Version:** 2.1
**Authors:** Architecture Team
**Last Updated:** 2026-01-28
