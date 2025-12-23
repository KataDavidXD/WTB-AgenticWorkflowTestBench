# WTB + AgentGit + FileTracker: Database Design (Option B - Anti-Corruption Layer)

## Design Philosophy

> **"Design at the smallest granularity, separate concerns, compose at higher levels."**
> 
> **"Use Anti-Corruption Layer - WTB owns its semantic layer, AgentGit remains unchanged."**

> **"WTB uses dual database patterns: AgentGit repositories for checkpoint state; SQLAlchemy UoW for WTB domain data. Never mix - adapters bridge the boundary."**

> **"Single Checkpoint Type + Node Boundary Pointers: All checkpoints are atomic at tool/message level. Node boundaries are POINTERS in WTB's wtb_node_boundaries table, NOT separate checkpoints."**

### WTB Storage Strategy: Dual Implementation Pattern

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                     WTB STORAGE ABSTRACTION                                      │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  PRINCIPLE: "Interface → Multiple Implementations"                              │
│                                                                                  │
│  Application Layer (ExecutionController, NodeReplacer, etc.)                    │
│         │                                                                        │
│         │ depends on abstractions only                                          │
│         ▼                                                                        │
│  ┌──────────────────────────────────────────────────────────────────────────┐   │
│  │                        IUnitOfWork (interface)                            │   │
│  │  ├── workflows: IWorkflowRepository                                       │   │
│  │  ├── executions: IExecutionRepository                                     │   │
│  │  ├── node_boundaries: INodeBoundaryRepository                             │   │
│  │  └── checkpoint_files: ICheckpointFileRepository                          │   │
│  └──────────────────────────────────────────────────────────────────────────┘   │
│                             │                                                    │
│         ┌───────────────────┼───────────────────┐                               │
│         │                   │                   │                               │
│         ▼                   ▼                   ▼                               │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐                    │
│  │   InMemory     │  │  SQLAlchemy    │  │   Future:      │                    │
│  │   UnitOfWork   │  │  UnitOfWork    │  │  PostgreSQL    │                    │
│  │                │  │                │  │  UnitOfWork    │                    │
│  │  For testing,  │  │  For production│  │                │                    │
│  │  dev, and fast │  │  with SQLite   │  │  For scaled    │                    │
│  │  iteration     │  │  persistence   │  │  deployments   │                    │
│  └────────────────┘  └────────────────┘  └────────────────┘                    │
│                                                                                  │
│  BENEFITS:                                                                       │
│  ├── Tests run fast with in-memory (no I/O)                                     │
│  ├── Production uses persistent storage                                          │
│  ├── Easy to switch via dependency injection                                     │
│  └── Same interface, different implementations                                   │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Architecture Decision: Option B

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                     Two-Database Architecture                                    │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│   AgentGit Database (UNCHANGED)              WTB Database (NEW)                 │
│   ════════════════════════════              ═══════════════════                 │
│   SQLite: agentgit.db                        SQLite/PostgreSQL: wtb.db          │
│                                                                                  │
│   ┌─────────────────────────┐               ┌─────────────────────────┐         │
│   │ users                   │               │ wtb_workflows           │         │
│   │ external_sessions       │               │ wtb_executions          │         │
│   │ internal_sessions       │               │ wtb_node_boundaries     │←──FK────│
│   │ checkpoints             │←──────────────│ wtb_checkpoint_files    │         │
│   └─────────────────────────┘               │ wtb_evaluation_results  │         │
│                                             └─────────────────────────┘         │
│           │                                                                      │
│           │                                 FileTracker Database                │
│           │                                 ════════════════════                │
│           │                                 PostgreSQL: filetracker.db          │
│           │                                                                      │
│           │                                 ┌─────────────────────────┐         │
│           └─────────────────────────────────│ commits                 │         │
│             (via wtb_checkpoint_files)      │ file_blobs              │         │
│                                             │ file_mementos           │         │
│                                             └─────────────────────────┘         │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Core Principles

1. **AgentGit is UNCHANGED** - No modifications to the installed package
2. **WTB owns semantic layer** - Node boundaries, file links are WTB concepts
3. **Cross-database references** - WTB stores AgentGit checkpoint IDs
4. **Adapter handles translation** - `AgentGitStateAdapter` maps between domains

---

## Abstraction Layers

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         LAYERED ARCHITECTURE                                     │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                        Application Layer                                 │   │
│  │                                                                          │   │
│  │   ExecutionController    NodeReplacer    EvaluationEngine               │   │
│  │         │                    │                 │                         │   │
│  │         │                    │                 │                         │   │
│  │         ▼                    ▼                 ▼                         │   │
│  │   ┌──────────────────────────────────────────────────────────────────┐  │   │
│  │   │                    IStateAdapter                                  │  │   │
│  │   │         (Anti-Corruption Layer / Interface)                       │  │   │
│  │   │                                                                   │  │   │
│  │   │   High-level operations: save_checkpoint, mark_node_completed,   │  │   │
│  │   │   rollback, link_file_commit - abstracts ALL database concerns   │  │   │
│  │   └───────────────────────────────────────────────────────────────────┘  │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                      │                                          │
│                                      ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                        Domain Layer                                      │   │
│  │                                                                          │   │
│  │   AgentGitStateAdapter  ──────────────────►  FileTrackerAdapter         │   │
│  │         │                                           │                    │   │
│  │         │ Uses AgentGit's existing                  │ Uses FileTracker's │   │
│  │         │ domain objects directly                   │ existing APIs      │   │
│  │         ▼                                           ▼                    │   │
│  │   ┌─────────────────┐                      ┌─────────────────┐          │   │
│  │   │ Checkpoint      │                      │ Commit          │          │   │
│  │   │ InternalSession │                      │ FileMemento     │          │   │
│  │   │ ExternalSession │                      │ Blob            │          │   │
│  │   │ ToolManager     │                      └─────────────────┘          │   │
│  │   └─────────────────┘                                                    │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                      │                                          │
│                                      ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                   Infrastructure Layer (Repository + UoW)                │   │
│  │                                                                          │   │
│  │   ┌────────────────────────────────────────────────────────────────┐    │   │
│  │   │                      Unit of Work                               │    │   │
│  │   │                                                                 │    │   │
│  │   │   Manages transaction boundaries across multiple repositories  │    │   │
│  │   │   begin() / commit() / rollback()                              │    │   │
│  │   └─────────────────────────────┬──────────────────────────────────┘    │   │
│  │                                  │                                       │   │
│  │   ┌──────────────────────────────┼──────────────────────────────────┐   │   │
│  │   │                              ▼                                   │   │   │
│  │   │   Repositories (Abstract Interface + Concrete Implementation)  │   │   │
│  │   │                                                                  │   │   │
│  │   │   AgentGit Repos (existing):       WTB Repos (new):             │   │   │
│  │   │   ├── CheckpointRepository         ├── NodeBoundaryRepository   │   │   │
│  │   │   ├── InternalSessionRepository    ├── CheckpointFileRepository │   │   │
│  │   │   ├── ExternalSessionRepository    ├── WorkflowRepository       │   │   │
│  │   │   └── UserRepository               └── ExecutionRepository      │   │   │
│  │   │                                                                  │   │   │
│  │   │   FileTracker Repos (existing):                                 │   │   │
│  │   │   ├── CommitRepository                                          │   │   │
│  │   │   ├── BlobRepository                                            │   │   │
│  │   │   └── FileMementoRepository                                     │   │   │
│  │   └──────────────────────────────────────────────────────────────────┘   │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                      │                                          │
│                                      ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                        Data Access Layer                                 │   │
│  │                                                                          │   │
│  │   ┌─────────────────────────────────────────────────────────────────┐   │   │
│  │   │                    SQLAlchemy ORM                                │   │   │
│  │   │                                                                  │   │   │
│  │   │   Engine / Session Factory / Declarative Base                   │   │   │
│  │   │   Model definitions (WTB only - AgentGit uses raw SQL)          │   │   │
│  │   └─────────────────────────────────────────────────────────────────┘   │   │
│  │                                  │                                       │   │
│  │   ┌──────────────────────────────▼──────────────────────────────────┐   │   │
│  │   │                    Databases                                     │   │   │
│  │   │                                                                  │   │   │
│  │   │   agentgit.db (SQLite)    wtb.db (SQLite/PG)    filetracker (PG)│   │   │
│  │   └──────────────────────────────────────────────────────────────────┘   │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## Database Schemas

