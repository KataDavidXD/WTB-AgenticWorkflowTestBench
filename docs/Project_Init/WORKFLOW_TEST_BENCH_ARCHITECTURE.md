# Workflow Test Bench (WTB) - Detailed Architecture Design

## Executive Analysis

### Current State Assessment

#### AgentGit V2 Analysis

```
Strengths:
├── Clean domain models (dataclasses)
├── MDP-style checkpointing (Node + Tool/Message granularity)
├── Branching and rollback support
├── Tool invocation tracking
└── Workflow graph abstraction (WorkflowNode, WorkflowEdge, WorkflowGraph)

Weaknesses:
├── Raw SQLite with embedded SQL in repositories
├── No connection pooling or transaction management
├── Tight coupling between repository and SQL dialect
├── No explicit Unit of Work pattern
└── Missing interface abstractions (all concrete classes)
```

#### FileTracker Analysis

```
Strengths:
├── ORM -> Repository pattern separation
├── Content-addressable storage (SHA-256)
├── Clean domain models (Commit, FileMemento)
└── Blob deduplication

Weaknesses:
├── ORM is actually raw SQL wrapper (not SQLAlchemy)
├── PostgreSQL-specific SQL syntax
├── No interface abstractions
└── Transaction management per-operation (not unit-of-work)
```

#### Integration Strategy: Option B (Anti-Corruption Layer)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        WTB INTEGRATION STRATEGY                                  │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Decision: Do NOT modify AgentGit or FileTracker packages                       │
│                                                                                  │
│  Instead:                                                                        │
│  ├── WTB owns new tables: wtb_node_boundaries, wtb_checkpoint_files             │
│  ├── WTB uses SQLAlchemy + Repository + Unit of Work for its tables            │
│  ├── AgentGitStateAdapter translates between WTB and AgentGit concepts          │
│  └── Cross-database references via stored IDs (logical FKs)                    │
│                                                                                  │
│  Benefits:                                                                       │
│  ├── No breaking changes to existing packages                                   │
│  ├── Clean separation of concerns                                               │
│  ├── Testable with dependency injection                                         │
│  └── Proper abstraction - high-level code unaware of database details          │
│                                                                                  │
│  See: docs/DATABASE_DESIGN.md for complete schema and implementation           │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 0. VALIDATED INFRASTRUCTURE (From Test Results)

> **94 AgentGit v2 tests + 40+ FileTracker tests PASSED**
> 
> This section documents verified capabilities based on comprehensive infrastructure testing.

### 0.0 Design Philosophy (Post-Test Refinement)

> **"Design at smallest granularity, separate concerns, compose at higher levels."**
> 
> **"Use Anti-Corruption Layer - WTB owns its semantic layer, AgentGit/FileTracker unchanged."**

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                    CORRECTED DESIGN UNDERSTANDING                                   │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  KEY INSIGHT #1: Checkpoint ≠ Node Boundary                                       │
│  ════════════════════════════════════════════                                     │
│                                                                                    │
│  OLD (Problematic):                                                                │
│  ├── "node" checkpoint ─────────────► Stored as checkpoint with type="node"       │
│  └── "tool_message" checkpoint ─────► Stored with parent_checkpoint_id            │
│      └── Problem: "node" checkpoint duplicates state from last tool checkpoint    │
│                                                                                    │
│  NEW (Correct):                                                                    │
│  ├── Checkpoint = ATOMIC state snapshot (always at tool/message level)            │
│  ├── Node Boundary = MARKER pointing to checkpoints (NOT a separate checkpoint!)  │
│  │   ├── entry_checkpoint_id → First checkpoint when entering node                │
│  │   └── exit_checkpoint_id  → Last checkpoint when exiting (rollback target)     │
│  └── Rollback is UNIFIED: Always to a checkpoint                                  │
│      ├── "Node rollback" = rollback to node_boundary.exit_checkpoint_id           │
│      └── "Tool rollback" = rollback to any checkpoint.id                          │
│                                                                                    │
│  KEY INSIGHT #2: Option B - Anti-Corruption Layer                                 │
│  ═══════════════════════════════════════════════                                  │
│                                                                                    │
│  Do NOT modify AgentGit or FileTracker packages. Instead:                         │
│  ├── WTB owns new tables in its own database (wtb.db):                            │
│  │   ├── wtb_node_boundaries (pointers to AgentGit checkpoints)                   │
│  │   ├── wtb_checkpoint_files (links to FileTracker commits)                      │
│  │   └── wtb_workflows, wtb_executions, etc.                                      │
│  ├── AgentGitStateAdapter translates between WTB and AgentGit concepts            │
│  └── Cross-database references via stored IDs (logical foreign keys)             │
│                                                                                    │
│  ABSTRACTION LAYERS (High-level unaware of DB):                                   │
│  ════════════════════════════════════════════                                     │
│                                                                                    │
│  Application  │ ExecutionController, NodeReplacer, EvaluationEngine              │
│       ↓       │ Uses IStateAdapter interface only                                 │
│  Domain       │ AgentGitStateAdapter implements IStateAdapter                     │
│       ↓       │ Uses AgentGit + WTB Repositories                                  │
│  Infrastructure│ Repository + Unit of Work (SQLAlchemy for WTB)                   │
│       ↓       │ Manages transactions, abstracts raw SQL                           │
│  Data Access  │ agentgit.db + wtb.db + filetracker (3 databases)                  │
│                                                                                    │
│  BENEFITS:                                                                         │
│  ├── No breaking changes to AgentGit/FileTracker                                  │
│  ├── No data duplication                                                          │
│  ├── Clean separation of concerns                                                 │
│  ├── Unified rollback logic                                                       │
│  ├── Testable with dependency injection                                           │
│  └── High-level code knows nothing about databases                                │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

### 0.1 AgentGit v2 - Verified Capabilities

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                    AGENTGIT V2 - TESTED & VERIFIED (94 tests)                       │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  ✅ CHECKPOINT SYSTEM                                                              │
│  ├── Checkpoint model: creation, to_dict/from_dict, metadata, tool_track_position │
│  ├── CheckpointRepository: CRUD, get_by_internal_session, update_metadata         │
│  ├── All checkpoints are atomic (tool/message level)                              │
│  └── Node boundaries stored separately in WTB's wtb_node_boundaries table         │
│                                                                                    │
│  ✅ SESSION MANAGEMENT                                                             │
│  ├── InternalSession: state, conversation_history, is_current                     │
│  ├── InternalSession_mdp: current_node_id, workflow_variables, execution_path     │
│  ├── ExternalSession: user container, branch_count, internal_session_ids          │
│  ├── Session hierarchy: parent_session_id, get_session_lineage()                  │
│  └── Isolation verified across external sessions                                  │
│                                                                                    │
│  ✅ BRANCHING & ROLLBACK                                                           │
│  ├── InternalSession_mdp.create_branch_from_checkpoint()                          │
│  ├── Preserves: checkpoint state, parent lineage, branch_point_checkpoint_id      │
│  └── Works at both node-level and tool/message-level                              │
│                                                                                    │
│  ✅ TOOL MANAGEMENT                                                                │
│  ├── ToolManager: record_invocation, get_tool_track_position, statistics          │
│  ├── ToolRollbackRegistry:                                                        │
│  │   ├── ToolSpec: forward function + optional reverse function                   │
│  │   ├── rollback(): Execute reverse in LIFO order                                │
│  │   ├── redo(): Re-execute forward functions                                     │
│  │   └── Checkpoint tools auto-skipped during rollback                            │
│  └── register_reversible_tool(name, forward_fn, reverse_fn)                       │
│                                                                                    │
│  ✅ AUDIT TRAIL                                                                    │
│  ├── AuditEvent: timestamp, event_type, severity, message, details, duration_ms   │
│  ├── EventType: TOOL_START/SUCCESS/ERROR, AGENT_DECISION, LLM_CALL,               │
│  │              CHECKPOINT_CREATED, ROLLBACK                                      │
│  ├── AuditTrail: add_event, get_errors, get_tool_events, get_summary              │
│  ├── Serialization: to_dict() / from_dict() for checkpoint storage                │
│  └── User-friendly display: format_user_display() with icons                      │
│                                                                                    │
│  ✅ EVENT BUS                                                                      │
│  ├── Pub/sub: subscribe(EventType, handler), publish(event)                       │
│  ├── Multiple subscribers per event type                                          │
│  ├── Event history: get_event_history()                                           │
│  └── Unsubscribe support                                                          │
│                                                                                    │
│  ✅ WORKFLOW GRAPH                                                                 │
│  ├── WorkflowNode, WorkflowEdge, WorkflowGraph                                    │
│  ├── get_outgoing_edges() for traversal                                           │
│  └── NodeManager: get_mdp_state, update_mdp_state                                 │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

### 0.2 FileTracker - Verified Capabilities

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                    FILETRACKER - TESTED & VERIFIED (40+ tests)                      │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  ✅ CONTENT-ADDRESSABLE STORAGE                                                    │
│  ├── SHA-256 hashing with automatic deduplication                                 │
│  ├── Storage: objects/{hash[:2]}/{hash[2:]} (Git-like sharding)                   │
│  ├── BlobORM: save, get, exists, delete                                           │
│  └── BlobRepository: save_file, restore, get_content                              │
│                                                                                    │
│  ✅ COMMIT & MEMENTO                                                               │
│  ├── Commit: commit_id (UUID), timestamp, message, mementos[]                     │
│  ├── FileMemento: file_path, file_hash, file_size (immutable)                     │
│  └── Multiple files per commit                                                    │
│                                                                                    │
│  ✅ BRANCHING SCENARIOS                                                            │
│  ├── Same path + different content → different hashes (both stored)               │
│  ├── Identical content across branches → deduplicated (same hash)                 │
│  └── All branch states independently restorable                                   │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

### 0.3 Critical Integration Patterns (Discovered During Testing)

#### Database Initialization Order (FK Constraints)

```sql
-- REQUIRED ORDER for AgentGit schema:
1. users              -- No dependencies
2. external_sessions  -- FK: users.id
3. internal_sessions  -- FK: external_sessions.id, checkpoints.id
4. checkpoints        -- FK: internal_sessions.id, users.id

-- Test fixtures must create: user(id=1), external_session(id=1)
```

#### Tool Execution Pattern (Verified)

```python
# ❌ WRONG: ToolManager does NOT have execute_tool()
# result = tool_manager.execute_tool("create_file", args)

# ✅ CORRECT: Use registry's ToolSpec.forward()
spec = tool_manager.tool_rollback_registry.get_tool("create_file")
result = spec.forward(args)
tool_manager.record_tool_invocation("create_file", args, result)
```

#### MDP State in Checkpoint Metadata (Verified)

```python
# AgentGit checkpoint.metadata structure (stored as JSON)
# Note: All checkpoints are at tool/message level; node boundaries are separate markers
checkpoint.metadata = {
    # Trigger info (what caused this checkpoint)
    "trigger_type": "tool_end",  # "tool_start", "tool_end", "llm_call", "user_request", "auto"
    "tool_name": "invoke_llm",   # If triggered by tool
    
    # Position tracking
    "node_id": "data_load",      # Which node this belongs to
    "tool_track_position": 5,    # Monotonic counter for ordering/rollback
    
    # MDP state
    "mdp_state": {
        "current_node_id": "data_load",
        "workflow_variables": {"input": "/path/to/data"},
        "execution_path": ["start", "load"]
    },
    
    # Optional: Store audit trail for complete recovery
    "audit_trail": {...}
}

# Node boundaries are stored in WTB's wtb_node_boundaries table (NOT in checkpoint metadata)
# node_boundary = {
#     "node_id": "data_load",
#     "entry_checkpoint_id": 3,  # First checkpoint in this node
#     "exit_checkpoint_id": 5,   # Last checkpoint (rollback target)
#     "node_status": "completed"
# }
```

#### Audit Trail + Checkpoint Integration (Verified)

```python
# Store audit trail with checkpoint
trail = AuditTrail(session_id=session.langgraph_session_id)
trail.add_event(AuditEvent(...))

checkpoint_repo.update_checkpoint_metadata(cp.id, {
    **cp.metadata,
    "audit_trail": trail.to_dict()
})

# Recover on rollback
restored_trail = AuditTrail.from_dict(checkpoint.metadata["audit_trail"])
print(restored_trail.format_user_display())  # User-friendly output
```

### 0.4 WTB Integration Recommendations (Based on Test Findings)

| Component | Recommendation | Rationale |
|-----------|---------------|-----------|
| **Checkpoint Granularity** | Unified tool-level; node boundaries as markers | No data duplication; clean separation of concerns |
| **State Adapter** | Anti-Corruption Layer (Option B) | AgentGit unchanged; WTB owns semantic layer |
| **Database Strategy** | Two databases: AgentGit + WTB | Cross-database refs via stored IDs; proper abstraction |
| **Event Bus** | Extend AgentGit's `EventBus` | Verified pub/sub, history, unsubscribe work correctly |
| **Audit Integration** | Store in `checkpoint.metadata["audit_trail"]` | Enables full audit recovery on rollback |
| **Session Model** | Use `ExternalSession` as WTB entry | Already tracks branches, checkpoints, internal sessions |
| **Tool Rollback** | Follow `ToolSpec(forward, reverse)` pattern | Verified rollback/redo work correctly |
| **File State** | Link via `wtb_checkpoint_files` table | WTB owns the link; FileTracker unchanged |

### 0.5 Database Architecture (Option B - Anti-Corruption Layer)

> **See `docs/DATABASE_DESIGN.md` for complete schema and implementation details.**

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                     TWO-DATABASE ARCHITECTURE (Option B)                         │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│   AgentGit Database (UNCHANGED)              WTB Database (NEW)                 │
│   ════════════════════════════              ═══════════════════                 │
│   SQLite: agentgit.db                        SQLite/PostgreSQL: wtb.db          │
│                                                                                  │
│   ┌─────────────────────────┐               ┌─────────────────────────┐         │
│   │ users                   │               │ wtb_workflows           │         │
│   │ external_sessions       │               │ wtb_executions          │         │
│   │ internal_sessions       │◄──logical FK──│ wtb_node_boundaries     │         │
│   │ checkpoints             │◄──────────────│ wtb_checkpoint_files    │         │
│   └─────────────────────────┘               │ wtb_evaluation_results  │         │
│                                             └─────────────────────────┘         │
│                                                       │                         │
│                                                       │ logical FK              │
│                                                       ▼                         │
│                                             ┌─────────────────────────┐         │
│                                             │ FileTracker (PostgreSQL)│         │
│                                             │ commits, file_blobs,    │         │
│                                             │ file_mementos           │         │
│                                             └─────────────────────────┘         │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

#### Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **AgentGit modification** | None (unchanged) | No breaking changes to existing package |
| **Node boundaries** | Separate `wtb_node_boundaries` table | Markers pointing to checkpoints, not duplicate data |
| **File links** | Separate `wtb_checkpoint_files` table | Bridge AgentGit checkpoints to FileTracker commits |
| **Abstraction pattern** | Repository + Unit of Work | Testable, transaction-safe, follows DDD |
| **ORM for WTB** | SQLAlchemy | Modern ORM for new tables; AgentGit uses raw SQL |

