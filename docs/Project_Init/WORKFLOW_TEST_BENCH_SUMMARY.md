# Workflow Test Bench (WTB) - Summary

**Last Updated:** 2026-02-01  
**Version:** 1.0.0  
**Status:** Current

---

## 1. What WTB Is

WTB (Workflow Test Bench) is a workflow testing framework focused on **transactional consistency**, **reproducibility**, and **diagnosability** for agentic workflows (e.g., LangGraph-based RAG pipelines).

WTB targets failure modes that routinely break evaluation conclusions in real systems:
- non-idempotent writes under retry
- partial commits / orphaned artifacts
- async side effects and ordering violations
- stale reads / cache inconsistencies
- dependency/version drift across tools and nodes

---

## 2. Why This Matters for Agentic RAG Evaluation

RAG evaluation is often treated as read-only QA scoring; however, agentic RAG systems frequently:
- write to indexes and caches,
- persist intermediate artifacts,
- run concurrently,
- evolve toolchains and environments over time.

These side effects can silently bias accuracy, inflate variance, and harm reproducibility.

---

## 3. Bio RAG Evaluation Plan (2026-02-01 Update)

The research plan in `examples/bio_rag_benchmarks/EXPERIMENTAL_PLAN.md` was updated to meet top-tier review standards:

- **Scope alignment (claim-faithful workloads)**: Adds write-bearing workloads (incremental indexing, cache/snapshot semantics, artifact persistence, shared-store concurrency, knowledge refresh events) alongside standard QA evaluation.
- **Stronger baselines**: Adds non-WTB best-practice baselines (transactional store + idempotency keys; workflow engine caching/checkpointing; environment snapshotting) to avoid strawman comparisons.
- **Reproducibility protocol**: Introduces a strict per-run manifest and a two-track evaluation policy (closed API model track + open-weight track).
- **Safety/Ethics**: Adds biomedical risk modes and mitigation/evaluation requirements (citation grounding, recency/retraction checks, safety-oriented error categories).
- **Threats to validity**: Explicitly documents construct/internal/external validity risks and mitigations.

---

## 4. Sequential Execution Fixes (v1.9)

Major structural changes to fix 10 flaws in sequential/batch execution:

- **New: `OutboxExecutionControllerDecorator`** (`wtb/application/services/outbox_controller_decorator.py`) -- OCP-compliant decorator that wraps `IExecutionController` and emits outbox events for the SDK path. Keeps the controller SRP-clean.
- **Controller routing restructured** -- Capability-based routing (`graph provided?` -> `adapter.has_graph()?` -> `node_executor`) replaces fragile `graph is not None` check. Enables proper resume.
- **Resume path** -- `_run_with_langgraph` now handles PAUSED vs PENDING distinctly. Resume uses `adapter.execute(None)` for native LangGraph checkpoint resume.
- **Factory wiring** -- All `WTBTestBenchFactory` methods now wire `OutboxExecutionControllerDecorator` and `ThreadPoolBatchTestRunner` (previously `batch_runner=None`).
- **ThreadPool graph support** -- `ThreadPoolBatchTestRunner._execute_with_controller_factory` now imports graphs from `graph_factory_module/name` (mirrors Ray pattern).

---

## 5. References

- `examples/bio_rag_benchmarks/EXPERIMENTAL_PLAN.md`
- `docs/Project_Init/WORKFLOW_TEST_BENCH_ARCHITECTURE.md`
- `docs/Project_Init/PROGRESS_TRACKER.md`

