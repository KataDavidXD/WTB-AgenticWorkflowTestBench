# Workflow Test Bench (WTB) - Architecture

**Last Updated:** 2026-03-31  
**Version:** 1.10.0  
**Status:** Current

---

## 1. Purpose

WTB provides a structured way to run agentic workflows with **transactional consistency** and **reproducibility** guarantees, with first-class support for:
- failure injection and diagnosability
- audit logging and causal traces
- environment isolation (including node-level virtual environments)
- repeatable benchmark execution at scale (including parallel batch runs)

---

## 2. Scope and Non-Goals

**In scope**
- workflow execution boundaries (start/run/rollback/replay)
- state persistence and checkpointing integration
- audit sessions and immutable run artifacts
- controlled failure injection (A–F scenarios)
- environment isolation and dependency pinning

**Out of scope (non-goals)**
- claiming medical correctness beyond benchmark metrics
- replacing database transactions; WTB composes with transactional stores
- guaranteeing determinism for external services (only traceability + isolation)

---

## 3. Key Concepts

- **Execution**: A single workflow run with a session id, checkpoints, and audit trail.
- **Unit of Work (UoW)**: Transaction boundary for state + metadata persistence.
- **Outbox Pattern**: Reliable cross-system event propagation and ordering.
- **Checkpoint**: A persisted workflow state snapshot enabling rollback/replay.
- **Environment (per node)**: Optional isolated dependency/runtime context.
- **Audit Session**: Immutable record of tool I/O, decisions, and state diffs.

---

## 4. Logical Components

At a high level WTB can be viewed as:

1. **Execution Orchestrator**
   - starts/stops executions, coordinates node runs, handles retries and failure injection
2. **State + Checkpoint Layer**
   - persists states/checkpoints, supports rollback/replay
3. **Audit + Trace Layer**
   - records tool inputs/outputs, state diffs, causal attribution
4. **Isolation Layer**
   - workspace isolation and optional node-level environment isolation
5. **Batch Runner**
   - parallel execution (thread pool / Ray) with isolated transaction boundaries

---

## 5. Invariants (What Must Always Hold)

- **Atomicity**: a workflow execution either commits all intended artifacts or rolls back.
- **Isolation**: concurrent runs do not leak artifacts across sessions unless explicitly configured.
- **Traceability**: every persisted write and tool call is attributable to an execution and node.
- **Recoverability**: failed executions can be rolled back to a consistent checkpoint.

---

## 6. Failure Injection Model (A–F)

WTB’s failure injection model is aligned to common workflow hazards:
- A: non-idempotent writes under retry
- B: partial commit / orphan artifacts
- C: async background side effects and ordering hazards
- D: stale reads / snapshot inconsistencies
- E: workflow-level monolithic environments (resource waste)
- F: tool/version drift and dependency conflicts

These scenarios are used both for **testing the framework** and for **evaluating RAG workflows**.

---

## 7. Data and Artifact Model (Conceptual)

WTB treats the following as first-class artifacts:
- run manifests (config/prompt/env hashes)
- tool I/O traces (request/response hashes, timing)
- intermediate workflow artifacts (retrieval sets, index snapshots, citations)
- audit logs and event streams (outbox)

All artifacts must be linkable to:
- execution id, node id, and timestamp window

---

## 8. Integration Points

- **LangGraph**: compile/invoke with checkpoint store integration.
- **Ray / ThreadPool**: batch execution with per-run isolation.
- **UV / virtual environments**: reproducible dependency management and node-level isolation.
- **Evaluation harness**: benchmark scorers (CRAG/MedRAG/BioASQ) and consistency metrics.

---

## 9. Operational Concerns

- **Performance**: checkpoint frequency is a tunable knob; track p50/p95/p99 overhead.
- **Storage growth**: content-addressable storage + compaction policies recommended.
- **Concurrency**: avoid shared mutable stores without explicit transactional boundaries.
- **External services**: treat as nondeterministic; ensure traceability and containment.

---

## 10. Testing Strategy

- unit tests for UoW, repositories, adapters, and invariants
- integration tests for rollback/replay and outbox ordering
- scenario tests (A–F) across sync/async implementations
- benchmark harness tests validating metric computation and trace completeness

---

## 11. Documentation Map

- Summary: `docs/Project_Init/WORKFLOW_TEST_BENCH_SUMMARY.md`
- Research plan: `examples/bio_rag_benchmarks/EXPERIMENTAL_PLAN.md`
- Engineering docs hub: `docs/Project_Init/INDEX.md`

---

## 12. Alignment to Top-tier Review Expectations

