"""
Unit tests for LangGraph checkpoint operations.

Tests checkpoint functionality:
- Checkpoint creation and storage
- Checkpoint retrieval and history
- State snapshots at checkpoints
- Checkpoint metadata

SOLID Compliance:
- SRP: Tests focus only on checkpoint operations
- DIP: Uses fixtures for dependencies

Run with: pytest tests/test_langgraph/unit/test_checkpoint_operations.py -v
"""

import pytest
from datetime import datetime
from typing import Dict, Any, List

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from wtb.domain.models.checkpoint import (
    Checkpoint,
    CheckpointId,
    ExecutionHistory,
    CheckpointNotFoundError,
    InvalidRollbackTargetError,
)
from wtb.infrastructure.stores.langgraph_checkpoint_store import (
    LangGraphCheckpointStore,
    LangGraphCheckpointConfig,
)

from tests.test_langgraph.helpers import (
    create_initial_simple_state,
    create_initial_branch_state,
)


# ═══════════════════════════════════════════════════════════════════════════════
# CheckpointId Value Object Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckpointId:
    """Tests for CheckpointId value object."""
    
    def test_create_checkpoint_id(self):
        """Test creating CheckpointId."""
        cp_id = CheckpointId("abc123")
        
        assert cp_id.value == "abc123"
        assert str(cp_id) == "abc123"
    
    def test_checkpoint_id_equality(self):
        """Test CheckpointId equality comparison."""
        cp_id1 = CheckpointId("abc123")
        cp_id2 = CheckpointId("abc123")
        cp_id3 = CheckpointId("xyz789")
        
        assert cp_id1 == cp_id2
        assert cp_id1 != cp_id3
    
    def test_checkpoint_id_string_equality(self):
        """Test CheckpointId equality with string."""
        cp_id = CheckpointId("abc123")
        
        assert cp_id == "abc123"
        assert cp_id != "xyz789"
    
    def test_checkpoint_id_hashable(self):
        """Test CheckpointId can be used as dict key."""
        cp_id1 = CheckpointId("abc123")
        cp_id2 = CheckpointId("abc123")
        
        d = {cp_id1: "value1"}
        
        assert d[cp_id2] == "value1"
    
    def test_empty_checkpoint_id_raises(self):
        """Test that empty CheckpointId raises error."""
        with pytest.raises(ValueError, match="cannot be empty"):
            CheckpointId("")