### 1. AgentGit Database (UNCHANGED - Reference Only)

These tables exist in `agentgit.db` and are managed by the AgentGit package.
WTB does NOT modify these - it only reads/writes through AgentGit's existing APIs.

```sql
-- ═══════════════════════════════════════════════════════════════════════════════
-- AGENTGIT SCHEMA (Reference - DO NOT MODIFY)
-- Source: AgentGit package, verified via tests/test_agentgit_v2.py
-- ═══════════════════════════════════════════════════════════════════════════════

-- FK Initialization Order: users → external_sessions → internal_sessions → checkpoints

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER DEFAULT 0,
    created_at TEXT,
    last_login TEXT,
    data TEXT,
    api_key TEXT,
    session_limit INTEGER DEFAULT 5
);

CREATE TABLE external_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    session_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    is_active INTEGER DEFAULT 1,
    data TEXT,                    -- JSON: session data
    metadata TEXT,                -- JSON: custom metadata
    branch_count INTEGER DEFAULT 0,
    total_checkpoints INTEGER DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE internal_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_session_id INTEGER NOT NULL,
    langgraph_session_id TEXT UNIQUE NOT NULL,
    state_data TEXT,              -- JSON: LangGraph state
    conversation_history TEXT,    -- JSON: messages
    created_at TEXT NOT NULL,
    is_current INTEGER DEFAULT 0,
    checkpoint_count INTEGER DEFAULT 0,
    parent_session_id INTEGER,
    branch_point_checkpoint_id INTEGER,
    tool_invocation_count INTEGER DEFAULT 0,
    metadata TEXT,                -- JSON: includes MDP state for InternalSession_mdp
    FOREIGN KEY (external_session_id) REFERENCES external_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_session_id) REFERENCES internal_sessions(id) ON DELETE SET NULL,
    FOREIGN KEY (branch_point_checkpoint_id) REFERENCES checkpoints(id) ON DELETE SET NULL
);

CREATE TABLE checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    internal_session_id INTEGER NOT NULL,
    checkpoint_name TEXT,
    checkpoint_data TEXT NOT NULL,  -- JSON: full state snapshot
    is_auto INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    user_id INTEGER,
    -- NOTE: AgentGit stores these in checkpoint_data/metadata JSON:
    --   metadata.checkpoint_type = "node" | "tool_message"
    --   metadata.parent_checkpoint_id (for tool_message)
    --   metadata.mdp_state = {current_node_id, workflow_variables, execution_path}
    --   metadata.audit_trail = {...}
    --   metadata.tool_track_position
    FOREIGN KEY (internal_session_id) REFERENCES internal_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);

-- Indexes (from AgentGit)
CREATE INDEX idx_external_sessions_user ON external_sessions(user_id);
CREATE INDEX idx_internal_sessions_external ON internal_sessions(external_session_id);
CREATE INDEX idx_internal_sessions_langgraph ON internal_sessions(langgraph_session_id);
CREATE INDEX idx_internal_sessions_parent ON internal_sessions(parent_session_id);
CREATE INDEX idx_checkpoints_session ON checkpoints(internal_session_id);
```

### 2. WTB Database (NEW - WTB Owned)

These tables are owned by WTB and stored in `wtb.db` (or a PostgreSQL database).
They reference AgentGit checkpoint IDs via cross-database foreign keys (logical, not enforced).

