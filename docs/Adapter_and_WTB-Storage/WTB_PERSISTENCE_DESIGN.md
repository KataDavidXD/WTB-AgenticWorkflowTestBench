# WTB Persistence Layer - Detailed Design

## Executive Summary

This document specifies the WTB data persistence layer with **dual implementation pattern**:
- **InMemoryUnitOfWork**: For unit tests and fast iteration (no I/O)
- **SQLAlchemyUnitOfWork**: For production with database persistence

### Design Principles

> **"Interface → Multiple Implementations: IUnitOfWork abstraction enables swappable storage backends."**

> **"WTB uses dual database patterns: AgentGit repositories for checkpoint state; SQLAlchemy UoW for WTB domain data. Never mix - adapters bridge the boundary."**

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                     WTB STORAGE ABSTRACTION ARCHITECTURE                         │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  Application Layer (ExecutionController, NodeReplacer, BatchTestRunner)         │
│         │                                                                        │
│         │ depends on abstractions only (DIP)                                    │
│         ▼                                                                        │
│  ┌──────────────────────────────────────────────────────────────────────────┐   │
│  │                        IUnitOfWork (interface)                            │   │
│  │                                                                           │   │
│  │  Properties:                                                              │   │
│  │  ├── workflows: IWorkflowRepository                                       │   │
│  │  ├── executions: IExecutionRepository                                     │   │
│  │  ├── node_boundaries: INodeBoundaryRepository                             │   │
│  │  ├── checkpoint_files: ICheckpointFileRepository                          │   │
│  │  ├── node_variants: INodeVariantRepository                                │   │
│  │  └── evaluation_results: IEvaluationResultRepository                      │   │
│  │                                                                           │   │
│  │  Methods:                                                                 │   │
│  │  ├── __enter__() / __exit__() - Context manager                          │   │
│  │  ├── commit() - Persist all changes                                       │   │
│  │  └── rollback() - Discard all changes                                     │   │
│  └──────────────────────────────────────────────────────────────────────────┘   │
│                             │                                                    │
│         ┌───────────────────┼───────────────────┐                               │
│         │                   │                   │                               │
│         ▼                   ▼                   ▼                               │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐                    │
│  │   InMemory     │  │  SQLAlchemy    │  │   Future:      │                    │
│  │   UnitOfWork   │  │  UnitOfWork    │  │  PostgreSQL    │                    │
│  │                │  │                │  │  UnitOfWork    │                    │
│  │  ─────────────│  │  ─────────────│  │  ─────────────│                    │
│  │  • Dict-based  │  │  • SQLite file │  │  • PostgreSQL  │                    │
│  │  • No I/O      │  │  • ORM models  │  │  • Connection  │                    │
│  │  • Fast tests  │  │  • Transactions│  │    pooling     │                    │
│  └────────────────┘  └────────────────┘  └────────────────┘                    │
│                                                                                  │
│  SELECTION VIA FACTORY:                                                          │
│  ──────────────────────                                                          │
│  uow = UnitOfWorkFactory.create(mode="inmemory")   # For tests                  │
│  uow = UnitOfWorkFactory.create(mode="sqlalchemy", db_url="sqlite:///wtb.db")   │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Repository Interfaces

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
        """Add new entity. Returns entity with ID populated."""
        pass
    
    @abstractmethod
    def update(self, entity: T) -> T:
        """Update existing entity."""
        pass
    
    @abstractmethod
    def delete(self, id: int | str) -> bool:
        """Delete entity by ID. Returns True if deleted."""
        pass


class IWorkflowRepository(IRepository['Workflow']):
    """Repository for WTB workflows."""
    
    @abstractmethod
    def list_all(self) -> List['Workflow']:
        """List all workflows."""
        pass
    
    @abstractmethod
    def get_by_name(self, name: str) -> Optional['Workflow']:
        """Get workflow by name."""
        pass


