# Thread & Process Architecture

**Last Updated:** 2026-01-27  
**Parent:** [Project_Init/INDEX.md](../Project_Init/INDEX.md)

---

## 1. Structure

### 1.1 Execution Strategies

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      EXECUTION STRATEGY HIERARCHY                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   IBatchTestRunner                                                           │
│   ├── RayBatchRunner           ◄── PRIMARY (distributed, recommended)       │
│   │   ├── Multi-process via Ray actors                                      │
│   │   ├── Workspace isolation per variant                                   │
│   │   └── Automatic scaling                                                 │
│   │                                                                          │
│   └── BatchTestRunner          ◄── Fallback (sequential)                    │
│       └── Single-threaded execution                                         │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 Thread Safety

| Component | Thread Safety | Mechanism |
|-----------|--------------|-----------|
| `WTBEventBus` | ✅ Thread-safe | `RLock` (reentrant) |
| `SQLAlchemyUnitOfWork` | ✅ Thread-safe | Session per thread |
| `LangGraphStateAdapter` | ✅ Thread-safe | Thread isolation via `thread_id` |
| `RayBatchRunner` | ✅ Process isolation | Ray actors |

### 1.3 Parallelism Model

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         PARALLELISM MODEL                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   Single Execution                                                           │
│   ════════════════                                                           │
│   Thread: Main                                                               │
│   Process: Single                                                            │
│   Isolation: thread_id in LangGraph                                          │
│                                                                              │
│   Batch Execution (Ray)                                                      │
│   ═════════════════════                                                      │
│   Threads: Multiple (one per Ray actor)                                      │
│   Processes: Multiple (Ray workers)                                          │
│   Isolation:                                                                 │
│   ├── Process: Ray actor isolation                                          │
│   ├── File system: Workspace per variant                                    │
│   └── State: thread_id per variant                                          │
│                                                                              │
│   Batch Execution (Sequential)                                               │
│   ═════════════════════════════                                              │
│   Thread: Single                                                             │
│   Process: Single                                                            │
│   Isolation: thread_id only                                                  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Issues

### 2.1 Active Issues

| ID | Issue | Priority | Status |
|----|-------|----------|--------|
| TP-001 | None identified | - | - |

### 2.2 Resolved Issues

| ID | Issue | Resolution |
|----|-------|------------|
| TP-R001 | Race conditions in parallel file access | ✅ Workspace isolation |
| TP-R002 | Event handler deadlock risk | ✅ RLock instead of Lock |

---

## 3. Gap Analysis (Brief)

| Design Intent | Implementation | Gap |
|--------------|----------------|-----|
| Ray as default executor | ✅ `RayBatchRunner` | None |
| Thread-safe event bus | ✅ RLock | None |
| Process isolation | ✅ Ray actors | None |
| Workspace isolation | ✅ Per-variant workspace | None |
| Sequential fallback | ✅ `BatchTestRunner` | None |

**Overall:** Full implementation, no gaps identified.

---

## 4. Best Practices

### 4.1 For Single Execution
```python
# Thread-safe by default via thread_id
result = bench.run(graph, initial_state={"query": "test"})
```

### 4.2 For Parallel Execution
```python
# Ray provides process isolation automatically
results = bench.run_batch(
    graph,
    variants=[{"node": "A"}, {"node": "B"}],
    test_cases=[{"query": "Q1"}, {"query": "Q2"}],
)
```

### 4.3 Event Handling
```python
# Safe for nested publishes due to RLock
def handler(event):
    bus.publish(AnotherEvent())  # Won't deadlock
```

---

## 5. Related Documents

| Document | Description |
|----------|-------------|
| [../Project_Init/INDEX.md](../Project_Init/INDEX.md) | Main documentation |
| [../Ray/INDEX.md](../Ray/INDEX.md) | Ray integration |
| [../EventBus_and_Audit_Session/INDEX.md](../EventBus_and_Audit_Session/INDEX.md) | Thread-safe events |