```sql
-- ═══════════════════════════════════════════════════════════════════════════════
-- WTB SCHEMA (NEW - WTB Owns)
-- Anti-Corruption Layer tables that bridge WTB concepts with AgentGit
-- ═══════════════════════════════════════════════════════════════════════════════

-- -----------------------------------------------------------------------------
-- NODE BOUNDARIES: Semantic markers pointing to AgentGit checkpoints
-- -----------------------------------------------------------------------------
-- This table separates "node completion" concept from "checkpoint" concept.
-- A node boundary is NOT a checkpoint - it's a pointer to the first/last
-- checkpoint within that node's execution.

CREATE TABLE wtb_node_boundaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    
    -- WTB Execution Context
    execution_id TEXT NOT NULL,          -- WTB execution UUID
    
    -- AgentGit References (cross-database, logical FK)
    internal_session_id INTEGER NOT NULL, -- → agentgit.internal_sessions.id
    
    -- Node Identification
    node_id TEXT NOT NULL,               -- Workflow node name
    
    -- Checkpoint Pointers (cross-database, logical FK to agentgit.checkpoints.id)
    entry_checkpoint_id INTEGER,         -- First checkpoint when entering node
    exit_checkpoint_id INTEGER,          -- Last checkpoint when exiting node (rollback target)
    
    -- Node Execution Status
    node_status TEXT NOT NULL CHECK (node_status IN ('started', 'completed', 'failed', 'skipped')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    
    -- Metrics
    tool_count INTEGER DEFAULT 0,
    checkpoint_count INTEGER DEFAULT 0,
    duration_ms INTEGER,
    
    -- Error Info
    error_message TEXT,
    error_details TEXT,                  -- JSON
    
    -- Uniqueness: One boundary per node per session
    UNIQUE(internal_session_id, node_id)
);

CREATE INDEX idx_wtb_node_boundaries_session ON wtb_node_boundaries(internal_session_id);
CREATE INDEX idx_wtb_node_boundaries_execution ON wtb_node_boundaries(execution_id);
CREATE INDEX idx_wtb_node_boundaries_status ON wtb_node_boundaries(node_status);

-- -----------------------------------------------------------------------------
-- CHECKPOINT-FILE LINKS: Bridge AgentGit checkpoints to FileTracker commits
-- -----------------------------------------------------------------------------
-- Each WTB checkpoint can optionally link to a FileTracker commit.
-- This enables coordinated rollback of both agent state AND file state.

CREATE TABLE wtb_checkpoint_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    
    -- AgentGit Reference (cross-database, logical FK)
    checkpoint_id INTEGER NOT NULL,      -- → agentgit.checkpoints.id
    
    -- FileTracker Reference (cross-database, logical FK)
    file_commit_id TEXT NOT NULL,        -- → filetracker.commits.commit_id (UUID)
    
    -- Summary (denormalized for quick access without querying FileTracker)
    file_count INTEGER DEFAULT 0,
    total_size_bytes INTEGER DEFAULT 0,
    
    created_at TEXT NOT NULL,
    
    -- One file commit per checkpoint
    UNIQUE(checkpoint_id)
);

CREATE INDEX idx_wtb_checkpoint_files_checkpoint ON wtb_checkpoint_files(checkpoint_id);
CREATE INDEX idx_wtb_checkpoint_files_commit ON wtb_checkpoint_files(file_commit_id);

-- -----------------------------------------------------------------------------
-- WORKFLOWS: WTB workflow definitions
-- -----------------------------------------------------------------------------
CREATE TABLE wtb_workflows (
    id TEXT PRIMARY KEY,                 -- UUID
    name TEXT NOT NULL,
    definition TEXT NOT NULL,            -- JSON: workflow graph definition
    version TEXT DEFAULT '1.0.0',
    created_at TEXT NOT NULL,
    updated_at TEXT,
    metadata TEXT                        -- JSON
);

-- -----------------------------------------------------------------------------
-- EXECUTIONS: WTB execution records
-- -----------------------------------------------------------------------------
CREATE TABLE wtb_executions (
    id TEXT PRIMARY KEY,                 -- UUID
    workflow_id TEXT NOT NULL,
    
    -- AgentGit References
    external_session_id INTEGER,         -- → agentgit.external_sessions.id
    internal_session_id INTEGER,         -- → agentgit.internal_sessions.id (current)
    
    -- Execution State
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'paused', 'completed', 'failed', 'cancelled')),
    current_node_id TEXT,
    
    -- Timestamps
    started_at TEXT,
    completed_at TEXT,
    paused_at TEXT,
    
    -- Configuration
    config TEXT,                         -- JSON: execution config
    metadata TEXT,                       -- JSON
    
    FOREIGN KEY (workflow_id) REFERENCES wtb_workflows(id)
);

CREATE INDEX idx_wtb_executions_workflow ON wtb_executions(workflow_id);
CREATE INDEX idx_wtb_executions_status ON wtb_executions(status);

-- -----------------------------------------------------------------------------
-- NODE VARIANTS: A/B testing node implementations
-- -----------------------------------------------------------------------------
CREATE TABLE wtb_node_variants (
    id TEXT PRIMARY KEY,                 -- UUID
    node_id TEXT NOT NULL,               -- Original node ID
    variant_name TEXT NOT NULL,
    implementation TEXT NOT NULL,        -- JSON: variant definition
    is_active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    metadata TEXT,                       -- JSON
    
    UNIQUE(node_id, variant_name)
);

-- -----------------------------------------------------------------------------
-- EVALUATION RESULTS: Test run metrics and scores
-- -----------------------------------------------------------------------------
CREATE TABLE wtb_evaluation_results (
    id TEXT PRIMARY KEY,                 -- UUID
    execution_id TEXT NOT NULL,
    
    -- Metrics
    metrics TEXT NOT NULL,               -- JSON: {accuracy, latency, cost, etc.}
    overall_score REAL,
    
    -- Evaluation Context
    evaluator_name TEXT,
    evaluated_at TEXT NOT NULL,
    
    FOREIGN KEY (execution_id) REFERENCES wtb_executions(id)
);

CREATE INDEX idx_wtb_evaluation_results_execution ON wtb_evaluation_results(execution_id);
```

### 3. FileTracker Database (Reference Only)

FileTracker uses PostgreSQL. WTB interacts via the existing FileTracker APIs.

```sql
-- ═══════════════════════════════════════════════════════════════════════════════
-- FILETRACKER SCHEMA (Reference - existing PostgreSQL database)
-- Source: file_processing package, verified via tests/test_file_processing.py
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE commits (
    commit_id VARCHAR(64) PRIMARY KEY,   -- UUID
    timestamp TIMESTAMP NOT NULL,
    message TEXT
);

CREATE TABLE file_blobs (
    content_hash VARCHAR(64) PRIMARY KEY, -- SHA-256
    storage_location TEXT NOT NULL,       -- objects/{hash[:2]}/{hash[2:]}
    size_bytes INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE file_mementos (
    id SERIAL PRIMARY KEY,
    commit_id VARCHAR(64) NOT NULL,
    file_path TEXT NOT NULL,
    file_hash VARCHAR(64) NOT NULL,
    file_size INTEGER NOT NULL,
    FOREIGN KEY (commit_id) REFERENCES commits(commit_id),
    FOREIGN KEY (file_hash) REFERENCES file_blobs(content_hash)
);

CREATE INDEX idx_file_mementos_commit ON file_mementos(commit_id);
```

---