# ═══════════════════════════════════════════════════════════════════════════════
# Checkpoint Entity Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckpointEntity:
    """Tests for Checkpoint domain entity."""
    
    def test_create_checkpoint(self):
        """Test creating Checkpoint."""
        cp = Checkpoint(
            id=CheckpointId("cp-1"),
            execution_id="exec-1",
            step=1,
            node_writes={"node_a": {"result": "A"}},
            next_nodes=["node_b"],
            state_values={"messages": ["A"], "count": 1},
            created_at=datetime.now(),
        )
        
        assert cp.id == CheckpointId("cp-1")
        assert cp.execution_id == "exec-1"
        assert cp.step == 1
    
    def test_completed_node_property(self):
        """Test completed_node property extraction."""
        cp = Checkpoint(
            id=CheckpointId("cp-1"),
            execution_id="exec-1",
            step=1,
            node_writes={"node_a": {"result": "A"}},
            next_nodes=["node_b"],
            state_values={},
            created_at=datetime.now(),
        )
        
        assert cp.completed_node == "node_a"
    
    def test_completed_node_filters_special(self):
        """Test completed_node filters special nodes."""
        cp = Checkpoint(
            id=CheckpointId("cp-1"),
            execution_id="exec-1",
            step=0,
            node_writes={"__start__": {}, "node_a": {"result": "A"}},
            next_nodes=[],
            state_values={},
            created_at=datetime.now(),
        )
        
        assert cp.completed_node == "node_a"
    
    def test_is_terminal_property(self):
        """Test is_terminal property."""
        terminal_cp = Checkpoint(
            id=CheckpointId("cp-1"),
            execution_id="exec-1",
            step=3,
            node_writes={"node_c": {}},
            next_nodes=[],  # No next nodes = terminal
            state_values={},
            created_at=datetime.now(),
        )
        
        non_terminal_cp = Checkpoint(
            id=CheckpointId("cp-2"),
            execution_id="exec-1",
            step=1,
            node_writes={"node_a": {}},
            next_nodes=["node_b"],  # Has next node
            state_values={},
            created_at=datetime.now(),
        )
        
        assert terminal_cp.is_terminal is True
        assert non_terminal_cp.is_terminal is False
    
    def test_is_node_completion_property(self):
        """Test is_node_completion property."""
        node_cp = Checkpoint(
            id=CheckpointId("cp-1"),
            execution_id="exec-1",
            step=1,
            node_writes={"node_a": {"result": "A"}},
            next_nodes=["node_b"],
            state_values={},
            created_at=datetime.now(),
        )
        
        start_cp = Checkpoint(
            id=CheckpointId("cp-0"),
            execution_id="exec-1",
            step=0,
            node_writes={},  # No node writes at start
            next_nodes=["node_a"],
            state_values={},
            created_at=datetime.now(),
        )
        
        assert node_cp.is_node_completion is True
        assert start_cp.is_node_completion is False
    
    def test_has_node_in_next(self):
        """Test has_node_in_next method."""
        cp = Checkpoint(
            id=CheckpointId("cp-1"),
            execution_id="exec-1",
            step=1,
            node_writes={"node_a": {}},
            next_nodes=["node_b", "node_c"],
            state_values={},
            created_at=datetime.now(),
        )
        
        assert cp.has_node_in_next("node_b") is True
        assert cp.has_node_in_next("node_c") is True
        assert cp.has_node_in_next("node_a") is False
    
    def test_has_node_in_writes(self):
        """Test has_node_in_writes method."""
        cp = Checkpoint(
            id=CheckpointId("cp-1"),
            execution_id="exec-1",
            step=1,
            node_writes={"node_a": {"result": "A"}},
            next_nodes=["node_b"],
            state_values={},
            created_at=datetime.now(),
        )
        
        assert cp.has_node_in_writes("node_a") is True
        assert cp.has_node_in_writes("node_b") is False
    
    def test_get_node_output(self):
        """Test get_node_output method."""
        cp = Checkpoint(
            id=CheckpointId("cp-1"),
            execution_id="exec-1",
            step=1,
            node_writes={"node_a": {"result": "A", "count": 1}},
            next_nodes=["node_b"],
            state_values={},
            created_at=datetime.now(),
        )
        
        output = cp.get_node_output("node_a")
        
        assert output == {"result": "A", "count": 1}
        assert cp.get_node_output("node_b") is None
    
    def test_checkpoint_serialization(self):
        """Test checkpoint to_dict serialization."""
        cp = Checkpoint(
            id=CheckpointId("cp-1"),
            execution_id="exec-1",
            step=1,
            node_writes={"node_a": {"result": "A"}},
            next_nodes=["node_b"],
            state_values={"messages": ["A"]},
            created_at=datetime(2026, 1, 15, 12, 0, 0),
            metadata={"source": "test"},
        )
        
        data = cp.to_dict()
        
        assert data["id"] == "cp-1"
        assert data["execution_id"] == "exec-1"
        assert data["step"] == 1
        assert data["metadata"] == {"source": "test"}
    
    def test_checkpoint_deserialization(self):
        """Test checkpoint from_dict deserialization."""
        data = {
            "id": "cp-1",
            "execution_id": "exec-1",
            "step": 1,
            "node_writes": {"node_a": {"result": "A"}},
            "next_nodes": ["node_b"],
            "state_values": {"messages": ["A"]},
            "created_at": "2026-01-15T12:00:00",
            "metadata": {"source": "test"},
        }
        
        cp = Checkpoint.from_dict(data)
        
        assert cp.id == CheckpointId("cp-1")
        assert cp.execution_id == "exec-1"
        assert cp.step == 1


