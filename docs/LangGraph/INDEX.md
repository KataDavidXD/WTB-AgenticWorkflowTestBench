# LangGraph Integration

**Last Updated:** 2026-01-27  
**Parent:** [Project_Init/INDEX.md](../Project_Init/INDEX.md)

---

## 1. Structure

### 1.1 State Adapter (`wtb/infrastructure/adapters/`)

```
wtb/infrastructure/adapters/
└── langgraph_state_adapter.py   # PRIMARY state persistence
    ├── LangGraphStateAdapter    # IStateAdapter implementation
    ├── LangGraphConfig          # Checkpointer configuration
    └── LangGraphStateAdapterFactory
```

### 1.2 Checkpoint Store (`wtb/infrastructure/stores/`)

```
wtb/infrastructure/stores/
├── inmemory_checkpoint_store.py # Testing
└── langgraph_checkpoint_store.py # Production (ICheckpointStore)
```

### 1.3 Domain Models (`wtb/domain/models/`)

```
wtb/domain/models/
└── checkpoint.py
    ├── Checkpoint              # Domain entity
    ├── CheckpointId            # Value object (str-based)
    └── ExecutionHistory        # Aggregate with business logic
```

### 1.4 Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    LANGGRAPH INTEGRATION ARCHITECTURE                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   ExecutionController                                                        │
│          │                                                                   │
│          ▼                                                                   │
│   LangGraphStateAdapter                                                      │
│   ├── set_workflow_graph(graph)  → Compile with checkpointer               │
│   ├── execute(initial_state)     → Automatic checkpointing                  │
│   ├── get_state_history()        → Time-travel support                      │
│   └── rollback(checkpoint_id)    → State restoration                        │
│          │                                                                   │
│          ▼                                                                   │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                    LANGGRAPH CHECKPOINTERS                           │   │
│   │                                                                      │   │
│   │   MemorySaver (testing)    SqliteSaver (dev)    PostgresSaver (prod)│   │
│   │        │                        │                      │             │   │
│   │        └────────────────────────┴──────────────────────┘             │   │
│   │                              │                                        │   │
│   │                    BaseCheckpointSaver                               │   │
│   │                    ├── get_state(config)                             │   │
│   │                    ├── get_state_history(config)                     │   │
│   │                    └── update_state(config, values)                  │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│   Thread Isolation: thread_id = "wtb-{execution_id}"                        │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.5 Checkpointer Selection

| Environment | Checkpointer | Package |
|-------------|--------------|---------|
| Unit Tests | `MemorySaver` | `langgraph-checkpoint` |
| Development | `SqliteSaver` | `langgraph-checkpoint-sqlite` |
| Production | `PostgresSaver` | `langgraph-checkpoint-postgres` |

---

## 2. Issues

### 2.1 Active Issues

| ID | Issue | Priority | Status |
|----|-------|----------|--------|
| LG-001 | ID type mismatch (int vs str) | P0 | In Progress |
| LG-002 | Checkpoint ID mapping overhead | P2 | Open |

### 2.2 LG-001: ID Type Mismatch

**Problem:** `IStateAdapter` returns `int`, but LangGraph uses UUID strings.

**Current Workaround:**
```python
# LangGraphStateAdapter maintains bidirectional mapping
self._checkpoint_id_map: Dict[str, int] = {}  # UUID → numeric
self._numeric_to_lg_id: Dict[int, str] = {}   # numeric → UUID
```

**Resolution:** Migrate to `ICheckpointStore` which uses `CheckpointId(str)`.

### 2.3 LG-002: Memory Overhead

**Problem:** ID mapping grows with checkpoint count.

**Suggested Fix:** Implement LRU cache or persist mapping.

---

## 3. Gap Analysis (Brief)

| Design Intent | Implementation | Gap |
|--------------|----------------|-----|
| LangGraph as PRIMARY adapter | ✅ `LangGraphStateAdapter` | None |
| Thread-based isolation | ✅ `thread_id = wtb-{exec_id}` | None |
| Time-travel support | ✅ `get_state_history()` | None |
| Automatic checkpointing | ✅ Per super-step | None |
| String-based checkpoint IDs | ⚠️ Interface returns int | Minor - mapping workaround |
| Graph recompilation with checkpointer | ✅ `force_recompile=True` | None |

**Overall:** Full LangGraph integration with minor ID type gap.

---

## 4. Key APIs

```python
# Compile with checkpointer
adapter = LangGraphStateAdapter(LangGraphConfig.for_development())
adapter.set_workflow_graph(graph)  # Recompiles with checkpointer

# Execute with thread isolation
config = {"configurable": {"thread_id": "wtb-exec-001"}}
result = adapter.execute(initial_state)

# Time travel
history = adapter.get_checkpoint_history()
adapter.rollback(checkpoint_id)

# State update (human-in-the-loop)
adapter.update_state({"modified": True}, as_node="node_name")
```

---

## 5. ACID Compliance

| Property | LangGraph Implementation |
|----------|-------------------------|
| **Atomicity** | Super-step commits are atomic |
| **Consistency** | State schema validated via TypedDict |
| **Isolation** | Thread-based (each execution isolated) |
| **Durability** | SqliteSaver/PostgresSaver persistence |

---

## 6. Related Documents

| Document | Description |
|----------|-------------|
| [../Project_Init/INDEX.md](../Project_Init/INDEX.md) | Main documentation |
| [../Project_Init/ARCHITECTURE_STRUCTURE.md](../Project_Init/ARCHITECTURE_STRUCTURE.md) | Full architecture |
| [../Adapter_and_WTB-Storage/INDEX.md](../Adapter_and_WTB-Storage/INDEX.md) | Adapter details |
| [../issues/GAP_ANALYSIS_2026_01_15_STATE_ADAPTER_ID_MISMATCH.md](../issues/GAP_ANALYSIS_2026_01_15_STATE_ADAPTER_ID_MISMATCH.md) | ID mismatch analysis |