## Entity Relationship Diagram

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                           THREE-DATABASE ARCHITECTURE                                    │
└─────────────────────────────────────────────────────────────────────────────────────────┘

  AGENTGIT DATABASE                    WTB DATABASE                    FILETRACKER DATABASE
  (SQLite - unchanged)                 (SQLite/PostgreSQL)             (PostgreSQL)
  ══════════════════                   ═══════════════════             ════════════════════

  ┌─────────────────┐                                                  
  │     users       │                  ┌─────────────────────┐         
  │─────────────────│                  │   wtb_workflows     │         
  │ id (PK)         │                  │─────────────────────│         
  │ username        │                  │ id (PK)             │         
  └────────┬────────┘                  │ name, definition    │         
           │ 1:N                       └──────────┬──────────┘         
           ▼                                      │ 1:N                
  ┌─────────────────┐                             ▼                    
  │external_sessions│                  ┌─────────────────────┐         
  │─────────────────│◄─────────────────│   wtb_executions    │         
  │ id (PK)         │  logical FK      │─────────────────────│         
  │ user_id (FK)    │                  │ id (PK)             │         
  │ session_name    │                  │ workflow_id (FK)    │         
  └────────┬────────┘                  │ external_session_id │─────────┤
           │ 1:N                       │ internal_session_id │─────────┤
           ▼                           │ status              │         │
  ┌─────────────────┐                  └─────────────────────┘         │
  │internal_sessions│◄─────────────────────────────────────────────────┤
  │─────────────────│  logical FK                                      │
  │ id (PK)         │                                                  │
  │ external_session│                  ┌─────────────────────┐         │
  │ parent_session  │◄─────────────────│ wtb_node_boundaries │         │
  │ langgraph_id    │  logical FK      │─────────────────────│         │
  │ metadata (JSON) │                  │ id (PK)             │         │
  │  └─ mdp_state   │                  │ internal_session_id │─────────┤
  └────────┬────────┘                  │ node_id             │         │
           │ 1:N                       │ entry_checkpoint_id │──────┐  │
           ▼                           │ exit_checkpoint_id  │──────┤  │
  ┌─────────────────┐                  │ node_status         │      │  │
  │   checkpoints   │◄─────────────────└─────────────────────┘      │  │
  │─────────────────│  logical FK              ▲                    │  │
  │ id (PK)         │◄─────────────────────────┴────────────────────┘  │
  │ internal_session│                                                  │
  │ checkpoint_data │                  ┌─────────────────────┐         │
  │  └─ metadata    │◄─────────────────│wtb_checkpoint_files │         │
  │     └─ type     │  logical FK      │─────────────────────│         │
  │     └─ mdp_state│                  │ checkpoint_id       │─────────┤
  │     └─ audit    │                  │ file_commit_id      │─────────┼───┐
  └─────────────────┘                  │ file_count          │         │   │
                                       └─────────────────────┘         │   │
                                                                       │   │
                                                                       │   │
                                                   ┌───────────────────┘   │
                                                   ▼                       │
                                       ┌─────────────────────┐             │
                                       │      commits        │◄────────────┘
                                       │─────────────────────│  logical FK
                                       │ commit_id (PK)      │
                                       │ timestamp           │
                                       │ message             │
                                       └──────────┬──────────┘
                                                  │ 1:N
                                                  ▼
                                       ┌─────────────────────┐
                                       │   file_mementos     │
                                       │─────────────────────│
                                       │ id (PK)             │
                                       │ commit_id (FK)      │
                                       │ file_path           │
                                       │ file_hash (FK)      │──────┐
                                       │ file_size           │      │
                                       └─────────────────────┘      │
                                                                    │
                                       ┌─────────────────────┐      │
                                       │    file_blobs       │◄─────┘
                                       │─────────────────────│
                                       │ content_hash (PK)   │
                                       │ storage_location    │
                                       │ size_bytes          │
                                       └─────────────────────┘
```

---

## Repository + Unit of Work Pattern

### Interface Definitions

```python
# ═══════════════════════════════════════════════════════════════════════════════
# wtb/infrastructure/interfaces/repository.py
# ═══════════════════════════════════════════════════════════════════════════════

from abc import ABC, abstractmethod
from typing import TypeVar, Generic, Optional, List

T = TypeVar('T')

class IRepository(ABC, Generic[T]):
    """Base repository interface following Repository pattern."""
    
    @abstractmethod
    def get_by_id(self, id: int | str) -> Optional[T]:
        """Get entity by ID."""
        pass
    
    @abstractmethod
    def add(self, entity: T) -> T:
        """Add new entity."""
        pass
    
    @abstractmethod
    def update(self, entity: T) -> T:
        """Update existing entity."""
        pass
    
    @abstractmethod
    def delete(self, id: int | str) -> bool:
        """Delete entity by ID."""
        pass


class INodeBoundaryRepository(IRepository['NodeBoundary']):
    """Repository for WTB node boundaries."""
    
    @abstractmethod
    def get_by_session(self, internal_session_id: int) -> List['NodeBoundary']:
        """Get all boundaries for a session."""
        pass
    
    @abstractmethod
    def get_by_node(self, internal_session_id: int, node_id: str) -> Optional['NodeBoundary']:
        """Get boundary for specific node."""
        pass
    
    @abstractmethod
    def get_completed(self, internal_session_id: int) -> List['NodeBoundary']:
        """Get completed node boundaries (for rollback targets)."""
        pass


class ICheckpointFileRepository(IRepository['CheckpointFile']):
    """Repository for checkpoint-file links."""
    
    @abstractmethod
    def get_by_checkpoint(self, checkpoint_id: int) -> Optional['CheckpointFile']:
        """Get file link for checkpoint."""
        pass
    
    @abstractmethod
    def get_by_file_commit(self, file_commit_id: str) -> List['CheckpointFile']:
        """Get all checkpoints linked to a file commit."""
        pass
```

### Unit of Work

```python
# ═══════════════════════════════════════════════════════════════════════════════
# wtb/infrastructure/interfaces/unit_of_work.py
# ═══════════════════════════════════════════════════════════════════════════════

from abc import ABC, abstractmethod
from contextlib import contextmanager

class IUnitOfWork(ABC):
    """
    Unit of Work pattern - manages transaction boundaries.
    
    Usage:
        with uow:
            uow.node_boundaries.add(boundary)
            uow.checkpoint_files.add(file_link)
            uow.commit()  # Both saved atomically
    """
    
    node_boundaries: 'INodeBoundaryRepository'
    checkpoint_files: 'ICheckpointFileRepository'
    executions: 'IExecutionRepository'
    workflows: 'IWorkflowRepository'
    
    @abstractmethod
    def __enter__(self) -> 'IUnitOfWork':
        pass
    
    @abstractmethod
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass
    
    @abstractmethod
    def commit(self):
        """Commit all changes."""
        pass
    
    @abstractmethod
    def rollback(self):
        """Rollback all changes."""
        pass
```

### SQLAlchemy Implementation

```python
# ═══════════════════════════════════════════════════════════════════════════════
# wtb/infrastructure/database/models.py
# SQLAlchemy ORM models for WTB-owned tables
# ═══════════════════════════════════════════════════════════════════════════════

from sqlalchemy import Column, Integer, String, Text, ForeignKey, UniqueConstraint, CheckConstraint
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()

class NodeBoundaryORM(Base):
    """ORM model for wtb_node_boundaries table."""
    
    __tablename__ = 'wtb_node_boundaries'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    execution_id = Column(String(64), nullable=False, index=True)
    internal_session_id = Column(Integer, nullable=False, index=True)
    node_id = Column(String(255), nullable=False)
    entry_checkpoint_id = Column(Integer, nullable=True)
    exit_checkpoint_id = Column(Integer, nullable=True)
    node_status = Column(String(20), nullable=False)
    started_at = Column(Text, nullable=False)
    completed_at = Column(Text, nullable=True)
    tool_count = Column(Integer, default=0)
    checkpoint_count = Column(Integer, default=0)
    duration_ms = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    error_details = Column(Text, nullable=True)  # JSON
    
    __table_args__ = (
        UniqueConstraint('internal_session_id', 'node_id', name='uq_session_node'),
        CheckConstraint(
            "node_status IN ('started', 'completed', 'failed', 'skipped')",
            name='ck_node_status'
        ),
    )


class CheckpointFileORM(Base):
    """ORM model for wtb_checkpoint_files table."""
    
    __tablename__ = 'wtb_checkpoint_files'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    checkpoint_id = Column(Integer, nullable=False, unique=True, index=True)
    file_commit_id = Column(String(64), nullable=False, index=True)
    file_count = Column(Integer, default=0)
    total_size_bytes = Column(Integer, default=0)
    created_at = Column(Text, nullable=False, default=lambda: datetime.utcnow().isoformat())