class IExecutionRepository(IRepository['Execution']):
    """Repository for WTB executions."""
    
    @abstractmethod
    def get_by_workflow(self, workflow_id: str) -> List['Execution']:
        """Get all executions for a workflow."""
        pass
    
    @abstractmethod
    def get_by_status(self, status: str) -> List['Execution']:
        """Get executions by status."""
        pass
    
    @abstractmethod
    def get_running(self) -> List['Execution']:
        """Get currently running executions."""
        pass


class INodeBoundaryRepository(IRepository['NodeBoundary']):
    """Repository for node boundaries (pointers to checkpoints)."""
    
    @abstractmethod
    def get_by_session(self, internal_session_id: int) -> List['NodeBoundary']:
        """Get all boundaries for a session."""
        pass
    
    @abstractmethod
    def get_by_node(self, internal_session_id: int, node_id: str) -> Optional['NodeBoundary']:
        """Get boundary for specific node in session."""
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


class INodeVariantRepository(IRepository['NodeVariant']):
    """Repository for node variants (A/B testing)."""
    
    @abstractmethod
    def get_by_node(self, node_id: str) -> List['NodeVariant']:
        """Get all variants for a node."""
        pass
    
    @abstractmethod
    def get_active(self, node_id: str) -> List['NodeVariant']:
        """Get active variants for a node."""
        pass


class IEvaluationResultRepository(IRepository['EvaluationResult']):
    """Repository for evaluation results."""
    
    @abstractmethod
    def get_by_execution(self, execution_id: str) -> List['EvaluationResult']:
        """Get all results for an execution."""
        pass


class IAuditLogRepository(IRepository['WTBAuditEntry']):
    """Repository for WTB audit logs (persistence)."""
    
    @abstractmethod
    def append_logs(self, execution_id: str, logs: List['WTBAuditEntry']) -> None:
        """Append a batch of logs for an execution."""
        pass
    
    @abstractmethod
    def get_by_execution(self, execution_id: str) -> List['WTBAuditEntry']:
        """Get all logs for an execution."""
        pass
```

---

## 3. Unit of Work Interface

```python
# ═══════════════════════════════════════════════════════════════════════════════
# wtb/infrastructure/interfaces/unit_of_work.py
# ═══════════════════════════════════════════════════════════════════════════════

from abc import ABC, abstractmethod