WTB’s evaluation and reporting are designed to meet common review criteria:
- **strong baselines** (not only naive “no-WTB” execution)
- **artifact availability** (manifests, traces, configs)
- **threats-to-validity disclosure**
- **safety/ethics** for biomedical use cases

---

## 13. Critical Decisions

This section records project-level decisions that affect scientific validity and reviewability.

### 13.1 Two-track evaluation policy (closed + open)
- **Decision**: Run a closed-LLM API track (best-effort model pinning + full tracing) and an open-weight track (fully pinned model/hash).
- **Rationale**: Closed APIs drift; open-weight replication is required for strong reproducibility claims.

### 13.2 Scope alignment via write-bearing workloads
- **Decision**: Evaluate both benchmark-faithful QA and claim-faithful **write-bearing workflows** (indexing/caching/artifact persistence/shared stores).
- **Rationale**: Prevent mismatch between “external writes” claims and read-only benchmark structure.

### 13.3 Strong non-WTB baselines are mandatory
- **Decision**: Include at least one “best-practice engineering” baseline (transactional store + idempotency keys) and one environment snapshotting baseline.
- **Rationale**: Avoid strawman comparisons; isolate what WTB uniquely contributes.

### 13.4 Run manifests are required for every experiment
- **Decision**: Every run emits a machine-readable manifest (model id/hash, prompt versions, corpora snapshot ids, env hashes, seeds, tool I/O hashes).
- **Rationale**: Enables auditability, replay, and clear provenance under review.

### 13.5 Biomedical safety reporting is required
- **Decision**: Report safety-oriented error categories and enforce citation-grounded responses in biomedical experiments.
- **Rationale**: Nature-style expectations require explicit harm analysis and mitigations.

### 13.6 BatchExecutionCoordinator for Rollback/Fork Operations (v1.8)
- **Decision**: Implement `BatchExecutionCoordinator` as a separate service layer for coordinating rollback/fork operations across batch test results.
- **Rationale**: 
  1. **Separation of Concerns**: Coordinator orchestrates, delegates to ExecutionController for state operations.
  2. **ACID Compliance**: Each operation uses fresh UoW for transaction isolation; StateAdapter is reused for efficiency.
  3. **Two-Phase Transaction**: State changes + outbox event in UoW transaction (Phase 1), file restore post-commit (Phase 2).
  4. **Information Preservation**: `BatchTestResult` now includes `file_commit_id`, `checkpoint_count`, `last_checkpoint_id` fields.
- **Trade-offs**: 
  - File restore is best-effort (eventual consistency via outbox retry)
  - StateAdapter reuse means it must be thread-safe if used concurrently

### 13.7 Rollback vs Fork Semantics
- **Decision**: Rollback is destructive (overwrites future checkpoints), Fork is non-destructive (creates new execution branch).
- **Rationale**: 
  1. **Rollback**: "Go back in time and redo" - used when you want to correct a variant's path
  2. **Fork**: "Keep original + explore branch" - used for A/B comparison without losing original execution
- **Graph Requirement**: Only `*_and_run()` operations require a graph; basic rollback/fork only need execution_id and checkpoint_id.

### 13.8 SDK Layer Separation for Batch Rollback/Fork (v1.8)
- **Decision**: SDK layer provides convenience methods (`rollback_batch_result()`, `fork_batch_result()`) that delegate to Application layer factories.
- **Rationale**:
  1. **DIP Compliance**: SDK never imports infrastructure components directly
  2. **Layer Separation**: Infrastructure wiring happens in Application factories (`BatchCoordinatorFactory`)
  3. **Single Responsibility**: SDK provides user-facing convenience, Application handles orchestration, Infrastructure handles persistence
- **Implementation**:
  - `WTBTestBench.get_batch_coordinator()` → delegates to `batch_runner.create_rollback_coordinator()` or `BatchCoordinatorFactory.create_default()`
  - `BatchRollbackResult` and `BatchForkResult` are SDK DTOs (not domain entities)
  - SDK exports coordinator and related types for advanced users

### 13.9 Rollback File Cleanup Architecture (v1.9)
- **Decision**: Implement optional file cleanup during rollback as an Outbox Pattern consumer feature.
- **Rationale**:
  1. **Outbox Pattern Separation**: `BatchExecutionCoordinator` produces events with cleanup configuration in payload; `OutboxProcessor` consumes and executes cleanup.
  2. **Optional Feature (Opt-in)**: Disabled by default via `WTBConfig.rollback_cleanup_enabled = False`. Users must explicitly enable.
  3. **No Schema Changes**: Uses existing `checkpoint_links` and `mementos` tables via `get_files_at_checkpoint()` method.
  4. **Interface Segregation**: New `IFileCleanupService` interface separate from `IFileTrackingService` (ISP compliance).