#### Abstraction Layers

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│  Application Layer                                                              │
│  ├── ExecutionController, NodeReplacer, EvaluationEngine                        │
│  └── Uses IStateAdapter interface (knows nothing about databases)               │
├─────────────────────────────────────────────────────────────────────────────────┤
│  Domain Layer (Anti-Corruption)                                                 │
│  ├── AgentGitStateAdapter: Translates WTB ↔ AgentGit concepts                  │
│  ├── Uses AgentGit's existing domain objects (Checkpoint, InternalSession)      │
│  └── Uses WTB's own repositories for node_boundaries, checkpoint_files         │
├─────────────────────────────────────────────────────────────────────────────────┤
│  Infrastructure Layer                                                           │
│  ├── Unit of Work: Transaction boundaries across repositories                  │
│  ├── Repositories: INodeBoundaryRepository, ICheckpointFileRepository          │
│  └── SQLAlchemy ORM models for WTB tables                                       │
├─────────────────────────────────────────────────────────────────────────────────┤
│  Data Access Layer                                                              │
│  ├── agentgit.db (SQLite) - managed by AgentGit package                        │
│  ├── wtb.db (SQLite/PostgreSQL) - managed by WTB                               │
│  └── filetracker (PostgreSQL) - managed by FileTracker package                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. Domain-Driven Design (DDD) Analysis

### Is DDD Suitable? ✅ YES

**Justification:**

1. **Complex Domain Logic**: Workflow execution, branching, rollback, and evaluation involve rich business rules
2. **Multiple Bounded Contexts**: WTB, AgentGit, FileTracker are distinct domains with clear boundaries
3. **Ubiquitous Language**: Clear domain vocabulary (Workflow, Node, Checkpoint, Branch, Evaluation)
4. **Strategic Value**: Core domain differentiator requiring long-term investment

### Bounded Contexts

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              BOUNDED CONTEXTS                                        │
├─────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                      │
│  ┌────────────────────────────┐    ┌────────────────────────────┐                   │
│  │   WTB Context (Core)       │    │   AgentGit Context         │                   │
│  │   ═══════════════════      │    │   ═══════════════════      │                   │
│  │                            │    │                            │                   │
│  │  Aggregates:               │    │  Aggregates:               │                   │
│  │  • TestWorkflow            │◄───┤  • ExternalSession         │                   │
│  │  • Execution               │    │  • InternalSession         │                   │
│  │  • NodeVariant             │    │  • Checkpoint              │                   │
│  │  • EvaluationResult        │    │                            │                   │
│  │  • BatchTest               │    │  Domain Services:          │                   │
│  │                            │    │  • CheckpointManager       │                   │
│  │  Domain Services:          │    │  • ToolManager             │                   │
│  │  • ExecutionController     │    │  • NodeManager             │                   │
│  │  • NodeReplacer            │    │                            │                   │
│  │  • EvaluationEngine        │    │                            │                   │
│  │  • BatchTestRunner         │    │                            │                   │
│  └─────────────┬──────────────┘    └────────────────────────────┘                   │
│                │                                                                     │
│                │ Anti-Corruption Layer                                              │
│                ▼                                                                     │
│  ┌────────────────────────────┐    ┌────────────────────────────┐                   │
│  │   FileTracker Context      │    │   Audit Context            │                   │
│  │   ═══════════════════      │    │   ═══════════════════      │                   │
│  │                            │    │                            │                   │
│  │  Aggregates:               │    │  Aggregates:               │                   │
│  │  • Commit                  │    │  • AuditSession            │                   │
│  │  • FileMemento             │    │  • AuditEvent              │                   │
│  │  • Blob                    │    │                            │                   │
│  └────────────────────────────┘    └────────────────────────────┘                   │
│                                                                                      │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. SOLID Principles Application

### Single Responsibility Principle (SRP)

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                         RESPONSIBILITY SEPARATION                                   │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  ExecutionController        → Controls workflow execution lifecycle               │
│  NodeReplacer               → Manages node variant substitution                   │
│  EvaluationEngine           → Computes metrics and scores                         │
│  BatchTestRunner            → Orchestrates parallel test runs                     │
│  StateAdapter               → Abstracts state persistence                         │
│  WorkflowParser             → Parses workflow definitions                         │
│                                                                                    │
│  Repository (each)          → CRUD for specific aggregate                         │
│  UnitOfWork                  → Transaction boundary management                    │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

### Open/Closed Principle (OCP)

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                         EXTENSION POINTS                                            │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  IStateAdapter                                                                     │
│  ├── InMemoryStateAdapter      (for testing)                                      │
│  ├── AgentGitStateAdapter      (production - AgentGit integration)                │
│  └── CustomStateAdapter        (future extensibility)                             │
│                                                                                    │
│  IEvaluator                                                                        │
│  ├── AccuracyEvaluator                                                            │
│  ├── LatencyEvaluator                                                             │
│  ├── CostEvaluator                                                                │
│  └── CustomEvaluator           (plugin system)                                    │
│                                                                                    │
│  INodeExecutor                                                                     │
│  ├── DefaultNodeExecutor                                                          │
│  ├── MockNodeExecutor          (for testing)                                      │
│  └── RemoteNodeExecutor        (distributed execution)                            │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

### Liskov Substitution Principle (LSP)

```python
# All adapters must be substitutable without breaking the system
class IStateAdapter(Protocol):
    def save_snapshot(self, state: ExecutionState) -> str: ...
    def load_snapshot(self, snapshot_id: str) -> ExecutionState: ...
    def create_branch(self, from_snapshot_id: str) -> str: ...
    def rollback(self, to_snapshot_id: str) -> ExecutionState: ...
```

### Interface Segregation Principle (ISP)

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                         SEGREGATED INTERFACES                                       │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  IExecutionController                                                              │
│  ├── IExecutionRunner         (run, pause, resume, stop)                          │
│  └── IExecutionInspector      (get_status, get_state)                             │
│                                                                                    │
│  INodeReplacer                                                                     │
│  ├── IVariantRegistry         (register, get, list)                               │
│  └── INodeSwapper             (swap_node, restore_original)                       │
│                                                                                    │
│  IRepository<T>                                                                    │
│  ├── IReadRepository<T>       (get, list, find)                                   │
│  └── IWriteRepository<T>      (create, update, delete)                            │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

### Dependency Inversion Principle (DIP)

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                         DEPENDENCY DIRECTION                                        │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  High-Level Modules                     Low-Level Modules                          │
│  ───────────────────                    ─────────────────                          │
│                                                                                    │
│  ExecutionController ────────────────►  IStateAdapter (interface)                 │
│         ▲                                      │                                   │
│         │                                      ▼                                   │
│         │                              AgentGitStateAdapter                        │
│         │                              InMemoryStateAdapter                        │
│         │                                                                          │
│  NodeReplacer ───────────────────────►  IVariantRegistry (interface)              │
│         ▲                                      │                                   │
│         │                                      ▼                                   │
│         │                              SQLVariantRegistry                          │
│         │                              InMemoryVariantRegistry                     │
│                                                                                    │
│  Both depend on abstractions, not concretions                                     │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Database Architecture Design

### Decision: Use SQLAlchemy with Optional Unit of Work

**Rationale:**

1. Current AgentGit uses raw SQLite - works but limits portability
2. FileTracker uses PostgreSQL with raw SQL - inconsistent
3. SQLAlchemy provides:
   - Database-agnostic ORM
   - Connection pooling
   - Transaction management
   - Migration support (Alembic)

### Pattern: Repository + Unit of Work

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                         DATABASE ABSTRACTION LAYERS                                 │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  Application Layer                                                                 │
│  ─────────────────                                                                 │
│       │                                                                            │
│       ▼                                                                            │
│  ┌─────────────────────────────────────────────────────────────────────────────┐  │
│  │                        Unit of Work (IUnitOfWork)                            │  │
│  │  ┌─────────────────────────────────────────────────────────────────────────┐│  │
│  │  │  • Begin transaction                                                     ││  │
│  │  │  • Commit transaction                                                    ││  │
│  │  │  • Rollback transaction                                                  ││  │
│  │  │  • Provides access to repositories                                       ││  │
│  │  └─────────────────────────────────────────────────────────────────────────┘│  │
│  └───────────────────────────────────┬─────────────────────────────────────────┘  │
│                                      │                                             │
│                                      ▼                                             │
│  ┌─────────────────────────────────────────────────────────────────────────────┐  │
│  │                        Repositories (IRepository<T>)                         │  │
│  │  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐               │  │
│  │  │ WorkflowRepo    │ │ ExecutionRepo   │ │ VariantRepo     │               │  │
│  │  │                 │ │                 │ │                 │               │  │
│  │  │ • add()         │ │ • add()         │ │ • add()         │               │  │
│  │  │ • get()         │ │ • get()         │ │ • get()         │               │  │
│  │  │ • update()      │ │ • update()      │ │ • update()      │               │  │
│  │  │ • delete()      │ │ • delete()      │ │ • delete()      │               │  │
│  │  │ • query()       │ │ • query()       │ │ • query()       │               │  │
│  │  └─────────────────┘ └─────────────────┘ └─────────────────┘               │  │
│  └───────────────────────────────────┬─────────────────────────────────────────┘  │
│                                      │                                             │
│                                      ▼                                             │
│  ┌─────────────────────────────────────────────────────────────────────────────┐  │
│  │                        SQLAlchemy Session                                    │  │
│  │  ┌─────────────────────────────────────────────────────────────────────────┐│  │
│  │  │  • Object-Relational Mapping                                             ││  │
│  │  │  • Query building                                                        ││  │
│  │  │  • Change tracking (Identity Map)                                        ││  │
│  │  │  • Lazy loading                                                          ││  │
│  │  └─────────────────────────────────────────────────────────────────────────┘│  │
│  └───────────────────────────────────┬─────────────────────────────────────────┘  │
│                                      │                                             │
│                                      ▼                                             │
│  ┌─────────────────────────────────────────────────────────────────────────────┐  │
│  │                        Database (SQLite / PostgreSQL)                        │  │
│  └─────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

### Unit of Work Decision Matrix

| Scenario                   | Use UoW?      | Justification                             |
| -------------------------- | ------------- | ----------------------------------------- |
| Multi-aggregate operations | ✅ Yes        | Ensures atomicity across aggregates       |
| Single aggregate CRUD      | ❌ No         | Repository suffices                       |
| Batch test creation        | ✅ Yes        | Workflow + Variants + Execution in one TX |
| Checkpoint creation        | ⚠️ Optional | AgentGit already handles                  |
| Read-only queries          | ❌ No         | No transaction needed                     |

### SQLAlchemy Models

```python
# wtb/infrastructure/database/models.py

from sqlalchemy import Column, Integer, String, DateTime, JSON, ForeignKey, Enum, Text
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime
import enum

Base = declarative_base()

class ExecutionStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class WorkflowModel(Base):
    """SQLAlchemy model for TestWorkflow aggregate."""
    __tablename__ = "wtb_workflows"
  
    id = Column(String(36), primary_key=True)  # UUID
    name = Column(String(255), nullable=False)
    description = Column(Text)
    definition = Column(JSON, nullable=False)  # Workflow graph as JSON
    version = Column(String(50), default="1.0.0")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    metadata_ = Column("metadata", JSON, default=dict)
  
    # Relationships
    executions = relationship("ExecutionModel", back_populates="workflow")
    node_variants = relationship("NodeVariantModel", back_populates="workflow")

class ExecutionModel(Base):
    """SQLAlchemy model for Execution aggregate."""
    __tablename__ = "wtb_executions"
  
    id = Column(String(36), primary_key=True)
    workflow_id = Column(String(36), ForeignKey("wtb_workflows.id"), nullable=False)
    status = Column(Enum(ExecutionStatus), default=ExecutionStatus.PENDING)
    current_node_id = Column(String(255))
  
    # State tracking
    initial_state = Column(JSON, default=dict)
    current_state = Column(JSON, default=dict)
    execution_path = Column(JSON, default=list)  # List of node IDs executed
  
    # AgentGit integration
    agentgit_session_id = Column(Integer)  # FK to AgentGit internal_sessions
    agentgit_checkpoint_id = Column(Integer)  # Latest checkpoint
  
    # Timing
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
  
    # Error handling
    error_message = Column(Text)
    error_node_id = Column(String(255))
  
    # Relationships
    workflow = relationship("WorkflowModel", back_populates="executions")
    evaluation_results = relationship("EvaluationResultModel", back_populates="execution")
  
    # Breakpoints
    breakpoints = Column(JSON, default=list)  # List of node IDs to pause at

class NodeVariantModel(Base):
    """SQLAlchemy model for NodeVariant aggregate."""
    __tablename__ = "wtb_node_variants"
  
    id = Column(String(36), primary_key=True)
    workflow_id = Column(String(36), ForeignKey("wtb_workflows.id"), nullable=False)
    original_node_id = Column(String(255), nullable=False)
    variant_name = Column(String(255), nullable=False)
    variant_definition = Column(JSON, nullable=False)  # Node definition
    description = Column(Text)
    is_active = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
  
    # Relationships
    workflow = relationship("WorkflowModel", back_populates="node_variants")

class BatchTestModel(Base):
    """SQLAlchemy model for BatchTest aggregate."""
    __tablename__ = "wtb_batch_tests"
  
    id = Column(String(36), primary_key=True)
    name = Column(String(255), nullable=False)
    workflow_id = Column(String(36), ForeignKey("wtb_workflows.id"), nullable=False)
    variant_combinations = Column(JSON, nullable=False)  # List of variant configs
    parallel_count = Column(Integer, default=1)
    status = Column(Enum(ExecutionStatus), default=ExecutionStatus.PENDING)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)
  
    # Results
    execution_ids = Column(JSON, default=list)  # List of execution IDs
    comparison_matrix = Column(JSON)

class EvaluationResultModel(Base):
    """SQLAlchemy model for EvaluationResult."""
    __tablename__ = "wtb_evaluation_results"
  
    id = Column(String(36), primary_key=True)
    execution_id = Column(String(36), ForeignKey("wtb_executions.id"), nullable=False)
    evaluator_name = Column(String(255), nullable=False)
    score = Column(Integer)  # 0-100 or custom range
    metrics = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
  
    # Relationships
    execution = relationship("ExecutionModel", back_populates="evaluation_results")
```

---

## 4. Domain Models (Entities & Value Objects)

