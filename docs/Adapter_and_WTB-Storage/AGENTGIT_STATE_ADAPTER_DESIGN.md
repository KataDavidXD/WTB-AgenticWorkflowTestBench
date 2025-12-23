# AgentGit State Adapter - Detailed Design

## Executive Summary

This document specifies the implementation of **AgentGitStateAdapter**: Production implementation of `IStateAdapter` that integrates with real AgentGit checkpoints.

For **WTB Data Persistence** (workflow/execution storage), see:
- [WTB_PERSISTENCE_DESIGN.md](./WTB_PERSISTENCE_DESIGN.md) - Dual implementation (InMemory + SQLAlchemy)

### Design Principles

> **"WTB uses dual database patterns: AgentGit repositories for checkpoint state; SQLAlchemy UoW for WTB domain data. Never mix - adapters bridge the boundary."**

> **"Single Checkpoint Type + Node Boundary Pointers: All checkpoints atomic at tool/message level. Node boundaries are POINTERS (entry_cp_id, exit_cp_id), NOT separate checkpoints."**

> **"Interface → Multiple Implementations: IStateAdapter and IUnitOfWork enable swappable backends."**

---

## 1. Architecture Analysis

### 1.1 Current State Assessment

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           CURRENT ARCHITECTURE                                   │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Application Layer                                                               │
│  ├── ExecutionController [DONE] - uses IStateAdapter                            │
│  └── NodeReplacer [DONE] - uses in-memory variant registry                      │
│                                                                                  │
│  Domain Layer                                                                    │
│  ├── IStateAdapter [DONE] - interface definition                                │
│  ├── InMemoryStateAdapter [DONE] - for testing                                  │
│  └── AgentGitStateAdapter [TODO] - THIS DOCUMENT                                │
│                                                                                  │
│  Infrastructure Layer                                                            │
│  ├── SQLAlchemy Models [DONE] - WTB ORM models                                  │
│  ├── Repositories [DONE] - WTB repositories                                     │
│  ├── UnitOfWork [DONE] - SQLAlchemy UoW                                         │
│  └── AgentGit Repos [EXTERNAL] - CheckpointRepository, InternalSessionRepository│
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 AgentGit Package Structure (Reference)

```
agentgit/
├── checkpoints/
│   └── checkpoint.py          # Checkpoint dataclass
├── sessions/
│   ├── internal_session.py    # InternalSession dataclass
│   └── internal_session_mdp.py # InternalSession_mdp (extends InternalSession)
├── managers/
│   ├── checkpoint_manager.py   # Base CheckpointManager
│   ├── checkpoint_manager_mdp.py # CheckpointManager_mdp (uses checkpoint types)
│   └── tool_manager.py        # ToolManager (tracks tool invocations)
├── database/
│   ├── db_config.py           # get_database_path()
│   └── repositories/
│       ├── checkpoint_repository.py    # CRUD for checkpoints
│       └── internal_session_repository.py # CRUD for sessions
└── core/
    └── rollback_protocol.py   # ToolRollbackRegistry, ToolSpec
```

### 1.3 Key Insight: Adapter + Manager Integration

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                     HOW ADAPTER INTEGRATES WITH AGENTGIT                         │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  The AgentGitStateAdapter acts as the ANTI-CORRUPTION LAYER:                     │
│                                                                                  │
│  ┌──────────────────────────────────────────────────────────────────────────┐   │
│  │                     AgentGitStateAdapter                                  │   │
│  │                                                                           │   │
│  │  Uses AgentGit Components Directly:                                       │   │
│  │  ─────────────────────────────────                                        │   │
│  │  ├── CheckpointRepository         → For raw checkpoint CRUD               │   │
│  │  ├── InternalSessionRepository    → For session CRUD                      │   │
│  │  ├── CheckpointManager_mdp        → For high-level checkpoint ops         │   │
│  │  │   └── Uses: CheckpointRepository internally                            │   │
│  │  └── ToolManager                  → For tool rollback tracking            │   │
│  │      └── Uses: ToolRollbackRegistry internally                            │   │
│  │                                                                           │   │
│  │  Uses WTB's Own SQLAlchemy UoW:                                           │   │
│  │  ────────────────────────────────                                         │   │
│  │  ├── NodeBoundaryRepository       → For node boundary markers             │   │
│  │  └── CheckpointFileRepository     → For file commit links                 │   │
│  │                                                                           │   │
│  └──────────────────────────────────────────────────────────────────────────┘   │
│                                                                                  │
│  KEY DECISION: Adapter does NOT re-wrap AgentGit repos in SQLAlchemy.           │
│  AgentGit uses raw SQLite - we use it as-is. WTB tables use SQLAlchemy.         │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. SOLID Principles Validation

### 2.1 Single Responsibility Principle (SRP) ✅

| Component | Responsibility |
|-----------|---------------|
| `AgentGitStateAdapter` | Bridge between WTB and AgentGit for state management |
| `CheckpointRepository` | CRUD for AgentGit checkpoints (unchanged) |
| `InternalSessionRepository` | CRUD for AgentGit sessions (unchanged) |
| `CheckpointManager_mdp` | High-level checkpoint operations (unchanged) |
| `NodeBoundaryRepository` | WTB node boundary persistence |
| `CheckpointFileRepository` | WTB file commit links |
| `SQLAlchemyUnitOfWork` | Transaction management for WTB tables |

### 2.2 Open/Closed Principle (OCP) ✅

```python
# IStateAdapter is open for extension, closed for modification
class IStateAdapter(ABC):
    @abstractmethod
    def save_checkpoint(...) -> int: ...
    @abstractmethod
    def rollback(...) -> ExecutionState: ...

# Implementations extend without modifying interface:
class InMemoryStateAdapter(IStateAdapter): ...  # Testing
class AgentGitStateAdapter(IStateAdapter): ...  # Production
# Future: class DistributedStateAdapter(IStateAdapter): ...
```

### 2.3 Liskov Substitution Principle (LSP) ✅

```python
# Both adapters are fully substitutable
def create_controller(adapter: IStateAdapter) -> ExecutionController:
    return ExecutionController(
        execution_repository=...,
        workflow_repository=...,
        state_adapter=adapter,  # InMemory or AgentGit - both work
    )

# Test:
test_adapter = InMemoryStateAdapter()
prod_adapter = AgentGitStateAdapter(db_path="data/agentgit.db")

# Both work identically from ExecutionController's perspective
```

### 2.4 Interface Segregation Principle (ISP) ✅

The `IStateAdapter` interface is already well-segregated:
- Session management methods
- Checkpoint operations
- Node boundary operations  
- Rollback & branching
- Query operations

No client needs to implement methods it doesn't use.

### 2.5 Dependency Inversion Principle (DIP) ✅