class ExecutionORM(Base):
    """ORM model for wtb_executions table."""
    
    __tablename__ = 'wtb_executions'
    
    id = Column(String(64), primary_key=True)
    workflow_id = Column(String(64), ForeignKey('wtb_workflows.id'), nullable=False)
    external_session_id = Column(Integer, nullable=True)
    internal_session_id = Column(Integer, nullable=True)
    status = Column(String(20), nullable=False)
    current_node_id = Column(String(255), nullable=True)
    started_at = Column(Text, nullable=True)
    completed_at = Column(Text, nullable=True)
    paused_at = Column(Text, nullable=True)
    config = Column(Text, nullable=True)  # JSON
    metadata = Column(Text, nullable=True)  # JSON
    
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'paused', 'completed', 'failed', 'cancelled')",
            name='ck_execution_status'
        ),
    )
```

### Concrete Repository Implementation

```python
# ═══════════════════════════════════════════════════════════════════════════════
# wtb/infrastructure/database/repositories/node_boundary_repository.py
# ═══════════════════════════════════════════════════════════════════════════════

from sqlalchemy.orm import Session
from typing import Optional, List

from ..models import NodeBoundaryORM
from ...interfaces.repository import INodeBoundaryRepository
from wtb.domain.models import NodeBoundary


class NodeBoundaryRepository(INodeBoundaryRepository):
    """SQLAlchemy implementation of node boundary repository."""
    
    def __init__(self, session: Session):
        self._session = session
    
    def _to_domain(self, orm: NodeBoundaryORM) -> NodeBoundary:
        """Convert ORM model to domain model."""
        return NodeBoundary(
            id=orm.id,
            execution_id=orm.execution_id,
            internal_session_id=orm.internal_session_id,
            node_id=orm.node_id,
            entry_checkpoint_id=orm.entry_checkpoint_id,
            exit_checkpoint_id=orm.exit_checkpoint_id,
            node_status=orm.node_status,
            started_at=orm.started_at,
            completed_at=orm.completed_at,
            tool_count=orm.tool_count,
            checkpoint_count=orm.checkpoint_count,
            duration_ms=orm.duration_ms,
            error_message=orm.error_message,
            error_details=orm.error_details
        )
    
    def _to_orm(self, domain: NodeBoundary) -> NodeBoundaryORM:
        """Convert domain model to ORM model."""
        return NodeBoundaryORM(
            id=domain.id,
            execution_id=domain.execution_id,
            internal_session_id=domain.internal_session_id,
            node_id=domain.node_id,
            entry_checkpoint_id=domain.entry_checkpoint_id,
            exit_checkpoint_id=domain.exit_checkpoint_id,
            node_status=domain.node_status,
            started_at=domain.started_at,
            completed_at=domain.completed_at,
            tool_count=domain.tool_count,
            checkpoint_count=domain.checkpoint_count,
            duration_ms=domain.duration_ms,
            error_message=domain.error_message,
            error_details=domain.error_details
        )
    
    def get_by_id(self, id: int) -> Optional[NodeBoundary]:
        orm = self._session.query(NodeBoundaryORM).filter_by(id=id).first()
        return self._to_domain(orm) if orm else None
    
    def add(self, entity: NodeBoundary) -> NodeBoundary:
        orm = self._to_orm(entity)
        self._session.add(orm)
        self._session.flush()  # Get the ID
        entity.id = orm.id
        return entity
    
    def update(self, entity: NodeBoundary) -> NodeBoundary:
        orm = self._session.query(NodeBoundaryORM).filter_by(id=entity.id).first()
        if orm:
            orm.exit_checkpoint_id = entity.exit_checkpoint_id
            orm.node_status = entity.node_status
            orm.completed_at = entity.completed_at
            orm.tool_count = entity.tool_count
            orm.checkpoint_count = entity.checkpoint_count
            orm.duration_ms = entity.duration_ms
            orm.error_message = entity.error_message
            orm.error_details = entity.error_details
            self._session.flush()
        return entity
    
    def delete(self, id: int) -> bool:
        orm = self._session.query(NodeBoundaryORM).filter_by(id=id).first()
        if orm:
            self._session.delete(orm)
            return True
        return False
    
    def get_by_session(self, internal_session_id: int) -> List[NodeBoundary]:
        orms = self._session.query(NodeBoundaryORM).filter_by(
            internal_session_id=internal_session_id
        ).all()
        return [self._to_domain(orm) for orm in orms]
    
    def get_by_node(self, internal_session_id: int, node_id: str) -> Optional[NodeBoundary]:
        orm = self._session.query(NodeBoundaryORM).filter_by(
            internal_session_id=internal_session_id,
            node_id=node_id
        ).first()
        return self._to_domain(orm) if orm else None
    
    def get_completed(self, internal_session_id: int) -> List[NodeBoundary]:
        orms = self._session.query(NodeBoundaryORM).filter_by(
            internal_session_id=internal_session_id,
            node_status='completed'
        ).all()
        return [self._to_domain(orm) for orm in orms]
```

### Unit of Work Implementation

```python
# ═══════════════════════════════════════════════════════════════════════════════
# wtb/infrastructure/database/unit_of_work.py
# ═══════════════════════════════════════════════════════════════════════════════

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from .models import Base
from .repositories.node_boundary_repository import NodeBoundaryRepository
from .repositories.checkpoint_file_repository import CheckpointFileRepository
from .repositories.execution_repository import ExecutionRepository
from .repositories.workflow_repository import WorkflowRepository
from ..interfaces.unit_of_work import IUnitOfWork


class SQLAlchemyUnitOfWork(IUnitOfWork):
    """SQLAlchemy implementation of Unit of Work."""
    
    def __init__(self, db_url: str = "sqlite:///wtb.db"):
        self._engine = create_engine(db_url)
        self._session_factory = sessionmaker(bind=self._engine)
        Base.metadata.create_all(self._engine)
        self._session: Session = None
    
    def __enter__(self) -> 'SQLAlchemyUnitOfWork':
        self._session = self._session_factory()
        
        # Initialize repositories with the session
        self.node_boundaries = NodeBoundaryRepository(self._session)
        self.checkpoint_files = CheckpointFileRepository(self._session)
        self.executions = ExecutionRepository(self._session)
        self.workflows = WorkflowRepository(self._session)
        
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        self._session.close()
    
    def commit(self):
        self._session.commit()
    
    def rollback(self):
        self._session.rollback()
```

### In-Memory Implementation (For Testing)

```python
# ═══════════════════════════════════════════════════════════════════════════════
# wtb/infrastructure/database/inmemory_unit_of_work.py
# In-memory implementation for testing - no database I/O
# ═══════════════════════════════════════════════════════════════════════════════