```python
# wtb/domain/models/workflow.py

from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
from datetime import datetime
from enum import Enum
import uuid

class ExecutionStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class WorkflowNode:
    """Value Object - Immutable workflow node definition."""
    id: str
    name: str
    type: str  # 'action', 'decision', 'start', 'end'
    tool_name: Optional[str] = None
    config: Dict[str, Any] = field(default_factory=dict)
  
    def __hash__(self):
        return hash(self.id)

@dataclass
class WorkflowEdge:
    """Value Object - Immutable edge between nodes."""
    source_id: str
    target_id: str
    condition: Optional[str] = None  # Condition expression or tool name
    priority: int = 0

@dataclass
class TestWorkflow:
    """Aggregate Root - Workflow definition."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""
    nodes: Dict[str, WorkflowNode] = field(default_factory=dict)
    edges: List[WorkflowEdge] = field(default_factory=list)
    entry_point: Optional[str] = None
    version: str = "1.0.0"
    created_at: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
  
    def add_node(self, node: WorkflowNode):
        self.nodes[node.id] = node
        if self.entry_point is None and node.type == "start":
            self.entry_point = node.id
  
    def add_edge(self, edge: WorkflowEdge):
        self.edges.append(edge)
  
    def get_outgoing_edges(self, node_id: str) -> List[WorkflowEdge]:
        return sorted(
            [e for e in self.edges if e.source_id == node_id],
            key=lambda e: e.priority,
            reverse=True
        )
  
    def validate(self) -> List[str]:
        """Validate workflow structure. Returns list of errors."""
        errors = []
        if not self.entry_point:
            errors.append("No entry point defined")
        if self.entry_point and self.entry_point not in self.nodes:
            errors.append(f"Entry point '{self.entry_point}' not found in nodes")
        # More validation...
        return errors

@dataclass
class ExecutionState:
    """Value Object - Snapshot of execution state."""
    current_node_id: Optional[str] = None
    workflow_variables: Dict[str, Any] = field(default_factory=dict)
    execution_path: List[str] = field(default_factory=list)
    node_results: Dict[str, Any] = field(default_factory=dict)
  
    def clone(self) -> "ExecutionState":
        return ExecutionState(
            current_node_id=self.current_node_id,
            workflow_variables=dict(self.workflow_variables),
            execution_path=list(self.execution_path),
            node_results=dict(self.node_results)
        )

@dataclass
class Execution:
    """Aggregate Root - Workflow execution instance."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str = ""
    status: ExecutionStatus = ExecutionStatus.PENDING
    state: ExecutionState = field(default_factory=ExecutionState)
  
    # AgentGit integration
    agentgit_session_id: Optional[int] = None
    agentgit_checkpoint_id: Optional[int] = None
  
    # Timing
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.now)
  
    # Error handling
    error_message: Optional[str] = None
    error_node_id: Optional[str] = None
  
    # Breakpoints
    breakpoints: List[str] = field(default_factory=list)
  
    def can_run(self) -> bool:
        return self.status in [ExecutionStatus.PENDING, ExecutionStatus.PAUSED]
  
    def can_pause(self) -> bool:
        return self.status == ExecutionStatus.RUNNING
  
    def can_resume(self) -> bool:
        return self.status == ExecutionStatus.PAUSED
  
    def start(self):
        if not self.can_run():
            raise ValueError(f"Cannot start execution in status {self.status}")
        self.status = ExecutionStatus.RUNNING
        self.started_at = datetime.now()
  
    def pause(self):
        if not self.can_pause():
            raise ValueError(f"Cannot pause execution in status {self.status}")
        self.status = ExecutionStatus.PAUSED
  
    def resume(self):
        if not self.can_resume():
            raise ValueError(f"Cannot resume execution in status {self.status}")
        self.status = ExecutionStatus.RUNNING
  
    def complete(self):
        self.status = ExecutionStatus.COMPLETED
        self.completed_at = datetime.now()
  
    def fail(self, error_message: str, node_id: Optional[str] = None):
        self.status = ExecutionStatus.FAILED
        self.error_message = error_message
        self.error_node_id = node_id
        self.completed_at = datetime.now()

@dataclass
class NodeVariant:
    """Aggregate Root - Node variant for A/B testing."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str = ""
    original_node_id: str = ""
    variant_name: str = ""
    variant_node: WorkflowNode = field(default_factory=lambda: WorkflowNode(id="", name="", type="action"))
    description: str = ""
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.now)
```

---

## 5. Execution Controller Design

### Interface Definition

```python
# wtb/domain/interfaces/execution_controller.py

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from ..models.workflow import Execution, ExecutionState, TestWorkflow

class IExecutionController(ABC):
    """Interface for controlling workflow execution lifecycle."""
  
    @abstractmethod
    def create_execution(
        self, 
        workflow: TestWorkflow,
        initial_state: Optional[Dict[str, Any]] = None,
        breakpoints: Optional[List[str]] = None
    ) -> Execution:
        """Create a new execution for a workflow."""
        pass
  
    @abstractmethod
    def run(self, execution_id: str) -> Execution:
        """Start or continue execution."""
        pass
  
    @abstractmethod
    def pause(self, execution_id: str) -> Execution:
        """Pause execution at current position."""
        pass
  
    @abstractmethod
    def resume(self, execution_id: str, modified_state: Optional[Dict[str, Any]] = None) -> Execution:
        """Resume paused execution, optionally with modified state."""
        pass
  
    @abstractmethod
    def stop(self, execution_id: str) -> Execution:
        """Stop and cancel execution."""
        pass
  
    @abstractmethod
    def rollback(self, execution_id: str, checkpoint_id: int) -> Execution:
        """Rollback execution to a previous checkpoint."""
        pass
  
    @abstractmethod
    def get_state(self, execution_id: str) -> ExecutionState:
        """Get current execution state."""
        pass
  
    @abstractmethod
    def get_status(self, execution_id: str) -> Execution:
        """Get execution with current status."""
        pass
```

### Implementation

```python
# wtb/application/services/execution_controller.py

from typing import Optional, Dict, Any, List
from datetime import datetime

from ...domain.interfaces.execution_controller import IExecutionController
from ...domain.interfaces.state_adapter import IStateAdapter
from ...domain.interfaces.node_executor import INodeExecutor
from ...domain.interfaces.repositories import IExecutionRepository
from ...domain.models.workflow import (
    Execution, ExecutionState, ExecutionStatus, TestWorkflow
)
from ...domain.events import ExecutionStarted, ExecutionPaused, ExecutionCompleted, NodeExecuted

class ExecutionController(IExecutionController):
    """
    Orchestrates workflow execution with support for:
    - Run/Pause/Resume/Stop lifecycle
    - Breakpoint handling
    - State persistence via adapters
    - Rollback capabilities
    """
  
    def __init__(
        self,
        execution_repository: IExecutionRepository,
        workflow_repository: "IWorkflowRepository",
        state_adapter: IStateAdapter,
        node_executor: INodeExecutor,
        event_publisher: "IEventPublisher"
    ):
        self._exec_repo = execution_repository
        self._workflow_repo = workflow_repository
        self._state_adapter = state_adapter
        self._node_executor = node_executor
        self._event_publisher = event_publisher
  
    def create_execution(
        self, 
        workflow: TestWorkflow,
        initial_state: Optional[Dict[str, Any]] = None,
        breakpoints: Optional[List[str]] = None
    ) -> Execution:
        """Create a new execution for a workflow."""
        # Validate workflow
        errors = workflow.validate()
        if errors:
            raise ValueError(f"Invalid workflow: {', '.join(errors)}")
      
        # Create execution
        execution = Execution(
            workflow_id=workflow.id,
            status=ExecutionStatus.PENDING,
            state=ExecutionState(
                current_node_id=workflow.entry_point,
                workflow_variables=initial_state or {}
            ),
            breakpoints=breakpoints or []
        )
      
        # Initialize state adapter (creates AgentGit session if applicable)
        adapter_session_id = self._state_adapter.initialize_session(
            execution_id=execution.id,
            initial_state=execution.state
        )
        execution.agentgit_session_id = adapter_session_id
      
        # Persist
        self._exec_repo.add(execution)
      
        return execution
  
    def run(self, execution_id: str) -> Execution:
        """Start or continue execution."""
        execution = self._exec_repo.get(execution_id)
        if not execution:
            raise ValueError(f"Execution {execution_id} not found")
      
        workflow = self._workflow_repo.get(execution.workflow_id)
        if not workflow:
            raise ValueError(f"Workflow {execution.workflow_id} not found")
      
        # Start execution
        execution.start()
        self._event_publisher.publish(ExecutionStarted(execution_id=execution.id))
      
        # Main execution loop
        try:
            while execution.status == ExecutionStatus.RUNNING:
                current_node_id = execution.state.current_node_id
              
                if not current_node_id:
                    # No more nodes - execution complete
                    execution.complete()
                    break
              
                # Check for breakpoint
                if current_node_id in execution.breakpoints:
                    execution.pause()
                    self._create_checkpoint(execution, f"Breakpoint: {current_node_id}")
                    break
              
                # Get node
                node = workflow.nodes.get(current_node_id)
                if not node:
                    raise ValueError(f"Node {current_node_id} not found")
              
                # Create checkpoint before node execution
                self._create_checkpoint(execution, f"Before: {current_node_id}")
              
                # Execute node
                result = self._node_executor.execute(
                    node=node,
                    context=execution.state.workflow_variables
                )
              
                # Update state
                execution.state.execution_path.append(current_node_id)
                execution.state.node_results[current_node_id] = result
              
                if isinstance(result, dict):
                    execution.state.workflow_variables.update(result)
              
                # Publish event
                self._event_publisher.publish(NodeExecuted(
                    execution_id=execution.id,
                    node_id=current_node_id,
                    result=result
                ))
              
                # Transition to next node
                next_node_id = self._determine_next_node(workflow, execution, result)
                execution.state.current_node_id = next_node_id
              
                # Save state
                self._state_adapter.save_snapshot(execution.state)
      
        except Exception as e:
            execution.fail(str(e), execution.state.current_node_id)
            self._event_publisher.publish(ExecutionFailed(
                execution_id=execution.id,
                error=str(e)
            ))
      
        # Persist final state
        self._exec_repo.update(execution)
      
        if execution.status == ExecutionStatus.COMPLETED:
            self._event_publisher.publish(ExecutionCompleted(execution_id=execution.id))
      
        return execution
  
    def pause(self, execution_id: str) -> Execution:
        """Pause execution at current position."""
        execution = self._exec_repo.get(execution_id)
        if not execution:
            raise ValueError(f"Execution {execution_id} not found")
      
        execution.pause()
        self._create_checkpoint(execution, "Manual Pause")
        self._exec_repo.update(execution)
      
        self._event_publisher.publish(ExecutionPaused(execution_id=execution.id))
      
        return execution
  
    def resume(
        self, 
        execution_id: str, 
        modified_state: Optional[Dict[str, Any]] = None
    ) -> Execution:
        """Resume paused execution, optionally with modified state."""
        execution = self._exec_repo.get(execution_id)
        if not execution:
            raise ValueError(f"Execution {execution_id} not found")
      
        if modified_state:
            execution.state.workflow_variables.update(modified_state)
      
        execution.resume()
        self._exec_repo.update(execution)
      
        # Continue execution
        return self.run(execution_id)
  
    def stop(self, execution_id: str) -> Execution:
        """Stop and cancel execution."""
        execution = self._exec_repo.get(execution_id)
        if not execution:
            raise ValueError(f"Execution {execution_id} not found")
      
        execution.status = ExecutionStatus.CANCELLED
        execution.completed_at = datetime.now()
        self._exec_repo.update(execution)
      
        return execution
  
    def rollback(self, execution_id: str, checkpoint_id: int) -> Execution:
        """Rollback execution to a previous checkpoint."""
        execution = self._exec_repo.get(execution_id)
        if not execution:
            raise ValueError(f"Execution {execution_id} not found")
      
        # Use state adapter to perform rollback
        restored_state = self._state_adapter.rollback(checkpoint_id)
      
        # Update execution state
        execution.state = restored_state
        execution.status = ExecutionStatus.PAUSED
        execution.agentgit_checkpoint_id = checkpoint_id
      
        self._exec_repo.update(execution)
      
        return execution
  
    def get_state(self, execution_id: str) -> ExecutionState:
        """Get current execution state."""
        execution = self._exec_repo.get(execution_id)
        if not execution:
            raise ValueError(f"Execution {execution_id} not found")
        return execution.state
  
    def get_status(self, execution_id: str) -> Execution:
        """Get execution with current status."""
        execution = self._exec_repo.get(execution_id)
        if not execution:
            raise ValueError(f"Execution {execution_id} not found")
        return execution
  
    def _create_checkpoint(self, execution: Execution, name: str):
        """Create a checkpoint via state adapter."""
        checkpoint_id = self._state_adapter.save_snapshot(
            state=execution.state,
            name=name,
            metadata={
                "execution_id": execution.id,
                "node_id": execution.state.current_node_id
            }
        )
        execution.agentgit_checkpoint_id = checkpoint_id
  
    def _determine_next_node(
        self, 
        workflow: TestWorkflow,
        execution: Execution,
        last_result: Any
    ) -> Optional[str]:
        """Determine the next node based on edges and conditions."""
        current_node_id = execution.state.current_node_id
        edges = workflow.get_outgoing_edges(current_node_id)
      
        for edge in edges:
            if edge.condition is None:
                # Unconditional edge
                return edge.target_id
          
            # Evaluate condition
            # This could use the node_executor to run condition tools
            condition_result = self._evaluate_condition(
                edge.condition,
                execution.state.workflow_variables,
                last_result
            )
          
            if condition_result:
                return edge.target_id
      
        return None  # No outgoing edge found - end of workflow
  
    def _evaluate_condition(
        self, 
        condition: str,
        variables: Dict[str, Any],
        last_result: Any
    ) -> bool:
        """Evaluate an edge condition."""
        # Simple implementation - can be extended
        context = {**variables, "_last_result": last_result}
        try:
            return bool(eval(condition, {"__builtins__": {}}, context))
        except:
            return False
```

---

## 6. Node Replacer Design

### Interface Definition

```python
# wtb/domain/interfaces/node_replacer.py

from abc import ABC, abstractmethod
from typing import Optional, List, Dict
from ..models.workflow import NodeVariant, WorkflowNode, TestWorkflow

class IVariantRegistry(ABC):
    """Interface for managing node variants."""
  
    @abstractmethod
    def register(self, variant: NodeVariant) -> NodeVariant:
        """Register a new node variant."""
        pass
  
    @abstractmethod
    def get(self, variant_id: str) -> Optional[NodeVariant]:
        """Get a variant by ID."""
        pass
  
    @abstractmethod
    def get_by_node(self, workflow_id: str, node_id: str) -> List[NodeVariant]:
        """Get all variants for a specific node."""
        pass
  
    @abstractmethod
    def list_for_workflow(self, workflow_id: str) -> List[NodeVariant]:
        """List all variants for a workflow."""
        pass
  
    @abstractmethod
    def deactivate(self, variant_id: str) -> bool:
        """Deactivate a variant."""
        pass

class INodeSwapper(ABC):
    """Interface for swapping nodes at runtime."""
  
    @abstractmethod
    def swap_node(
        self, 
        workflow: TestWorkflow,
        original_node_id: str,
        variant: NodeVariant
    ) -> TestWorkflow:
        """Swap a node with its variant, returning modified workflow."""
        pass
  
    @abstractmethod
    def restore_original(
        self,
        workflow: TestWorkflow,
        node_id: str
    ) -> TestWorkflow:
        """Restore original node, returning modified workflow."""
        pass
  
    @abstractmethod
    def apply_variant_set(
        self,
        workflow: TestWorkflow,
        variant_set: Dict[str, str]  # node_id -> variant_id
    ) -> TestWorkflow:
        """Apply a set of variants to a workflow."""
        pass

class INodeReplacer(IVariantRegistry, INodeSwapper):
    """Combined interface for node replacement functionality."""
    pass
```

### Implementation

