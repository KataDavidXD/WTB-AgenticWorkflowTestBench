# Ray Parallel Execution

**Last Updated:** 2026-01-27  
**Parent:** [Project_Init/INDEX.md](../Project_Init/INDEX.md)

---

## 1. Structure

### 1.1 Application Services (`wtb/application/services/`)

```
wtb/application/services/
├── ray_batch_runner.py          # RayBatchRunner (IBatchTestRunner)
├── batch_test_runner.py         # Sequential fallback
└── actor_lifecycle.py           # Ray actor management
```

### 1.2 Domain Events (`wtb/domain/events/`)

```
wtb/domain/events/
└── ray_events.py                # 17 Ray-specific event types
    ├── RayBatchTestStartedEvent
    ├── RayBatchTestCompletedEvent
    ├── RayActorPoolCreatedEvent
    ├── RayActorInitializedEvent
    ├── RayVariantExecutionStartedEvent
    ├── RayVariantExecutionCompletedEvent
    ├── RayBackpressureAppliedEvent
    └── ...
```

### 1.3 Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       RAY BATCH EXECUTION ARCHITECTURE                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   TestBench.run_batch()                                                      │
│          │                                                                   │
│          ▼                                                                   │
│   IBatchTestRunner                                                           │
│   ├── RayBatchRunner (PRIMARY)     BatchTestRunner (fallback)               │
│   │         │                              │                                 │
│   │         ▼                              ▼                                 │
│   │   Ray Cluster                    Sequential Execution                   │
│   │   ┌─────────────────────────────────────────────────┐                   │
│   │   │                 ACTOR POOL                       │                   │
│   │   │                                                  │                   │
│   │   │   ┌────────────────┐    ┌────────────────┐      │                   │
│   │   │   │ Variant Actor 1│    │ Variant Actor 2│      │                   │
│   │   │   │                │    │                │      │                   │
│   │   │   │ ┌────────────┐ │    │ ┌────────────┐ │      │                   │
│   │   │   │ │ Workspace A│ │    │ │ Workspace B│ │      │                   │
│   │   │   │ │ (isolated) │ │    │ │ (isolated) │ │      │                   │
│   │   │   │ └────────────┘ │    │ └────────────┘ │      │                   │
│   │   │   │                │    │                │      │                   │
│   │   │   │ LangGraph      │    │ LangGraph      │      │                   │
│   │   │   │ StateAdapter   │    │ StateAdapter   │      │                   │
│   │   │   └────────────────┘    └────────────────┘      │                   │
│   │   │                                                  │                   │
│   │   └──────────────────────────────────────────────────┘                   │
│   │                                                                          │
│   └─────────────────────────────────────────────────────────────────────────│
│                                                                              │
│   Events: RayBatchTestStartedEvent → RayVariant... → RayBatchTestCompleted  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.4 Key Components

| Component | Purpose | SOLID |
|-----------|---------|-------|
| `RayBatchRunner` | Parallel variant execution | SRP ✅ |
| `VariantExecutionActor` | Per-variant isolated execution | SRP ✅ |
| `ActorLifecycleManager` | Actor pool management | SRP ✅ |
| `Workspace` | File system isolation per actor | DIP ✅ |

---

## 2. Issues

### 2.1 Active Issues

| ID | Issue | Priority | Status |
|----|-------|----------|--------|
| RAY-001 | None identified | - | - |

### 2.2 Resolved Issues

| ID | Issue | Resolution |
|----|-------|------------|
| RAY-R001 | Workspace race conditions | ✅ Workspace isolation implemented |
| RAY-R002 | File system conflicts | ✅ Per-variant workspace forking |

---

## 3. Gap Analysis (Brief)

| Design Intent | Implementation | Gap |
|--------------|----------------|-----|
| Ray actor pool | ✅ `RayBatchRunner` | None |
| Workspace isolation | ✅ Per-variant workspace | None |
| Backpressure handling | ✅ `RayBackpressureAppliedEvent` | None |
| Progress tracking | ✅ `RayBatchTestProgressEvent` | None |
| Actor restart on failure | ✅ Implemented | None |
| File tracking per variant | ✅ Integrated | None |

**Overall:** Full Ray integration, no gaps identified.

---

## 4. Ray Events (17 Types)

| Category | Events |
|----------|--------|
| **Batch Lifecycle** | Started, Completed, Failed, Cancelled, Progress |
| **Actor Pool** | PoolCreated, ActorInitialized, ActorFailed, ActorRestarted |
| **Variant Execution** | Started, Completed, Failed, Cancelled |
| **File Tracking** | FilesTracked, FilesTrackingFailed |
| **Backpressure** | Applied, Released |

---

## 5. Python Version Note

> **Python 3.12 required** [[memory:13431629]]
> 
> Ray does not support Python 3.13 on Windows. The `.python-version` file is set to 3.12.

---

## 6. Related Documents

| Document | Description |
|----------|-------------|
| [../Project_Init/INDEX.md](../Project_Init/INDEX.md) | Main documentation |
| [../Project_Init/ARCHITECTURE_STRUCTURE.md](../Project_Init/ARCHITECTURE_STRUCTURE.md) | Full architecture |
| [../issues/WORKSPACE_ISOLATION_DESIGN.md](../issues/WORKSPACE_ISOLATION_DESIGN.md) | Workspace design |
