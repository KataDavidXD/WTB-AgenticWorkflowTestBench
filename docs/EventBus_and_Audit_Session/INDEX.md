# Event Bus & Audit Session

**Last Updated:** 2026-01-27  
**Parent:** [Project_Init/INDEX.md](../Project_Init/INDEX.md)

---

## 1. Structure

### 1.1 Event Bus (`wtb/infrastructure/events/`)

```
wtb/infrastructure/events/
├── __init__.py
├── wtb_event_bus.py             # Thread-safe pub/sub (RLock)
└── wtb_audit_trail.py           # Audit entry recording
```

### 1.2 Domain Events (`wtb/domain/events/`)

```
wtb/domain/events/
├── __init__.py                  # 70+ event exports
├── execution_events.py          # Started, Paused, Completed, Failed
├── node_events.py               # NodeStarted, NodeCompleted, NodeFailed
├── checkpoint_events.py         # CheckpointCreated, Rollback, Branch
├── langgraph_events.py          # LangGraph audit events
├── file_processing_events.py    # FileCommit, FileRestore events
├── ray_events.py                # Ray batch/actor events (17 types)
├── environment_events.py        # Venv lifecycle events (13 types)
└── workspace_events.py          # Workspace isolation events (17 types)
```

### 1.3 Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           EVENT FLOW                                         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   ExecutionController ──────► WTBEventBus ──────► AuditEventListener        │
│          │                        │                      │                   │
│          │                   (RLock)                (auto-record)            │
│          │                        │                      ▼                   │
│          │                        │              WTBAuditTrail               │
│          │                        │                      │                   │
│          │                   Bridge (optional)       flush()                 │
│          │                        │                      │                   │
│          ▼                        ▼                      ▼                   │
│   LangGraphStateAdapter     AgentGit (if enabled)  IAuditLogRepository      │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.4 Key Features

| Feature | Implementation | Notes |
|---------|---------------|-------|
| Thread Safety | `RLock` (reentrant) | Supports nested publishes |
| Bounded History | `deque(maxlen=1000)` | O(1) operations |
| AgentGit Bridge | Optional via `enable_agentgit_bridge()` | Avoids import cycles |
| Event Persistence | `IAuditLogRepository` | SQLAlchemy or InMemory |

---

## 2. Issues

### 2.1 Active Issues

| ID | Issue | Priority | Status |
|----|-------|----------|--------|
| EVT-001 | AgentGit bridge memory leak risk | P2 | Open |
| EVT-002 | Event history unbounded without max_history | P3 | Resolved |

### 2.2 EVT-001: Bridge Handler Leak

**Problem:** Repeated `enable_agentgit_bridge()` / `disable_agentgit_bridge()` without proper cleanup could accumulate handlers.

**Mitigation:** Always call `disable_agentgit_bridge()` before re-enabling.

**Suggested Fix:** Consider weak references for bridge handlers.

---

## 3. Gap Analysis (Brief)

| Design Intent | Implementation | Gap |
|--------------|----------------|-----|
| Thread-safe event bus | ✅ `WTBEventBus` with RLock | None |
| Bounded history | ✅ `deque(maxlen=1000)` | None |
| Audit persistence | ✅ `IAuditLogRepository` | None |
| AgentGit bridge | ✅ Optional, explicit enable | None (design deviation documented) |
| Rich domain events | ✅ 70+ event types | Exceeds design |

**Design Deviations (Intentional):**

| Design | Implementation | Reason |
|--------|---------------|--------|
| Wrap AgentGit EventBus | Standalone WTBEventBus | Avoid import cycles |
| `Lock` for thread safety | `RLock` (reentrant) | Support nested publishes |
| List for history | `deque` | O(1) append and eviction |

---

## 4. Related Documents

| Document | Description |
|----------|-------------|
| [../Project_Init/INDEX.md](../Project_Init/INDEX.md) | Main documentation |
| [../Project_Init/ARCHITECTURE_STRUCTURE.md](../Project_Init/ARCHITECTURE_STRUCTURE.md) | Full architecture |
| [../LangGraph/INDEX.md](../LangGraph/INDEX.md) | LangGraph events |