```python
# wtb/application/services/node_replacer.py

from typing import Optional, List, Dict
from copy import deepcopy

from ...domain.interfaces.node_replacer import INodeReplacer
from ...domain.interfaces.repositories import INodeVariantRepository
from ...domain.models.workflow import NodeVariant, WorkflowNode, TestWorkflow

class NodeReplacer(INodeReplacer):
    """
    Manages node variants for A/B testing and hot-swapping.
  
    Features:
    - Register and manage node variants
    - Hot-swap nodes in workflows
    - Apply variant sets for batch testing
    - Track original nodes for restoration
    """
  
    def __init__(self, variant_repository: INodeVariantRepository):
        self._variant_repo = variant_repository
        # Cache original nodes for restoration
        self._original_cache: Dict[str, Dict[str, WorkflowNode]] = {}
  
    # === IVariantRegistry Implementation ===
  
    def register(self, variant: NodeVariant) -> NodeVariant:
        """Register a new node variant."""
        # Validate variant
        if not variant.original_node_id:
            raise ValueError("Variant must specify original_node_id")
        if not variant.variant_name:
            raise ValueError("Variant must have a name")
      
        # Check for duplicates
        existing = self.get_by_node(variant.workflow_id, variant.original_node_id)
        for v in existing:
            if v.variant_name == variant.variant_name:
                raise ValueError(f"Variant '{variant.variant_name}' already exists for node {variant.original_node_id}")
      
        return self._variant_repo.add(variant)
  
    def get(self, variant_id: str) -> Optional[NodeVariant]:
        """Get a variant by ID."""
        return self._variant_repo.get(variant_id)
  
    def get_by_node(self, workflow_id: str, node_id: str) -> List[NodeVariant]:
        """Get all variants for a specific node."""
        return self._variant_repo.find_by_node(workflow_id, node_id)
  
    def list_for_workflow(self, workflow_id: str) -> List[NodeVariant]:
        """List all variants for a workflow."""
        return self._variant_repo.find_by_workflow(workflow_id)
  
    def deactivate(self, variant_id: str) -> bool:
        """Deactivate a variant."""
        variant = self._variant_repo.get(variant_id)
        if not variant:
            return False
        variant.is_active = False
        self._variant_repo.update(variant)
        return True
  
    # === INodeSwapper Implementation ===
  
    def swap_node(
        self, 
        workflow: TestWorkflow,
        original_node_id: str,
        variant: NodeVariant
    ) -> TestWorkflow:
        """Swap a node with its variant, returning modified workflow."""
        # Validate
        if original_node_id not in workflow.nodes:
            raise ValueError(f"Node {original_node_id} not found in workflow")
      
        # Cache original if not already cached
        cache_key = workflow.id
        if cache_key not in self._original_cache:
            self._original_cache[cache_key] = {}
      
        if original_node_id not in self._original_cache[cache_key]:
            self._original_cache[cache_key][original_node_id] = deepcopy(
                workflow.nodes[original_node_id]
            )
      
        # Create modified workflow (immutable pattern)
        modified = deepcopy(workflow)
      
        # Swap the node
        variant_node = deepcopy(variant.variant_node)
        variant_node.id = original_node_id  # Preserve original ID for edge connections
        modified.nodes[original_node_id] = variant_node
      
        return modified
  
    def restore_original(
        self,
        workflow: TestWorkflow,
        node_id: str
    ) -> TestWorkflow:
        """Restore original node, returning modified workflow."""
        cache_key = workflow.id
      
        if cache_key not in self._original_cache:
            raise ValueError(f"No cached originals for workflow {workflow.id}")
      
        if node_id not in self._original_cache[cache_key]:
            raise ValueError(f"Original node {node_id} not cached")
      
        # Create modified workflow
        modified = deepcopy(workflow)
        modified.nodes[node_id] = deepcopy(self._original_cache[cache_key][node_id])
      
        return modified
  
    def apply_variant_set(
        self,
        workflow: TestWorkflow,
        variant_set: Dict[str, str]  # node_id -> variant_id
    ) -> TestWorkflow:
        """Apply a set of variants to a workflow."""
        modified = deepcopy(workflow)
      
        for node_id, variant_id in variant_set.items():
            variant = self.get(variant_id)
            if not variant:
                raise ValueError(f"Variant {variant_id} not found")
            if variant.original_node_id != node_id:
                raise ValueError(f"Variant {variant_id} is for node {variant.original_node_id}, not {node_id}")
          
            modified = self.swap_node(modified, node_id, variant)
      
        return modified
  
    def create_variant_from_node(
        self,
        workflow: TestWorkflow,
        node_id: str,
        variant_name: str,
        modifications: Dict[str, any]
    ) -> NodeVariant:
        """Create a variant by modifying an existing node."""
        if node_id not in workflow.nodes:
            raise ValueError(f"Node {node_id} not found")
      
        original = workflow.nodes[node_id]
        variant_node = deepcopy(original)
      
        # Apply modifications
        for key, value in modifications.items():
            if hasattr(variant_node, key):
                setattr(variant_node, key, value)
            elif key in variant_node.config:
                variant_node.config[key] = value
      
        variant = NodeVariant(
            workflow_id=workflow.id,
            original_node_id=node_id,
            variant_name=variant_name,
            variant_node=variant_node
        )
      
        return self.register(variant)
```

---

## 7. State Adapter Interface (Anti-Corruption Layer)

```python
# wtb/domain/interfaces/state_adapter.py

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from ..models.workflow import ExecutionState

class IStateAdapter(ABC):
    """
    Anti-corruption layer between WTB and state persistence systems.
  
    Implementations:
    - InMemoryStateAdapter: For testing
    - AgentGitStateAdapter: Production integration with AgentGit
    - FileTrackerStateAdapter: For file-based state (optional)
    """
  
    @abstractmethod
    def initialize_session(
        self, 
        execution_id: str,
        initial_state: ExecutionState
    ) -> Optional[int]:
        """
        Initialize a new session for execution.
      
        Returns:
            Session ID from underlying system (e.g., AgentGit session ID)
        """
        pass
  
    @abstractmethod
    def save_snapshot(
        self,
        state: ExecutionState,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Save a state snapshot (checkpoint).
      
        Returns:
            Snapshot/Checkpoint ID
        """
        pass
  
    @abstractmethod
    def load_snapshot(self, snapshot_id: int) -> ExecutionState:
        """Load a state snapshot by ID."""
        pass
  
    @abstractmethod
    def create_branch(self, from_snapshot_id: int) -> int:
        """
        Create a new branch from a snapshot.
      
        Returns:
            New session ID for the branch
        """
        pass
  
    @abstractmethod
    def rollback(self, to_snapshot_id: int) -> ExecutionState:
        """
        Rollback to a specific snapshot.
      
        This may create a new branch depending on implementation.
      
        Returns:
            Restored execution state
        """
        pass
  
    @abstractmethod
    def get_snapshots(self, session_id: int) -> list:
        """Get all snapshots for a session."""
        pass
  
    @abstractmethod
    def cleanup(self, session_id: int, keep_latest: int = 5) -> int:
        """
        Cleanup old snapshots, keeping only the most recent.
      
        Returns:
            Number of snapshots removed
        """
        pass
```

### AgentGit Adapter Implementation

```python
# wtb/infrastructure/adapters/agentgit_adapter.py

from typing import Optional, Dict, Any, List

from agentgit.managers.checkpoint_manager_mdp import CheckpointManager_mdp
from agentgit.sessions.internal_session_mdp import InternalSession_mdp
from agentgit.database.repositories.checkpoint_repository import CheckpointRepository
from agentgit.database.repositories.internal_session_repository import InternalSessionRepository

from ...domain.interfaces.state_adapter import IStateAdapter
from ...domain.models.workflow import ExecutionState

class AgentGitStateAdapter(IStateAdapter):
    """
    Adapts AgentGit's checkpoint system to WTB's state management needs.
  
    Bridges:
    - WTB ExecutionState <-> AgentGit InternalSession_mdp
    - WTB Snapshots <-> AgentGit Checkpoints
    """
  
    def __init__(self, db_path: str, external_session_id: int = 1):
        self._db_path = db_path
        self._external_session_id = external_session_id
      
        # Initialize AgentGit components
        self._checkpoint_repo = CheckpointRepository(db_path)
        self._session_repo = InternalSessionRepository(db_path)
        self._checkpoint_manager = CheckpointManager_mdp(self._checkpoint_repo)
      
        # Current session reference
        self._current_session: Optional[InternalSession_mdp] = None
  
    def initialize_session(
        self, 
        execution_id: str,
        initial_state: ExecutionState
    ) -> Optional[int]:
        """Initialize a new AgentGit session for this execution."""
        session = InternalSession_mdp(
            external_session_id=self._external_session_id,
            current_node_id=initial_state.current_node_id,
            workflow_variables=dict(initial_state.workflow_variables),
            execution_path=list(initial_state.execution_path)
        )
      
        # Set metadata for WTB tracking
        session.metadata = {
            "wtb_execution_id": execution_id,
            "session_type": "wtb_execution"
        }
      
        # Persist
        self._current_session = self._session_repo.create(session)
      
        return self._current_session.id
  
    def save_snapshot(
        self,
        state: ExecutionState,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> int:
        """Save a checkpoint via AgentGit."""
        if not self._current_session:
            raise RuntimeError("No active session. Call initialize_session first.")
      
        # Update session state
        self._current_session.current_node_id = state.current_node_id
        self._current_session.workflow_variables = dict(state.workflow_variables)
        self._current_session.execution_path = list(state.execution_path)
        self._current_session.session_state["node_results"] = state.node_results
      
        self._session_repo.update(self._current_session)
      
        # Create checkpoint
        checkpoint = self._checkpoint_manager.create_node_checkpoint(
            internal_session=self._current_session,
            node_id=state.current_node_id or "unknown",
            workflow_version="1.0.0",
            tool_track_position=0
        )
      
        # Update checkpoint metadata with WTB info
        if checkpoint and metadata:
            checkpoint.metadata.update(metadata)
            self._checkpoint_repo.update_checkpoint_metadata(
                checkpoint.id, 
                checkpoint.metadata
            )
      
        return checkpoint.id if checkpoint else -1
  
    def load_snapshot(self, snapshot_id: int) -> ExecutionState:
        """Load a checkpoint as ExecutionState."""
        checkpoint = self._checkpoint_manager.get_checkpoint(snapshot_id)
        if not checkpoint:
            raise ValueError(f"Checkpoint {snapshot_id} not found")
      
        mdp_state = checkpoint.metadata.get("mdp_state", {})
      
        return ExecutionState(
            current_node_id=mdp_state.get("current_node_id"),
            workflow_variables=dict(mdp_state.get("workflow_variables", {})),
            execution_path=list(mdp_state.get("execution_path", [])),
            node_results=checkpoint.session_state.get("node_results", {})
        )
  
    def create_branch(self, from_snapshot_id: int) -> int:
        """Create a new session branched from a checkpoint."""
        checkpoint = self._checkpoint_manager.get_checkpoint(from_snapshot_id)
        if not checkpoint:
            raise ValueError(f"Checkpoint {from_snapshot_id} not found")
      
        # Create branched session
        branched = InternalSession_mdp.create_branch_from_checkpoint(
            checkpoint=checkpoint,
            external_session_id=self._external_session_id,
            parent_session_id=self._current_session.id if self._current_session else 0
        )
      
        # Persist branched session
        branched = self._session_repo.create(branched)
        self._current_session = branched
      
        return branched.id
  
    def rollback(self, to_snapshot_id: int) -> ExecutionState:
        """Rollback to a checkpoint, creating a new branch."""
        # Create branch from the checkpoint
        self.create_branch(to_snapshot_id)
      
        # Load and return the state
        return self.load_snapshot(to_snapshot_id)
  
    def get_snapshots(self, session_id: int) -> List[Dict[str, Any]]:
        """Get all checkpoints for a session."""
        checkpoints = self._checkpoint_repo.get_by_internal_session(session_id)
      
        return [
            {
                "id": cp.id,
                "name": cp.checkpoint_name,
                "created_at": cp.created_at.isoformat() if cp.created_at else None,
                "node_id": cp.metadata.get("node_id"),
                "is_auto": cp.is_auto
            }
            for cp in checkpoints
        ]
  
    def cleanup(self, session_id: int, keep_latest: int = 5) -> int:
        """Cleanup old auto-checkpoints."""
        return self._checkpoint_repo.delete_auto_checkpoints(
            session_id, 
            keep_latest=keep_latest
        )
```

---

## 8. Repository Interfaces and Unit of Work

```python
# wtb/domain/interfaces/repositories.py

from abc import ABC, abstractmethod
from typing import Optional, List, TypeVar, Generic

T = TypeVar('T')

class IReadRepository(ABC, Generic[T]):
    """Read-only repository interface."""
  
    @abstractmethod
    def get(self, id: str) -> Optional[T]:
        """Get entity by ID."""
        pass
  
    @abstractmethod
    def list(self, limit: int = 100, offset: int = 0) -> List[T]:
        """List entities with pagination."""
        pass
  
    @abstractmethod
    def exists(self, id: str) -> bool:
        """Check if entity exists."""
        pass

class IWriteRepository(ABC, Generic[T]):
    """Write repository interface."""
  
    @abstractmethod
    def add(self, entity: T) -> T:
        """Add a new entity."""
        pass
  
    @abstractmethod
    def update(self, entity: T) -> T:
        """Update an existing entity."""
        pass
  
    @abstractmethod
    def delete(self, id: str) -> bool:
        """Delete an entity."""
        pass

class IRepository(IReadRepository[T], IWriteRepository[T]):
    """Combined read/write repository interface."""
    pass

# Specific repository interfaces
class IWorkflowRepository(IRepository["TestWorkflow"]):
    """Repository for TestWorkflow aggregates."""
  
    @abstractmethod
    def find_by_name(self, name: str) -> Optional["TestWorkflow"]:
        """Find workflow by name."""
        pass

class IExecutionRepository(IRepository["Execution"]):
    """Repository for Execution aggregates."""
  
    @abstractmethod
    def find_by_workflow(self, workflow_id: str) -> List["Execution"]:
        """Find executions for a workflow."""
        pass
  
    @abstractmethod
    def find_by_status(self, status: "ExecutionStatus") -> List["Execution"]:
        """Find executions by status."""
        pass

class INodeVariantRepository(IRepository["NodeVariant"]):
    """Repository for NodeVariant aggregates."""
  
    @abstractmethod
    def find_by_workflow(self, workflow_id: str) -> List["NodeVariant"]:
        """Find variants for a workflow."""
        pass
  
    @abstractmethod
    def find_by_node(self, workflow_id: str, node_id: str) -> List["NodeVariant"]:
        """Find variants for a specific node."""
        pass

class IBatchTestRepository(IRepository["BatchTest"]):
    """Repository for BatchTest aggregates."""
    pass

class IEvaluationResultRepository(IRepository["EvaluationResult"]):
    """Repository for EvaluationResult entities."""
  
    @abstractmethod
    def find_by_execution(self, execution_id: str) -> List["EvaluationResult"]:
        """Find results for an execution."""
        pass
```

### Unit of Work

```python
# wtb/domain/interfaces/unit_of_work.py

from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Generator

from .repositories import (
    IWorkflowRepository,
    IExecutionRepository,
    INodeVariantRepository,
    IBatchTestRepository,
    IEvaluationResultRepository
)

class IUnitOfWork(ABC):
    """
    Unit of Work pattern interface.
  
    Manages transaction boundaries and provides access to repositories.
    Use when operations span multiple aggregates.
    """
  
    workflows: IWorkflowRepository
    executions: IExecutionRepository
    variants: INodeVariantRepository
    batch_tests: IBatchTestRepository
    evaluation_results: IEvaluationResultRepository
  
    @abstractmethod
    def __enter__(self) -> "IUnitOfWork":
        """Begin transaction."""
        pass
  
    @abstractmethod
    def __exit__(self, exc_type, exc_val, exc_tb):
        """End transaction (rollback on exception)."""
        pass
  
    @abstractmethod
    def commit(self):
        """Commit the transaction."""
        pass
  
    @abstractmethod
    def rollback(self):
        """Rollback the transaction."""
        pass
```

### SQLAlchemy Unit of Work Implementation

```python
# wtb/infrastructure/database/unit_of_work.py

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from ...domain.interfaces.unit_of_work import IUnitOfWork
from .repositories import (
    SQLWorkflowRepository,
    SQLExecutionRepository,
    SQLNodeVariantRepository,
    SQLBatchTestRepository,
    SQLEvaluationResultRepository
)

class SQLAlchemyUnitOfWork(IUnitOfWork):
    """SQLAlchemy-based Unit of Work implementation."""
  
    def __init__(self, connection_string: str):
        self._engine = create_engine(connection_string)
        self._session_factory = sessionmaker(bind=self._engine)
        self._session: Session = None
  
    def __enter__(self) -> "SQLAlchemyUnitOfWork":
        self._session = self._session_factory()
      
        # Initialize repositories with shared session
        self.workflows = SQLWorkflowRepository(self._session)
        self.executions = SQLExecutionRepository(self._session)
        self.variants = SQLNodeVariantRepository(self._session)
        self.batch_tests = SQLBatchTestRepository(self._session)
        self.evaluation_results = SQLEvaluationResultRepository(self._session)
      
        return self
  
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        self._session.close()
  
    def commit(self):
        try:
            self._session.commit()
        except Exception:
            self.rollback()
            raise
  
    def rollback(self):
        self._session.rollback()
```