from typing import Dict, Optional, List
from wtb.domain.models import Workflow, Execution, NodeBoundary, CheckpointFile
from ..interfaces.unit_of_work import IUnitOfWork
from ..interfaces.repository import (
    IWorkflowRepository, IExecutionRepository,
    INodeBoundaryRepository, ICheckpointFileRepository
)


class InMemoryWorkflowRepository(IWorkflowRepository):
    """In-memory workflow repository for testing."""
    
    def __init__(self):
        self._store: Dict[str, Workflow] = {}
    
    def get_by_id(self, id: str) -> Optional[Workflow]:
        return self._store.get(id)
    
    def add(self, entity: Workflow) -> Workflow:
        self._store[entity.id] = entity
        return entity
    
    def update(self, entity: Workflow) -> Workflow:
        self._store[entity.id] = entity
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._store:
            del self._store[id]
            return True
        return False
    
    def list_all(self) -> List[Workflow]:
        return list(self._store.values())


class InMemoryExecutionRepository(IExecutionRepository):
    """In-memory execution repository for testing."""
    
    def __init__(self):
        self._store: Dict[str, Execution] = {}
    
    def get_by_id(self, id: str) -> Optional[Execution]:
        return self._store.get(id)
    
    def add(self, entity: Execution) -> Execution:
        self._store[entity.id] = entity
        return entity
    
    def update(self, entity: Execution) -> Execution:
        self._store[entity.id] = entity
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._store:
            del self._store[id]
            return True
        return False
    
    def get_by_workflow(self, workflow_id: str) -> List[Execution]:
        return [e for e in self._store.values() if e.workflow_id == workflow_id]
    
    def get_by_status(self, status: str) -> List[Execution]:
        return [e for e in self._store.values() if e.status == status]


class InMemoryNodeBoundaryRepository(INodeBoundaryRepository):
    """In-memory node boundary repository for testing."""
    
    def __init__(self):
        self._store: Dict[int, NodeBoundary] = {}
        self._next_id: int = 1
    
    def get_by_id(self, id: int) -> Optional[NodeBoundary]:
        return self._store.get(id)
    
    def add(self, entity: NodeBoundary) -> NodeBoundary:
        entity.id = self._next_id
        self._next_id += 1
        self._store[entity.id] = entity
        return entity
    
    def update(self, entity: NodeBoundary) -> NodeBoundary:
        self._store[entity.id] = entity
        return entity
    
    def delete(self, id: int) -> bool:
        if id in self._store:
            del self._store[id]
            return True
        return False
    
    def get_by_session(self, internal_session_id: int) -> List[NodeBoundary]:
        return [b for b in self._store.values() 
                if b.internal_session_id == internal_session_id]
    
    def get_by_node(self, internal_session_id: int, node_id: str) -> Optional[NodeBoundary]:
        for b in self._store.values():
            if b.internal_session_id == internal_session_id and b.node_id == node_id:
                return b
        return None
    
    def get_completed(self, internal_session_id: int) -> List[NodeBoundary]:
        return [b for b in self._store.values()
                if b.internal_session_id == internal_session_id 
                and b.node_status == 'completed']


class InMemoryCheckpointFileRepository(ICheckpointFileRepository):
    """In-memory checkpoint file repository for testing."""
    
    def __init__(self):
        self._store: Dict[int, CheckpointFile] = {}
        self._next_id: int = 1
    
    def get_by_id(self, id: int) -> Optional[CheckpointFile]:
        return self._store.get(id)
    
    def add(self, entity: CheckpointFile) -> CheckpointFile:
        entity.id = self._next_id
        self._next_id += 1
        self._store[entity.id] = entity
        return entity
    
    def update(self, entity: CheckpointFile) -> CheckpointFile:
        self._store[entity.id] = entity
        return entity
    
    def delete(self, id: int) -> bool:
        if id in self._store:
            del self._store[id]
            return True
        return False
    
    def get_by_checkpoint(self, checkpoint_id: int) -> Optional[CheckpointFile]:
        for cf in self._store.values():
            if cf.checkpoint_id == checkpoint_id:
                return cf
        return None
    
    def get_by_file_commit(self, file_commit_id: str) -> List[CheckpointFile]:
        return [cf for cf in self._store.values() 
                if cf.file_commit_id == file_commit_id]


class InMemoryUnitOfWork(IUnitOfWork):
    """
    In-memory Unit of Work for testing.
    
    Benefits:
    - No database I/O (fast tests)
    - Isolated per test (no cleanup needed)
    - Same interface as SQLAlchemyUnitOfWork
    
    Usage:
        uow = InMemoryUnitOfWork()
        with uow:
            uow.workflows.add(workflow)
            uow.executions.add(execution)
            uow.commit()  # No-op for in-memory, but keeps interface consistent
    """
    
    def __init__(self):
        # Initialize repositories
        self.workflows = InMemoryWorkflowRepository()
        self.executions = InMemoryExecutionRepository()
        self.node_boundaries = InMemoryNodeBoundaryRepository()
        self.checkpoint_files = InMemoryCheckpointFileRepository()
        
        # Track pending changes for commit/rollback simulation
        self._committed = False
    
    def __enter__(self) -> 'InMemoryUnitOfWork':
        self._committed = False
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type and not self._committed:
            self.rollback()
    
    def commit(self):
        """Mark as committed (no-op for in-memory, data is already in place)."""
        self._committed = True
    
    def rollback(self):
        """
        For in-memory, rollback is a no-op since we don't have transactions.
        In real tests, you typically create a fresh UoW per test.
        """
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Factory for creating UnitOfWork instances
# ═══════════════════════════════════════════════════════════════════════════════

class UnitOfWorkFactory:
    """
    Factory for creating the appropriate UnitOfWork implementation.
    
    Usage:
        # For testing
        uow = UnitOfWorkFactory.create_inmemory()
        
        # For production
        uow = UnitOfWorkFactory.create_sqlalchemy("sqlite:///wtb.db")
    """
    
    @staticmethod
    def create_inmemory() -> InMemoryUnitOfWork:
        """Create in-memory UoW for testing."""
        return InMemoryUnitOfWork()
    
    @staticmethod
    def create_sqlalchemy(db_url: str = "sqlite:///wtb.db") -> SQLAlchemyUnitOfWork:
        """Create SQLAlchemy UoW for production."""
        return SQLAlchemyUnitOfWork(db_url)
    
    @staticmethod
    def create(mode: str = "inmemory", db_url: str = None) -> IUnitOfWork:
        """
        Create UoW based on mode configuration.
        
        Args:
            mode: "inmemory" or "sqlalchemy"
            db_url: Database URL (required for sqlalchemy mode)
        """
        if mode == "inmemory":
            return UnitOfWorkFactory.create_inmemory()
        elif mode == "sqlalchemy":
            if not db_url:
                raise ValueError("db_url required for sqlalchemy mode")
            return UnitOfWorkFactory.create_sqlalchemy(db_url)
        else:
            raise ValueError(f"Unknown mode: {mode}")
