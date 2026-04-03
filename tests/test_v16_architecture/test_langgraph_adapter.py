"""
Tests for LangGraphStateAdapter - String ID compliance (v1.6).

Uses wtb.testing fixtures for consistent testing patterns.
"""
import pytest
import tempfile
import os

from wtb.domain.models.workflow import ExecutionState
from wtb.domain.interfaces.state_adapter import CheckpointTrigger, CheckpointInfo
from wtb.testing import create_minimal_graph, create_conditional_graph, StateAdapterTestMixin


# Skip all tests if langgraph not available
pytest.importorskip("langgraph")


# ════════════════════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def temp_db_path():
    """Create a temporary SQLite database path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.unlink(path)
    except (OSError, PermissionError):
        pass


@pytest.fixture
def adapter_memory():
    """Create LangGraphStateAdapter with in-memory checkpointer."""
    from wtb.infrastructure.adapters.langgraph_state_adapter import (
        LangGraphStateAdapter,
        LangGraphConfig,
    )
    config = LangGraphConfig.for_testing()
    adapter = LangGraphStateAdapter(config)
    adapter.set_workflow_graph(create_minimal_graph())
    return adapter


@pytest.fixture
def adapter_sqlite(temp_db_path):
    """Create LangGraphStateAdapter with SQLite checkpointer."""
    from wtb.infrastructure.adapters.langgraph_state_adapter import (
        LangGraphStateAdapter,
        LangGraphConfig,
        CheckpointerType,
    )
    config = LangGraphConfig(
        checkpointer_type=CheckpointerType.SQLITE,
        connection_string=temp_db_path,
    )
    adapter = LangGraphStateAdapter(config)
    adapter.set_workflow_graph(create_minimal_graph())
    return adapter


# ════════════════════════════════════════════════════════════════════════════════
# String ID Tests - Memory Checkpointer
# ════════════════════════════════════════════════════════════════════════════════

class TestLangGraphMemoryStringIDs:
    """Tests for string ID compliance with in-memory checkpointer."""
    
    def test_initialize_session_returns_string(self, adapter_memory):
        """Session ID should be string."""
        session_id = adapter_memory.initialize_session("test-exec-1", ExecutionState())
        
        assert isinstance(session_id, str)
        assert session_id == "wtb-test-exec-1"
    
    def test_save_checkpoint_returns_string(self, adapter_memory):
        """Checkpoint ID should be string."""
        adapter_memory.initialize_session("test-exec-2", ExecutionState())
        
        checkpoint_id = adapter_memory.save_checkpoint(
            state=ExecutionState(),
            node_id="node_a",
            trigger=CheckpointTrigger.NODE_START,
        )
        
        assert isinstance(checkpoint_id, str)
        assert len(checkpoint_id) > 0
    
    def test_get_checkpoints_returns_checkpoint_info(self, adapter_memory):
        """get_checkpoints should return list of CheckpointInfo."""
        session_id = adapter_memory.initialize_session("test-exec-3", ExecutionState())
        
        adapter_memory.save_checkpoint(
            state=ExecutionState(),
            node_id="node_a",
            trigger=CheckpointTrigger.NODE_START,
        )
        
        checkpoints = adapter_memory.get_checkpoints(session_id)
        
        assert len(checkpoints) >= 1
        for cp in checkpoints:
            assert isinstance(cp, CheckpointInfo)
            assert isinstance(cp.id, str)
            assert isinstance(cp.step, int)
    
    def test_mark_node_started_returns_string(self, adapter_memory):
        """Boundary ID should be string."""
        adapter_memory.initialize_session("test-exec-4", ExecutionState())
        
        entry_cp = adapter_memory.save_checkpoint(
            state=ExecutionState(),
            node_id="node_a",
            trigger=CheckpointTrigger.NODE_START,
        )
        
        boundary_id = adapter_memory.mark_node_started(
            node_id="node_a",
            entry_checkpoint_id=entry_cp,
        )
        
        assert isinstance(boundary_id, str)
    
    def test_mark_node_completed_accepts_string(self, adapter_memory):
        """mark_node_completed should accept string IDs."""
        adapter_memory.initialize_session("test-exec-5", ExecutionState())
        
        entry_cp = adapter_memory.save_checkpoint(
            state=ExecutionState(),
            node_id="node_a",
            trigger=CheckpointTrigger.NODE_START,
        )
        
        boundary_id = adapter_memory.mark_node_started(
            node_id="node_a",
            entry_checkpoint_id=entry_cp,
        )
        
        exit_cp = adapter_memory.save_checkpoint(
            state=ExecutionState(),
            node_id="node_a",
            trigger=CheckpointTrigger.NODE_END,
        )
        
        # Should not raise
        adapter_memory.mark_node_completed(
            node_id="node_a",
            exit_checkpoint_id=exit_cp,
        )


# ════════════════════════════════════════════════════════════════════════════════
# String ID Tests - SQLite Checkpointer
# ════════════════════════════════════════════════════════════════════════════════

class TestLangGraphSQLiteStringIDs:
    """Tests for string ID compliance with SQLite checkpointer."""
    
    def test_initialize_session_returns_string(self, adapter_sqlite):
        """Session ID should be string."""
        session_id = adapter_sqlite.initialize_session("sqlite-test-1", ExecutionState())
        
        assert isinstance(session_id, str)
        assert session_id == "wtb-sqlite-test-1"
    
    def test_save_checkpoint_returns_string(self, adapter_sqlite):
        """Checkpoint ID should be string."""
        adapter_sqlite.initialize_session("sqlite-test-2", ExecutionState())
        
        checkpoint_id = adapter_sqlite.save_checkpoint(
            state=ExecutionState(),
            node_id="node_a",
            trigger=CheckpointTrigger.NODE_START,
        )
        
        assert isinstance(checkpoint_id, str)
        assert len(checkpoint_id) > 0
    
    def test_rollback_accepts_string(self, adapter_sqlite):
        """rollback should accept string checkpoint_id."""
        adapter_sqlite.initialize_session("rollback-test", ExecutionState())
        
        cp1 = adapter_sqlite.save_checkpoint(
            state=ExecutionState(),
            node_id="node_a",
            trigger=CheckpointTrigger.NODE_START,
        )
        
        adapter_sqlite.save_checkpoint(
            state=ExecutionState(),
            node_id="node_a",
            trigger=CheckpointTrigger.NODE_END,
        )
        
        # Rollback using string ID
        result = adapter_sqlite.rollback(cp1)
        
        assert result is not None  # Returns ExecutionState


# ════════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ════════════════════════════════════════════════════════════════════════════════

class TestLangGraphACIDCompliance:
    """Tests for ACID properties in LangGraph adapter."""
    
    def test_consistency_all_ids_are_strings(self, adapter_sqlite):
        """All IDs should be strings throughout the workflow."""
        session_id = adapter_sqlite.initialize_session("acid-test", ExecutionState())
        
        cp1 = adapter_sqlite.save_checkpoint(
            state=ExecutionState(),
            node_id="node_a",
            trigger=CheckpointTrigger.NODE_START,
        )
        
        boundary_id = adapter_sqlite.mark_node_started("node_a", cp1)
        
        cp2 = adapter_sqlite.save_checkpoint(
            state=ExecutionState(),
            node_id="node_a",
            trigger=CheckpointTrigger.NODE_END,
        )
        
        adapter_sqlite.mark_node_completed("node_a", cp2)
        
        # Verify all IDs are strings
        assert isinstance(session_id, str)
        assert isinstance(cp1, str)
        assert isinstance(cp2, str)
        assert isinstance(boundary_id, str)
        
        # Verify retrieved data also uses strings
        checkpoints = adapter_sqlite.get_checkpoints(session_id)
        for cp in checkpoints:
            assert isinstance(cp.id, str)
        
        boundaries = adapter_sqlite.get_node_boundaries(session_id)
        for b in boundaries:
            assert isinstance(b.id, str)
            assert isinstance(b.entry_checkpoint_id, str)
    
    def test_isolation_separate_sessions(self, adapter_sqlite):
        """Separate sessions should be isolated."""
        # Session 1
        session1 = adapter_sqlite.initialize_session("iso-exec-1", ExecutionState())
        adapter_sqlite.save_checkpoint(
            state=ExecutionState(),
            node_id="node_a",
            trigger=CheckpointTrigger.NODE_START,
        )
        
        # Session 2
        session2 = adapter_sqlite.initialize_session("iso-exec-2", ExecutionState())
        adapter_sqlite.save_checkpoint(
            state=ExecutionState(),
            node_id="node_b",
            trigger=CheckpointTrigger.NODE_START,
        )
        
        assert session1 != session2
        
        # Checkpoints should be isolated
        cp1 = adapter_sqlite.get_checkpoints(session1)
        cp2 = adapter_sqlite.get_checkpoints(session2)
        
        # They should not share checkpoints
        cp1_ids = {cp.id for cp in cp1}
        cp2_ids = {cp.id for cp in cp2}
        assert cp1_ids.isdisjoint(cp2_ids), "Sessions should have separate checkpoints"
    
    def test_durability_persists_across_instances(self, temp_db_path):
        """Data should persist across adapter instances (SQLite only)."""
        from wtb.infrastructure.adapters.langgraph_state_adapter import (
            LangGraphStateAdapter,
            LangGraphConfig,
            CheckpointerType,
        )
        
        config = LangGraphConfig(
            checkpointer_type=CheckpointerType.SQLITE,
            connection_string=temp_db_path,
        )
        
        # First adapter
        adapter1 = LangGraphStateAdapter(config)
        adapter1.set_workflow_graph(create_minimal_graph())
        session_id = adapter1.initialize_session("durable-test", ExecutionState())
        checkpoint_id = adapter1.save_checkpoint(
            state=ExecutionState(),
            node_id="node_a",
            trigger=CheckpointTrigger.NODE_START,
        )
        
        del adapter1  # Force cleanup
        
        # Second adapter
        adapter2 = LangGraphStateAdapter(config)
        adapter2.set_workflow_graph(create_minimal_graph())
        adapter2.set_current_session(session_id, execution_id="durable-test")
        
        # Should be able to retrieve checkpoint
        checkpoints = adapter2.get_checkpoints(session_id)
        checkpoint_ids = [cp.id for cp in checkpoints]
        
        assert checkpoint_id in checkpoint_ids, "Checkpoint should persist across instances"


# ════════════════════════════════════════════════════════════════════════════════
# Conditional Edge Tests
# ════════════════════════════════════════════════════════════════════════════════

class TestLangGraphConditionalEdges:
    """Tests for conditional edge (branching) support."""
    
    @pytest.fixture
    def adapter_conditional(self):
        """Create adapter with conditional graph."""
        from wtb.infrastructure.adapters.langgraph_state_adapter import (
            LangGraphStateAdapter,
            LangGraphConfig,
        )
        config = LangGraphConfig.for_testing()
        adapter = LangGraphStateAdapter(config)
        adapter.set_workflow_graph(create_conditional_graph())
        return adapter
    
    def test_conditional_graph_compiles(self, adapter_conditional):
        """Conditional graph should compile without errors."""
        assert adapter_conditional.get_compiled_graph() is not None
    
    def test_conditional_route_to_default(self, adapter_conditional):
        """Without route flag, should go to node_b."""
        session_id = adapter_conditional.initialize_session("cond-test-1", ExecutionState())
        
        # Execute graph (no route specified -> goes to node_b)
        graph = adapter_conditional.get_compiled_graph()
        config = {"configurable": {"thread_id": session_id}}
        
        result = graph.invoke(
            {"value": 0, "messages": [], "route": None},
            config=config
        )
        
        assert "a_executed" in result["messages"]
        assert "b_executed" in result["messages"]
        assert "c_executed" not in result["messages"]
    
    def test_conditional_route_to_branch(self, adapter_conditional):
        """With route='c', should go to node_c."""
        session_id = adapter_conditional.initialize_session("cond-test-2", ExecutionState())
        
        graph = adapter_conditional.get_compiled_graph()
        config = {"configurable": {"thread_id": session_id}}
        
        result = graph.invoke(
            {"value": 0, "messages": [], "route": "c"},
            config=config
        )
        
        assert "a_executed" in result["messages"]
        assert "c_executed" in result["messages"]
        assert "b_executed" not in result["messages"]
        # node_c adds 10, node_a adds 1
        assert result["value"] == 11
    
    def test_conditional_checkpoints_track_branch(self, adapter_conditional):
        """Checkpoints should correctly track which branch was taken."""
        session_id = adapter_conditional.initialize_session("cond-test-3", ExecutionState())
        
        graph = adapter_conditional.get_compiled_graph()
        config = {"configurable": {"thread_id": session_id}}
        
        # Take the branch to node_c
        graph.invoke(
            {"value": 0, "messages": [], "route": "c"},
            config=config
        )
        
        # Get checkpoints - should have entries for node_a and node_c
        checkpoints = adapter_conditional.get_checkpoints(session_id)
        
        assert len(checkpoints) >= 2, "Should have at least 2 checkpoints"
        # All checkpoint IDs should be strings
        for cp in checkpoints:
            assert isinstance(cp.id, str)
    
    def test_conditional_rollback_before_branch(self, adapter_conditional):
        """Should be able to rollback to before the branch point."""
        session_id = adapter_conditional.initialize_session("cond-test-4", ExecutionState())
        
        graph = adapter_conditional.get_compiled_graph()
        config = {"configurable": {"thread_id": session_id}}
        
        # First run: take branch to node_c
        graph.invoke(
            {"value": 0, "messages": [], "route": "c"},
            config=config
        )
        
        # Get checkpoint history
        checkpoints = adapter_conditional.get_checkpoints(session_id)
        
        if len(checkpoints) >= 2:
            # Rollback to first checkpoint (before branch)
            first_cp = checkpoints[-1]  # Oldest checkpoint
            
            rolled_back = adapter_conditional.rollback(first_cp.id)
            
            assert rolled_back is not None
            # Value should be reset (depends on which checkpoint we got)
            assert isinstance(rolled_back.workflow_variables, dict)
    
    def test_conditional_different_paths_isolated(self, adapter_conditional):
        """Different executions can take different paths independently."""
        # First execution: take path B
        session1 = adapter_conditional.initialize_session("iso-path-1", ExecutionState())
        graph = adapter_conditional.get_compiled_graph()
        
        result1 = graph.invoke(
            {"value": 0, "messages": [], "route": None},
            config={"configurable": {"thread_id": session1}}
        )
        
        # Second execution: take path C
        session2 = adapter_conditional.initialize_session("iso-path-2", ExecutionState())
        
        result2 = graph.invoke(
            {"value": 0, "messages": [], "route": "c"},
            config={"configurable": {"thread_id": session2}}
        )
        
        # Verify isolation - different paths, different results
        assert "b_executed" in result1["messages"]
        assert "c_executed" in result2["messages"]
        assert result1["value"] == 2  # node_a(+1) + node_b(+1)
        assert result2["value"] == 11  # node_a(+1) + node_c(+10)