---

## 9. Overall Project Structure

```
wtb/
├── __init__.py
├── domain/                          # Core Domain Layer
│   ├── __init__.py
│   ├── models/                      # Domain Models (Entities, Value Objects, Aggregates)
│   │   ├── __init__.py
│   │   ├── workflow.py              # TestWorkflow, WorkflowNode, WorkflowEdge
│   │   ├── execution.py             # Execution, ExecutionState
│   │   ├── variant.py               # NodeVariant
│   │   ├── batch_test.py            # BatchTest
│   │   └── evaluation.py            # EvaluationResult
│   │
│   ├── interfaces/                  # Abstract Interfaces (Ports)
│   │   ├── __init__.py
│   │   ├── execution_controller.py  # IExecutionController
│   │   ├── node_replacer.py         # INodeReplacer, IVariantRegistry, INodeSwapper
│   │   ├── state_adapter.py         # IStateAdapter
│   │   ├── node_executor.py         # INodeExecutor
│   │   ├── evaluator.py             # IEvaluator
│   │   ├── repositories.py          # IRepository<T>, IWorkflowRepository, etc.
│   │   └── unit_of_work.py          # IUnitOfWork
│   │
│   ├── events/                      # Domain Events
│   │   ├── __init__.py
│   │   ├── execution_events.py      # ExecutionStarted, ExecutionCompleted, etc.
│   │   └── node_events.py           # NodeExecuted, NodeFailed
│   │
│   └── services/                    # Domain Services
│       ├── __init__.py
│       └── workflow_validator.py    # Workflow validation logic
│
├── application/                     # Application Layer
│   ├── __init__.py
│   ├── services/                    # Application Services
│   │   ├── __init__.py
│   │   ├── execution_controller.py  # ExecutionController implementation
│   │   ├── node_replacer.py         # NodeReplacer implementation
│   │   ├── batch_test_runner.py     # BatchTestRunner
│   │   └── evaluation_engine.py     # EvaluationEngine
│   │
│   ├── commands/                    # Command Handlers (CQRS style)
│   │   ├── __init__.py
│   │   ├── create_workflow.py
│   │   ├── run_execution.py
│   │   └── register_variant.py
│   │
│   └── queries/                     # Query Handlers
│       ├── __init__.py
│       ├── get_execution.py
│       └── list_workflows.py
│
├── infrastructure/                  # Infrastructure Layer
│   ├── __init__.py
│   ├── database/                    # Database Implementation
│   │   ├── __init__.py
│   │   ├── models.py                # SQLAlchemy ORM Models
│   │   ├── repositories/            # Repository Implementations
│   │   │   ├── __init__.py
│   │   │   ├── workflow_repository.py
│   │   │   ├── execution_repository.py
│   │   │   └── variant_repository.py
│   │   ├── unit_of_work.py          # SQLAlchemy UoW
│   │   └── migrations/              # Alembic migrations
│   │
│   ├── adapters/                    # External System Adapters
│   │   ├── __init__.py
│   │   ├── agentgit_adapter.py      # AgentGit integration
│   │   ├── filetracker_adapter.py   # FileTracker integration
│   │   └── inmemory_adapter.py      # In-memory for testing
│   │
│   └── external/                    # External APIs
│       ├── __init__.py
│       └── ide_client.py            # IDE WebSocket client
│
├── api/                             # API Layer
│   ├── __init__.py
│   ├── rest/                        # REST API
│   │   ├── __init__.py
│   │   ├── workflows.py             # Workflow endpoints
│   │   ├── executions.py            # Execution endpoints
│   │   └── batch_tests.py           # Batch test endpoints
│   │
│   └── websocket/                   # WebSocket API
│       ├── __init__.py
│       └── realtime.py              # Real-time updates
│
├── config/                          # Configuration
│   ├── __init__.py
│   ├── settings.py                  # Application settings
│   └── dependency_injection.py      # DI container setup
│
└── tests/                           # Tests
    ├── __init__.py
    ├── unit/
    │   ├── domain/
    │   ├── application/
    │   └── infrastructure/
    ├── integration/
    └── e2e/
```

---

## 10. Dependency Injection Configuration

```python
# wtb/config/dependency_injection.py

from dependency_injector import containers, providers

from ..domain.interfaces.execution_controller import IExecutionController
from ..domain.interfaces.node_replacer import INodeReplacer
from ..domain.interfaces.state_adapter import IStateAdapter
from ..domain.interfaces.unit_of_work import IUnitOfWork

from ..application.services.execution_controller import ExecutionController
from ..application.services.node_replacer import NodeReplacer
from ..application.services.batch_test_runner import BatchTestRunner
from ..application.services.evaluation_engine import EvaluationEngine

from ..infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork
from ..infrastructure.adapters.agentgit_adapter import AgentGitStateAdapter
from ..infrastructure.adapters.inmemory_adapter import InMemoryStateAdapter

class Container(containers.DeclarativeContainer):
    """Dependency injection container for WTB."""
  
    config = providers.Configuration()
  
    # Database / Unit of Work
    unit_of_work = providers.Singleton(
        SQLAlchemyUnitOfWork,
        connection_string=config.database.connection_string
    )
  
    # State Adapter (configurable)
    state_adapter = providers.Selector(
        config.state_adapter,
        agentgit=providers.Factory(
            AgentGitStateAdapter,
            db_path=config.agentgit.db_path,
            external_session_id=config.agentgit.external_session_id
        ),
        inmemory=providers.Factory(InMemoryStateAdapter)
    )
  
    # Node Executor
    node_executor = providers.Factory(
        DefaultNodeExecutor,
        tool_registry=...
    )
  
    # Event Publisher
    event_publisher = providers.Singleton(EventPublisher)
  
    # Application Services
    execution_controller = providers.Factory(
        ExecutionController,
        execution_repository=unit_of_work.provided.executions,
        workflow_repository=unit_of_work.provided.workflows,
        state_adapter=state_adapter,
        node_executor=node_executor,
        event_publisher=event_publisher
    )
  
    node_replacer = providers.Factory(
        NodeReplacer,
        variant_repository=unit_of_work.provided.variants
    )
  
    batch_test_runner = providers.Factory(
        BatchTestRunner,
        execution_controller=execution_controller,
        node_replacer=node_replacer,
        batch_test_repository=unit_of_work.provided.batch_tests
    )
  
    evaluation_engine = providers.Factory(
        EvaluationEngine,
        result_repository=unit_of_work.provided.evaluation_results
    )
```

---

## 11. Database Migration Strategy

### For AgentGit Refactoring (Optional)

```python
# If refactoring AgentGit to use SQLAlchemy:

# agentgit/infrastructure/database/models.py
from sqlalchemy import Column, Integer, String, DateTime, JSON, ForeignKey
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

class InternalSessionModel(Base):
    __tablename__ = "internal_sessions"
  
    id = Column(Integer, primary_key=True, autoincrement=True)
    external_session_id = Column(Integer, nullable=False)
    langgraph_session_id = Column(String(255), unique=True, nullable=False)
    state_data = Column(JSON)
    conversation_history = Column(JSON)
    created_at = Column(DateTime, nullable=False)
    is_current = Column(Integer, default=0)
    checkpoint_count = Column(Integer, default=0)
    parent_session_id = Column(Integer, ForeignKey("internal_sessions.id"))
    branch_point_checkpoint_id = Column(Integer, ForeignKey("checkpoints.id"))
    tool_invocation_count = Column(Integer, default=0)
    metadata_ = Column("metadata", JSON)
  
    checkpoints = relationship("CheckpointModel", back_populates="session")

class CheckpointModel(Base):
    __tablename__ = "checkpoints"
  
    id = Column(Integer, primary_key=True, autoincrement=True)
    internal_session_id = Column(Integer, ForeignKey("internal_sessions.id"), nullable=False)
    checkpoint_name = Column(String(255))
    checkpoint_data = Column(JSON, nullable=False)
    is_auto = Column(Integer, default=0)
    created_at = Column(DateTime, nullable=False)
    user_id = Column(Integer)
  
    session = relationship("InternalSessionModel", back_populates="checkpoints")
```

### Migration Script Example

```python
# alembic/versions/001_initial.py

from alembic import op
import sqlalchemy as sa

def upgrade():
    # Create WTB tables
    op.create_table(
        'wtb_workflows',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text),
        sa.Column('definition', sa.JSON, nullable=False),
        sa.Column('version', sa.String(50)),
        sa.Column('created_at', sa.DateTime),
        sa.Column('updated_at', sa.DateTime),
        sa.Column('metadata', sa.JSON)
    )
  
    op.create_table(
        'wtb_executions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('workflow_id', sa.String(36), sa.ForeignKey('wtb_workflows.id')),
        # ... more columns
    )
  
    # Add indexes
    op.create_index('ix_wtb_executions_workflow', 'wtb_executions', ['workflow_id'])
    op.create_index('ix_wtb_executions_status', 'wtb_executions', ['status'])

def downgrade():
    op.drop_table('wtb_executions')
    op.drop_table('wtb_workflows')
```

---

## 12. Summary and Recommendations

### Key Decisions

| Decision          | Choice           | Rationale                                  |
| ----------------- | ---------------- | ------------------------------------------ |
| DDD               | ✅ Adopt         | Complex domain with clear bounded contexts |
| SQLAlchemy        | ✅ Adopt for WTB | Database portability, migration support    |
| Unit of Work      | ✅ Selective     | Multi-aggregate operations only            |
| Interface-based   | ✅ Adopt         | SOLID compliance, testability              |
| AgentGit refactor | ⚠️ Optional    | Works for now, can migrate later           |

### Implementation Phases (Updated with Validation Status)

```
Phase 0: Infrastructure Validation ✅ COMPLETE
├── ✅ AgentGit v2 tests (94 tests passing)
├── ✅ FileTracker tests (40+ tests passing)
├── ✅ Verified: Checkpoint, Session, Audit, Rollback
├── ✅ Verified: Content-addressable storage, Commit/Memento
└── ✅ Documented integration patterns

Phase 1: Core Domain & Infrastructure
├── ✅ Domain models exist (AgentGit dataclasses)
├── ⏳ SQLAlchemy ORM models for WTB tables
├── ⏳ Repository interfaces (IRepository<T>)
├── ⏳ AgentGit state adapter (IStateAdapter)
└── ✅ EventBus ready for extension

Phase 2: Execution Controller
├── ⏳ IExecutionController interface
├── ✅ NodeManager available (AgentGit)
├── ⏳ Breakpoint handling
├── ✅ CheckpointManager_mdp integration ready
└── ✅ Dual granularity checkpoints verified

Phase 3: Node Replacer
├── ⏳ IVariantRegistry interface
├── ⏳ INodeSwapper implementation
├── ✅ WorkflowGraph supports node operations
└── ⏳ Variant set application

Phase 4: Batch Testing
├── ⏳ BatchTestRunner
├── ⏳ Parallel execution
├── ⏳ Comparison matrix
└── ✅ Session isolation verified

Phase 5: Evaluation Engine
├── ⏳ IEvaluator interface
├── ⏳ Built-in evaluators
├── ✅ AuditTrail metrics available
└── ⏳ Scoring aggregation

Phase 6: API Layer
├── ⏳ REST endpoints
├── ⏳ WebSocket real-time
├── ⏳ EventBus for agent git notifications ready
└── ⏳ IDE integration
```

### Risk Mitigations

| Risk                 | Mitigation                              |
| -------------------- | --------------------------------------- |
| AgentGit coupling    | Anti-corruption layer via IStateAdapter |
| Database portability | SQLAlchemy ORM abstraction              |
| Testing difficulty   | Interface-based design enables mocking  |
| Performance at scale | Connection pooling, indexed queries     |

---

## 13. Critical Architecture Decisions

### 13.1 Database Strategy: SQLite vs PostgreSQL

#### Decision Matrix

| System                | Current DB | Replace?      | Recommendation                                                  |
| --------------------- | ---------- | ------------- | --------------------------------------------------------------- |
| **AgentGit**    | SQLite     | ⚠️ Partial  | Keep SQLite for embedded use, add PostgreSQL option via adapter |
| **FileTracker** | PostgreSQL | ❌ No         | Keep PostgreSQL (blob storage benefits from it)                 |
| **WTB**         | New        | ✅ SQLAlchemy | Use SQLAlchemy for DB-agnostic abstraction                      |

#### Rationale: Don't Replace All SQLite

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                      DATABASE STRATEGY RECOMMENDATION                               │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  SQLite Strengths (Keep for AgentGit):                                            │
│  ├── Zero-config, embedded (ideal for single-user/dev scenarios)                  │
│  ├── File-based (easy backup, portability)                                        │
│  ├── Fast for small-medium datasets                                               │
│  └── Sufficient for checkpoint data (typically <1000 checkpoints/session)         │
│                                                                                    │
│  PostgreSQL Strengths (Keep for FileTracker, Add for WTB):                         │
│  ├── Concurrent access (batch testing requires parallel writes)                   │
│  ├── Advanced JSON queries (workflow definitions, evaluation metrics)             │
│  ├── Better for large-scale production deployments                                │
│  └── Connection pooling (important for web API scenarios)                         │
│                                                                                    │
│  HYBRID APPROACH:                                                                  │
│  ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐                  │
│  │    AgentGit     │   │   FileTracker   │   │       WTB       │                  │
│  │    ─────────    │   │   ───────────   │   │     ───────     │                  │
│  │                 │   │                 │   │                 │                  │
│  │  SQLite (dev)   │   │   PostgreSQL    │   │  SQLAlchemy     │                  │
│  │  PostgreSQL*    │   │   (production)  │   │  (configurable) │                  │
│  │  (production)   │   │                 │   │                 │                  │
│  └─────────────────┘   └─────────────────┘   └─────────────────┘                  │
│                                                                                    │
│  * Via IDbAdapter interface in AgentGit refactor (Phase 2)                        │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

#### Implementation Strategy

```python
# Option 1: Introduce Database Adapter Interface in AgentGit
# agentgit/database/adapters/base.py

from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any

class IDbAdapter(ABC):
    """Database adapter interface for AgentGit."""
  
    @abstractmethod
    def execute(self, query: str, params: tuple = ()) -> Any: ...
  
    @abstractmethod
    def executemany(self, query: str, params_list: List[tuple]) -> None: ...
  
    @abstractmethod
    def fetchone(self) -> Optional[tuple]: ...
  
    @abstractmethod
    def fetchall(self) -> List[tuple]: ...
  
    @abstractmethod
    def commit(self) -> None: ...
  
    @abstractmethod
    def rollback(self) -> None: ...
  
    @abstractmethod
    def get_placeholder(self) -> str:
        """Return ? for SQLite, %s for PostgreSQL."""
        pass

class SQLiteAdapter(IDbAdapter):
    """SQLite implementation - for dev/embedded use."""
    def get_placeholder(self) -> str:
        return "?"

class PostgreSQLAdapter(IDbAdapter):
    """PostgreSQL implementation - for production."""
    def get_placeholder(self) -> str:
        return "%s"
```

---

### 13.2 Checkpoint Granularity Decision: Single Type + Node Boundary Pointers