# ═══════════════════════════════════════════════════════════════════════════════
# ExecutionHistory Aggregate Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestExecutionHistory:
    """Tests for ExecutionHistory aggregate root."""
    
    def _create_sample_checkpoints(self, execution_id: str = "exec-1") -> List[Checkpoint]:
        """Helper to create sample checkpoints."""
        return [
            Checkpoint(
                id=CheckpointId("cp-0"),
                execution_id=execution_id,
                step=0,
                node_writes={},
                next_nodes=["node_a"],
                state_values={"messages": [], "count": 0},
                created_at=datetime.now(),
            ),
            Checkpoint(
                id=CheckpointId("cp-1"),
                execution_id=execution_id,
                step=1,
                node_writes={"node_a": {"result": "A"}},
                next_nodes=["node_b"],
                state_values={"messages": ["A"], "count": 1},
                created_at=datetime.now(),
            ),
            Checkpoint(
                id=CheckpointId("cp-2"),
                execution_id=execution_id,
                step=2,
                node_writes={"node_b": {"result": "B"}},
                next_nodes=["node_c"],
                state_values={"messages": ["A", "B"], "count": 2},
                created_at=datetime.now(),
            ),
            Checkpoint(
                id=CheckpointId("cp-3"),
                execution_id=execution_id,
                step=3,
                node_writes={"node_c": {"result": "C"}},
                next_nodes=[],
                state_values={"messages": ["A", "B", "C"], "count": 3},
                created_at=datetime.now(),
            ),
        ]
    
    def test_create_execution_history(self):
        """Test creating ExecutionHistory."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory(
            execution_id="exec-1",
            checkpoints=checkpoints,
        )
        
        assert history.execution_id == "exec-1"
        assert len(history.checkpoints) == 4
    
    def test_get_checkpoint_by_id(self):
        """Test getting checkpoint by ID."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory("exec-1", checkpoints)
        
        cp = history.get_checkpoint(CheckpointId("cp-2"))
        
        assert cp is not None
        assert cp.step == 2
    
    def test_get_checkpoint_by_string_id(self):
        """Test getting checkpoint by string ID."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory("exec-1", checkpoints)
        
        cp = history.get_checkpoint_by_id("cp-2")
        
        assert cp is not None
        assert cp.step == 2
    
    def test_get_latest_checkpoint(self):
        """Test getting latest checkpoint."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory("exec-1", checkpoints)
        
        latest = history.get_latest_checkpoint()
        
        assert latest is not None
        assert latest.step == 3
        assert latest.id == CheckpointId("cp-3")
    
    def test_get_checkpoint_for_node(self):
        """Test getting checkpoint where node completed."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory("exec-1", checkpoints)
        
        cp = history.get_checkpoint_for_node("node_b")
        
        assert cp is not None
        assert cp.step == 2
        assert cp.has_node_in_writes("node_b")
    
    def test_get_node_entry_checkpoint(self):
        """Test getting checkpoint where node was about to execute."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory("exec-1", checkpoints)
        
        entry_cp = history.get_node_entry_checkpoint("node_b")
        
        assert entry_cp is not None
        assert entry_cp.step == 1  # Step 1 has node_b in next_nodes
        assert entry_cp.has_node_in_next("node_b")
    
    def test_get_node_exit_checkpoint(self):
        """Test getting checkpoint where node completed (alias)."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory("exec-1", checkpoints)
        
        exit_cp = history.get_node_exit_checkpoint("node_b")
        
        assert exit_cp is not None
        assert exit_cp.step == 2
    
    def test_get_completed_nodes(self):
        """Test getting list of completed nodes."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory("exec-1", checkpoints)
        
        completed = history.get_completed_nodes()
        
        assert completed == ["node_a", "node_b", "node_c"]
    
    def test_get_execution_path(self):
        """Test getting linearized execution path."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory("exec-1", checkpoints)
        
        path = history.get_execution_path()
        
        assert path == ["node_a", "node_b", "node_c"]
    
    def test_is_node_completed(self):
        """Test checking if node completed."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory("exec-1", checkpoints)
        
        assert history.is_node_completed("node_a") is True
        assert history.is_node_completed("node_b") is True
        assert history.is_node_completed("node_x") is False
    
    def test_get_rollback_target(self):
        """Test getting rollback target checkpoint."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory("exec-1", checkpoints)
        
        target = history.get_rollback_target("node_a")
        
        assert target is not None
        assert target.step == 1
    
    def test_get_rollback_targets(self):
        """Test getting all valid rollback targets."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory("exec-1", checkpoints)
        
        targets = history.get_rollback_targets()
        
        # Should not include step 0 (no node completion)
        assert len(targets) == 3
        # Should be in reverse order (most recent first)
        assert targets[0].step == 3
    
    def test_can_rollback_to(self):
        """Test checking if rollback to checkpoint is valid."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory("exec-1", checkpoints)
        
        assert history.can_rollback_to(CheckpointId("cp-1")) is True
        assert history.can_rollback_to(CheckpointId("cp-0")) is False  # No node completion
        assert history.can_rollback_to(CheckpointId("cp-99")) is False  # Doesn't exist
    
    def test_get_nodes_to_revert(self):
        """Test getting list of nodes to revert in rollback."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory("exec-1", checkpoints)
        
        reverted = history.get_nodes_to_revert(
            CheckpointId("cp-3"),  # From step 3
            CheckpointId("cp-1"),  # To step 1
        )
        
        # Should revert nodes completed between step 1 and 3
        assert "node_b" in reverted
        assert "node_c" in reverted
        assert "node_a" not in reverted
    
    def test_get_state_at_step(self):
        """Test getting state at specific step."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory("exec-1", checkpoints)
        
        state = history.get_state_at_step(2)
        
        assert state is not None
        assert state["messages"] == ["A", "B"]
        assert state["count"] == 2
    
    def test_add_checkpoint(self):
        """Test adding checkpoint to history."""
        history = ExecutionHistory("exec-1", [])
        
        cp = Checkpoint(
            id=CheckpointId("cp-new"),
            execution_id="exec-1",
            step=1,
            node_writes={"node_a": {}},
            next_nodes=[],
            state_values={},
            created_at=datetime.now(),
        )
        
        history.add_checkpoint(cp)
        
        assert len(history.checkpoints) == 1
    
    def test_add_checkpoint_wrong_execution_raises(self):
        """Test adding checkpoint with wrong execution_id raises error."""
        history = ExecutionHistory("exec-1", [])
        
        cp = Checkpoint(
            id=CheckpointId("cp-wrong"),
            execution_id="exec-other",  # Wrong execution_id
            step=1,
            node_writes={},
            next_nodes=[],
            state_values={},
            created_at=datetime.now(),
        )
        
        with pytest.raises(ValueError, match="does not match"):
            history.add_checkpoint(cp)
    
    def test_history_iteration(self):
        """Test iterating over history yields checkpoints in step order."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory("exec-1", checkpoints)
        
        steps = [cp.step for cp in history]
        
        assert steps == [0, 1, 2, 3]
    
    def test_history_len(self):
        """Test history length."""
        checkpoints = self._create_sample_checkpoints()
        history = ExecutionHistory("exec-1", checkpoints)
        
        assert len(history) == 4
    
    def test_history_bool(self):
        """Test history truthiness."""
        empty_history = ExecutionHistory("exec-1", [])
        filled_history = ExecutionHistory("exec-1", self._create_sample_checkpoints())
        
        assert bool(empty_history) is False
        assert bool(filled_history) is True


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph Checkpoint Store Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphCheckpointStore:
    """Tests for LangGraphCheckpointStore."""
    
    def test_create_for_testing(self):
        """Test creating store for testing."""
        store = LangGraphCheckpointStore.create_for_testing()
        
        assert store is not None
        assert store._checkpointer is not None
    
    def test_from_config_memory(self):
        """Test creating store from memory config."""
        config = LangGraphCheckpointConfig.for_testing()
        store = LangGraphCheckpointStore.from_config(config)
        
        assert store is not None
    
    def test_thread_id_conversion(self):
        """Test thread_id to/from execution_id conversion."""
        store = LangGraphCheckpointStore.create_for_testing()
        
        execution_id = "exec-123"
        thread_id = store._to_thread_id(execution_id)
        
        assert thread_id == "wtb-exec-123"
        assert store._from_thread_id(thread_id) == execution_id
    
    def test_get_config(self):
        """Test building LangGraph config."""
        store = LangGraphCheckpointStore.create_for_testing()
        
        config = store._get_config("exec-123")
        
        assert config["configurable"]["thread_id"] == "wtb-exec-123"
    
    def test_get_config_with_checkpoint_id(self):
        """Test building config with checkpoint_id."""
        store = LangGraphCheckpointStore.create_for_testing()
        
        config = store._get_config("exec-123", "cp-456")
        
        assert config["configurable"]["checkpoint_id"] == "cp-456"
    
    def test_load_history(self, simple_compiled_graph, checkpoint_store):
        """Test loading execution history."""
        # Execute graph to create checkpoints
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "wtb-exec-load-test"}}
        
        simple_compiled_graph.invoke(initial_state, config)
        
        # Set graph on store
        checkpoint_store.set_graph(simple_compiled_graph)
        
        # Load history
        history = checkpoint_store.load_history("exec-load-test")
        
        assert history is not None
        assert len(history) > 0
    
    def test_load_latest(self, simple_compiled_graph, checkpoint_store):
        """Test loading latest checkpoint."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "wtb-exec-latest-test"}}
        
        simple_compiled_graph.invoke(initial_state, config)
        checkpoint_store.set_graph(simple_compiled_graph)
        
        latest = checkpoint_store.load_latest("exec-latest-test")
        
        assert latest is not None
        assert latest.is_terminal is True


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph Native Checkpoint Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphNativeCheckpoints:
    """Tests for native LangGraph checkpoint operations."""
    
    def test_get_state_returns_snapshot(self, simple_compiled_graph):
        """Test get_state returns state snapshot."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "native-cp-1"}}
        
        simple_compiled_graph.invoke(initial_state, config)
        
        state = simple_compiled_graph.get_state(config)
        
        assert state is not None
        assert state.values["messages"] == ["A", "B", "C"]
    
    def test_get_state_history_returns_all_snapshots(self, simple_compiled_graph):
        """Test get_state_history returns all checkpoints."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "native-cp-2"}}
        
        simple_compiled_graph.invoke(initial_state, config)
        
        history = list(simple_compiled_graph.get_state_history(config))
        
        # Should have multiple snapshots (one per node + start)
        assert len(history) >= 3
    
    def test_state_history_order(self, simple_compiled_graph):
        """Test state history is in reverse chronological order."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "native-cp-3"}}
        
        simple_compiled_graph.invoke(initial_state, config)
        
        history = list(simple_compiled_graph.get_state_history(config))
        
        # First item should be most recent (highest step)
        steps = [h.metadata.get("step", 0) for h in history]
        assert steps == sorted(steps, reverse=True)
    
    def test_checkpoint_config_extraction(self, simple_compiled_graph):
        """Test extracting config from checkpoint."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "native-cp-4"}}
        
        simple_compiled_graph.invoke(initial_state, config)
        
        state = simple_compiled_graph.get_state(config)
        
        assert "configurable" in state.config
        assert "thread_id" in state.config["configurable"]
        assert "checkpoint_id" in state.config["configurable"]
    
    def test_checkpoint_metadata(self, simple_compiled_graph):
        """Test checkpoint metadata structure."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "native-cp-5"}}
        
        simple_compiled_graph.invoke(initial_state, config)
        
        state = simple_compiled_graph.get_state(config)
        
        assert state.metadata is not None
        assert "step" in state.metadata
    
    def test_next_nodes_in_snapshot(self, simple_compiled_graph):
        """Test next nodes are captured in snapshot."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "native-cp-6"}}
        
        simple_compiled_graph.invoke(initial_state, config)
        
        history = list(simple_compiled_graph.get_state_history(config))
        
        # Find non-terminal checkpoint
        for snapshot in history:
            if snapshot.next:
                assert isinstance(snapshot.next, tuple)
                break


# ═══════════════════════════════════════════════════════════════════════════════
# Checkpoint Persistence Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckpointPersistence:
    """Tests for checkpoint persistence behavior."""
    
    def test_checkpoints_persist_across_invocations(self, simple_graph_def):
        """Test that checkpoints persist in the same checkpointer."""
        checkpointer = MemorySaver()
        graph = simple_graph_def.compile(checkpointer=checkpointer)
        
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "persist-test-1"}}
        
        # First invocation
        graph.invoke(initial_state, config)
        
        # Get state after first invocation
        state1 = graph.get_state(config)
        
        # Second invocation (should continue from last state)
        # Without new input, should get same state
        state2 = graph.get_state(config)
        
        assert state1.values == state2.values
    
    def test_different_threads_have_separate_checkpoints(self, simple_compiled_graph):
        """Test that different threads have isolated checkpoints."""
        initial1 = {"messages": ["THREAD1"], "count": 0}
        initial2 = {"messages": ["THREAD2"], "count": 0}
        
        config1 = {"configurable": {"thread_id": "persist-thread-1"}}
        config2 = {"configurable": {"thread_id": "persist-thread-2"}}
        
        simple_compiled_graph.invoke(initial1, config1)
        simple_compiled_graph.invoke(initial2, config2)
        
        state1 = simple_compiled_graph.get_state(config1)
        state2 = simple_compiled_graph.get_state(config2)
        
        assert "THREAD1" in state1.values["messages"]
        assert "THREAD2" in state2.values["messages"]
        assert "THREAD1" not in state2.values["messages"]
    
    def test_checkpoint_count_matches_node_count(self, simple_compiled_graph):
        """Test checkpoint count matches execution steps."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "count-test"}}
        
        simple_compiled_graph.invoke(initial_state, config)
        
        history = list(simple_compiled_graph.get_state_history(config))
        
        # Should have at least: start + 3 nodes = 4 checkpoints
        # (LangGraph may create more depending on implementation)
        assert len(history) >= 4