```
┌─────────────────────────────────────────────────────────────────┐
│  High-Level Module              Abstraction           Low-Level │
│  ────────────────               ───────────           ───────── │
│                                                                  │
│  ExecutionController ─────────► IStateAdapter ◄───── AgentGitStateAdapter
│                                                      InMemoryStateAdapter
│                                                                  │
│  Both depend on abstraction, not concretions                    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Database Operations Pattern Decision

### 3.1 The Dual Pattern Principle

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        DATABASE OPERATIONS PATTERN                               │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  PRINCIPLE: "Two systems, two patterns, one adapter boundary"                   │
│                                                                                  │
│  ┌────────────────────────────────┐  ┌────────────────────────────────┐        │
│  │       AgentGit Domain          │  │         WTB Domain              │        │
│  │  ══════════════════════════    │  │  ══════════════════════════    │        │
│  │                                │  │                                 │        │
│  │  Pattern: Raw SQLite           │  │  Pattern: SQLAlchemy + UoW     │        │
│  │  ──────────────────────        │  │  ─────────────────────────     │        │
│  │  • CheckpointRepository        │  │  • WorkflowRepository          │        │
│  │  • InternalSessionRepository   │  │  • ExecutionRepository         │        │
│  │  • ExternalSessionRepository   │  │  • NodeBoundaryRepository      │        │
│  │                                │  │  • CheckpointFileRepository    │        │
│  │  Database: agentgit.db         │  │  Database: wtb.db              │        │
│  │                                │  │                                 │        │
│  │  Why keep raw SQLite?          │  │  Why use SQLAlchemy + UoW?     │        │
│  │  • External package - no mods  │  │  • WTB owns these tables       │        │
│  │  • Already works correctly     │  │  • Need transaction management │        │
│  │  • Tested with 94 tests        │  │  • Multi-aggregate operations  │        │
│  │                                │  │  • Testable with DI            │        │
│  └───────────────┬────────────────┘  └────────────────┬───────────────┘        │
│                  │                                     │                        │
│                  │  ┌──────────────────────────────┐   │                        │
│                  └──┤   AgentGitStateAdapter       ├───┘                        │
│                     │   (Anti-Corruption Layer)    │                            │
│                     │                              │                            │
│                     │   Uses BOTH patterns:        │                            │
│                     │   • AgentGit repos directly  │                            │
│                     │   • WTB UoW for WTB tables   │                            │
│                     └──────────────────────────────┘                            │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 Summary: Database Pattern Decision

| Database | Tables | Pattern | Reason |
|----------|--------|---------|--------|
| agentgit.db | users, sessions, checkpoints | Raw SQLite (existing) | External package, don't modify |
| wtb.db | wtb_workflows, wtb_executions, wtb_node_boundaries, etc. | SQLAlchemy + UoW | WTB-owned, need transactions |
| filetracker.db | commits, blobs, mementos | PostgreSQL (existing) | External package, don't modify |

---

## 4. AgentGitStateAdapter Implementation Design

### 4.1 Class Diagram

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         AgentGitStateAdapter                                     │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Attributes:                                                                     │
│  ───────────                                                                     │
│  - _agentgit_db_path: str                 # Path to agentgit.db                 │
│  - _wtb_db_url: str                       # URL for wtb.db                       │
│  - _checkpoint_repo: CheckpointRepository # AgentGit (raw SQL)                  │
│  - _session_repo: InternalSessionRepository # AgentGit (raw SQL)                │
│  - _checkpoint_manager: CheckpointManager_mdp # AgentGit high-level             │
│  - _tool_manager: ToolManager             # AgentGit tool tracking              │
│  - _current_session: InternalSession_mdp  # Current active session             │
│  - _current_execution_id: str             # Current WTB execution ID            │
│  - _external_session_id: int              # AgentGit external session           │
│                                                                                  │
│  Methods (from IStateAdapter):                                                   │
│  ─────────────────────────────                                                   │
│  + initialize_session(execution_id, initial_state) → int                        │
│  + save_checkpoint(state, node_id, trigger, ...) → int                          │
│  + load_checkpoint(checkpoint_id) → ExecutionState                              │
│  + link_file_commit(checkpoint_id, file_commit_id, ...) → bool                  │
│  + get_file_commit(checkpoint_id) → Optional[str]                               │
│  + mark_node_started(node_id, entry_checkpoint_id) → int                        │
│  + mark_node_completed(node_id, exit_checkpoint_id, ...) → bool                 │
│  + mark_node_failed(node_id, error_message) → bool                              │
│  + get_node_boundaries(session_id) → List[NodeBoundaryInfo]                     │
│  + get_node_boundary(session_id, node_id) → Optional[NodeBoundaryInfo]          │
│  + rollback(to_checkpoint_id) → ExecutionState                                  │
│  + create_branch(from_checkpoint_id) → int                                      │
│  + get_checkpoints(session_id, node_id) → List[CheckpointInfo]                  │
│  + get_node_rollback_targets(session_id) → List[CheckpointInfo]                 │
│  + get_checkpoints_in_node(session_id, node_id) → List[CheckpointInfo]          │
│  + get_current_session_id() → Optional[int]                                     │
│  + set_current_session(session_id) → bool                                       │
│  + cleanup(session_id, keep_latest) → int                                       │
│                                                                                  │
│  Private Methods:                                                                │
│  ────────────────                                                                │
│  - _ensure_external_session() → int                                             │
│  - _create_internal_session(execution_id, initial_state) → InternalSession_mdp │
│  - _build_checkpoint_metadata(state, node_id, trigger, ...) → Dict             │
│  - _execution_state_to_session_state(state) → Dict                              │
│  - _checkpoint_to_execution_state(checkpoint) → ExecutionState                  │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 4.2 Sequence Diagram: Checkpoint Creation

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    SEQUENCE: save_checkpoint()                                   │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ExecutionController          AgentGitStateAdapter          AgentGit Repos       │
│        │                              │                          │              │
│        │  save_checkpoint(state,      │                          │              │
│        │    node_id, trigger)         │                          │              │
│        ├─────────────────────────────►│                          │              │
│        │                              │                          │              │
│        │                              │  Update session state    │              │
│        │                              ├─────────────────────────►│              │
│        │                              │  session_repo.update()   │              │
│        │                              │                          │              │
│        │                              │  Build checkpoint        │              │
│        │                              │  from InternalSession    │              │
│        │                              ├─────────────────────────►│              │
│        │                              │  checkpoint_repo.create()│              │
│        │                              │◄─────────────────────────┤              │
│        │                              │  checkpoint_id           │              │
│        │                              │                          │              │
│        │                              │  Update metadata         │              │
│        │                              ├─────────────────────────►│              │
│        │                              │  checkpoint_repo.        │              │
│        │                              │    update_metadata()     │              │
│        │                              │                          │              │
│        │◄─────────────────────────────┤                          │              │
│        │  checkpoint_id               │                          │              │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 4.3 Sequence Diagram: Node Boundary Operations

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    SEQUENCE: mark_node_started() / mark_node_completed()         │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ExecutionController     AgentGitStateAdapter     WTB SQLAlchemy UoW            │
│        │                        │                        │                      │
│        │  mark_node_started     │                        │                      │
│        │  (node_id, cp_id)      │                        │                      │
│        ├───────────────────────►│                        │                      │
│        │                        │                        │                      │
│        │                        │  with UoW:             │                      │
│        │                        ├───────────────────────►│                      │
│        │                        │                        │                      │
│        │                        │  Create NodeBoundary   │                      │
│        │                        │  (status="started")    │                      │
│        │                        ├───────────────────────►│                      │
│        │                        │  node_boundaries.add() │                      │
│        │                        │                        │                      │
│        │                        │  uow.commit()          │                      │
│        │                        ├───────────────────────►│                      │
│        │                        │◄───────────────────────┤                      │
│        │                        │  boundary_id           │                      │
│        │◄───────────────────────┤                        │                      │
│        │  boundary_id           │                        │                      │
│                                                                                  │
│  Note: Node boundaries are stored in WTB's wtb_node_boundaries table,           │
│        NOT in AgentGit. This is the Anti-Corruption Layer in action.            │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 4.4 Rollback Flow

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    SEQUENCE: rollback(to_checkpoint_id)                          │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ExecutionController   AgentGitStateAdapter   CheckpointRepo   ToolManager      │
│        │                      │                    │               │            │
│        │  rollback(cp_id)     │                    │               │            │
│        ├─────────────────────►│                    │               │            │
│        │                      │                    │               │            │
│        │                      │  get_by_id(cp_id)  │               │            │
│        │                      ├───────────────────►│               │            │
│        │                      │◄───────────────────┤               │            │
│        │                      │  checkpoint        │               │            │
│        │                      │                    │               │            │
│        │                      │  Get tool_track_position          │            │
│        │                      │  from checkpoint.metadata          │            │
│        │                      │                    │               │            │
│        │                      │  Rollback tools from position     │            │
│        │                      ├───────────────────────────────────►│            │
│        │                      │  rollback_tools_from_position()   │            │
│        │                      │◄───────────────────────────────────┤            │
│        │                      │                    │               │            │
│        │                      │  Optionally restore files         │            │
│        │                      │  (if has linked file_commit)      │            │
│        │                      │                    │               │            │
│        │                      │  Build ExecutionState from        │            │
│        │                      │  checkpoint.metadata.mdp_state    │            │
│        │                      │                    │               │            │
│        │◄─────────────────────┤                    │               │            │
│        │  restored_state      │                    │               │            │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. WTB Persistence Design

### 5.1 Current Gap Analysis

The `ExecutionController` currently:
- Creates `Execution` domain objects ✅
- Uses `IStateAdapter` for checkpoints ✅
- Calls `execution_repository.add()` and `.update()` ✅

But:
- Test uses in-memory repositories (not persisted)
- Need to ensure `SQLAlchemyUnitOfWork` is used in production

### 5.2 Persistence Flow

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         WTB PERSISTENCE FLOW                                     │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ExecutionController.create_execution()                                          │
│        │                                                                         │
│        ├── 1. Create Execution domain object                                     │
│        ├── 2. Initialize state adapter session → AgentGit                        │
│        └── 3. Save to WTB database                                               │
│              ├── uow.executions.add(execution)                                   │
│              └── uow.commit()                                                    │
│                    └── Writes to wtb_executions table                            │
│                                                                                  │
│  ExecutionController.run()                                                       │
│        │                                                                         │
│        ├── [Loop: for each node]                                                 │
│        │     ├── save_checkpoint() → AgentGit checkpoints table                  │
│        │     ├── mark_node_started() → WTB wtb_node_boundaries                   │
│        │     ├── execute node                                                    │
│        │     ├── save_checkpoint() → AgentGit checkpoints table                  │
│        │     └── mark_node_completed() → WTB wtb_node_boundaries                 │
│        │                                                                         │
│        └── Update execution in WTB                                               │
│              ├── uow.executions.update(execution)                                │
│              └── uow.commit()                                                    │
│                    └── Updates wtb_executions table                              │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 5.3 ExecutionController Factory Pattern

```python
# New: wtb/application/factories.py