class IUnitOfWork(ABC):
    """
    Unit of Work pattern - manages transaction boundaries.
    
    All repository operations within a UoW share the same transaction.
    Changes are only persisted on commit().
    
    Usage:
        with uow:
            uow.workflows.add(workflow)
            uow.executions.add(execution)
            uow.commit()  # Both saved atomically
    
    If an exception occurs, changes are automatically rolled back.
    """
    
    # Repository properties (set by concrete implementations)
    workflows: 'IWorkflowRepository'
    executions: 'IExecutionRepository'
    node_boundaries: 'INodeBoundaryRepository'
    checkpoint_files: 'ICheckpointFileRepository'
    node_variants: 'INodeVariantRepository'
    evaluation_results: 'IEvaluationResultRepository'
    audit_logs: 'IAuditLogRepository'
    
    @abstractmethod
    def __enter__(self) -> 'IUnitOfWork':
        """Enter context - begin transaction."""
        pass
    
    @abstractmethod
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context - rollback if exception occurred."""
        pass
    
    @abstractmethod
    def commit(self):
        """Commit all changes to the database."""
        pass
    
    @abstractmethod
    def rollback(self):
        """Rollback all uncommitted changes."""
        pass
```

---

## 4. In-Memory Implementation

```python
# ═══════════════════════════════════════════════════════════════════════════════
# wtb/infrastructure/persistence/inmemory/repositories.py
# In-memory repositories for testing - no database I/O
# ═══════════════════════════════════════════════════════════════════════════════

from typing import Dict, Optional, List
from copy import deepcopy

from wtb.domain.models import (
    Workflow, Execution, NodeBoundary, CheckpointFile,
    NodeVariant, EvaluationResult
)
from wtb.infrastructure.interfaces.repository import (
    IWorkflowRepository, IExecutionRepository, INodeBoundaryRepository,
    ICheckpointFileRepository, INodeVariantRepository, IEvaluationResultRepository
)


class InMemoryWorkflowRepository(IWorkflowRepository):
    """In-memory workflow repository."""
    
    def __init__(self):
        self._store: Dict[str, Workflow] = {}
    
    def get_by_id(self, id: str) -> Optional[Workflow]:
        return deepcopy(self._store.get(id))
    
    def add(self, entity: Workflow) -> Workflow:
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def update(self, entity: Workflow) -> Workflow:
        if entity.id not in self._store:
            raise ValueError(f"Workflow {entity.id} not found")
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._store:
            del self._store[id]
            return True
        return False
    
    def list_all(self) -> List[Workflow]:
        return [deepcopy(w) for w in self._store.values()]
    
    def get_by_name(self, name: str) -> Optional[Workflow]:
        for w in self._store.values():
            if w.name == name:
                return deepcopy(w)
        return None


class InMemoryExecutionRepository(IExecutionRepository):
    """In-memory execution repository."""
    
    def __init__(self):
        self._store: Dict[str, Execution] = {}
    
    def get_by_id(self, id: str) -> Optional[Execution]:
        return deepcopy(self._store.get(id))
    
    def add(self, entity: Execution) -> Execution:
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def update(self, entity: Execution) -> Execution:
        if entity.id not in self._store:
            raise ValueError(f"Execution {entity.id} not found")
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._store:
            del self._store[id]
            return True
        return False
    
    def get_by_workflow(self, workflow_id: str) -> List[Execution]:
        return [deepcopy(e) for e in self._store.values() 
                if e.workflow_id == workflow_id]
    
    def get_by_status(self, status: str) -> List[Execution]:
        return [deepcopy(e) for e in self._store.values() 
                if e.status == status]
    
    def get_running(self) -> List[Execution]:
        return self.get_by_status('running')


class InMemoryNodeBoundaryRepository(INodeBoundaryRepository):
    """In-memory node boundary repository."""
    
    def __init__(self):
        self._store: Dict[int, NodeBoundary] = {}
        self._next_id: int = 1
    
    def get_by_id(self, id: int) -> Optional[NodeBoundary]:
        return deepcopy(self._store.get(id))
    
    def add(self, entity: NodeBoundary) -> NodeBoundary:
        entity.id = self._next_id
        self._next_id += 1
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def update(self, entity: NodeBoundary) -> NodeBoundary:
        if entity.id not in self._store:
            raise ValueError(f"NodeBoundary {entity.id} not found")
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def delete(self, id: int) -> bool:
        if id in self._store:
            del self._store[id]
            return True
        return False
    
    def get_by_session(self, internal_session_id: int) -> List[NodeBoundary]:
        return [deepcopy(b) for b in self._store.values() 
                if b.internal_session_id == internal_session_id]
    
    def get_by_node(self, internal_session_id: int, node_id: str) -> Optional[NodeBoundary]:
        for b in self._store.values():
            if b.internal_session_id == internal_session_id and b.node_id == node_id:
                return deepcopy(b)
        return None
    
    def get_completed(self, internal_session_id: int) -> List[NodeBoundary]:
        return [deepcopy(b) for b in self._store.values()
                if b.internal_session_id == internal_session_id 
                and b.node_status == 'completed']


class InMemoryCheckpointFileRepository(ICheckpointFileRepository):
    """In-memory checkpoint file repository."""
    
    def __init__(self):
        self._store: Dict[int, CheckpointFile] = {}
        self._next_id: int = 1
    
    def get_by_id(self, id: int) -> Optional[CheckpointFile]:
        return deepcopy(self._store.get(id))
    
    def add(self, entity: CheckpointFile) -> CheckpointFile:
        entity.id = self._next_id
        self._next_id += 1
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def update(self, entity: CheckpointFile) -> CheckpointFile:
        if entity.id not in self._store:
            raise ValueError(f"CheckpointFile {entity.id} not found")
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def delete(self, id: int) -> bool:
        if id in self._store:
            del self._store[id]
            return True
        return False
    
    def get_by_checkpoint(self, checkpoint_id: int) -> Optional[CheckpointFile]:
        for cf in self._store.values():
            if cf.checkpoint_id == checkpoint_id:
                return deepcopy(cf)
        return None
    
    def get_by_file_commit(self, file_commit_id: str) -> List[CheckpointFile]:
        return [deepcopy(cf) for cf in self._store.values() 
                if cf.file_commit_id == file_commit_id]


class InMemoryNodeVariantRepository(INodeVariantRepository):
    """In-memory node variant repository."""
    
    def __init__(self):
        self._store: Dict[str, NodeVariant] = {}
    
    def get_by_id(self, id: str) -> Optional[NodeVariant]:
        return deepcopy(self._store.get(id))
    
    def add(self, entity: NodeVariant) -> NodeVariant:
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def update(self, entity: NodeVariant) -> NodeVariant:
        if entity.id not in self._store:
            raise ValueError(f"NodeVariant {entity.id} not found")
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._store:
            del self._store[id]
            return True
        return False
    
    def get_by_node(self, node_id: str) -> List[NodeVariant]:
        return [deepcopy(v) for v in self._store.values() 
                if v.node_id == node_id]
    
    def get_active(self, node_id: str) -> List[NodeVariant]:
        return [deepcopy(v) for v in self._store.values() 
                if v.node_id == node_id and v.is_active]


class InMemoryEvaluationResultRepository(IEvaluationResultRepository):
    """In-memory evaluation result repository."""
    
    def __init__(self):
        self._store: Dict[str, EvaluationResult] = {}
    
    def get_by_id(self, id: str) -> Optional[EvaluationResult]:
        return deepcopy(self._store.get(id))
    
    def add(self, entity: EvaluationResult) -> EvaluationResult:
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def update(self, entity: EvaluationResult) -> EvaluationResult:
        if entity.id not in self._store:
            raise ValueError(f"EvaluationResult {entity.id} not found")
        self._store[entity.id] = deepcopy(entity)
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._store:
            del self._store[id]
            return True
        return False
    
    def get_by_execution(self, execution_id: str) -> List[EvaluationResult]:
        return [deepcopy(r) for r in self._store.values() 
                if r.execution_id == execution_id]
```

```python
# ═══════════════════════════════════════════════════════════════════════════════
# wtb/infrastructure/persistence/inmemory/unit_of_work.py
# ═══════════════════════════════════════════════════════════════════════════════

from wtb.infrastructure.interfaces.unit_of_work import IUnitOfWork
from .repositories import (
    InMemoryWorkflowRepository, InMemoryExecutionRepository,
    InMemoryNodeBoundaryRepository, InMemoryCheckpointFileRepository,
    InMemoryNodeVariantRepository, InMemoryEvaluationResultRepository
)


class InMemoryUnitOfWork(IUnitOfWork):
    """
    In-memory Unit of Work for testing.
    
    Benefits:
    - No database I/O (extremely fast tests)
    - Isolated per test instance (no cleanup needed)
    - Same interface as SQLAlchemyUnitOfWork (LSP compliant)
    
    Usage:
        uow = InMemoryUnitOfWork()
        with uow:
            uow.workflows.add(workflow)
            uow.executions.add(execution)
            uow.commit()  # No-op, data already in memory
    
    Note: For true isolation between tests, create a new InMemoryUnitOfWork
    instance for each test.
    """
    
    def __init__(self):
        # Initialize all repositories
        self.workflows = InMemoryWorkflowRepository()
        self.executions = InMemoryExecutionRepository()
        self.node_boundaries = InMemoryNodeBoundaryRepository()
        self.checkpoint_files = InMemoryCheckpointFileRepository()
        self.node_variants = InMemoryNodeVariantRepository()
        self.evaluation_results = InMemoryEvaluationResultRepository()
        
        self._in_transaction = False
    
    def __enter__(self) -> 'InMemoryUnitOfWork':
        self._in_transaction = True
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self._in_transaction = False
        # No rollback needed - in-memory doesn't have pending state
    
    def commit(self):
        """No-op for in-memory - data is already persisted in dicts."""
        pass
    
    def rollback(self):
        """No-op for in-memory - create new UoW instance for isolation."""
        pass
```

---

## 5. SQLAlchemy Implementation

```python
# ═══════════════════════════════════════════════════════════════════════════════
# wtb/infrastructure/persistence/sqlalchemy/unit_of_work.py
# ═══════════════════════════════════════════════════════════════════════════════

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from wtb.infrastructure.interfaces.unit_of_work import IUnitOfWork
from .models import Base
from .repositories import (
    SQLAlchemyWorkflowRepository, SQLAlchemyExecutionRepository,
    SQLAlchemyNodeBoundaryRepository, SQLAlchemyCheckpointFileRepository,
    SQLAlchemyNodeVariantRepository, SQLAlchemyEvaluationResultRepository
)


class SQLAlchemyUnitOfWork(IUnitOfWork):
    """
    SQLAlchemy Unit of Work for production.
    
    Features:
    - Proper transaction management
    - Automatic rollback on exception
    - Connection pooling (for PostgreSQL)
    - Schema auto-creation
    
    Usage:
        uow = SQLAlchemyUnitOfWork("sqlite:///wtb.db")
        with uow:
            uow.workflows.add(workflow)
            uow.executions.add(execution)
            uow.commit()  # Persisted to database
    """
    
    def __init__(self, db_url: str = "sqlite:///data/wtb.db"):
        self._engine = create_engine(db_url, echo=False)
        self._session_factory = sessionmaker(bind=self._engine)
        
        # Create tables if they don't exist
        Base.metadata.create_all(self._engine)
        
        self._session: Session = None
    
    def __enter__(self) -> 'SQLAlchemyUnitOfWork':
        self._session = self._session_factory()
        
        # Initialize repositories with the session
        self.workflows = SQLAlchemyWorkflowRepository(self._session)
        self.executions = SQLAlchemyExecutionRepository(self._session)
        self.node_boundaries = SQLAlchemyNodeBoundaryRepository(self._session)
        self.checkpoint_files = SQLAlchemyCheckpointFileRepository(self._session)
        self.node_variants = SQLAlchemyNodeVariantRepository(self._session)
        self.evaluation_results = SQLAlchemyEvaluationResultRepository(self._session)
        
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        self._session.close()
    
    def commit(self):
        """Commit all changes to the database."""
        self._session.commit()
    
    def rollback(self):
        """Rollback all uncommitted changes."""
        self._session.rollback()
```

---

## 6. Factory Pattern

```python
# ═══════════════════════════════════════════════════════════════════════════════
# wtb/infrastructure/persistence/factory.py
# ═══════════════════════════════════════════════════════════════════════════════

from typing import Optional
from wtb.infrastructure.interfaces.unit_of_work import IUnitOfWork
from .inmemory.unit_of_work import InMemoryUnitOfWork
from .sqlalchemy.unit_of_work import SQLAlchemyUnitOfWork


class UnitOfWorkFactory:
    """
    Factory for creating the appropriate UnitOfWork implementation.
    
    Supports dependency injection and configuration-based switching.
    
    Usage:
        # For testing (no database)
        uow = UnitOfWorkFactory.create_inmemory()
        
        # For development (SQLite file)
        uow = UnitOfWorkFactory.create_sqlalchemy("sqlite:///data/wtb.db")
        
        # For production (PostgreSQL)
        uow = UnitOfWorkFactory.create_sqlalchemy("postgresql://user:pass@host/db")
        
        # Config-based
        uow = UnitOfWorkFactory.create(
            mode=config.wtb_storage_mode,
            db_url=config.wtb_db_url
        )
    """
    
    @staticmethod
    def create_inmemory() -> InMemoryUnitOfWork:
        """Create in-memory UoW for testing."""
        return InMemoryUnitOfWork()
    
    @staticmethod
    def create_sqlalchemy(db_url: str = "sqlite:///data/wtb.db") -> SQLAlchemyUnitOfWork:
        """Create SQLAlchemy UoW for production."""
        return SQLAlchemyUnitOfWork(db_url)
    
    @staticmethod
    def create(
        mode: str = "inmemory",
        db_url: Optional[str] = None
    ) -> IUnitOfWork:
        """
        Create UoW based on mode configuration.
        
        Args:
            mode: "inmemory" or "sqlalchemy"
            db_url: Database URL (required for sqlalchemy mode)
            
        Returns:
            IUnitOfWork implementation
            
        Raises:
            ValueError: If mode is unknown or db_url missing for sqlalchemy
        """
        if mode == "inmemory":
            return UnitOfWorkFactory.create_inmemory()
        
        elif mode == "sqlalchemy":
            if not db_url:
                db_url = "sqlite:///data/wtb.db"  # Default to SQLite
            return UnitOfWorkFactory.create_sqlalchemy(db_url)
        
        else:
            raise ValueError(f"Unknown storage mode: {mode}. Use 'inmemory' or 'sqlalchemy'")
```

---

## 7. Configuration Integration

```python
# ═══════════════════════════════════════════════════════════════════════════════
# wtb/config.py
# ═══════════════════════════════════════════════════════════════════════════════

from dataclasses import dataclass
from typing import Optional
import os


@dataclass
class WTBConfig:
    """WTB configuration with storage options."""
    
    # Storage mode: "inmemory" or "sqlalchemy"
    wtb_storage_mode: str = "inmemory"
    
    # Database URL (for sqlalchemy mode)
    wtb_db_url: Optional[str] = None
    
    # AgentGit database path
    agentgit_db_path: str = "data/agentgit.db"
    
    # State adapter mode: "inmemory" or "agentgit"
    state_adapter_mode: str = "inmemory"
    
    @classmethod
    def from_env(cls) -> 'WTBConfig':
        """Create config from environment variables."""
        return cls(
            wtb_storage_mode=os.getenv("WTB_STORAGE_MODE", "inmemory"),
            wtb_db_url=os.getenv("WTB_DB_URL"),
            agentgit_db_path=os.getenv("AGENTGIT_DB_PATH", "data/agentgit.db"),
            state_adapter_mode=os.getenv("STATE_ADAPTER_MODE", "inmemory"),
        )
    
    @classmethod
    def for_testing(cls) -> 'WTBConfig':
        """Create config for unit tests (all in-memory)."""
        return cls(
            wtb_storage_mode="inmemory",
            state_adapter_mode="inmemory",
        )
    
    @classmethod
    def for_development(cls) -> 'WTBConfig':
        """Create config for development (SQLite persistence)."""
        return cls(
            wtb_storage_mode="sqlalchemy",
            wtb_db_url="sqlite:///data/wtb.db",
            agentgit_db_path="data/agentgit.db",
            state_adapter_mode="agentgit",
        )
    
    @classmethod
    def for_production(cls, db_url: str) -> 'WTBConfig':
        """Create config for production (PostgreSQL)."""
        return cls(
            wtb_storage_mode="sqlalchemy",
            wtb_db_url=db_url,
            agentgit_db_path="data/agentgit.db",
            state_adapter_mode="agentgit",
        )
```

---

## 8. Usage Examples

### 8.1 Unit Test Example

```python
# tests/test_execution_controller.py

import pytest
from wtb.infrastructure.persistence.factory import UnitOfWorkFactory
from wtb.infrastructure.adapters.inmemory_state_adapter import InMemoryStateAdapter
from wtb.application.services.execution_controller import ExecutionController


class TestExecutionController:
    """Tests using in-memory storage (fast, isolated)."""
    
    @pytest.fixture
    def uow(self):
        """Fresh in-memory UoW for each test."""
        return UnitOfWorkFactory.create_inmemory()
    
    @pytest.fixture
    def state_adapter(self):
        """Fresh in-memory state adapter for each test."""
        return InMemoryStateAdapter()
    
    @pytest.fixture
    def controller(self, uow, state_adapter):
        """Controller with in-memory dependencies."""
        return ExecutionController(
            uow=uow,
            state_adapter=state_adapter,
        )
    
    def test_create_execution(self, controller, uow):
        """Test creating an execution."""
        with uow:
            # Create workflow first
            workflow = Workflow(id="wf-1", name="test", definition={})
            uow.workflows.add(workflow)
            uow.commit()
        
        with uow:
            execution = controller.create_execution("wf-1")
            
            assert execution.id is not None
            assert execution.workflow_id == "wf-1"
            assert execution.status == "pending"
```

### 8.2 Production Example

```python
# wtb/main.py

from wtb.config import WTBConfig
from wtb.infrastructure.persistence.factory import UnitOfWorkFactory
from wtb.infrastructure.adapters.agentgit_state_adapter import AgentGitStateAdapter
from wtb.application.services.execution_controller import ExecutionController


def create_production_controller() -> ExecutionController:
    """Create controller with production dependencies."""
    config = WTBConfig.for_production("postgresql://user:pass@localhost/wtb")
    
    uow = UnitOfWorkFactory.create(
        mode=config.wtb_storage_mode,
        db_url=config.wtb_db_url,
    )
    
    state_adapter = AgentGitStateAdapter(
        agentgit_db_path=config.agentgit_db_path,
        wtb_uow=uow,
    )
    
    return ExecutionController(
        uow=uow,
        state_adapter=state_adapter,
    )


# Usage
controller = create_production_controller()

with controller.uow:
    execution = controller.create_execution("my-workflow")
    result = controller.run(execution.id)
    controller.uow.commit()
```

---

## 9. Summary

### Storage Implementation Matrix

| Mode | UnitOfWork | Use Case | Persistence | I/O |
|------|------------|----------|-------------|-----|
| `inmemory` | `InMemoryUnitOfWork` | Unit tests, fast iteration | No | None |
| `sqlalchemy` (SQLite) | `SQLAlchemyUnitOfWork` | Development, single-user | Yes (file) | Low |
| `sqlalchemy` (PostgreSQL) | `SQLAlchemyUnitOfWork` | Production, multi-user | Yes (server) | Medium |

### Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Pattern | Repository + Unit of Work | Clean separation, transaction management |
| Abstraction | Interfaces (IUnitOfWork, IRepository) | Enables swappable implementations |
| Default | InMemory for tests | Fast feedback, no setup |
| Production | SQLAlchemy with PostgreSQL | Battle-tested, scalable |
| Factory | UnitOfWorkFactory | Clean dependency injection |

### Implementation Checklist

- [ ] Create `wtb/infrastructure/interfaces/repository.py` with all interfaces
- [ ] Create `wtb/infrastructure/interfaces/unit_of_work.py` with IUnitOfWork
- [ ] Create `wtb/infrastructure/persistence/inmemory/` package with all repos + UoW
- [ ] Create `wtb/infrastructure/persistence/sqlalchemy/` package with all repos + UoW
- [ ] Create `wtb/infrastructure/persistence/factory.py` with UnitOfWorkFactory
- [ ] Create `wtb/config.py` with WTBConfig
- [ ] Add unit tests for in-memory repositories
- [ ] Add integration tests for SQLAlchemy repositories
- [ ] Update ExecutionController to use IUnitOfWork

