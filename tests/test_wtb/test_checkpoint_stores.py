"""
Unit tests for Checkpoint Store Implementations.

Tests for:
- InMemoryCheckpointStore
- LangGraphCheckpointStore (basic functionality)

These tests verify ICheckpointStore contract compliance.
"""

import pytest
from datetime import datetime, timedelta
from typing import List

from wtb.domain.models.checkpoint import (
    CheckpointId,
    Checkpoint,
    ExecutionHistory,
)
from wtb.infrastructure.stores.inmemory_checkpoint_store import (
    InMemoryCheckpointStore,
    InMemoryCheckpointStoreFactory,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def store() -> InMemoryCheckpointStore:
    """Fresh InMemoryCheckpointStore for each test."""
    return InMemoryCheckpointStore()


@pytest.fixture
def sample_checkpoint() -> Checkpoint:
    """Sample checkpoint for testing."""
    return Checkpoint(
        id=CheckpointId("cp-001"),
        execution_id="exec-001",
        step=1,
        node_writes={"node_a": {"result": "success"}},
        next_nodes=["node_b"],
        state_values={"value": 42},
        created_at=datetime.now(),
    )


@pytest.fixture
def sample_checkpoints() -> List[Checkpoint]:
    """Multiple sample checkpoints for testing."""
    now = datetime.now()
    return [
        Checkpoint(
            id=CheckpointId("cp-001"),
            execution_id="exec-001",
            step=0,
            node_writes={},
            next_nodes=["node_a"],
            state_values={"input": "data"},
            created_at=now,
        ),
        Checkpoint(
            id=CheckpointId("cp-002"),
            execution_id="exec-001",
            step=1,
            node_writes={"node_a": {"result": "processed"}},
            next_nodes=["node_b"],
            state_values={"output_a": "processed"},
            created_at=now + timedelta(seconds=1),
        ),
        Checkpoint(
            id=CheckpointId("cp-003"),
            execution_id="exec-001",
            step=2,
            node_writes={"node_b": {"result": "analyzed"}},
            next_nodes=[],
            state_values={"final": "done"},
            created_at=now + timedelta(seconds=2),
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# InMemoryCheckpointStore Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestInMemoryCheckpointStore:
    """Tests for InMemoryCheckpointStore."""
    
    # ─────────────────────────────────────────────────────────────────────────
    # Basic CRUD Operations
    # ─────────────────────────────────────────────────────────────────────────
    
    def test_save_and_load(self, store: InMemoryCheckpointStore, sample_checkpoint: Checkpoint):
        """Test saving and loading a checkpoint."""
        store.save(sample_checkpoint)
        
        loaded = store.load(sample_checkpoint.id)
        
        assert loaded is not None
        assert loaded.id == sample_checkpoint.id
        assert loaded.execution_id == sample_checkpoint.execution_id
        assert loaded.step == sample_checkpoint.step
    
    def test_load_nonexistent(self, store: InMemoryCheckpointStore):
        """Test loading a nonexistent checkpoint."""
        loaded = store.load(CheckpointId("nonexistent"))
        assert loaded is None
    
    def test_save_creates_copy(self, store: InMemoryCheckpointStore, sample_checkpoint: Checkpoint):
        """Test that save creates a copy (no reference issues)."""
        store.save(sample_checkpoint)
        
        loaded = store.load(sample_checkpoint.id)
        
        # Should be equal but not the same object
        assert loaded is not sample_checkpoint
        assert loaded.state_values == sample_checkpoint.state_values
    
    def test_load_creates_copy(self, store: InMemoryCheckpointStore, sample_checkpoint: Checkpoint):
        """Test that load returns a copy."""
        store.save(sample_checkpoint)
        
        loaded1 = store.load(sample_checkpoint.id)
        loaded2 = store.load(sample_checkpoint.id)
        
        # Should be different objects
        assert loaded1 is not loaded2
        assert loaded1.state_values == loaded2.state_values
    
    def test_delete(self, store: InMemoryCheckpointStore, sample_checkpoint: Checkpoint):
        """Test deleting a checkpoint."""
        store.save(sample_checkpoint)
        
        result = store.delete(sample_checkpoint.id)
        assert result is True
        
        loaded = store.load(sample_checkpoint.id)
        assert loaded is None
    
    def test_delete_nonexistent(self, store: InMemoryCheckpointStore):
        """Test deleting a nonexistent checkpoint."""
        result = store.delete(CheckpointId("nonexistent"))
        assert result is False
    
    def test_exists_true(self, store: InMemoryCheckpointStore, sample_checkpoint: Checkpoint):
        """Test exists returns True for saved checkpoint."""
        store.save(sample_checkpoint)
        assert store.exists(sample_checkpoint.id) is True
    
    def test_exists_false(self, store: InMemoryCheckpointStore):
        """Test exists returns False for nonexistent checkpoint."""
        assert store.exists(CheckpointId("nonexistent")) is False
    
    # ─────────────────────────────────────────────────────────────────────────
    # Execution-based Operations
    # ─────────────────────────────────────────────────────────────────────────
    
    def test_load_by_execution(self, store: InMemoryCheckpointStore, sample_checkpoints: List[Checkpoint]):
        """Test loading all checkpoints for an execution."""
        for cp in sample_checkpoints:
            store.save(cp)
        
        loaded = store.load_by_execution("exec-001")
        
        assert len(loaded) == 3
        assert all(cp.execution_id == "exec-001" for cp in loaded)
    
    def test_load_by_execution_empty(self, store: InMemoryCheckpointStore):
        """Test loading from nonexistent execution."""
        loaded = store.load_by_execution("nonexistent")
        assert loaded == []
    
    def test_load_history(self, store: InMemoryCheckpointStore, sample_checkpoints: List[Checkpoint]):
        """Test loading as ExecutionHistory aggregate."""
        for cp in sample_checkpoints:
            store.save(cp)
        
        history = store.load_history("exec-001")
        
        assert isinstance(history, ExecutionHistory)
        assert history.execution_id == "exec-001"
        assert len(history) == 3
        
        # Verify domain logic is available
        assert history.get_completed_nodes() == ["node_a", "node_b"]
    
    def test_load_latest(self, store: InMemoryCheckpointStore, sample_checkpoints: List[Checkpoint]):
        """Test loading latest checkpoint."""
        for cp in sample_checkpoints:
            store.save(cp)
        
        latest = store.load_latest("exec-001")
        
        assert latest is not None
        assert latest.step == 2  # Highest step
        assert latest.id == CheckpointId("cp-003")
    
    def test_load_latest_empty(self, store: InMemoryCheckpointStore):
        """Test loading latest from empty execution."""
        latest = store.load_latest("nonexistent")
        assert latest is None
    
    def test_delete_by_execution(self, store: InMemoryCheckpointStore, sample_checkpoints: List[Checkpoint]):
        """Test deleting all checkpoints for an execution."""
        for cp in sample_checkpoints:
            store.save(cp)
        
        count = store.delete_by_execution("exec-001")
        
        assert count == 3
        assert store.load_by_execution("exec-001") == []
    
    def test_delete_by_execution_empty(self, store: InMemoryCheckpointStore):
        """Test deleting from nonexistent execution."""
        count = store.delete_by_execution("nonexistent")
        assert count == 0
    
    def test_count(self, store: InMemoryCheckpointStore, sample_checkpoints: List[Checkpoint]):
        """Test counting checkpoints for an execution."""
        for cp in sample_checkpoints:
            store.save(cp)
        
        assert store.count("exec-001") == 3
        assert store.count("nonexistent") == 0
    
    # ─────────────────────────────────────────────────────────────────────────
    # Multi-Execution Isolation
    # ─────────────────────────────────────────────────────────────────────────
    
    def test_execution_isolation(self, store: InMemoryCheckpointStore):
        """Test that different executions are isolated."""
        cp1 = Checkpoint(
            id=CheckpointId("cp-exec1-001"),
            execution_id="exec-001",
            step=0,
            node_writes={},
            next_nodes=[],
            state_values={"exec": 1},
            created_at=datetime.now(),
        )
        
        cp2 = Checkpoint(
            id=CheckpointId("cp-exec2-001"),
            execution_id="exec-002",
            step=0,
            node_writes={},
            next_nodes=[],
            state_values={"exec": 2},
            created_at=datetime.now(),
        )
        
        store.save(cp1)
        store.save(cp2)
        
        exec1_cps = store.load_by_execution("exec-001")
        exec2_cps = store.load_by_execution("exec-002")
        
        assert len(exec1_cps) == 1
        assert len(exec2_cps) == 1
        assert exec1_cps[0].state_values["exec"] == 1
        assert exec2_cps[0].state_values["exec"] == 2
    
    # ─────────────────────────────────────────────────────────────────────────
    # Thread Safety (Basic)
    # ─────────────────────────────────────────────────────────────────────────
    
    def test_concurrent_reads(self, store: InMemoryCheckpointStore, sample_checkpoint: Checkpoint):
        """Test concurrent reads don't interfere."""
        import threading
        
        store.save(sample_checkpoint)
        results = []
        errors = []
        
        def read_checkpoint():
            try:
                loaded = store.load(sample_checkpoint.id)
                if loaded:
                    results.append(loaded.id)
            except Exception as e:
                errors.append(e)
        
        threads = [threading.Thread(target=read_checkpoint) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0
        assert len(results) == 10
    
    # ─────────────────────────────────────────────────────────────────────────
    # Testing Utilities
    # ─────────────────────────────────────────────────────────────────────────
    
    def test_reset(self, store: InMemoryCheckpointStore, sample_checkpoints: List[Checkpoint]):
        """Test reset clears all data."""
        for cp in sample_checkpoints:
            store.save(cp)
        
        store.reset()
        
        assert store.get_total_checkpoint_count() == 0
        assert store.get_all_execution_ids() == []
    
    def test_get_all_execution_ids(self, store: InMemoryCheckpointStore):
        """Test getting all execution IDs."""
        cp1 = Checkpoint(
            id=CheckpointId("cp-1"),
            execution_id="exec-001",
            step=0,
            node_writes={},
            next_nodes=[],
            state_values={},
            created_at=datetime.now(),
        )
        
        cp2 = Checkpoint(
            id=CheckpointId("cp-2"),
            execution_id="exec-002",
            step=0,
            node_writes={},
            next_nodes=[],
            state_values={},
            created_at=datetime.now(),
        )
        
        store.save(cp1)
        store.save(cp2)
        
        exec_ids = store.get_all_execution_ids()
        assert set(exec_ids) == {"exec-001", "exec-002"}
    
    def test_get_total_checkpoint_count(self, store: InMemoryCheckpointStore, sample_checkpoints: List[Checkpoint]):
        """Test getting total checkpoint count."""
        for cp in sample_checkpoints:
            store.save(cp)
        
        assert store.get_total_checkpoint_count() == 3


# ═══════════════════════════════════════════════════════════════════════════════
# InMemoryCheckpointStoreFactory Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestInMemoryCheckpointStoreFactory:
    """Tests for InMemoryCheckpointStoreFactory."""
    
    def test_create_returns_store(self):
        """Test create returns a store instance."""
        factory = InMemoryCheckpointStoreFactory()
        store = factory.create()
        
        assert isinstance(store, InMemoryCheckpointStore)
    
    def test_create_returns_shared_instance(self):
        """Test create returns same instance."""
        factory = InMemoryCheckpointStoreFactory()
        store1 = factory.create()
        store2 = factory.create()
        
        assert store1 is store2
    
    def test_create_for_testing_returns_new_instance(self):
        """Test create_for_testing returns new instance each time."""
        factory = InMemoryCheckpointStoreFactory()
        store1 = factory.create_for_testing()
        store2 = factory.create_for_testing()
        
        assert store1 is not store2
    
    def test_create_isolated(self):
        """Test create_isolated returns independent stores."""
        factory = InMemoryCheckpointStoreFactory()
        store1 = factory.create_isolated()
        store2 = factory.create_isolated()
        
        # Save to store1
        cp = Checkpoint(
            id=CheckpointId("cp-1"),
            execution_id="exec-001",
            step=0,
            node_writes={},
            next_nodes=[],
            state_values={},
            created_at=datetime.now(),
        )
        store1.save(cp)
        
        # store2 should be empty
        assert store1.count("exec-001") == 1
        assert store2.count("exec-001") == 0


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraphCheckpointStore Basic Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestLangGraphCheckpointStoreBasic:
    """Basic tests for LangGraphCheckpointStore (without full LangGraph graph)."""
    
    def test_import(self):
        """Test that LangGraphCheckpointStore can be imported."""
        from wtb.infrastructure.stores.langgraph_checkpoint_store import (
            LangGraphCheckpointStore,
            LangGraphCheckpointConfig,
        )
        
        assert LangGraphCheckpointStore is not None
        assert LangGraphCheckpointConfig is not None
    
    def test_config_for_testing(self):
        """Test creating testing config."""
        from wtb.infrastructure.stores.langgraph_checkpoint_store import LangGraphCheckpointConfig
        
        config = LangGraphCheckpointConfig.for_testing()
        assert config.checkpointer_type == "memory"
    
    def test_config_for_development(self):
        """Test creating development config."""
        from wtb.infrastructure.stores.langgraph_checkpoint_store import LangGraphCheckpointConfig
        
        config = LangGraphCheckpointConfig.for_development("test.db")
        assert config.checkpointer_type == "sqlite"
        assert config.connection_string == "test.db"
    
    def test_config_for_production(self):
        """Test creating production config."""
        from wtb.infrastructure.stores.langgraph_checkpoint_store import LangGraphCheckpointConfig
        
        config = LangGraphCheckpointConfig.for_production("postgresql://test")
        assert config.checkpointer_type == "postgres"
        assert config.connection_string == "postgresql://test"
    
    def test_create_for_testing(self):
        """Test creating store for testing."""
        from wtb.infrastructure.stores.langgraph_checkpoint_store import LangGraphCheckpointStore
        
        store = LangGraphCheckpointStore.create_for_testing()
        assert store is not None
        assert store._checkpointer is not None
    
    def test_thread_id_conversion(self):
        """Test thread_id conversion."""
        from wtb.infrastructure.stores.langgraph_checkpoint_store import LangGraphCheckpointStore
        
        store = LangGraphCheckpointStore.create_for_testing()
        
        thread_id = store._to_thread_id("exec-001")
        assert thread_id == "wtb-exec-001"
        
        exec_id = store._from_thread_id("wtb-exec-001")
        assert exec_id == "exec-001"
    
    def test_get_config(self):
        """Test building LangGraph config."""
        from wtb.infrastructure.stores.langgraph_checkpoint_store import LangGraphCheckpointStore
        
        store = LangGraphCheckpointStore.create_for_testing()
        
        config = store._get_config("exec-001")
        assert config["configurable"]["thread_id"] == "wtb-exec-001"
        
        config_with_cp = store._get_config("exec-001", "cp-123")
        assert config_with_cp["configurable"]["checkpoint_id"] == "cp-123"


# ═══════════════════════════════════════════════════════════════════════════════
# Integration Pattern Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckpointStoreContract:
    """Tests that verify ICheckpointStore contract is satisfied."""
    
    @pytest.fixture
    def stores(self):
        """Return list of store implementations to test."""
        return [
            InMemoryCheckpointStore(),
            # Add LangGraphCheckpointStore when graph is available
        ]
    
    def test_all_stores_implement_interface(self, stores):
        """Test all stores implement ICheckpointStore methods."""
        from wtb.domain.interfaces.checkpoint_store import ICheckpointStore
        
        required_methods = [
            'save', 'load', 'load_by_execution', 'load_history',
            'load_latest', 'delete', 'delete_by_execution', 'exists', 'count'
        ]
        
        for store in stores:
            assert isinstance(store, ICheckpointStore)
            for method in required_methods:
                assert hasattr(store, method), f"{store.__class__.__name__} missing {method}"