```

---

## State Adapter Implementation

```python
# ═══════════════════════════════════════════════════════════════════════════════
# wtb/infrastructure/adapters/agentgit_state_adapter.py
# Anti-Corruption Layer: Translates between WTB and AgentGit
# ═══════════════════════════════════════════════════════════════════════════════

from typing import Optional, Dict, Any, List
from datetime import datetime

# AgentGit imports (existing package, unchanged)
from agentgit.checkpoints.checkpoint import Checkpoint
from agentgit.sessions.internal_session_mdp import InternalSession_mdp
from agentgit.database.repositories.checkpoint_repository import CheckpointRepository
from agentgit.database.repositories.internal_session_repository import InternalSessionRepository
from agentgit.managers.checkpoint_manager_mdp import CheckpointManager_mdp
from agentgit.managers.tool_manager import ToolManager

# WTB imports
from wtb.domain.interfaces.state_adapter import IStateAdapter, CheckpointTrigger, CheckpointInfo, NodeBoundaryInfo
from wtb.domain.models.workflow import ExecutionState
from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork


class AgentGitStateAdapter(IStateAdapter):
    """
    Anti-Corruption Layer between WTB and AgentGit.
    
    - Uses AgentGit's existing APIs for checkpoints and sessions
    - Uses WTB's own database for node_boundaries and file links
    - Translates between WTB concepts (unified checkpoints) and AgentGit concepts
    """
    
    def __init__(
        self,
        agentgit_db_path: str,
        wtb_db_url: str = "sqlite:///wtb.db",
        file_tracker=None  # Optional FileTracker integration
    ):
        # AgentGit repositories (existing)
        self._checkpoint_repo = CheckpointRepository(agentgit_db_path)
        self._session_repo = InternalSessionRepository(agentgit_db_path)
        self._checkpoint_manager = CheckpointManager_mdp(self._checkpoint_repo)
        
        # WTB database (new)
        self._wtb_db_url = wtb_db_url
        
        # FileTracker (optional)
        self._file_tracker = file_tracker
        
        # Current session tracking
        self._current_session_id: Optional[int] = None
        self._current_execution_id: Optional[str] = None
        self._tool_manager: Optional[ToolManager] = None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Session Management
    # ═══════════════════════════════════════════════════════════════════════════
    
    def initialize_session(
        self, 
        execution_id: str,
        initial_state: ExecutionState
    ) -> Optional[int]:
        """Initialize session using AgentGit's InternalSession_mdp."""
        session = InternalSession_mdp(
            id=None,
            external_session_id=initial_state.external_session_id,
            current_node_id=initial_state.current_node_id,
            workflow_variables=initial_state.workflow_variables
        )
        session_id = self._session_repo.create(session)
        
        self._current_session_id = session_id
        self._current_execution_id = execution_id
        self._tool_manager = ToolManager(tools=[])
        
        return session_id
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Checkpoint Operations (Using AgentGit)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def save_checkpoint(
        self,
        state: ExecutionState,
        node_id: str,
        trigger: CheckpointTrigger,
        tool_name: Optional[str] = None,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Save checkpoint using AgentGit.
        
        Maps WTB's unified checkpoint concept to AgentGit's structure by
        storing trigger_type and node_id in metadata.
        """
        if not self._current_session_id:
            raise RuntimeError("No active session")
        
        # Build checkpoint metadata (AgentGit stores this in JSON)
        cp_metadata = metadata or {}
        cp_metadata.update({
            "trigger_type": trigger.value,
            "node_id": node_id,
            "tool_name": tool_name,
            "tool_track_position": self._tool_manager.get_tool_track_position() if self._tool_manager else 0,
            "mdp_state": {
                "current_node_id": state.current_node_id,
                "workflow_variables": state.workflow_variables,
                "execution_path": state.execution_path
            }
        })
        
        # Create checkpoint using AgentGit's API
        checkpoint = Checkpoint(
            id=None,
            internal_session_id=self._current_session_id,
            session_state=state.to_dict(),
            metadata=cp_metadata,
            is_auto=(trigger != CheckpointTrigger.USER_REQUEST),
            created_at=datetime.utcnow().isoformat()
        )
        
        if name:
            checkpoint.checkpoint_name = name
        
        checkpoint_id = self._checkpoint_repo.create(checkpoint)
        return checkpoint_id
    
    def load_checkpoint(self, checkpoint_id: int) -> ExecutionState:
        """Load checkpoint from AgentGit."""
        checkpoint = self._checkpoint_repo.get_by_id(checkpoint_id)
        if not checkpoint:
            raise ValueError(f"Checkpoint {checkpoint_id} not found")
        
        return ExecutionState.from_dict(checkpoint.session_state)
    
    def link_file_commit(
        self, 
        checkpoint_id: int, 
        file_commit_id: str,
        file_count: int = 0,
        total_size_bytes: int = 0
    ) -> bool:
        """Link FileTracker commit to checkpoint (stored in WTB database)."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            from wtb.domain.models import CheckpointFile
            
            file_link = CheckpointFile(
                checkpoint_id=checkpoint_id,
                file_commit_id=file_commit_id,
                file_count=file_count,
                total_size_bytes=total_size_bytes,
                created_at=datetime.utcnow().isoformat()
            )
            uow.checkpoint_files.add(file_link)
            uow.commit()
            return True
    
    def get_file_commit(self, checkpoint_id: int) -> Optional[str]:
        """Get linked file commit ID from WTB database."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            file_link = uow.checkpoint_files.get_by_checkpoint(checkpoint_id)
            return file_link.file_commit_id if file_link else None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Node Boundary Operations (WTB Database)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def mark_node_started(self, node_id: str, entry_checkpoint_id: int) -> int:
        """Mark node start (stored in WTB database)."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            from wtb.domain.models import NodeBoundary
            
            boundary = NodeBoundary(
                execution_id=self._current_execution_id,
                internal_session_id=self._current_session_id,
                node_id=node_id,
                entry_checkpoint_id=entry_checkpoint_id,
                exit_checkpoint_id=None,
                node_status='started',
                started_at=datetime.utcnow().isoformat()
            )
            boundary = uow.node_boundaries.add(boundary)
            uow.commit()
            return boundary.id
    
    def mark_node_completed(
        self, 
        node_id: str, 
        exit_checkpoint_id: int,
        tool_count: int = 0,
        checkpoint_count: int = 0
    ) -> bool:
        """Mark node completed (update in WTB database)."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            boundary = uow.node_boundaries.get_by_node(
                self._current_session_id, node_id
            )
            if boundary:
                boundary.exit_checkpoint_id = exit_checkpoint_id
                boundary.node_status = 'completed'
                boundary.completed_at = datetime.utcnow().isoformat()
                boundary.tool_count = tool_count
                boundary.checkpoint_count = checkpoint_count
                uow.node_boundaries.update(boundary)
                uow.commit()
                return True
            return False
    
    def mark_node_failed(self, node_id: str, error_message: str) -> bool:
        """Mark node failed (update in WTB database)."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            boundary = uow.node_boundaries.get_by_node(
                self._current_session_id, node_id
            )
            if boundary:
                boundary.node_status = 'failed'
                boundary.error_message = error_message
                boundary.completed_at = datetime.utcnow().isoformat()
                uow.node_boundaries.update(boundary)
                uow.commit()
                return True
            return False
    
    def get_node_boundaries(self, session_id: int) -> List[NodeBoundaryInfo]:
        """Get all node boundaries from WTB database."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            boundaries = uow.node_boundaries.get_by_session(session_id)
            return [
                NodeBoundaryInfo(
                    id=b.id,
                    node_id=b.node_id,
                    entry_checkpoint_id=b.entry_checkpoint_id,
                    exit_checkpoint_id=b.exit_checkpoint_id,
                    node_status=b.node_status,
                    tool_count=b.tool_count,
                    checkpoint_count=b.checkpoint_count,
                    started_at=b.started_at,
                    completed_at=b.completed_at
                )
                for b in boundaries
            ]
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Unified Rollback
    # ═══════════════════════════════════════════════════════════════════════════
    
    def rollback(self, to_checkpoint_id: int) -> ExecutionState:
        """
        Unified rollback to any checkpoint.
        
        "Node-level rollback": caller passes node_boundary.exit_checkpoint_id
        "Tool-level rollback": caller passes any checkpoint.id
        
        The logic is IDENTICAL - we always restore to a checkpoint.
        """
        # 1. Load checkpoint from AgentGit
        checkpoint = self._checkpoint_repo.get_by_id(to_checkpoint_id)
        if not checkpoint:
            raise ValueError(f"Checkpoint {to_checkpoint_id} not found")
        
        # 2. Reverse tools after this checkpoint (via AgentGit ToolManager)
        if self._tool_manager:
            target_position = checkpoint.metadata.get('tool_track_position', 0)
            self._tool_manager.rollback_to_position(target_position)
        
        # 3. Restore files (via FileTracker if linked)
        file_commit_id = self.get_file_commit(to_checkpoint_id)
        if file_commit_id and self._file_tracker:
            self._file_tracker.restore_commit(file_commit_id)
        
        # 4. Return restored state
        return ExecutionState.from_dict(checkpoint.session_state)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Query Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_node_rollback_targets(self, session_id: int) -> List[CheckpointInfo]:
        """Get completed node boundaries as rollback targets."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            boundaries = uow.node_boundaries.get_completed(session_id)
            
            targets = []
            for b in boundaries:
                if b.exit_checkpoint_id:
                    cp = self._checkpoint_repo.get_by_id(b.exit_checkpoint_id)
                    if cp:
                        targets.append(CheckpointInfo(
                            id=cp.id,
                            name=cp.checkpoint_name,
                            node_id=b.node_id,
                            tool_track_position=cp.metadata.get('tool_track_position', 0),
                            trigger_type=CheckpointTrigger(
                                cp.metadata.get('trigger_type', 'auto')
                            ),
                            created_at=cp.created_at,
                            is_auto=cp.is_auto,
                            has_file_commit=self.get_file_commit(cp.id) is not None
                        ))
            return targets
    
    def get_checkpoints(
        self, 
        session_id: int,
        node_id: Optional[str] = None
    ) -> List[CheckpointInfo]:
        """Get checkpoints from AgentGit with optional node filter."""
        checkpoints = self._checkpoint_repo.get_by_internal_session(session_id)
        
        results = []
        for cp in checkpoints:
            cp_node_id = cp.metadata.get('node_id')
            
            # Filter by node if specified
            if node_id and cp_node_id != node_id:
                continue
            
            results.append(CheckpointInfo(
                id=cp.id,
                name=cp.checkpoint_name,
                node_id=cp_node_id,
                tool_track_position=cp.metadata.get('tool_track_position', 0),
                trigger_type=CheckpointTrigger(
                    cp.metadata.get('trigger_type', 'auto')
                ),
                created_at=cp.created_at,
                is_auto=cp.is_auto,
                has_file_commit=self.get_file_commit(cp.id) is not None
            ))
        
        return results
    
    def get_current_session_id(self) -> Optional[int]:
        return self._current_session_id
    
    def set_current_session(self, session_id: int) -> bool:
        session = self._session_repo.get_by_id(session_id)
        if session:
            self._current_session_id = session_id
            return True
        return False
    
    def cleanup(self, session_id: int, keep_latest: int = 5) -> int:
        """Cleanup old auto-checkpoints via AgentGit."""
        checkpoints = self._checkpoint_repo.get_by_internal_session(session_id)
        auto_checkpoints = [cp for cp in checkpoints if cp.is_auto]
        
        # Sort by creation time, keep latest
        auto_checkpoints.sort(key=lambda cp: cp.created_at, reverse=True)
        to_delete = auto_checkpoints[keep_latest:]
        
        deleted = 0
        for cp in to_delete:
            self._checkpoint_repo.delete(cp.id)
            deleted += 1
        
        return deleted
