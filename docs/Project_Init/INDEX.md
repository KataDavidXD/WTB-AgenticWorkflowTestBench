# WTB (Workflow Test Bench) - Architecture Documentation

**Last Updated:** 2026-01-28  
**Version:** 1.0.1  
**Status:** Implemented

---

## Overview

WTB is a workflow testing framework that enables:
- Workflow execution with checkpointing (LangGraph-native)
- Node variant A/B testing with parallel execution (Ray)
- Rollback and time-travel capabilities
- File tracking with content-addressable storage
- Cross-database transaction consistency (Outbox Pattern)

---

## Documentation Index

### Core Documentation

| Document | Description | Status |
|----------|-------------|--------|
| **[RAG_GYM_WTB_COMPARISON.md](./RAG_GYM_WTB_COMPARISON.md)** | **RAG-Gym three-way comparison experiment** | **NEW** |
| **[ARCHITECTURE_REVIEW_2026_01_28.md](./ARCHITECTURE_REVIEW_2026_01_28.md)** | **Latest architecture review (SOLID/ACID audit)** | **NEW** |
| [ARCHITECTURE_ISSUES.md](./ARCHITECTURE_ISSUES.md) | Consolidated issues analysis and roadmap | Current |
| [ARCHITECTURE_STRUCTURE.md](./ARCHITECTURE_STRUCTURE.md) | Codebase structure and layer organization | Current |
| [ASYNC_ARCHITECTURE_PLAN.md](./ASYNC_ARCHITECTURE_PLAN.md) | Full async architecture plan (v2.0) | Proposed |
| [ASYNC_FILE_TRACKING_SUMMARY.md](./ASYNC_FILE_TRACKING_SUMMARY.md) | Async implementation status & scenarios | Implemented |
| [PROGRESS_TRACKER.md](./PROGRESS_TRACKER.md) | Implementation progress and TODO list | Current |

### Section Documentation

| Section | Description | Status |
|---------|-------------|--------|
| [Adapter_and_WTB-Storage](../Adapter_and_WTB-Storage/INDEX.md) | State adapters, persistence, Unit of Work | Current |
| [EventBus_and_Audit_Session](../EventBus_and_Audit_Session/INDEX.md) | Event pub/sub, audit trail | Current |
| [LangGraph](../LangGraph/INDEX.md) | LangGraph checkpoint integration | Current |
| [Ray](../Ray/INDEX.md) | Parallel batch execution | Current |
| [file_processing](../file_processing/INDEX.md) | Content-addressable file storage | Current |
| [external_api](../external_api/INDEX.md) | REST, WebSocket, gRPC APIs | Current |
| [thread_and_process](../thread_and_process/INDEX.md) | Parallelism and thread safety | Current |
| [venv_management](../venv_management/INDEX.md) | UV virtual environment management | Current |
| [history](../history/INDEX.md) | Documentation history | Current |

---

## Quick Reference

### Key Directories

```
wtb/
├── api/           # REST, WebSocket, gRPC interfaces
├── application/   # Application services (ExecutionController, BatchRunner)
├── domain/        # Domain models, events, interfaces (DDD)
├── infrastructure/# Adapters, database, events, file tracking
└── sdk/           # User-facing SDK (TestBench, WorkflowProject)
```

### Architecture Principles

| Principle | Implementation |
|-----------|----------------|
| **SOLID** | Interface segregation, dependency inversion via abstractions |
| **ACID** | Unit of Work pattern, Outbox Pattern for cross-DB consistency |
| **DDD** | Bounded contexts, aggregates, domain events |
| **Clean Architecture** | Layered structure with dependency direction inward |

### Primary Interfaces

| Interface | Purpose | Primary Implementation |
|-----------|---------|----------------------|
| `ICheckpointStore` | Checkpoint persistence | `LangGraphCheckpointStore` |
| `IExecutionController` | Workflow lifecycle | `ExecutionController` |
| `IBatchTestRunner` | Parallel test execution | `RayBatchRunner` |
| `IFileTrackingService` | File state management | `FileTrackerService` |
| `IUnitOfWork` | Transaction boundaries | `SQLAlchemyUnitOfWork` |

---

## Related Documents

| Location | Content |
|----------|---------|
| [../LangGraph/](../LangGraph/) | LangGraph integration details |
| [../Ray/](../Ray/) | Ray parallel execution docs |

---

## Change Log

| Date | Change | Author |
|------|--------|--------|
| 2026-02-02 | **RAG_GYM_WTB_COMPARISON.md** - Three-way comparison experiment (Original vs LangGraph vs WTB) | Senior Architect |
| 2026-02-02 | **examples/bio_rag_benchmarks/rag_gym_wtb/** - Implemented comparison project with scenarios A-F | Senior Architect |
| 2026-01-28 | **ARCHITECTURE_REVIEW_2026_01_28.md** - Comprehensive SOLID/ACID audit, 14 issues identified | Senior Architect |
| 2026-01-28 | **Fixed** CP-002: NodeBoundary repository returns str IDs (not CheckpointId) | Senior Architect |
| 2026-01-28 | **Created** OutboxMapper for DRY compliance | Senior Architect |
| 2026-01-28 | **Created** test_node_boundary_consistency.py (16 tests) | Senior Architect |
| 2026-01-28 | **Created** migration 005_node_boundary_cleanup.sql | Senior Architect |
| 2026-01-28 | **ASYNC_FILE_TRACKING_SUMMARY.md** - Async file tracking implementation & test scenarios | System |
| 2026-01-27 | **ASYNC_ARCHITECTURE_PLAN.md v1.1** - Code review fixes (10 issues, 4 suggestions) | System |
| 2026-01-27 | **Added ASYNC_ARCHITECTURE_PLAN.md (v2.0 roadmap)** | System |
| 2026-01-27 | Verified v1.7 ACID compliance (Ray/ThreadPool) | System |
| 2026-01-27 | Added ARCHITECTURE_ISSUES.md (consolidated analysis) | System |
| 2026-01-27 | Updated PROGRESS_TRACKER.md with TODO list | System |
| 2026-01-27 | Initial documentation structure | System |
| 2026-01-16 | Workspace isolation implemented | - |
| 2026-01-15 | LangGraph checkpoint store added | - |