- **Architecture**:
  ```
  BatchExecutionCoordinator (Event Producer)
    ├── Creates ROLLBACK_FILE_RESTORE event
    └── Includes cleanup config in payload (from WTBConfig)
  
  OutboxProcessor (Event Consumer)
    ├── Injected with IFileCleanupService
    ├── Calls identify_orphaned_files()
    └── Calls cleanup_orphaned_files()
  
  OutboxLifecycleManager
    └── Creates OutboxProcessor with file_cleanup_service
  ```
- **Safety Controls**:
  - `rollback_cleanup_dry_run`: Log-only mode (no actual deletion)
  - `rollback_cleanup_backup`: Backup files before deletion (default: true)
  - `rollback_cleanup_max_files`: Limit to prevent runaway deletion (default: 100)
- **Events & Audit Trail**:
  - `OutboxEventType.FILE_CLEANUP_COMPLETED`: Emitted after cleanup completes
  - `WTBAuditEventType.FILES_CLEANED_UP`: Audit trail entry
  - `FileCleanupCompletedEvent`: Domain event with full cleanup details
- **Trade-offs**:
  - Cleanup is eventual (happens when OutboxProcessor processes the event)
  - Requires OutboxProcessor to be running with FileCleanupService injected

### 13.10 Safe Condition Evaluation (C8)
- **Decision**: Replaced `eval()` with AST-based `safe_eval_condition()` that only allows comparisons, boolean logic, and variable lookups—no arbitrary code execution.
- **Rationale**: Prevents code injection when evaluating workflow or user-supplied conditions.

### 13.11 Outbox Atomicity via Deferred Commit (C2)
- **Decision**: `OutboxExecutionControllerDecorator` sets `deferred_commit=True` on the inner controller so business data and outbox events commit in one atomic transaction.
- **Rationale**: Keeps outbox rows aligned with the same UoW commit; avoids split transactions.

### 13.12 Unified str checkpoint_id (C1 / Phase 3)
- **Decision**: Migrated `checkpoint_id` from `int` to `str` (UUID) across domain models, events, interfaces, and infrastructure to match LangGraph’s native string IDs.
- **Rationale**: End-to-end type consistency and fewer adapter coercion errors.

### 13.13 NodeStatus Enum (M9)
- **Decision**: Replaced raw string node status with `NodeStatus(str, Enum)` for type safety while preserving string compatibility at boundaries.
- **Rationale**: Stronger invariants in application code without breaking persistence or string-based APIs.

### 13.14 SQLAlchemy Engine Caching (M20)
- **Decision**: Introduced `engine_cache.get_engine()` with an LRU cache so each UoW does not construct a new engine.
- **Rationale**: Reduces connection/engine churn and resource use under frequent UoW creation.

### 13.15 Domain Audit Type (C14 / DIP)
- **Decision**: Introduced `AuditEntry` in the domain layer; domain interfaces no longer import infrastructure audit types.
- **Rationale**: Dependency Inversion: the domain owns the audit contract; infrastructure maps to storage.

### 13.16 Centralized Graph Factory Loader (DIP)
- **Decision**: Consolidated all `importlib.import_module` graph-loading calls into a single `graph_loader.load_graph_factory()` in the application layer. Removed `VariantCombination.create_graph()` from the domain model.
- **Rationale**: Domain models hold data only (`graph_factory_module`, `graph_factory_name`). The application layer (ray_batch_runner, batch_test_runner, workflow_project) uses `graph_loader` to resolve these references. Eliminates upward domain -> application dependency and triples of duplicated `importlib` code.

### 13.17 Checkpointer Ownership in Fork Adapters (v1.10)
- **Decision**: Added `_owns_checkpointer` flag to `LangGraphStateAdapter`. Fork adapters share the parent's checkpointer but do not own it; only the owning adapter closes the SQLite connection in `close()`/`__del__`.
- **Rationale**: `create_fork()` creates a temporary adapter that shares the parent's checkpointer. When Python GC'd the temporary adapter, `__del__` closed the shared SQLite connection, causing "Cannot operate on a closed database" on subsequent operations. The ownership flag prevents non-owning adapters from closing shared resources.

### 13.18 Cascade-Safe Workflow Unregistration (v1.10)
- **Decision**: `ProjectService.unregister_workflow()` now deletes associated executions before deleting the workflow itself.
- **Rationale**: SQLAlchemy's `ExecutionORM.workflow_id` is `nullable=False`. Without explicit cascade cleanup, deleting a workflow triggers SQLAlchemy to set `workflow_id=NULL` on related executions, violating the NOT NULL constraint. Explicit delete-children-first ensures FK integrity.