class ExecutionControllerFactory:
    """Factory for creating properly wired ExecutionController instances."""
    
    @staticmethod
    def create_production(
        agentgit_db_path: str = None,
        wtb_db_url: str = None,
    ) -> ExecutionController:
        """
        Create ExecutionController with production dependencies.
        
        Uses:
        - AgentGitStateAdapter for checkpoint operations
        - SQLAlchemyUnitOfWork for WTB persistence
        """
        config = get_database_config()
        
        db_path = agentgit_db_path or str(config.agentgit_db_path)
        db_url = wtb_db_url or config.wtb_db_url
        
        uow = SQLAlchemyUnitOfWork(db_url)
        
        with uow:
            adapter = AgentGitStateAdapter(
                agentgit_db_path=db_path,
                wtb_db_url=db_url,
            )
            
            return ExecutionController(
                execution_repository=uow.executions,
                workflow_repository=uow.workflows,
                state_adapter=adapter,
            )
    
    @staticmethod
    def create_testing() -> ExecutionController:
        """Create ExecutionController with in-memory dependencies."""
        return ExecutionController(
            execution_repository=InMemoryExecutionRepository(),
            workflow_repository=InMemoryWorkflowRepository(),
            state_adapter=InMemoryStateAdapter(),
        )
```

---

## 6. Implementation Specification

### 6.1 AgentGitStateAdapter Full Implementation

```python
# wtb/infrastructure/adapters/agentgit_state_adapter.py

"""
AgentGit State Adapter Implementation.

Anti-corruption layer between WTB and AgentGit.
Uses AgentGit's raw SQLite repositories directly.
Uses WTB's SQLAlchemy UoW for WTB-owned tables.
"""

from typing import Optional, Dict, Any, List
from datetime import datetime
from copy import deepcopy
import uuid

# AgentGit imports (external package, unchanged)
from agentgit.checkpoints.checkpoint import Checkpoint
from agentgit.sessions.internal_session_mdp import InternalSession_mdp
from agentgit.database.repositories.checkpoint_repository import CheckpointRepository
from agentgit.database.repositories.internal_session_repository import InternalSessionRepository
from agentgit.database.repositories.external_session_repository import ExternalSessionRepository
from agentgit.managers.checkpoint_manager_mdp import CheckpointManager_mdp
from agentgit.managers.tool_manager import ToolManager

# WTB imports
from wtb.domain.interfaces.state_adapter import (
    IStateAdapter,
    CheckpointTrigger,
    CheckpointInfo,
    NodeBoundaryInfo,
)
from wtb.domain.models import ExecutionState
from wtb.domain.models.node_boundary import NodeBoundary
from wtb.domain.models.checkpoint_file import CheckpointFile
from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork


class AgentGitStateAdapter(IStateAdapter):
    """
    Production implementation of IStateAdapter using AgentGit.
    
    Architecture:
    - Uses AgentGit's CheckpointRepository/InternalSessionRepository directly
    - Uses WTB's SQLAlchemyUnitOfWork for node_boundaries and checkpoint_files
    - Tool rollback via AgentGit's ToolManager
    
    Usage:
        adapter = AgentGitStateAdapter(
            agentgit_db_path="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db"
        )
        
        session_id = adapter.initialize_session(execution_id, initial_state)
        checkpoint_id = adapter.save_checkpoint(state, node_id, trigger)
        restored = adapter.rollback(checkpoint_id)
    """
    
    def __init__(
        self,
        agentgit_db_path: str,
        wtb_db_url: str = "sqlite:///data/wtb.db",
        external_session_id: Optional[int] = None,
        tools: Optional[List] = None,
    ):
        """
        Initialize the adapter.
        
        Args:
            agentgit_db_path: Path to agentgit.db
            wtb_db_url: SQLAlchemy URL for wtb.db
            external_session_id: Optional external session ID (creates if None)
            tools: Optional list of tools for ToolManager
        """
        self._agentgit_db_path = agentgit_db_path
        self._wtb_db_url = wtb_db_url
        
        # Initialize AgentGit repositories (raw SQLite)
        self._checkpoint_repo = CheckpointRepository(agentgit_db_path)
        self._session_repo = InternalSessionRepository(agentgit_db_path)
        self._external_session_repo = ExternalSessionRepository(agentgit_db_path)
        
        # Initialize AgentGit managers
        self._checkpoint_manager = CheckpointManager_mdp(self._checkpoint_repo)
        self._tool_manager = ToolManager(tools=tools or [])
        
        # Session tracking
        self._external_session_id = external_session_id
        self._current_session: Optional[InternalSession_mdp] = None
        self._current_execution_id: Optional[str] = None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Session Management
    # ═══════════════════════════════════════════════════════════════════════════
    
    def initialize_session(
        self, 
        execution_id: str,
        initial_state: ExecutionState
    ) -> Optional[int]:
        """Initialize a new AgentGit session for this execution."""
        # Ensure external session exists
        if self._external_session_id is None:
            self._external_session_id = self._ensure_external_session()
        
        # Create InternalSession_mdp
        session = InternalSession_mdp(
            external_session_id=self._external_session_id,
            langgraph_session_id=f"wtb_{execution_id}_{uuid.uuid4().hex[:8]}",
            session_state=initial_state.to_dict(),
            conversation_history=[],
            created_at=datetime.now(),
            is_current=True,
            # MDP extensions
            current_node_id=initial_state.current_node_id,
            workflow_variables=dict(initial_state.workflow_variables),
            execution_path=list(initial_state.execution_path),
        )
        
        session.metadata = {
            "wtb_execution_id": execution_id,
            "session_type": "wtb_workflow",
            "created_at": datetime.now().isoformat(),
        }
        
        # Persist via AgentGit repository
        saved_session = self._session_repo.create(session)
        
        self._current_session = saved_session
        self._current_execution_id = execution_id
        self._tool_manager.clear_track()
        
        return saved_session.id
    
    def _ensure_external_session(self) -> int:
        """Ensure an external session exists for WTB, create if needed."""
        # Check for existing WTB external session
        sessions = self._external_session_repo.get_by_user(user_id=1)
        for session in sessions:
            if session.session_name == "WTB_Session":
                return session.id
        
        # Create new external session
        from agentgit.sessions.external_session import ExternalSession
        external = ExternalSession(
            user_id=1,
            session_name="WTB_Session",
            created_at=datetime.now(),
            is_active=True,
            metadata={"type": "wtb_workflow_container"}
        )
        saved = self._external_session_repo.create(external)
        return saved.id
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Checkpoint Operations
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
        """Save a checkpoint using AgentGit."""
        if not self._current_session:
            raise RuntimeError("No active session. Call initialize_session first.")
        
        # Update session state
        self._current_session.current_node_id = node_id
        self._current_session.workflow_variables = dict(state.workflow_variables)
        self._current_session.execution_path = list(state.execution_path)
        self._current_session.session_state = state.to_dict()
        
        # Save session update
        self._session_repo.update(self._current_session)
        
        # Build checkpoint metadata
        tool_position = self._tool_manager.get_tool_track_position()
        cp_metadata = metadata or {}
        cp_metadata.update({
            "trigger_type": trigger.value,
            "node_id": node_id,
            "tool_name": tool_name,
            "tool_track_position": tool_position,
            "wtb_execution_id": self._current_execution_id,
            "mdp_state": {
                "current_node_id": state.current_node_id,
                "workflow_variables": state.workflow_variables,
                "execution_path": state.execution_path,
            }
        })
        
        # Create checkpoint via AgentGit
        checkpoint = Checkpoint.from_internal_session(
            internal_session=self._current_session,
            checkpoint_name=name,
            is_auto=(trigger != CheckpointTrigger.USER_REQUEST),
            tool_invocations=[inv.to_dict() for inv in self._tool_manager.get_tool_track()],
        )
        checkpoint.metadata = cp_metadata
        
        saved_cp = self._checkpoint_repo.create(checkpoint)
        
        return saved_cp.id
    
    def load_checkpoint(self, checkpoint_id: int) -> ExecutionState:
        """Load checkpoint and return ExecutionState."""
        checkpoint = self._checkpoint_repo.get_by_id(checkpoint_id)
        if not checkpoint:
            raise ValueError(f"Checkpoint {checkpoint_id} not found")
        
        return self._checkpoint_to_execution_state(checkpoint)
    
    def _checkpoint_to_execution_state(self, checkpoint: Checkpoint) -> ExecutionState:
        """Convert AgentGit Checkpoint to WTB ExecutionState."""
        mdp_state = checkpoint.metadata.get("mdp_state", {})
        
        return ExecutionState(
            current_node_id=mdp_state.get("current_node_id") or checkpoint.metadata.get("node_id"),
            workflow_variables=mdp_state.get("workflow_variables", {}),
            execution_path=mdp_state.get("execution_path", []),
            node_results=checkpoint.session_state.get("node_results", {}),
        )
    
    def link_file_commit(
        self, 
        checkpoint_id: int, 
        file_commit_id: str,
        file_count: int = 0,
        total_size_bytes: int = 0
    ) -> bool:
        """Link FileTracker commit to checkpoint (stored in WTB database)."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            file_link = CheckpointFile(
                checkpoint_id=checkpoint_id,
                file_commit_id=file_commit_id,
                file_count=file_count,
                total_size_bytes=total_size_bytes,
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
        """Mark node as started (stored in WTB database)."""
        if not self._current_session:
            raise RuntimeError("No active session")
        
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            boundary = NodeBoundary(
                execution_id=self._current_execution_id or "",
                internal_session_id=self._current_session.id,
                node_id=node_id,
                entry_checkpoint_id=entry_checkpoint_id,
                node_status="started",
            )
            saved = uow.node_boundaries.add(boundary)
            uow.commit()
            return saved.id
    
    def mark_node_completed(
        self, 
        node_id: str, 
        exit_checkpoint_id: int,
        tool_count: int = 0,
        checkpoint_count: int = 0
    ) -> bool:
        """Mark node as completed (update in WTB database)."""
        if not self._current_session:
            return False
        
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            boundary = uow.node_boundaries.get_by_node(
                self._current_session.id, node_id
            )
            if boundary:
                boundary.exit_checkpoint_id = exit_checkpoint_id
                boundary.node_status = "completed"
                boundary.completed_at = datetime.now()
                boundary.tool_count = tool_count
                boundary.checkpoint_count = checkpoint_count
                uow.node_boundaries.update(boundary)
                uow.commit()
                return True
            return False
    
    def mark_node_failed(self, node_id: str, error_message: str) -> bool:
        """Mark node as failed (update in WTB database)."""
        if not self._current_session:
            return False
        
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            boundary = uow.node_boundaries.get_by_node(
                self._current_session.id, node_id
            )
            if boundary:
                boundary.node_status = "failed"
                boundary.error_message = error_message
                boundary.completed_at = datetime.now()
                uow.node_boundaries.update(boundary)
                uow.commit()
                return True
            return False
    
    def get_node_boundaries(self, session_id: int) -> List[NodeBoundaryInfo]:
        """Get all node boundaries from WTB database."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            boundaries = uow.node_boundaries.get_by_session(session_id)
            return [self._boundary_to_info(b) for b in boundaries]
    
    def get_node_boundary(self, session_id: int, node_id: str) -> Optional[NodeBoundaryInfo]:
        """Get specific node boundary from WTB database."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            boundary = uow.node_boundaries.get_by_node(session_id, node_id)
            return self._boundary_to_info(boundary) if boundary else None
    
    def _boundary_to_info(self, boundary: NodeBoundary) -> NodeBoundaryInfo:
        """Convert NodeBoundary domain model to NodeBoundaryInfo."""
        return NodeBoundaryInfo(
            id=boundary.id,
            node_id=boundary.node_id,
            entry_checkpoint_id=boundary.entry_checkpoint_id or 0,
            exit_checkpoint_id=boundary.exit_checkpoint_id or 0,
            node_status=boundary.node_status,
            tool_count=boundary.tool_count,
            checkpoint_count=boundary.checkpoint_count,
            started_at=boundary.started_at.isoformat() if boundary.started_at else "",
            completed_at=boundary.completed_at.isoformat() if boundary.completed_at else None,
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Rollback & Branching
    # ═══════════════════════════════════════════════════════════════════════════
    
    def rollback(self, to_checkpoint_id: int) -> ExecutionState:
        """
        Unified rollback to a checkpoint.
        
        Steps:
        1. Load checkpoint from AgentGit
        2. Reverse tools after checkpoint's tool_track_position
        3. Restore files if linked
        4. Return ExecutionState from checkpoint
        """
        checkpoint = self._checkpoint_repo.get_by_id(to_checkpoint_id)
        if not checkpoint:
            raise ValueError(f"Checkpoint {to_checkpoint_id} not found")
        
        # Get tool track position from checkpoint
        target_position = checkpoint.metadata.get("tool_track_position", 0)
        
        # Rollback tools from current position to target
        if self._tool_manager.get_tool_track_position() > target_position:
            self._tool_manager.rollback_tools_from_position(target_position)
        
        # Restore files if linked
        file_commit_id = self.get_file_commit(to_checkpoint_id)
        if file_commit_id:
            # TODO: Integrate with FileTracker when available
            # self._file_tracker.restore_commit(file_commit_id)
            pass
        
        # Restore tool track from checkpoint
        self._tool_manager.restore_track_from_checkpoint(checkpoint.tool_invocations)
        
        return self._checkpoint_to_execution_state(checkpoint)
    
    def create_branch(self, from_checkpoint_id: int) -> int:
        """Create a new branch from a checkpoint."""
        checkpoint = self._checkpoint_repo.get_by_id(from_checkpoint_id)
        if not checkpoint:
            raise ValueError(f"Checkpoint {from_checkpoint_id} not found")
        
        if not self._current_session:
            raise RuntimeError("No active session")
        
        # Create branched session
        branched = InternalSession_mdp.create_branch_from_checkpoint(
            checkpoint=checkpoint,
            external_session_id=self._external_session_id,
            parent_session_id=self._current_session.id,
        )
        
        saved = self._session_repo.create(branched)
        self._current_session = saved
        
        # Restore tool track
        self._tool_manager.restore_track_from_checkpoint(checkpoint.tool_invocations)
        
        return saved.id
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Query Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_checkpoints(
        self, 
        session_id: int,
        node_id: Optional[str] = None
    ) -> List[CheckpointInfo]:
        """Get checkpoints from AgentGit."""
        checkpoints = self._checkpoint_repo.get_by_internal_session(session_id)
        
        result = []
        for cp in checkpoints:
            cp_node_id = cp.metadata.get("node_id")
            
            if node_id and cp_node_id != node_id:
                continue
            
            result.append(self._checkpoint_to_info(cp))
        
        return sorted(result, key=lambda c: c.tool_track_position)
    
    def get_node_rollback_targets(self, session_id: int) -> List[CheckpointInfo]:
        """Get exit checkpoints of completed nodes."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            boundaries = uow.node_boundaries.get_completed(session_id)
            
            targets = []
            for b in boundaries:
                if b.exit_checkpoint_id:
                    cp = self._checkpoint_repo.get_by_id(b.exit_checkpoint_id)
                    if cp:
                        info = self._checkpoint_to_info(cp)
                        info.name = f"Node: {b.node_id}"
                        targets.append(info)
            return targets
    
    def get_checkpoints_in_node(self, session_id: int, node_id: str) -> List[CheckpointInfo]:
        """Get all checkpoints within a node."""
        return self.get_checkpoints(session_id, node_id)
    
    def _checkpoint_to_info(self, checkpoint: Checkpoint) -> CheckpointInfo:
        """Convert AgentGit Checkpoint to CheckpointInfo."""
        trigger_str = checkpoint.metadata.get("trigger_type", "auto")
        try:
            trigger = CheckpointTrigger(trigger_str)
        except ValueError:
            trigger = CheckpointTrigger.AUTO
        
        return CheckpointInfo(
            id=checkpoint.id,
            name=checkpoint.checkpoint_name,
            node_id=checkpoint.metadata.get("node_id"),
            tool_track_position=checkpoint.metadata.get("tool_track_position", 0),
            trigger_type=trigger,
            created_at=checkpoint.created_at.isoformat() if checkpoint.created_at else "",
            is_auto=checkpoint.is_auto,
            has_file_commit=self.get_file_commit(checkpoint.id) is not None,
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Session Management
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_current_session_id(self) -> Optional[int]:
        """Get current session ID."""
        return self._current_session.id if self._current_session else None
    
    def set_current_session(self, session_id: int) -> bool:
        """Set current session."""
        session = self._session_repo.get_by_id(session_id)
        if session:
            # Convert to MDP session if needed
            if isinstance(session, InternalSession_mdp):
                self._current_session = session
            else:
                # Wrap in MDP session
                self._current_session = InternalSession_mdp(
                    id=session.id,
                    external_session_id=session.external_session_id,
                    langgraph_session_id=session.langgraph_session_id,
                    session_state=session.session_state,
                    conversation_history=session.conversation_history,
                    created_at=session.created_at,
                    is_current=session.is_current,
                    checkpoint_count=session.checkpoint_count,
                    parent_session_id=session.parent_session_id,
                    branch_point_checkpoint_id=session.branch_point_checkpoint_id,
                )
            return True
        return False
    
    def cleanup(self, session_id: int, keep_latest: int = 5) -> int:
        """Cleanup old auto-checkpoints."""
        return self._checkpoint_repo.delete_auto_checkpoints(session_id, keep_latest)
```

---

## 7. Checkpoint Granularity Decision: Single Type + Node Boundary Pointers

### 7.1 Architectural Decision Analysis

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    CHECKPOINT GRANULARITY DESIGN DECISION                        │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  QUESTION: How should WTB handle AgentGit's dual granularity capability?        │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  OPTION A: Single Granularity + Node Boundary Pointers  ← RECOMMENDED      │ │
│  │  ─────────────────────────────────────────────────────                      │ │
│  │                                                                             │ │
│  │  All checkpoints are EQUAL (atomic, at tool/message level)                 │ │
│  │  Node boundaries are POINTERS (entry_cp_id, exit_cp_id) in WTB table       │ │
│  │                                                                             │ │
│  │  AgentGit checkpoints:           WTB wtb_node_boundaries:                  │ │
│  │  ─────────────────────           ────────────────────────                  │ │
│  │  [CP #1] tool=load_csv     ─┐    Node: data_load                           │ │
│  │  [CP #2] tool=validate      ├──► entry_checkpoint_id = 1                   │ │
│  │  [CP #3] tool=transform    ─┘    exit_checkpoint_id  = 3                   │ │
│  │  [CP #4] tool=normalize    ─┐    Node: preprocess                          │ │
│  │  [CP #5] tool=fill_na      ─┘    entry_checkpoint_id = 4                   │ │
│  │                                  exit_checkpoint_id  = 5                   │ │
│  │                                                                             │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
│  ┌────────────────────────────────────────────────────────────────────────────┐ │
│  │  OPTION B: Dual Checkpoint Types (Hierarchical)         ← NOT RECOMMENDED  │ │
│  │  ──────────────────────────────────────────────                             │ │
│  │                                                                             │ │
│  │  [CP #1] type="node", node_id="data_load"                                  │ │
│  │      ├── [CP #2] type="tool_message", parent=1, tool="load_csv"            │ │
│  │      ├── [CP #3] type="tool_message", parent=1, tool="validate"            │ │
│  │      └── [CP #4] type="tool_message", parent=1, tool="transform"           │ │
│  │  [CP #5] type="node" (exit)  ← DUPLICATES state from CP #4!                │ │
│  │                                                                             │ │
│  │  Problems:                                                                  │ │
│  │  • State duplication (node CP = copy of last tool CP)                      │ │
│  │  • Complex parent/child hierarchy management                               │ │
│  │  • More checkpoints = more storage overhead                                │ │
│  │  • Hierarchical linking adds adapter complexity                            │ │
│  │                                                                             │ │
│  └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 7.2 Decision: OPTION A (Single Granularity + Node Boundary Pointers)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                      DECISION RATIONALE                                          │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  WHY OPTION A IS BETTER FOR WTB:                                                │
│  ═══════════════════════════════                                                │
│                                                                                  │
│  1. NO STATE DUPLICATION                                                         │
│     ─────────────────────                                                        │
│     A "node checkpoint" would duplicate the exact same state as the last        │
│     tool checkpoint in that node. Why store it twice?                           │
│                                                                                  │
│     Option B: [Tool CP #4] state={x:1} + [Node CP #5] state={x:1} ← DUPLICATE   │
│     Option A: [Tool CP #4] state={x:1} + node_boundary.exit_cp=4  ← EFFICIENT   │
│                                                                                  │
│  2. UNIFIED ROLLBACK                                                             │
│     ────────────────────                                                         │
│     rollback(checkpoint_id) works identically for:                              │
│     • "Node rollback" → pass node_boundary.exit_checkpoint_id                   │
│     • "Tool rollback" → pass any checkpoint.id                                  │
│                                                                                  │
│     ONE method, ONE logic path, ZERO special cases                              │
│                                                                                  │
│  3. CLEANER ANTI-CORRUPTION LAYER                                                │
│     ─────────────────────────────                                                │
│     • AgentGit owns checkpoints (unchanged, flat)                               │
│     • WTB owns node semantics (wtb_node_boundaries table)                       │
│     • Adapter bridges them - no hierarchy management in checkpoints             │
│                                                                                  │
│  4. MATCHES EXISTING DESIGN                                                      │
│     ───────────────────────                                                      │
│     The current IStateAdapter interface already uses this model:                │
│     • save_checkpoint() - creates atomic checkpoints                            │
│     • mark_node_started(node_id, entry_checkpoint_id) - stores pointer         │
│     • mark_node_completed(node_id, exit_checkpoint_id) - stores pointer        │
│     • Rollback is to checkpoint ID, not "node" or "tool" separately            │
│                                                                                  │
│  5. QUERY BY GRANULARITY = FILTERING, NOT SEPARATE STORAGE                      │
│     ─────────────────────────────────────────────────────                        │
│     • get_node_rollback_targets() = returns exit_checkpoint for each node      │
│     • get_checkpoints_in_node() = filter checkpoints by metadata.node_id       │
│     • No need for checkpoint_type field or parent/child linking                 │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 7.3 Implementation: How Granularity Works with Single Checkpoint Type

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    GRANULARITY VIA FILTERING                                     │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  All checkpoints stored uniformly in AgentGit:                                  │
│  ─────────────────────────────────────────────                                   │
│  [CP #1] metadata.node_id="data_load", tool="load_csv",   tool_track_pos=1     │
│  [CP #2] metadata.node_id="data_load", tool="validate",   tool_track_pos=2     │
│  [CP #3] metadata.node_id="data_load", tool="transform",  tool_track_pos=3     │
│  [CP #4] metadata.node_id="preprocess", tool="normalize", tool_track_pos=4     │
│  [CP #5] metadata.node_id="preprocess", tool="fill_na",   tool_track_pos=5     │
│  [CP #6] metadata.node_id="train", tool="fit_model",      tool_track_pos=6     │
│                                                                                  │
│  Node boundaries stored in WTB:                                                  │
│  ──────────────────────────────                                                  │
│  wtb_node_boundaries:                                                            │
│  ├── node_id="data_load",  entry_cp=1, exit_cp=3, status="completed"           │
│  ├── node_id="preprocess", entry_cp=4, exit_cp=5, status="completed"           │
│  └── node_id="train",      entry_cp=6, exit_cp=NULL, status="started"          │
│                                                                                  │
│  COARSE GRANULARITY (Node-level rollback targets):                               │
│  ──────────────────────────────────────────────────                              │
│  get_node_rollback_targets(session_id) → [CP #3, CP #5]                         │
│                                                                                  │
│  Implementation:                                                                 │
│  ```python                                                                       │
│  def get_node_rollback_targets(self, session_id: int) -> List[CheckpointInfo]: │
│      boundaries = uow.node_boundaries.get_completed(session_id)                 │
│      return [self.load_checkpoint(b.exit_checkpoint_id) for b in boundaries]   │
│  ```                                                                             │
│                                                                                  │
│  FINE GRANULARITY (All checkpoints in a node):                                   │
│  ──────────────────────────────────────────────                                  │
│  get_checkpoints_in_node(session_id, "data_load") → [CP #1, CP #2, CP #3]       │
│                                                                                  │
│  Implementation:                                                                 │
│  ```python                                                                       │
│  def get_checkpoints_in_node(self, session_id: int, node_id: str):              │
│      all_cps = self._checkpoint_repo.get_by_internal_session(session_id)        │
│      return [cp for cp in all_cps if cp.metadata.get("node_id") == node_id]    │
│  ```                                                                             │
│                                                                                  │
│  UNIFIED ROLLBACK (Same method for both):                                        │
│  ─────────────────────────────────────────                                       │
│  adapter.rollback(3)  # Rollback to end of data_load (node-level)               │
│  adapter.rollback(2)  # Rollback to after validate (tool-level)                 │
│                                                                                  │
│  BOTH call the same rollback() method - the granularity only affects            │
│  which checkpoints are SHOWN to the user, not how rollback executes.            │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 7.4 What Metadata Each Checkpoint Contains

```python
# Every checkpoint has identical structure - no "type" field needed

checkpoint.metadata = {
    # Core fields (always present)
    "node_id": "data_load",           # Which node this checkpoint belongs to
    "trigger_type": "tool_end",       # What triggered: tool_start, tool_end, llm_call, etc.
    "tool_track_position": 3,         # Position in tool sequence
    "wtb_execution_id": "exec-abc",   # WTB execution correlation
    
    # Tool-specific (if trigger_type is tool_start/tool_end)
    "tool_name": "transform",
    
    # MDP state for restoration
    "mdp_state": {
        "current_node_id": "data_load",
        "workflow_variables": {"x": 1, "y": 2},
        "execution_path": ["start", "data_load"],
    }
}
```

### 7.5 When Would Dual Checkpoint Types Be Needed?

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    WHEN OPTION B MIGHT BE CONSIDERED                             │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Scenario 1: UI Tree View of Checkpoints                                         │
│  ─────────────────────────────────────────                                       │
│  If you need to display checkpoints as a tree:                                  │
│    Node: data_load                                                               │
│    ├── CP #1: load_csv                                                          │
│    ├── CP #2: validate                                                          │
│    └── CP #3: transform                                                         │
│                                                                                  │
│  SOLUTION with Option A: Group by metadata.node_id                              │
│  ```python                                                                       │
│  def get_checkpoints_grouped_by_node(session_id):                               │
│      cps = self.get_checkpoints(session_id)                                     │
│      grouped = defaultdict(list)                                                │
│      for cp in cps:                                                             │
│          grouped[cp.node_id].append(cp)                                         │
│      return grouped                                                              │
│  ```                                                                             │
│                                                                                  │
│  Scenario 2: Delete Node = Delete All Checkpoints                               │
│  ──────────────────────────────────────────────────                              │
│  If you need cascade delete of all checkpoints in a node:                       │
│                                                                                  │
│  SOLUTION with Option A: Delete by metadata.node_id filter                      │
│  ```python                                                                       │
│  def delete_node_checkpoints(session_id, node_id):                              │
│      cps = self.get_checkpoints_in_node(session_id, node_id)                    │
│      for cp in cps:                                                             │
│          self._checkpoint_repo.delete(cp.id)                                    │
│  ```                                                                             │
│                                                                                  │
│  CONCLUSION: All "dual granularity" use cases can be solved by                  │
│              FILTERING on metadata.node_id, no need for checkpoint_type         │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 7.6 Design Principle (Add to All Documentation)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    GRANULARITY DESIGN PRINCIPLE                                  │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  "Single Checkpoint Type + Node Boundary Pointers"                              │
│  ═════════════════════════════════════════════════                              │
│                                                                                  │
│  1. ALL checkpoints are ATOMIC at tool/message level (finest granularity)       │
│     • No checkpoint_type field                                                   │
│     • No parent_checkpoint_id / child_checkpoint_ids                            │
│     • Each checkpoint tagged with metadata.node_id                              │
│                                                                                  │
│  2. Node boundaries are POINTERS in WTB's wtb_node_boundaries table             │
│     • entry_checkpoint_id = first checkpoint when entering node                 │
│     • exit_checkpoint_id = last checkpoint when exiting (rollback target)       │
│     • NOT separate checkpoints - just metadata about checkpoint range           │
│                                                                                  │
│  3. Rollback is UNIFIED                                                          │
│     • adapter.rollback(checkpoint_id) - one method for all                      │
│     • "Node rollback" = rollback(node_boundary.exit_checkpoint_id)              │
│     • "Fine rollback" = rollback(any_checkpoint_id)                             │
│                                                                                  │
│  4. Query by granularity = FILTERING on existing data                           │
│     • get_node_rollback_targets() = returns exit_checkpoint for each node      │
│     • get_checkpoints_in_node() = filter by metadata.node_id                   │
│                                                                                  │
│  5. Separation of concerns                                                       │
│     • AgentGit owns checkpoints (flat, unchanged)                               │
│     • WTB owns node semantics (wtb_node_boundaries table)                       │
│     • Adapter bridges them cleanly                                              │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### 7.7 Sequence Diagram: Single Granularity Checkpoint Flow

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│            SINGLE GRANULARITY CHECKPOINT CREATION SEQUENCE                       │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ExecutionController          AgentGitStateAdapter           AgentGit + WTB     │
│        │                              │                          │              │
│        │  === ENTERING NODE ===       │                          │              │
│        │                              │                          │              │
│        │  save_checkpoint(            │                          │              │
│        │    state, "data_load",       │                          │              │
│        │    trigger=TOOL_END,         │                          │              │
│        │    tool="load_csv")          │                          │              │
│        ├─────────────────────────────►│                          │              │
│        │                              │  Create checkpoint       │              │
│        │                              │  metadata.node_id =      │              │
│        │                              │    "data_load"           │              │
│        │                              ├─────────────────────────►│ AgentGit     │
│        │◄─────────────────────────────┤  checkpoint_id = 1       │              │
│        │                              │                          │              │
│        │  mark_node_started(          │                          │              │
│        │    "data_load", cp_id=1)     │                          │              │
│        ├─────────────────────────────►│                          │              │
│        │                              │  Create node boundary    │              │
│        │                              │  entry_checkpoint_id = 1 │              │
│        │                              ├─────────────────────────►│ WTB          │
│        │◄─────────────────────────────┤  boundary_id = 1         │              │
│        │                              │                          │              │
│        │  === MORE TOOLS ===          │                          │              │
│        │                              │                          │              │
│        │  save_checkpoint(...)        │                          │              │
│        ├─────────────────────────────►│  → CP #2, #3             │              │
│        │                              │                          │              │
│        │  === EXITING NODE ===        │                          │              │
│        │                              │                          │              │
│        │  mark_node_completed(        │                          │              │
│        │    "data_load",              │                          │              │
│        │    exit_cp_id=3)             │                          │              │
│        ├─────────────────────────────►│                          │              │
│        │                              │  Update node boundary    │              │
│        │                              │  exit_checkpoint_id = 3  │              │
│        │                              │  status = "completed"    │              │
│        │                              ├─────────────────────────►│ WTB          │
│        │◄─────────────────────────────┤  success                 │              │
│        │                              │                          │              │
│  RESULT:                                                                         │
│  ├── AgentGit: CP #1, #2, #3 (all same type, tagged with node_id)               │
│  └── WTB: node_boundary(entry_cp=1, exit_cp=3)                                  │
│                                                                                  │
│  To rollback to "end of data_load": rollback(3)                                 │
│  To rollback to "after validate": rollback(2)                                   │
│  Same method, same logic, different target.                                     │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 8. Required Updates to Existing Components

### 7.1 NodeBoundary Domain Model

```python
# wtb/domain/models/node_boundary.py (update)

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class NodeBoundary:
    """Node boundary domain model."""
    
    id: Optional[int] = None
    execution_id: str = ""
    internal_session_id: int = 0
    node_id: str = ""
    entry_checkpoint_id: Optional[int] = None
    exit_checkpoint_id: Optional[int] = None
    node_status: str = "started"
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    tool_count: int = 0
    checkpoint_count: int = 0
    duration_ms: Optional[int] = None
    error_message: Optional[str] = None
    error_details: Optional[str] = None
```

### 7.2 CheckpointFile Domain Model

```python
# wtb/domain/models/checkpoint_file.py (update)

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class CheckpointFile:
    """Checkpoint-file link domain model."""
    
    id: Optional[int] = None
    checkpoint_id: int = 0
    file_commit_id: str = ""
    file_count: int = 0
    total_size_bytes: int = 0
    created_at: datetime = field(default_factory=datetime.now)
```

### 7.3 NodeBoundaryRepository Update

```python
# wtb/infrastructure/database/repositories/node_boundary_repository.py (add methods)

def get_by_node(self, session_id: int, node_id: str) -> Optional[NodeBoundary]:
    """Get node boundary by session and node ID."""
    orm = (
        self._session.query(NodeBoundaryORM)
        .filter_by(internal_session_id=session_id, node_id=node_id)
        .first()
    )
    return self._to_domain(orm) if orm else None

def get_completed(self, session_id: int) -> List[NodeBoundary]:
    """Get completed node boundaries for a session."""
    orms = (
        self._session.query(NodeBoundaryORM)
        .filter_by(internal_session_id=session_id, node_status="completed")
        .all()
    )
    return [self._to_domain(orm) for orm in orms]
```

### 7.4 CheckpointFileRepository Update

```python
# wtb/infrastructure/database/repositories/checkpoint_file_repository.py (add method)

def get_by_checkpoint(self, checkpoint_id: int) -> Optional[CheckpointFile]:
    """Get file link by checkpoint ID."""
    orm = (
        self._session.query(CheckpointFileORM)
        .filter_by(checkpoint_id=checkpoint_id)
        .first()
    )
    return self._to_domain(orm) if orm else None
```

---

## 8. Testing Strategy

### 8.1 Unit Tests

```python
# tests/test_wtb/test_agentgit_state_adapter.py

class TestAgentGitStateAdapter:
    """Tests for AgentGitStateAdapter."""
    
    @pytest.fixture
    def adapter(self, tmp_path):
        """Create adapter with temp databases."""
        agentgit_db = tmp_path / "agentgit.db"
        wtb_db = tmp_path / "wtb.db"
        
        return AgentGitStateAdapter(
            agentgit_db_path=str(agentgit_db),
            wtb_db_url=f"sqlite:///{wtb_db}",
        )
    
    def test_initialize_session(self, adapter):
        """Test session initialization creates AgentGit session."""
        state = ExecutionState(current_node_id="start")
        
        session_id = adapter.initialize_session("exec-1", state)
        
        assert session_id is not None
        assert adapter.get_current_session_id() == session_id
    
    def test_save_and_load_checkpoint(self, adapter):
        """Test checkpoint round-trip."""
        state = ExecutionState(
            current_node_id="node1",
            workflow_variables={"x": 1},
        )
        adapter.initialize_session("exec-1", state)
        
        cp_id = adapter.save_checkpoint(
            state=state,
            node_id="node1",
            trigger=CheckpointTrigger.AUTO,
        )
        
        loaded = adapter.load_checkpoint(cp_id)
        assert loaded.current_node_id == "node1"
        assert loaded.workflow_variables["x"] == 1
    
    def test_node_boundary_lifecycle(self, adapter):
        """Test node boundary create/update."""
        state = ExecutionState(current_node_id="node1")
        adapter.initialize_session("exec-1", state)
        session_id = adapter.get_current_session_id()
        
        # Start node
        entry_cp = adapter.save_checkpoint(state, "node1", CheckpointTrigger.AUTO)
        boundary_id = adapter.mark_node_started("node1", entry_cp)
        assert boundary_id > 0
        
        # Complete node
        exit_cp = adapter.save_checkpoint(state, "node1", CheckpointTrigger.AUTO)
        assert adapter.mark_node_completed("node1", exit_cp)
        
        # Verify
        boundary = adapter.get_node_boundary(session_id, "node1")
        assert boundary is not None
        assert boundary.node_status == "completed"
    
    def test_rollback(self, adapter):
        """Test rollback restores state."""
        state1 = ExecutionState(
            current_node_id="node1",
            workflow_variables={"x": 1},
        )
        adapter.initialize_session("exec-1", state1)
        
        cp1 = adapter.save_checkpoint(state1, "node1", CheckpointTrigger.AUTO)
        
        state2 = ExecutionState(
            current_node_id="node2",
            workflow_variables={"x": 2},
        )
        adapter.save_checkpoint(state2, "node2", CheckpointTrigger.AUTO)
        
        restored = adapter.rollback(cp1)
        assert restored.current_node_id == "node1"
        assert restored.workflow_variables["x"] == 1
```

### 8.2 Integration Tests

```python
# tests/test_wtb/test_integration_agentgit.py

class TestAgentGitIntegration:
    """Integration tests with real AgentGit database."""
    
    def test_full_execution_with_checkpoints(self, tmp_path):
        """Test complete execution flow with AgentGit persistence."""
        # Setup
        agentgit_db = tmp_path / "agentgit.db"
        wtb_db = tmp_path / "wtb.db"
        
        adapter = AgentGitStateAdapter(
            agentgit_db_path=str(agentgit_db),
            wtb_db_url=f"sqlite:///{wtb_db}",
        )
        
        uow = SQLAlchemyUnitOfWork(f"sqlite:///{wtb_db}")
        
        with uow:
            controller = ExecutionController(
                execution_repository=uow.executions,
                workflow_repository=uow.workflows,
                state_adapter=adapter,
            )
            
            # Create workflow
            workflow = TestWorkflow(name="test")
            workflow.add_node(WorkflowNode("start", "Start", "start"))
            workflow.add_node(WorkflowNode("end", "End", "end"))
            workflow.add_edge(WorkflowEdge("start", "end"))
            uow.workflows.add(workflow)
            uow.commit()
            
            # Run execution
            execution = controller.create_execution(workflow)
            result = controller.run(execution.id)
            
            assert result.status == ExecutionStatus.COMPLETED
            
            # Verify checkpoints in AgentGit
            session_id = execution.agentgit_session_id
            checkpoints = adapter.get_checkpoints(session_id)
            assert len(checkpoints) >= 2  # At least entry + exit
            
            # Verify node boundaries in WTB
            boundaries = adapter.get_node_boundaries(session_id)
            assert len(boundaries) >= 1
```

---

## 9. Summary

### 9.1 Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| AgentGit repos access | Direct (raw SQL) | Don't modify external package |
| WTB tables access | SQLAlchemy + UoW | WTB owns these, need transactions |
| Adapter responsibility | Anti-corruption layer | Isolate domain boundaries |
| Node boundaries storage | WTB database (pointers) | WTB-specific concept; points to AgentGit checkpoints |
| Checkpoint storage | AgentGit database (flat) | Leverage existing system, all checkpoints equal |
| **Checkpoint granularity** | **Single type + boundary pointers** | **No state duplication, unified rollback, simpler adapter** |

### 9.2 Design Principles (To Add to PROJECT_PRINCIPLES.md)

> **PRINCIPLE 1: "WTB uses dual database patterns: AgentGit repositories for checkpoint state; SQLAlchemy UoW for WTB domain data. Never mix - adapters bridge the boundary."**

> **PRINCIPLE 2: "Single Checkpoint Type + Node Boundary Pointers: All checkpoints are atomic at tool/message level. Node boundaries are POINTERS (entry_cp_id, exit_cp_id) in WTB's wtb_node_boundaries table, NOT separate checkpoints. Granularity is achieved by FILTERING, not by checkpoint type hierarchy."**

### 9.3 Implementation Checklist

- [ ] Create `AgentGitStateAdapter` in `wtb/infrastructure/adapters/`
  - [ ] Uses AgentGit repositories directly (raw SQL, unchanged)
  - [ ] Uses WTB UoW for node boundaries and checkpoint files
  - [ ] Single checkpoint type - all checkpoints tagged with metadata.node_id
  - [ ] Node boundaries are pointers (entry_cp_id, exit_cp_id), not separate checkpoints
- [ ] Update `NodeBoundary` domain model with all fields
- [ ] Update `CheckpointFile` domain model with all fields
- [ ] Add `get_by_node()` and `get_completed()` to `NodeBoundaryRepository`
- [ ] Add `get_by_checkpoint()` to `CheckpointFileRepository`
- [ ] Implement granularity filtering via metadata.node_id:
  - [ ] `get_node_rollback_targets()` - returns exit_checkpoint for each completed node
  - [ ] `get_checkpoints_in_node()` - filters by metadata.node_id
- [ ] Create `ExecutionControllerFactory` for dependency wiring
- [ ] Add unit tests for `AgentGitStateAdapter`
- [ ] Add integration tests with real databases
- [ ] Update `PROGRESS_TRACKER.md` when complete

### 9.4 Clarification: Section 13.2 in WORKFLOW_TEST_BENCH_ARCHITECTURE.md

Section 13.2 describes AgentGit's **capability** for dual granularity checkpoints (node vs tool_message types with parent/child linking). However, WTB **does not use** this hierarchical model. Instead:

| Architecture Doc (13.2) Says | WTB Implementation Decision |
|------------------------------|----------------------------|
| checkpoint_type = "node" \| "tool_message" | **Not used** - all checkpoints are equal |
| parent_checkpoint_id / child_checkpoint_ids | **Not used** - no hierarchy linking |
| Separate node checkpoints at entry/exit | **Not used** - would duplicate state |
| get_rollback_targets(granularity=NODE) | **Implemented via** get_node_rollback_targets() using node boundary exit_cp |

The Section 13.2 design was exploratory. The final decision is: **Single Checkpoint Type + Node Boundary Pointers** as documented in this design document (Section 7).

---

## 10. Related Documents

| Document | Description |
|----------|-------------|
| [WTB_PERSISTENCE_DESIGN.md](./WTB_PERSISTENCE_DESIGN.md) | WTB storage abstraction (InMemory + SQLAlchemy UoW) |
| [DATABASE_DESIGN.md](../Project_Init/DATABASE_DESIGN.md) | Complete database schema for all three systems |
| [WORKFLOW_TEST_BENCH_ARCHITECTURE.md](../Project_Init/WORKFLOW_TEST_BENCH_ARCHITECTURE.md) | Overall WTB architecture, Section 13 for key decisions |
| [WORKFLOW_TEST_BENCH_SUMMARY.md](../Project_Init/WORKFLOW_TEST_BENCH_SUMMARY.md) | Executive summary with configuration examples |
