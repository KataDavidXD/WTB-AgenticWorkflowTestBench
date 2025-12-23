# WTB Documentation Index

## Quick Navigation

| Document | Purpose | Read When |
|----------|---------|-----------|
| [WORKFLOW_TEST_BENCH_SUMMARY.md](./WORKFLOW_TEST_BENCH_SUMMARY.md) | Executive summary, data flows, user flows | Start here for overview |
| [WORKFLOW_TEST_BENCH_ARCHITECTURE.md](./WORKFLOW_TEST_BENCH_ARCHITECTURE.md) | Complete architecture, detailed design decisions | Deep technical understanding |
| [DATABASE_DESIGN.md](./DATABASE_DESIGN.md) | Three-database schema, Repository + UoW patterns | Database work |
| [PROGRESS_TRACKER.md](./PROGRESS_TRACKER.md) | Implementation status | Check what's done/pending |

## Detailed Design Documents

| Document | Purpose |
|----------|---------|
| [../Adapter_and_WTB-Storage/AGENTGIT_STATE_ADAPTER_DESIGN.md](../Adapter_and_WTB-Storage/AGENTGIT_STATE_ADAPTER_DESIGN.md) | AgentGitStateAdapter implementation, checkpoint design |
| [../Adapter_and_WTB-Storage/WTB_PERSISTENCE_DESIGN.md](../Adapter_and_WTB-Storage/WTB_PERSISTENCE_DESIGN.md) | WTB storage (InMemory + SQLAlchemy UoW) |
| [../Adapter_and_WTB-Storage/ARCHITECTURE_FIX_DESIGN.md](../Adapter_and_WTB-Storage/ARCHITECTURE_FIX_DESIGN.md) | Outbox Pattern, IntegrityChecker, cross-DB consistency |
| [../EventBus_and_Audit_Session/WTB_EVENTBUS_AUDIT_DESIGN.md](../EventBus_and_Audit_Session/WTB_EVENTBUS_AUDIT_DESIGN.md) | Event Bus & Audit Trail integration with AgentGit |

## Section Quick Reference

### Architecture Deep Dive (`WORKFLOW_TEST_BENCH_ARCHITECTURE.md`)

This document is the technical source of truth (3000+ lines). Use this map to navigate the key components:

| Section | Topic | Why Read It |
|---------|-------|-------------|
| **§0** | **Validated Infrastructure** | Understand the foundation (AgentGit v2, FileTracker) verified by tests. |
| **§3** | **Database Architecture** | Learn about the **Repository + Unit of Work** pattern and Dual-Database strategy. |
| **§4** | **Domain Models** | Definitions of core entities: `TestWorkflow`, `Execution`, `NodeVariant`. |
| **§5** | **Execution Controller** | Design of the core orchestration engine (Run/Pause/Resume/Rollback). |
| **§7** | **State Adapter** | How WTB integrates with AgentGit without tight coupling (**Anti-Corruption Layer**). |
| **§9** | **Project Structure** | Where files should go (Directory Layout). |
| **§13** | **Critical Decisions** | **Must Read**. DB choice, Checkpoint Granularity, Event Bus, Storage patterns. |
| **§15** | **Validation Status** | Checklist of implemented vs. pending components. |
| **§16** | **Parallel Sessions** | Design for **Batch Testing** and concurrent execution isolation. |

### Other Key References

| Topic | Document | Section |
|-------|----------|---------|
| **Executive Summary** | [WORKFLOW_TEST_BENCH_SUMMARY.md](./WORKFLOW_TEST_BENCH_SUMMARY.md) | All |
| **User Flows** | [WORKFLOW_TEST_BENCH_SUMMARY.md](./WORKFLOW_TEST_BENCH_SUMMARY.md) | §4 |
| **Data Flow** | [WORKFLOW_TEST_BENCH_SUMMARY.md](./WORKFLOW_TEST_BENCH_SUMMARY.md) | §3 |
| **Parallel Summary** | [WORKFLOW_TEST_BENCH_SUMMARY.md](./WORKFLOW_TEST_BENCH_SUMMARY.md) | §5.5 |
| **Event Bus & Audit** | [../EventBus_and_Audit_Session/WTB_EVENTBUS_AUDIT_DESIGN.md](../EventBus_and_Audit_Session/WTB_EVENTBUS_AUDIT_DESIGN.md) | All |

## Key Design Principles

1. **Dual Database Pattern**: AgentGit repos for checkpoints; SQLAlchemy UoW for WTB domain data
2. **Single Checkpoint Type**: All checkpoints atomic; node boundaries are POINTERS not separate checkpoints
3. **Interface Abstraction**: IStateAdapter, IUnitOfWork enable swappable implementations
4. **Anti-Corruption Layer**: Adapters bridge WTB ↔ AgentGit ↔ FileTracker boundaries
5. **Parallel Session Isolation**: Each parallel execution gets isolated context (ThreadPoolExecutor + app-level isolation)
6. **Event Bus Reuse**: WTB wraps AgentGit EventBus; bridge AgentGit events to WTB; independent Audit Trails

## Architecture Overview

```
Application Layer:  ExecutionController, NodeReplacer, BatchTestRunner
       ↓
Domain Layer:       IStateAdapter, IUnitOfWork (interfaces)
       ↓
Infrastructure:     AgentGitStateAdapter, SQLAlchemyUnitOfWork, InMemoryUnitOfWork
       ↓
Data Layer:         agentgit.db (SQLite WAL) | wtb.db (SQLite/PostgreSQL) | FileTracker (PostgreSQL)
```
