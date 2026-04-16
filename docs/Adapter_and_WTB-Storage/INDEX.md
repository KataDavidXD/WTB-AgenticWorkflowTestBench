# Adapter & WTB Storage

**Last Updated:** 2026-01-27  
**Parent:** [Project_Init/INDEX.md](../Project_Init/INDEX.md)

---

## 1. Structure

### 1.1 State Adapters (`wtb/infrastructure/adapters/`)

```
wtb/infrastructure/adapters/
├── __init__.py
├── langgraph_state_adapter.py   # PRIMARY - LangGraph checkpointers
├── inmemory_state_adapter.py    # Testing - Dict-based storage
└── agentgit_state_adapter.py    # DEPRECATED - AgentGit integration
```

### 1.2 Adapter Hierarchy

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        STATE ADAPTER IMPLEMENTATIONS                         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   IStateAdapter (Interface)                                                  │
│   ├── LangGraphStateAdapter     ◄── PRIMARY (production)                    │
│   │   ├── MemorySaver           (unit tests)                                │
│   │   ├── SqliteSaver           (development)                               │
│   │   └── PostgresSaver         (production)                                │
│   │                                                                          │
│   ├── InMemoryStateAdapter      ◄── Testing only                            │
│   │                                                                          │
│   └── AgentGitStateAdapter      ◄── DEPRECATED                              │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.3 WTB Persistence (`wtb/infrastructure/database/`)

```
wtb/infrastructure/database/
├── unit_of_work.py              # SQLAlchemyUnitOfWork (ACID)
├── inmemory_unit_of_work.py     # InMemoryUnitOfWork (testing)
├── models.py                     # SQLAlchemy ORM models
├── repositories/                 # Repository implementations
│   ├── workflow_repository.py
│   ├── execution_repository.py
│   ├── outbox_repository.py     # Outbox Pattern
│   └── ...
└── migrations/                   # SQL migrations
```

### 1.4 Key Components

| Component | Purpose | SOLID |
|-----------|---------|-------|
| `LangGraphStateAdapter` | Checkpoint persistence via LangGraph | DIP ✅ |
| `SQLAlchemyUnitOfWork` | Transaction boundaries | SRP ✅ |
| `OutboxRepository` | Cross-DB consistency | SRP ✅ |
| `LangGraphConfig` | Checkpointer configuration | OCP ✅ |

---

## 2. Issues

### 2.1 Active Issues

| ID | Issue | Priority | Status |
|----|-------|----------|--------|
| ADP-001 | IStateAdapter returns `int`, domain uses `str` | P0 | In Progress |
| ADP-002 | Checkpoint ID mapping memory overhead | P2 | Open |
| ADP-003 | AgentGit adapter deprecation cleanup | P3 | Backlog |

### 2.2 ADP-001: ID Type Mismatch

**Problem:** `IStateAdapter.save_checkpoint()` returns `int`, but LangGraph uses UUID strings.

**Current Workaround:**
```python
# LangGraphStateAdapter maintains mapping
self._checkpoint_id_map: Dict[str, int] = {}  # UUID → numeric
self._numeric_to_lg_id: Dict[int, str] = {}   # numeric → UUID
```

**Resolution:** Migrate to `ICheckpointStore` which uses `CheckpointId(str)`.

### 2.3 ADP-002: Memory Overhead

**Problem:** ID mapping dictionaries grow unbounded.

**Impact:** Long-running executions may accumulate memory.

**Suggested Fix:** LRU cache or database persistence for ID mapping.

---

## 3. Gap Analysis (Brief)

| Design Intent | Implementation | Gap |
|--------------|----------------|-----|
| LangGraph as PRIMARY adapter | ✅ `LangGraphStateAdapter` | None |
| AgentGit as backup | ⏸️ Deprecated, not maintained | Intentional |
| String-based checkpoint IDs | ⚠️ Interface uses int | Minor - mapping workaround |
| Unit of Work for ACID | ✅ `SQLAlchemyUnitOfWork` | None |
| Outbox Pattern for cross-DB | ✅ Implemented | None |

**Overall:** Implementation matches design with minor ID type gap.

---

## 4. Related Documents

| Document | Description |
|----------|-------------|
| [../Project_Init/INDEX.md](../Project_Init/INDEX.md) | Main documentation |
| [../Project_Init/ARCHITECTURE_STRUCTURE.md](../Project_Init/ARCHITECTURE_STRUCTURE.md) | Full architecture |
| [../LangGraph/INDEX.md](../LangGraph/INDEX.md) | LangGraph integration |
| [../issues/GAP_ANALYSIS_2026_01_15_STATE_ADAPTER_ID_MISMATCH.md](../issues/GAP_ANALYSIS_2026_01_15_STATE_ADAPTER_ID_MISMATCH.md) | ID mismatch details |