### 13.19 Idempotent Workflow Registration in Batch Runners (v1.10)
- **Decision**: Batch runner's isolated UoW sessions check `uow.workflows.exists(id)` before `add()`, replacing the blind `try/except` INSERT pattern.
- **Rationale**: Each batch thread gets an isolated UoW/session sharing the same SQLite DB. The previous `try: add() except: pass` left the SQLAlchemy session in a dirty/rolled-back state after UNIQUE constraint failures, causing all subsequent operations on that session to fail.

### 13.20 WTBTestBench Resource Lifecycle (v1.11)
- **Decision**: Added `close()`, `__enter__`/`__exit__`, and `__del__` to `WTBTestBench`. `close()` shuts down the batch runner's thread pool, closes the state adapter's checkpointer connection, and exits the UoW session.
- **Rationale**: Without explicit teardown, creating multiple benches in one process accumulated open SQLite connections, ORM sessions, and executor threads. Particularly impactful in `--demo=all` scenarios where 5 benches run sequentially.

### 13.21 Full FK Cascade in Workflow Unregistration (v1.11)
- **Decision**: `unregister_workflow` now deletes ALL FK children in leaf-first order: evaluation_results -> executions -> node_variants -> batch_tests -> workflow.
- **Rationale**: The v1.10 fix only cascaded `executions`. The ORM also has `node_variants.workflow_id` and `batch_tests.workflow_id` FKs, plus `evaluation_results.execution_id`. Deleting a workflow with variants or batch tests would still raise `IntegrityError`.

### 13.22 Fork Adapter Construction via object.__new__() (v1.11)
- **Decision**: `create_fork()` uses `object.__new__(LangGraphStateAdapter)` instead of calling `__init__`, manually copying only the needed attributes.
- **Rationale**: Calling `__init__` opens a new `sqlite3.connect()` for a `SqliteSaver` that is immediately discarded (overwritten with the parent's checkpointer). In a batch test with N variants, this leaked N database connections until GC finalization.

### 13.23 Batch Worker State Adapter Mode Alignment (v1.11)
- **Decision**: Changed `WTBConfig.for_development()` to set `state_adapter_mode="langgraph"` instead of `"agentgit"`.
- **Rationale**: The batch worker's `ExecutionControllerFactory._create_state_adapter()` recognized `"langgraph"` but not `"agentgit"`, defaulting to `InMemoryStateAdapter`. This meant batch worker checkpoints were never persisted to SQLite, making rollback/fork operations impossible after batch execution.

### 13.24 LangGraph Checkpoint ID Capture (v1.11)
- **Decision**: After LangGraph execution completes, `_run_with_langgraph` now captures `execution.checkpoint_id` from the latest graph state snapshot.
- **Rationale**: The node-executor path set `checkpoint_id` via `_create_checkpoint()`, but the LangGraph path never captured it. This left `BatchTestResult.last_checkpoint_id` as `None`, preventing rollback/fork from finding the target checkpoint.

### 13.25 Session Expiry After Batch Execution (v1.11)
- **Decision**: After `run_batch_test()` completes, the bench's ORM session is expired via `session.expire_all()`.
- **Rationale**: Batch workers commit executions through isolated sessions. The bench's long-lived session caches the identity map from creation time and won't see these new rows until expired. Without this, `bench.get_execution(exec_id)` returns stale/missing data after batch runs.

---

## 14. Change Log

| Date | Change |
|------|--------|
| 2026-04-03 | v1.11: Resource lifecycle hardening - WTBTestBench.close(), full FK cascade, fork leak fix, batch state_adapter_mode alignment, checkpoint_id capture (13.20-13.25) |
| 2026-04-03 | v1.10: Checkpointer ownership flag, cascade-safe unregistration, idempotent batch workflow registration (13.17-13.19) |
| 2026-03-31 | v1.10: Documented architecture fix sprint in Critical Decisions (13.10-13.15): safe condition eval, deferred outbox commit, str checkpoint_id, NodeStatus enum, engine cache, domain AuditEntry |
| 2026-02-13 | v1.9: Added rollback file cleanup architecture (§13.9); optional opt-in feature via WTBConfig, Outbox Pattern consumer, events/audit trail |
| 2026-02-05 | v1.8: Added SDK batch rollback/fork integration (§13.8); `WTBTestBench` convenience methods + `BatchCoordinatorFactory` for layer separation |
| 2026-02-05 | v1.8: Added BatchExecutionCoordinator (§13.6, §13.7); implemented rollback/fork coordination for batch test results |
| 2026-02-01 | Created architecture doc; added §13 Critical Decisions aligned with Bio RAG research plan updates |