```

---

## Summary

### Data Storage Distribution

| Component | Database | Owner | Purpose |
|-----------|----------|-------|---------|
| users, external_sessions, internal_sessions, checkpoints | agentgit.db | AgentGit | Core state management |
| wtb_node_boundaries | wtb.db | WTB | Node completion markers (pointers to checkpoints) |
| wtb_checkpoint_files | wtb.db | WTB | FileTracker links |
| wtb_workflows, wtb_executions | wtb.db | WTB | Workflow management |
| commits, file_blobs, file_mementos | filetracker.db | FileTracker | File versioning |

### WTB Storage Implementations

| Implementation | Use Case | Persistence | Performance |
|----------------|----------|-------------|-------------|
| `InMemoryUnitOfWork` | Unit tests, fast iteration | No (RAM only) | Fastest |
| `SQLAlchemyUnitOfWork` (SQLite) | Development, single-user production | Yes (file) | Fast |
| `SQLAlchemyUnitOfWork` (PostgreSQL) | Scaled production | Yes (server) | Scalable |

### Key Benefits of Option B

1. **No AgentGit modifications** - Installed package remains unchanged
2. **Clean separation** - WTB owns its semantic concepts
3. **Proper abstraction** - IStateAdapter hides all database complexity
4. **Repository + UoW** - Proper patterns for testability and transactions
5. **Dual implementation** - InMemory for tests, SQLAlchemy for production
6. **SQLAlchemy for WTB** - Modern ORM for new tables
7. **Cross-database references** - Logical FKs via stored IDs

### Design Principles

| Principle | Description |
|-----------|-------------|
| **Dual Database Pattern** | AgentGit repos for checkpoint state; SQLAlchemy UoW for WTB domain data |
| **Single Checkpoint Type** | All checkpoints atomic at tool/message level; node boundaries are pointers |
| **Interface Abstraction** | IUnitOfWork, IRepository enable swappable implementations |
| **Dependency Injection** | UnitOfWorkFactory enables test/production switching |