#### Final Design Decision

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│               CHECKPOINT GRANULARITY: SINGLE TYPE + NODE BOUNDARY POINTERS          │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  DECISION: All checkpoints are EQUAL (atomic at tool/message level)               │
│  ═══════════════════════════════════════════════════════════════════              │
│                                                                                    │
│  • No checkpoint_type field needed                                                │
│  • No parent_checkpoint_id / child_checkpoint_ids hierarchy                       │
│  • Each checkpoint tagged with metadata.node_id only                              │
│  • Node boundaries are POINTERS in WTB's wtb_node_boundaries table                │
│                                                                                    │
│  WHY NOT DUAL CHECKPOINT TYPES?                                                    │
│  ────────────────────────────────                                                  │
│  • State duplication: Node checkpoint = copy of last tool checkpoint (wasteful)   │
│  • Complex hierarchy management in adapter                                         │
│  • More checkpoints = more storage overhead                                        │
│                                                                                    │
│  HOW GRANULARITY WORKS:                                                            │
│  ──────────────────────                                                            │
│                                                                                    │
│  AgentGit checkpoints (all equal):     WTB wtb_node_boundaries:                   │
│  ───────────────────────────────────   ─────────────────────────────              │
│  [CP #1] node_id="data_load"      ─┐   Node: data_load                            │
│  [CP #2] node_id="data_load"       ├──► entry_checkpoint_id = 1                   │
│  [CP #3] node_id="data_load"      ─┘    exit_checkpoint_id  = 3                   │
│  [CP #4] node_id="preprocess"     ─┐   Node: preprocess                           │
│  [CP #5] node_id="preprocess"     ─┘    entry_checkpoint_id = 4                   │
│                                         exit_checkpoint_id  = 5                   │
│                                                                                    │
│  GRANULARITY = FILTERING, NOT SEPARATE STORAGE:                                   │
│  ──────────────────────────────────────────────                                    │
│  • get_node_rollback_targets() → returns exit_checkpoint for each completed node  │
│  • get_checkpoints_in_node() → filters by metadata.node_id                        │
│  • Unified rollback() → same method for node-level and tool-level                 │
│                                                                                    │
│  HIERARCHY VISUALIZATION:                                                          │
│  ════════════════════════                                                          │
│                                                                                    │
│  ┌─────────────────────────────────────────────────────────────────────────────┐  │
│  │                        Node: data_load                                       │  │
│  │   [Node CP #1] ─────────────────────────────────────────────────────────────│  │
│  │        │                                                                     │  │
│  │        ├── [Tool/Msg CP #2] tool: load_csv                                  │  │
│  │        ├── [Tool/Msg CP #3] tool: validate_schema                           │  │
│  │        └── [Tool/Msg CP #4] message: "Data loaded"                          │  │
│  └─────────────────────────────────────────────────────────────────────────────┘  │
│                                      │                                             │
│                                      ▼                                             │
│  ┌─────────────────────────────────────────────────────────────────────────────┐  │
│  │                        Node: preprocess                                      │  │
│  │   [Node CP #5] ─────────────────────────────────────────────────────────────│  │
│  │        │                                                                     │  │
│  │        ├── [Tool/Msg CP #6] tool: normalize                                 │  │
│  │        └── [Tool/Msg CP #7] tool: fill_na                                   │  │
│  └─────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

#### WTB Integration Strategy

```python
# How WTB should handle dual granularity:

class CheckpointGranularity(Enum):
    """Checkpoint granularity levels for WTB operations."""
    NODE = "node"           # Default for WTB - workflow step level
    TOOL_MESSAGE = "tool_message"  # Optional fine-grained - for debugging
    AUTO = "auto"           # Let system decide based on context

class IStateAdapter(ABC):
    """Extended to support granularity selection."""
  
    @abstractmethod
    def save_snapshot(
        self,
        state: ExecutionState,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        granularity: CheckpointGranularity = CheckpointGranularity.NODE  # NEW
    ) -> int:
        """Save snapshot with specified granularity."""
        pass
  
    @abstractmethod
    def get_rollback_targets(
        self, 
        session_id: int,
        granularity: CheckpointGranularity = CheckpointGranularity.NODE
    ) -> List[CheckpointInfo]:
        """
        Get valid rollback targets.
      
        If NODE: Returns only node-level checkpoints
        If TOOL_MESSAGE: Returns all checkpoints
        If AUTO: Returns node checkpoints, plus tool/msg if within current node
        """
        pass
```

#### Recommendation: Use Node Granularity by Default in WTB

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                   WTB CHECKPOINT STRATEGY                                           │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  DEFAULT BEHAVIOR (Node Granularity):                                              │
│  ─────────────────────────────────────                                             │
│  • WTB creates checkpoints at NODE boundaries only                                 │
│  • Matches workflow semantics (each node = one testable unit)                      │
│  • Simpler rollback UX for end users                                              │
│  • Lower storage overhead                                                          │
│                                                                                    │
│  OPTIONAL BEHAVIOR (Tool/Message Granularity):                                     │
│  ─────────────────────────────────────────────                                     │
│  • Enable via config flag: `fine_grained_checkpoints: true`                        │
│  • Useful for debugging complex nodes                                              │
│  • Required for tool-level rollback in error recovery flow                         │
│                                                                                    │
│  IMPLEMENTATION:                                                                   │
│                                                                                    │
│  class ExecutionController(IExecutionController):                                  │
│      def __init__(self, ..., fine_grained: bool = False):                          │
│          self._fine_grained = fine_grained                                         │
│                                                                                    │
│      def _create_checkpoint(self, execution, name):                                │
│          granularity = (                                                           │
│              CheckpointGranularity.TOOL_MESSAGE                                    │
│              if self._fine_grained                                                 │
│              else CheckpointGranularity.NODE                                       │
│          )                                                                         │
│          return self._state_adapter.save_snapshot(                                 │
│              state=execution.state,                                                │
│              name=name,                                                            │
│              granularity=granularity                                               │
│          )                                                                         │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

---

### 13.3 Event Bus Architecture: Unified vs Separate

#### Decision: Extend AgentGit Event Bus for WTB

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                      EVENT BUS ARCHITECTURE DECISION                                │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  OPTION 1: Separate Event Buses (NOT RECOMMENDED)                                  │
│  ───────────────────────────────────────────────                                   │
│  ┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐              │
│  │  AgentGit Bus   │     │    WTB Bus      │     │   Audit Bus     │              │
│  │  ────────────   │     │  ──────────     │     │  ───────────    │              │
│  │ CheckpointEvent │     │ ExecutionEvent  │     │  AuditEvent     │              │
│  │ SessionEvent    │     │ NodeEvent       │     │                 │              │
│  └─────────────────┘     └─────────────────┘     └─────────────────┘              │
│       ❌ Duplicated infrastructure                                                 │
│       ❌ Hard to correlate events across systems                                   │
│       ❌ Multiple event histories to manage                                        │
│                                                                                    │
│  OPTION 2: Unified Event Bus (RECOMMENDED)                                         │
│  ─────────────────────────────────────────                                         │
│                                                                                    │
│  ┌─────────────────────────────────────────────────────────────────────────────┐  │
│  │                        UNIFIED EVENT BUS                                     │  │
│  │                        ═════════════════                                     │  │
│  │                                                                              │  │
│  │  DomainEvent (base)                                                          │  │
│  │       │                                                                      │  │
│  │       ├── AgentGitEvents                                                     │  │
│  │       │   ├── CheckpointCreatedEvent                                         │  │
│  │       │   ├── SessionCreatedEvent                                            │  │
│  │       │   ├── RollbackPerformedEvent                                         │  │
│  │       │   └── ToolExecutedEvent                                              │  │
│  │       │                                                                      │  │
│  │       ├── WTBEvents (NEW)                                                    │  │
│  │       │   ├── ExecutionStartedEvent                                          │  │
│  │       │   ├── ExecutionPausedEvent                                           │  │
│  │       │   ├── ExecutionCompletedEvent                                        │  │
│  │       │   ├── ExecutionFailedEvent                                           │  │
│  │       │   ├── NodeExecutedEvent                                              │  │
│  │       │   ├── NodeReplacedEvent                                              │  │
│  │       │   ├── BatchTestStartedEvent                                          │  │
│  │       │   └── EvaluationCompletedEvent                                       │  │
│  │       │                                                                      │  │
│  │       └── AuditEvents (Derived from existing AuditTrail)                     │  │
│  │           ├── AuditToolEvent                                                 │  │
│  │           ├── AuditLLMEvent                                                  │  │
│  │           └── AuditErrorEvent                                                │  │
│  │                                                                              │  │
│  │  EventBus (singleton per application)                                        │  │
│  │  ├── subscribe(event_type, handler)                                          │  │
│  │  ├── publish(event)                                                          │  │
│  │  ├── get_event_history(filter)                                               │  │
│  │  └── export_to_audit_trail()  ← Bridge to existing AuditTrail                │  │
│  │                                                                              │  │
│  └─────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                    │
│       ✅ Single source of truth for all events                                     │
│       ✅ Easy correlation (execution_id links all events)                          │
│       ✅ Unified history and replay                                                │

---

### 13.4 WTB Storage Abstraction: Dual Implementation Pattern

#### Decision: Interface + Multiple Implementations

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                      WTB STORAGE ABSTRACTION DECISION                               │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  PRINCIPLE: "Interface → Multiple Implementations"                                │
│                                                                                    │
│  Application Layer (ExecutionController, NodeReplacer, etc.)                      │
│         │                                                                          │
│         │ depends on abstractions only (Dependency Inversion Principle)           │
│         ▼                                                                          │
│  ┌──────────────────────────────────────────────────────────────────────────────┐ │
│  │                        IUnitOfWork (interface)                                │ │
│  │  ├── workflows: IWorkflowRepository                                           │ │
│  │  ├── executions: IExecutionRepository                                         │ │
│  │  ├── node_boundaries: INodeBoundaryRepository                                 │ │
│  │  ├── checkpoint_files: ICheckpointFileRepository                              │ │
│  │  ├── node_variants: INodeVariantRepository                                    │ │
│  │  └── evaluation_results: IEvaluationResultRepository                          │ │
│  └──────────────────────────────────────────────────────────────────────────────┘ │
│                             │                                                      │
│         ┌───────────────────┼───────────────────┐                                 │
│         │                   │                   │                                 │
│         ▼                   ▼                   ▼                                 │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐                      │
│  │   InMemory     │  │  SQLAlchemy    │  │   Future:      │                      │
│  │   UnitOfWork   │  │  UnitOfWork    │  │  PostgreSQL    │                      │
│  │                │  │                │  │  UnitOfWork    │                      │
│  │  ─────────────│  │  ─────────────│  │  ─────────────│                      │
│  │  • Dict-based  │  │  • SQLite file │  │  • PostgreSQL  │                      │
│  │  • No I/O      │  │  • ORM models  │  │  • Connection  │                      │
│  │  • Fast tests  │  │  • Transactions│  │    pooling     │                      │
│  └────────────────┘  └────────────────┘  └────────────────┘                      │
│                                                                                    │
│  SELECTION:                                                                        │
│  ──────────                                                                        │
│  uow = UnitOfWorkFactory.create(mode="inmemory")       # For unit tests           │
│  uow = UnitOfWorkFactory.create(mode="sqlalchemy",     # For production           │
│                                 db_url="sqlite:///wtb.db")                        │
│                                                                                    │
│  BENEFITS:                                                                         │
│  ─────────                                                                         │
│  ✅ Tests run extremely fast (no I/O)                                             │
│  ✅ Production uses persistent storage with transactions                          │
│  ✅ Easy to switch via dependency injection                                       │
│  ✅ Same interface, different implementations (LSP compliant)                     │
│  ✅ No code changes needed when switching modes                                   │
│                                                                                    │
│  See: docs/Adapter_and_WTB-Storage/WTB_PERSISTENCE_DESIGN.md for full details    │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```
│       ✅ AuditTrail becomes a subscriber/consumer                                  │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

#### Event Bus Extension Implementation

```python
# wtb/events/wtb_events.py

from datetime import datetime
from typing import Optional, Dict, Any, List
from agentgit.events.event_bus import DomainEvent

# ============== WTB-Specific Events ==============

class ExecutionStartedEvent(DomainEvent):
    """Published when a workflow execution starts."""
  
    def __init__(
        self,
        timestamp: datetime,
        execution_id: str,
        workflow_id: str,
        workflow_name: str,
        initial_state: Dict[str, Any],
        event_id: str = ""
    ):
        super().__init__(timestamp, event_id)
        self.execution_id = execution_id
        self.workflow_id = workflow_id
        self.workflow_name = workflow_name
        self.initial_state = initial_state

class ExecutionPausedEvent(DomainEvent):
    """Published when execution is paused (breakpoint or manual)."""
  
    def __init__(
        self,
        timestamp: datetime,
        execution_id: str,
        paused_at_node: str,
        reason: str,  # "breakpoint", "manual", "error"
        checkpoint_id: Optional[int] = None,
        event_id: str = ""
    ):
        super().__init__(timestamp, event_id)
        self.execution_id = execution_id
        self.paused_at_node = paused_at_node
        self.reason = reason
        self.checkpoint_id = checkpoint_id

class ExecutionCompletedEvent(DomainEvent):
    """Published when execution completes successfully."""
  
    def __init__(
        self,
        timestamp: datetime,
        execution_id: str,
        final_state: Dict[str, Any],
        duration_ms: float,
        nodes_executed: int,
        event_id: str = ""
    ):
        super().__init__(timestamp, event_id)
        self.execution_id = execution_id
        self.final_state = final_state
        self.duration_ms = duration_ms
        self.nodes_executed = nodes_executed

class ExecutionFailedEvent(DomainEvent):
    """Published when execution fails."""
  
    def __init__(
        self,
        timestamp: datetime,
        execution_id: str,
        failed_at_node: str,
        error_message: str,
        error_type: str,
        checkpoint_id: Optional[int] = None,
        event_id: str = ""
    ):
        super().__init__(timestamp, event_id)
        self.execution_id = execution_id
        self.failed_at_node = failed_at_node
        self.error_message = error_message
        self.error_type = error_type
        self.checkpoint_id = checkpoint_id

class NodeExecutedEvent(DomainEvent):
    """Published after each node execution."""
  
    def __init__(
        self,
        timestamp: datetime,
        execution_id: str,
        node_id: str,
        node_name: str,
        result: Any,
        duration_ms: float,
        checkpoint_id: Optional[int] = None,
        event_id: str = ""
    ):
        super().__init__(timestamp, event_id)
        self.execution_id = execution_id
        self.node_id = node_id
        self.node_name = node_name
        self.result = result
        self.duration_ms = duration_ms
        self.checkpoint_id = checkpoint_id

class NodeReplacedEvent(DomainEvent):
    """Published when a node is swapped with a variant."""
  
    def __init__(
        self,
        timestamp: datetime,
        workflow_id: str,
        original_node_id: str,
        variant_id: str,
        variant_name: str,
        event_id: str = ""
    ):
        super().__init__(timestamp, event_id)
        self.workflow_id = workflow_id
        self.original_node_id = original_node_id
        self.variant_id = variant_id
        self.variant_name = variant_name

class BatchTestStartedEvent(DomainEvent):
    """Published when a batch test begins."""
  
    def __init__(
        self,
        timestamp: datetime,
        batch_test_id: str,
        workflow_id: str,
        variant_count: int,
        parallel_count: int,
        event_id: str = ""
    ):
        super().__init__(timestamp, event_id)
        self.batch_test_id = batch_test_id
        self.workflow_id = workflow_id
        self.variant_count = variant_count
        self.parallel_count = parallel_count

class EvaluationCompletedEvent(DomainEvent):
    """Published when evaluation finishes for an execution."""
  
    def __init__(
        self,
        timestamp: datetime,
        execution_id: str,
        evaluator_name: str,
        score: float,
        metrics: Dict[str, Any],
        event_id: str = ""
    ):
        super().__init__(timestamp, event_id)
        self.execution_id = execution_id
        self.evaluator_name = evaluator_name
        self.score = score
        self.metrics = metrics
```

---

### 13.4 Audit Integration Strategy

#### Bridge AuditTrail with Event Bus

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                      AUDIT + EVENT BUS INTEGRATION                                  │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  CURRENT STATE:                                                                    │
│  ──────────────                                                                    │
│  • AuditTrail is a standalone in-memory structure                                  │
│  • AuditTrailCallback captures LangChain events → AuditTrail                       │
│  • Not connected to EventBus                                                       │
│  • No persistence (stored in checkpoint metadata only)                             │
│                                                                                    │
│  PROPOSED INTEGRATION:                                                             │
│  ─────────────────────                                                             │
│                                                                                    │
│  ┌─────────────────────────────────────────────────────────────────────────────┐  │
│  │                                                                              │  │
│  │   ┌──────────────┐    ┌──────────────┐    ┌──────────────────────────────┐  │  │
│  │   │  LangChain   │    │   WTB        │    │   AgentGit                   │  │  │
│  │   │  Callbacks   │    │  Services    │    │   Managers                   │  │  │
│  │   └──────┬───────┘    └──────┬───────┘    └──────────────┬───────────────┘  │  │
│  │          │                   │                           │                   │  │
│  │          ▼                   ▼                           ▼                   │  │
│  │   ┌─────────────────────────────────────────────────────────────────────┐   │  │
│  │   │                         EVENT BUS                                    │   │  │
│  │   │  • Unified publish/subscribe                                         │   │  │
│  │   │  • Event correlation via execution_id                                │   │  │
│  │   └───────────────────────────────┬─────────────────────────────────────┘   │  │
│  │                                   │                                          │  │
│  │               ┌───────────────────┼───────────────────┐                     │  │
│  │               ▼                   ▼                   ▼                     │  │
│  │   ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐           │  │
│  │   │  AuditTrail      │ │  WebSocket       │ │  Metrics         │           │  │
│  │   │  Subscriber      │ │  Publisher       │ │  Collector       │           │  │
│  │   │                  │ │                  │ │                  │           │  │
│  │   │  Converts events │ │  Pushes to IDE   │ │  Aggregates for  │           │  │
│  │   │  to AuditEvent   │ │  in real-time    │ │  evaluation      │           │  │
│  │   └────────┬─────────┘ └──────────────────┘ └──────────────────┘           │  │
│  │            │                                                                 │  │
│  │            ▼                                                                 │  │
│  │   ┌──────────────────┐                                                      │  │
│  │   │  PostgreSQL      │   ← Persistent audit storage                         │  │
│  │   │  audit_sessions  │                                                      │  │
│  │   │  audit_events    │                                                      │  │
│  │   └──────────────────┘                                                      │  │
│  │                                                                              │  │
│  └─────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

#### Audit Subscriber Implementation

```python
# wtb/infrastructure/audit/audit_subscriber.py

from datetime import datetime
from typing import Optional
from agentgit.events.event_bus import EventBus, DomainEvent
from agentgit.audit.audit_trail import AuditTrail, AuditEvent, EventType, EventSeverity

# Import WTB events
from wtb.events.wtb_events import (
    ExecutionStartedEvent,
    ExecutionCompletedEvent,
    ExecutionFailedEvent,
    ExecutionPausedEvent,
    NodeExecutedEvent,
)

# Import AgentGit events
from agentgit.events.agent_events import (
    CheckpointCreatedEvent,
    ToolExecutedEvent,
    RollbackPerformedEvent,
)

class AuditEventSubscriber:
    """
    Subscribes to EventBus and converts domain events to AuditEvents.
  
    This bridges the unified event bus with the existing AuditTrail system,
    maintaining backward compatibility while enabling unified event flow.
    """
  
    def __init__(
        self, 
        event_bus: EventBus,
        audit_trail: Optional[AuditTrail] = None,
        persist_to_db: bool = True
    ):
        self._event_bus = event_bus
        self._audit_trail = audit_trail or AuditTrail()
        self._persist_to_db = persist_to_db
      
        # Subscribe to all relevant events
        self._register_handlers()
  
    def _register_handlers(self):
        """Register handlers for all event types."""
        # WTB Events
        self._event_bus.subscribe(ExecutionStartedEvent, self._on_execution_started)
        self._event_bus.subscribe(ExecutionCompletedEvent, self._on_execution_completed)
        self._event_bus.subscribe(ExecutionFailedEvent, self._on_execution_failed)
        self._event_bus.subscribe(ExecutionPausedEvent, self._on_execution_paused)
        self._event_bus.subscribe(NodeExecutedEvent, self._on_node_executed)
      
        # AgentGit Events
        self._event_bus.subscribe(CheckpointCreatedEvent, self._on_checkpoint_created)
        self._event_bus.subscribe(ToolExecutedEvent, self._on_tool_executed)
        self._event_bus.subscribe(RollbackPerformedEvent, self._on_rollback)
  
    def _on_execution_started(self, event: ExecutionStartedEvent):
        audit_event = AuditEvent(
            timestamp=event.timestamp,
            event_type=EventType.AGENT_DECISION,  # Reuse existing type
            severity=EventSeverity.INFO,
            message=f"Workflow '{event.workflow_name}' execution started",
            details={
                "execution_id": event.execution_id,
                "workflow_id": event.workflow_id,
                "initial_state_keys": list(event.initial_state.keys())
            }
        )
        self._record(audit_event)
  
    def _on_execution_completed(self, event: ExecutionCompletedEvent):
        audit_event = AuditEvent(
            timestamp=event.timestamp,
            event_type=EventType.TOOL_SUCCESS,  # Reuse - workflow completion
            severity=EventSeverity.SUCCESS,
            message=f"Workflow execution completed successfully",
            details={
                "execution_id": event.execution_id,
                "nodes_executed": event.nodes_executed
            },
            duration_ms=event.duration_ms
        )
        self._record(audit_event)
  
    def _on_execution_failed(self, event: ExecutionFailedEvent):
        audit_event = AuditEvent(
            timestamp=event.timestamp,
            event_type=EventType.TOOL_ERROR,
            severity=EventSeverity.ERROR,
            message=f"Workflow execution failed at node '{event.failed_at_node}'",
            details={
                "execution_id": event.execution_id,
                "failed_node": event.failed_at_node,
                "error_type": event.error_type
            },
            error=event.error_message
        )
        self._record(audit_event)
  
    def _on_execution_paused(self, event: ExecutionPausedEvent):
        audit_event = AuditEvent(
            timestamp=event.timestamp,
            event_type=EventType.CHECKPOINT_CREATED,
            severity=EventSeverity.INFO,
            message=f"Execution paused at node '{event.paused_at_node}' ({event.reason})",
            details={
                "execution_id": event.execution_id,
                "paused_node": event.paused_at_node,
                "reason": event.reason,
                "checkpoint_id": event.checkpoint_id
            }
        )
        self._record(audit_event)
  
    def _on_node_executed(self, event: NodeExecutedEvent):
        audit_event = AuditEvent(
            timestamp=event.timestamp,
            event_type=EventType.TOOL_SUCCESS,
            severity=EventSeverity.SUCCESS,
            message=f"Node '{event.node_name}' executed",
            details={
                "execution_id": event.execution_id,
                "node_id": event.node_id,
                "checkpoint_id": event.checkpoint_id
            },
            duration_ms=event.duration_ms
        )
        self._record(audit_event)
  
    def _on_checkpoint_created(self, event: CheckpointCreatedEvent):
        audit_event = AuditEvent(
            timestamp=event.timestamp,
            event_type=EventType.CHECKPOINT_CREATED,
            severity=EventSeverity.INFO,
            message=f"Checkpoint '{event.checkpoint_name or event.checkpoint_id}' created",
            details={
                "checkpoint_id": event.checkpoint_id,
                "session_id": event.session_id,
                "is_auto": event.is_auto,
                "tool_count": event.tool_count
            }
        )
        self._record(audit_event)
  
    def _on_tool_executed(self, event: ToolExecutedEvent):
        severity = EventSeverity.SUCCESS if event.success else EventSeverity.ERROR
        event_type = EventType.TOOL_SUCCESS if event.success else EventType.TOOL_ERROR
      
        audit_event = AuditEvent(
            timestamp=event.timestamp,
            event_type=event_type,
            severity=severity,
            message=f"Tool '{event.tool_name}' {'completed' if event.success else 'failed'}",
            details={
                "tool_name": event.tool_name,
                "args": event.args
            },
            error=event.error_message,
            duration_ms=event.duration_ms
        )
        self._record(audit_event)
  
    def _on_rollback(self, event: RollbackPerformedEvent):
        audit_event = AuditEvent(
            timestamp=event.timestamp,
            event_type=EventType.ROLLBACK,
            severity=EventSeverity.WARNING,
            message=f"Rollback performed to checkpoint {event.checkpoint_id}",
            details={
                "checkpoint_id": event.checkpoint_id,
                "original_session": event.original_session_id,
                "new_session": event.new_session_id,
                "tools_reversed": event.tools_reversed
            }
        )
        self._record(audit_event)
  
    def _record(self, audit_event: AuditEvent):
        """Record event to trail and optionally persist."""
        self._audit_trail.add_event(audit_event)
      
        if self._persist_to_db:
            # TODO: Implement database persistence
            # self._audit_repo.save(audit_event)
            pass
  
    @property
    def audit_trail(self) -> AuditTrail:
        return self._audit_trail
```

---

### 13.5 Complete Event Flow Diagram

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                         COMPLETE EVENT FLOW                                         │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  User Action                                                                       │
│      │                                                                             │
│      ▼                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────────────┐ │
│  │  WTB ExecutionController                                                      │ │
│  │      │                                                                        │ │
│  │      ├── run()                                                                │ │
│  │      │    └── publish(ExecutionStartedEvent)                                  │ │
│  │      │                                                                        │ │
│  │      ├── [for each node]                                                      │ │
│  │      │    ├── _create_checkpoint()                                            │ │
│  │      │    │    └── AgentGit CheckpointManager                                 │ │
│  │      │    │         └── publish(CheckpointCreatedEvent)                       │ │
│  │      │    │                                                                   │ │
│  │      │    ├── node_executor.execute()                                         │ │
│  │      │    │    └── AgentGit ToolManager                                       │ │
│  │      │    │         └── publish(ToolExecutedEvent)                            │ │
│  │      │    │                                                                   │ │
│  │      │    └── publish(NodeExecutedEvent)                                      │ │
│  │      │                                                                        │ │
│  │      ├── [if breakpoint]                                                      │ │
│  │      │    └── publish(ExecutionPausedEvent)                                   │ │
│  │      │                                                                        │ │
│  │      ├── [if error]                                                           │ │
│  │      │    └── publish(ExecutionFailedEvent)                                   │ │
│  │      │                                                                        │ │
│  │      └── [on complete]                                                        │ │
│  │           └── publish(ExecutionCompletedEvent)                                │ │
│  └──────────────────────────────────────────────────────────────────────────────┘ │
│                                     │                                              │
│                                     ▼                                              │
│  ┌──────────────────────────────────────────────────────────────────────────────┐ │
│  │                            EVENT BUS                                          │ │
│  │                     (Single Global Instance)                                  │ │
│  └──────────────────────────────────────────────────────────────────────────────┘ │
│           │                    │                    │                   │          │
│           ▼                    ▼                    ▼                   ▼          │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  │ AuditSubscriber │  │ WebSocketPush   │  │ MetricsCollect  │  │ IDE Sync        │
│  │                 │  │                 │  │                 │  │                 │
│  │ → AuditTrail    │  │ → Browser UI    │  │ → Prometheus    │  │ → Dashboard     │
│  │ → PostgreSQL    │  │ → Real-time     │  │ → Evaluation    │  │ → Branch Tree   │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘  └─────────────────┘
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 14. Summary of Architecture Decisions

| Question                                 | Decision                           | Rationale                                                                                                              | Validated? |
| ---------------------------------------- | ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------- | ---------- |
| **Replace all SQLite?**            | ❌ No, Hybrid approach             | SQLite for embedded/dev (AgentGit), PostgreSQL for production (FileTracker, WTB audit), SQLAlchemy abstraction for WTB | ✅ Tests confirm SQLite works |
| **Dual granularity checkpoints?**  | Single Type + Node Boundary Pointers | All checkpoints are atomic at tool/message level. Node boundaries are POINTERS (entry_cp, exit_cp), NOT separate checkpoints. Granularity = filtering. | ✅ Design validated |
| **WTB Storage Pattern?**           | Dual Implementation (InMemory + SQLAlchemy) | IUnitOfWork interface with InMemoryUnitOfWork for tests, SQLAlchemyUnitOfWork for production. Factory pattern for switching. | ✅ Designed |
| **Event Bus for WTB?**             | ✅ Yes                             | Decouples components, enables async audit, metrics, and real-time UI updates                                           | ✅ EventBus verified |
| **Extend AgentGit Event Bus?**     | ✅ Yes, Unified                    | Single EventBus with WTB-specific events extending DomainEvent base                                                    | ✅ Pub/sub works |
| **Audit + Event Bus integration?** | ✅ AuditSubscriber pattern         | AuditTrail becomes an event consumer; enables persistence and correlation                                              | ✅ AuditTrail verified |

### Key Design Principles

| Principle | Description |
|-----------|-------------|
| **Dual Database Pattern** | AgentGit repositories (raw SQLite) for checkpoint state; SQLAlchemy UoW for WTB domain data. Never mix - adapters bridge the boundary. |
| **Single Checkpoint Type** | All checkpoints atomic at tool/message level. Node boundaries are POINTERS in wtb_node_boundaries, NOT separate checkpoints. |
| **Interface Abstraction** | IStateAdapter, IUnitOfWork, IRepository enable swappable implementations (InMemory for tests, SQLAlchemy for production). |
| **Dependency Injection** | UnitOfWorkFactory.create(mode) enables test/production switching. No hard-coded database dependencies. |

---

## 15. Implementation Validation Status

### Verified Components (From Infrastructure Tests)

| Component | Test Count | Status | Notes |
|-----------|------------|--------|-------|
| **Checkpoint** | 5 | ✅ Ready | Model + serialization |
| **CheckpointRepository** | 5 | ✅ Ready | Full CRUD |
| **CheckpointManager_mdp** | 3 | ✅ Ready | Dual granularity |
| **InternalSession** | 5 | ✅ Ready | Base session |
| **InternalSession_mdp** | 2 | ✅ Ready | MDP extensions |
| **ExternalSession** | 10 | ✅ Ready | User container |
| **ExternalSessionRepository** | 4 | ✅ Ready | CRUD + user queries |
| **ToolManager** | 5 | ✅ Ready | Tracking + stats |
| **ToolRollbackRegistry** | 8 | ✅ Ready | Forward/reverse + redo |
| **AuditTrail** | 9 | ✅ Ready | Full audit system |
| **AuditEvent** | 5 | ✅ Ready | All event types |
| **EventBus** | 4 | ✅ Ready | Pub/sub verified |
| **WorkflowGraph** | 5 | ✅ Ready | Node/Edge structure |
| **NodeManager** | 3 | ✅ Ready | MDP orchestration |
| **BlobORM/Repository** | 10+ | ✅ Ready | Content-addressable |
| **Commit/FileMemento** | 10+ | ✅ Ready | Versioning |

### Components to Implement (WTB-Specific)

| Component | Priority | Dependencies | Notes |
|-----------|----------|--------------|-------|
| **IExecutionController** | P0 | CheckpointManager_mdp, NodeManager | Main WTB interface |
| **INodeReplacer** | P0 | WorkflowGraph | Variant swapping |
| **IStateAdapter** | P0 | All session/checkpoint repos | Anti-corruption layer |
| **AgentGitStateAdapter** | P1 | IStateAdapter | Production adapter |
| **InMemoryStateAdapter** | P1 | IStateAdapter | For testing |
| **IUnitOfWork** | P0 | - | WTB storage abstraction |
| **InMemoryUnitOfWork** | P0 | IUnitOfWork | For unit tests |
| **SQLAlchemyUnitOfWork** | P1 | IUnitOfWork | For production |
| **UnitOfWorkFactory** | P0 | Both UoW implementations | DI factory |
| **IEvaluator** | P2 | Execution results | Scoring interface |
| **BatchTestRunner** | P2 | ExecutionController | Parallel testing |
| **WTB Event Types** | P1 | EventBus | ExecutionStarted, NodeExecuted, etc. |
| **AuditEventSubscriber** | P2 | EventBus, AuditTrail | Bridge pattern |

### Database Schema Status

```
AgentGit Schema (Verified - UNCHANGED):
├── users              ✅ Tested
├── external_sessions  ✅ Tested
├── internal_sessions  ✅ Tested (includes MDP fields)
└── checkpoints        ✅ Tested (includes metadata JSON)

FileTracker Schema (Verified - UNCHANGED):
├── commits            ✅ Tested
├── file_blobs         ✅ Tested
└── file_mementos      ✅ Tested

WTB Schema (Dual Implementation):
├── wtb_workflows          ✅ Designed (InMemory + SQLAlchemy)
├── wtb_executions         ✅ Designed (InMemory + SQLAlchemy)
├── wtb_node_boundaries    ✅ Designed (pointers to AgentGit checkpoints)
├── wtb_checkpoint_files   ✅ Designed (links to FileTracker commits)
├── wtb_node_variants      ✅ Designed (InMemory + SQLAlchemy)
└── wtb_evaluation_results ✅ Designed (InMemory + SQLAlchemy)

WTB Storage Abstraction:
├── IUnitOfWork            ✅ Designed (interface)
├── InMemoryUnitOfWork     ✅ Designed (for tests)
├── SQLAlchemyUnitOfWork   ✅ Designed (for production)
├── IRepository<T>         ✅ Designed (generic interface)
├── InMemory*Repository    ✅ Designed (6 repos)
└── SQLAlchemy*Repository  ⏳ Pending implementation
```

### Key Test Files

```
tests/
├── test_agentgit_v2.py     # 94 tests - Core infrastructure
├── test_file_processing.py # 40+ tests - FileTracker
└── TEST_SUMMARY.md         # Quick reference for integration patterns
```

---

## 16. Parallel Internal Session Design (Batch Testing)

### 16.1 Problem Statement

BatchTestRunner needs to execute multiple variant combinations **in parallel**, each with its own independent execution context including:
- InternalSession (checkpoint chain)
- StateAdapter (session tracking)
- ExecutionController (orchestration)

### 16.2 Current Architecture Concerns

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                    CURRENT DESIGN - SINGLE-THREADED LIMITATION                       │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  AgentGitStateAdapter                                                              │
│  ┌─────────────────────────────────────────────────────────────────────────────┐  │
│  │  _current_session: Optional[InternalSession_mdp] = None  ← 单例状态!         │  │
│  │  _current_execution_id: Optional[str] = None             ← 单例状态!         │  │
│  │  _tool_track_position: int = 0                           ← 单例状态!         │  │
│  └─────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                    │
│  问题:                                                                             │
│  • 一个 Adapter 实例只能跟踪一个 InternalSession                                   │
│  • 并行执行时，多个执行会覆盖 _current_session                                     │
│  • 导致状态混乱和数据损坏                                                          │
│                                                                                    │
│  SQLite 并发问题:                                                                  │
│  • SQLite 默认模式下写锁是排他的                                                   │
│  • parallel_count > 1 时可能导致 "database is locked" 错误                         │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

### 16.3 Recommended Solution: ParallelExecutionContext

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                    PARALLEL EXECUTION CONTEXT PATTERN                               │
├────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                    │
│  BatchTestRunner                                                                   │
│       │                                                                            │
│       ├── create_parallel_context(variant_combination) ──────────────────────────┐│
│       │       │                                                                   ││
│       │       │  创建独立的执行上下文:                                              ││
│       │       ├── ExecutionController instance (独立)                             ││
│       │       ├── AgentGitStateAdapter instance (独立)                            ││
│       │       ├── UnitOfWork instance (独立 DB session)                           ││
│       │       └── InternalSession (独立 checkpoint chain)                         ││
│       │                                                                           ││
│       └── ThreadPoolExecutor / asyncio.gather() ──────────────────────────────────┘│
│               │                                                                    │
│       ┌───────┼───────────────────────────────────────────────────┐               │
│       │       │                                                   │               │
│       ▼       ▼                                                   ▼               │
│  ┌────────────────┐  ┌────────────────┐              ┌────────────────┐           │
│  │   Context A    │  │   Context B    │     ...      │   Context N    │           │
│  │   ──────────   │  │   ──────────   │              │   ──────────   │           │
│  │                │  │                │              │                │           │
│  │  Adapter A     │  │  Adapter B     │              │  Adapter N     │           │
│  │  Session A     │  │  Session B     │              │  Session N     │           │
│  │  Controller A  │  │  Controller B  │              │  Controller N  │           │
│  │                │  │                │              │                │           │
│  │  独立 checkpoint │  │  独立 checkpoint │              │  独立 checkpoint │           │
│  │  chain         │  │  chain         │              │  chain         │           │
│  └────────────────┘  └────────────────┘              └────────────────┘           │
│         │                   │                              │                      │
│         │                   │                              │                      │
│         └───────────────────┴──────────────────────────────┘                      │
│                              │                                                     │
│                              ▼                                                     │
│                   ┌────────────────────────┐                                      │
│                   │    AgentGit Database   │                                      │
│                   │    (WAL Mode 启用)      │                                      │
│                   │                        │                                      │
│                   │  internal_sessions:    │                                      │
│                   │  ├── Session A (id=1)  │                                      │
│                   │  ├── Session B (id=2)  │                                      │
│                   │  └── Session N (id=N)  │                                      │
│                   └────────────────────────┘                                      │
│                                                                                    │
└────────────────────────────────────────────────────────────────────────────────────┘
```

### 16.4 Implementation Design

#### ParallelExecutionContext

```python
# wtb/application/services/parallel_context.py

from dataclasses import dataclass
from typing import Optional
from contextlib import contextmanager

@dataclass
class ParallelExecutionContext:
    """
    Isolated execution context for parallel batch testing.
    
    Each parallel execution gets its own:
    - StateAdapter (with its own current_session tracking)
    - ExecutionController
    - UnitOfWork (with its own DB session)
    """
    execution_controller: 'ExecutionController'
    state_adapter: 'IStateAdapter'
    unit_of_work: 'IUnitOfWork'
    variant_combination: 'VariantCombination'
    
    def __enter__(self) -> 'ParallelExecutionContext':
        self.unit_of_work.__enter__()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.unit_of_work.__exit__(exc_type, exc_val, exc_tb)


class ParallelContextFactory:
    """
    Factory for creating isolated parallel execution contexts.
    """
    
    def __init__(
        self,
        config: 'WTBConfig',
        workflow_repository: 'IWorkflowRepository',
    ):
        self._config = config
        self._workflow_repo = workflow_repository
    
    def create_context(
        self,
        variant_combination: 'VariantCombination',
    ) -> ParallelExecutionContext:
        """
        Create an isolated execution context.
        
        Each context has its own:
        - UnitOfWork (独立 DB session)
        - StateAdapter (独立 current_session)
        - ExecutionController
        """
        # Create independent UoW
        uow = UnitOfWorkFactory.create(
            mode=self._config.wtb_storage_mode,
            db_url=self._config.wtb_db_url,
        )
        
        # Create independent StateAdapter
        # 关键: 每个 parallel context 有自己的 adapter 实例!
        state_adapter = self._create_state_adapter()
        
        # Create independent ExecutionController
        controller = ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=self._workflow_repo,
            state_adapter=state_adapter,
        )
        
        return ParallelExecutionContext(
            execution_controller=controller,
            state_adapter=state_adapter,
            unit_of_work=uow,
            variant_combination=variant_combination,
        )
    
    def _create_state_adapter(self) -> 'IStateAdapter':
        """Create a new state adapter instance."""
        if self._config.state_adapter_mode == "inmemory":
            return InMemoryStateAdapter()
        else:
            return AgentGitStateAdapter(
                agentgit_db_path=self._config.agentgit_db_path,
                wtb_db_url=self._config.wtb_db_url,
            )
```

#### BatchTestRunner with Parallel Execution

```python
# wtb/application/services/batch_test_runner.py

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List

class BatchTestRunner:
    """
    Orchestrates parallel execution of batch A/B tests.
    
    Key Design:
    - Each variant combination runs in its own ParallelExecutionContext
    - Contexts are fully isolated (no shared state)
    - SQLite uses WAL mode for concurrent writes
    """
    
    def __init__(
        self,
        context_factory: ParallelContextFactory,
        max_workers: Optional[int] = None,
    ):
        self._context_factory = context_factory
        self._max_workers = max_workers
    
    def run_batch_test(self, batch_test: BatchTest) -> BatchTest:
        """Run all variant combinations in parallel."""
        batch_test.start()
        
        try:
            # Determine parallelism
            workers = min(
                batch_test.parallel_count,
                len(batch_test.variant_combinations),
                self._max_workers or 4,
            )
            
            # Execute in parallel
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        self._run_single_variant,
                        batch_test.workflow_id,
                        combination,
                        batch_test.initial_state,
                    ): combination
                    for combination in batch_test.variant_combinations
                }
                
                for future in as_completed(futures):
                    combination = futures[future]
                    try:
                        result = future.result()
                        batch_test.add_result(result)
                    except Exception as e:
                        batch_test.add_result(BatchTestResult(
                            combination_name=combination.name,
                            execution_id="",
                            success=False,
                            error_message=str(e),
                        ))
            
            batch_test.complete()
            batch_test.build_comparison_matrix()
            
        except Exception as e:
            batch_test.fail(str(e))
        
        return batch_test
    
    def _run_single_variant(
        self,
        workflow_id: str,
        combination: VariantCombination,
        initial_state: Dict[str, Any],
    ) -> BatchTestResult:
        """
        Run a single variant combination in isolated context.
        
        关键: 每个 variant 在独立的 context 中运行!
        """
        # Create isolated context
        context = self._context_factory.create_context(combination)
        
        with context:
            # Get workflow
            workflow = context.unit_of_work.workflows.get_by_id(workflow_id)
            if not workflow:
                raise ValueError(f"Workflow {workflow_id} not found")
            
            # Apply variants
            modified_workflow = self._apply_variants(workflow, combination)
            
            # Create and run execution
            execution = context.execution_controller.create_execution(
                workflow=modified_workflow,
                initial_state=initial_state,
            )
            
            start_time = datetime.now()
            result_execution = context.execution_controller.run(execution.id)
            duration = (datetime.now() - start_time).total_seconds() * 1000
            
            context.unit_of_work.commit()
            
            return BatchTestResult(
                combination_name=combination.name,
                execution_id=execution.id,
                success=result_execution.status == ExecutionStatus.COMPLETED,
                duration_ms=int(duration),
                error_message=result_execution.error_message,
            )
```

### 16.5 SQLite WAL Mode Configuration

```python
# wtb/infrastructure/database/config.py

def configure_sqlite_for_concurrent_access(db_path: str) -> Engine:
    """
    Configure SQLite with WAL mode for better concurrent access.
    
    WAL (Write-Ahead Logging) allows:
    - Multiple readers simultaneously
    - One writer while readers are active
    - Better performance for concurrent workloads
    """
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={
            "check_same_thread": False,  # Allow multi-threaded access
            "timeout": 30,  # Wait up to 30s for lock
        },
        pool_pre_ping=True,
    )
    
    # Enable WAL mode
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA synchronous=NORMAL"))
        conn.execute(text("PRAGMA busy_timeout=30000"))  # 30 seconds
    
    return engine
```

### 16.6 Session Lifecycle Management

```python
# wtb/infrastructure/adapters/session_manager.py

class SessionLifecycleManager:
    """
    Manages cleanup and timeout for parallel sessions.
    """
    
    def __init__(
        self,
        session_repo: InternalSessionRepository,
        timeout_seconds: int = 3600,  # 1 hour default
    ):
        self._session_repo = session_repo
        self._timeout_seconds = timeout_seconds
    
    def cleanup_abandoned_sessions(self) -> int:
        """
        Clean up sessions that have been abandoned.
        
        A session is considered abandoned if:
        - Status is "running" but last activity > timeout
        - Has no checkpoints created in timeout period
        """
        cutoff = datetime.now() - timedelta(seconds=self._timeout_seconds)
        
        abandoned = self._session_repo.get_inactive_since(cutoff)
        
        for session in abandoned:
            session.is_current = False
            session.metadata["abandoned_at"] = datetime.now().isoformat()
            session.metadata["abandon_reason"] = "timeout"
            self._session_repo.update(session)
        
        return len(abandoned)
    
    def get_active_session_count(self) -> int:
        """Get count of currently active sessions."""
        return self._session_repo.count_active()
```

### 16.7 Design Decision Summary

| Aspect | Decision | Rationale |
|--------|----------|-----------|
| **Context Isolation** | Each parallel execution gets own StateAdapter, Controller, UoW | Prevents state corruption from shared mutable state |
| **Thread Safety** | ThreadPoolExecutor with isolated contexts | Python GIL + isolated contexts ensures safety |
| **SQLite Concurrency** | WAL mode + busy_timeout | Allows concurrent reads, serialized writes with retry |
| **Session Tracking** | No shared _current_session | Each adapter tracks its own session independently |
| **Resource Cleanup** | SessionLifecycleManager with timeout | Prevents resource leaks from abandoned sessions |

### 16.8 Test Strategy for Parallel Sessions

```python
# tests/test_wtb/test_parallel_sessions.py

class TestParallelSessionIsolation:
    """Tests for parallel session isolation."""
    
    def test_parallel_sessions_do_not_share_state(self):
        """Verify parallel sessions are fully isolated."""
        factory = ParallelContextFactory(config, workflow_repo)
        
        context_a = factory.create_context(variant_a)
        context_b = factory.create_context(variant_b)
        
        # Different adapter instances
        assert context_a.state_adapter is not context_b.state_adapter
        
        # Initialize sessions
        session_a = context_a.state_adapter.initialize_session("exec_a", state)
        session_b = context_b.state_adapter.initialize_session("exec_b", state)
        
        # Different session IDs
        assert session_a != session_b
        
        # Each adapter tracks its own session
        assert context_a.state_adapter._current_session.id == session_a
        assert context_b.state_adapter._current_session.id == session_b
    
    def test_parallel_checkpoints_are_independent(self):
        """Verify checkpoints from parallel sessions don't interfere."""
        # Run two sessions in parallel
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_a = executor.submit(run_workflow_in_context, context_a)
            future_b = executor.submit(run_workflow_in_context, context_b)
            
            result_a = future_a.result()
            result_b = future_b.result()
        
        # Each execution has its own checkpoint chain
        checkpoints_a = checkpoint_repo.get_by_session(result_a.session_id)
        checkpoints_b = checkpoint_repo.get_by_session(result_b.session_id)
        
        # No overlap in checkpoint IDs
        ids_a = {cp.id for cp in checkpoints_a}
        ids_b = {cp.id for cp in checkpoints_b}
        assert ids_a.isdisjoint(ids_b)
```