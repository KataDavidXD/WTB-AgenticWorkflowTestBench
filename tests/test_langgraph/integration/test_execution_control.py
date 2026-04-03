"""
Integration tests for LangGraph advanced execution control.

Tests advanced execution control features:
- Rollback to checkpoint
- Branching from checkpoint
- Pause and resume
- Single node execution
- Time travel operations

SOLID Compliance:
- SRP: Tests focus on execution control
- OCP: Extensible for new control patterns
- DIP: Uses fixtures for dependencies

ACID Compliance:
- Atomicity: Rollback operations are all-or-nothing
- Consistency: State invariants maintained after control operations
- Isolation: Branch operations create isolated execution paths
- Durability: State changes persist through checkpoints

Run with: pytest tests/test_langgraph/integration/test_execution_control.py -v
"""

import pytest
from typing import Dict, Any, List
from datetime import datetime

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from wtb.infrastructure.stores.langgraph_checkpoint_store import LangGraphCheckpointStore
from wtb.domain.models.checkpoint import CheckpointId, ExecutionHistory

from tests.test_langgraph.helpers import (
    create_initial_simple_state,
    create_initial_branch_state,
    create_initial_advanced_state,
    BranchState,
    AdvancedState,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Rollback Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollback:
    """Tests for rollback operations."""
    
    def test_rollback_to_previous_checkpoint(self, simple_compiled_graph):
        """Test rolling back to a previous checkpoint."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "rollback-test-1"}}
        
        # Execute full graph
        simple_compiled_graph.invoke(initial_state, config)
        
        # Get history
        history = list(simple_compiled_graph.get_state_history(config))
        
        # Find checkpoint after node_a completed (next should be node_b)
        # Checkpoint metadata has: source, step, parents (NOT writes)
        # We identify checkpoint by values or by next node
        checkpoint_after_a = None
        for snap in history:
            # After node_a completes, next should contain node_b
            # and values should have messages == ["A"]
            if snap.values.get("messages") == ["A"] and "node_b" in (snap.next or ()):
                checkpoint_after_a = snap
                break
        
        assert checkpoint_after_a is not None, f"Could not find checkpoint after node_a. History: {[(s.values, s.next) for s in history]}"
        
        # Get state at that checkpoint
        old_config = checkpoint_after_a.config
        state_at_a = simple_compiled_graph.get_state(old_config)
        
        assert state_at_a.values["messages"] == ["A"]
    
    def test_rollback_and_resume(self, branching_compiled_graph):
        """Test rollback and resume execution."""
        initial_state = create_initial_branch_state(switch=False)
        config = {"configurable": {"thread_id": "rollback-resume-1"}}
        
        # Execute: A -> B -> C
        result1 = branching_compiled_graph.invoke(initial_state, config)
        assert "C" in result1["messages"]
        
        # Get checkpoint at B
        history = list(branching_compiled_graph.get_state_history(config))
        checkpoint_b = None
        for snap in history:
            if "B" in snap.values.get("messages", []) and "C" not in snap.values.get("messages", []):
                checkpoint_b = snap
                break
        
        assert checkpoint_b is not None
        
        # Update state at B to take different branch
        config_at_b = checkpoint_b.config
        branching_compiled_graph.update_state(config_at_b, {"switch": True})
        
        # Resume from B -> should go to D
        result2 = branching_compiled_graph.invoke(None, config)
        
        # Should now have D instead of C
        assert "D" in result2["messages"]
    
    def test_rollback_preserves_original_history(self, simple_compiled_graph):
        """Test that rollback preserves original checkpoint history."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "rollback-history-1"}}
        
        simple_compiled_graph.invoke(initial_state, config)
        
        history_before = list(simple_compiled_graph.get_state_history(config))
        
        # Get a mid-point checkpoint
        mid_checkpoint = history_before[len(history_before) // 2]
        mid_config = mid_checkpoint.config
        
        # Just get state (simulating rollback preparation)
        simple_compiled_graph.get_state(mid_config)
        
        history_after = list(simple_compiled_graph.get_state_history(config))
        
        # History should be preserved
        assert len(history_after) == len(history_before)
    
    def test_rollback_with_state_modification(self, branching_compiled_graph):
        """Test rollback with state modification before resume."""
        initial_state = create_initial_branch_state(switch=False)
        initial_state["run_ids"] = ["RUN_1"]
        config = {"configurable": {"thread_id": "rollback-modify-1"}}
        
        # Execute: A -> B -> C
        branching_compiled_graph.invoke(initial_state, config)
        
        # Get checkpoint at B (after node_b completed, before C/D)
        history = list(branching_compiled_graph.get_state_history(config))
        checkpoint_b = None
        for snap in history:
            # After node_b, path should include node_b and next should be node_c or node_d
            if "node_b" in snap.values.get("path", []) and snap.next:
                checkpoint_b = snap
                break
        
        if checkpoint_b:
            # Update state to add RUN_2 marker
            config_at_b = checkpoint_b.config
            branching_compiled_graph.update_state(config_at_b, {"run_ids": ["RUN_2"]})
            
            # Resume
            result = branching_compiled_graph.invoke(None, config)
            
            # Should have both run markers
            assert "RUN_1" in result["run_ids"]
            assert "RUN_2" in result["run_ids"]


# ═══════════════════════════════════════════════════════════════════════════════
# Branching Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBranching:
    """Tests for branching from checkpoints."""
    
    def test_branch_to_alternate_path(self, branching_compiled_graph):
        """Test branching to take alternate path."""
        initial_state = create_initial_branch_state(switch=False)
        config = {"configurable": {"thread_id": "branch-alt-1"}}
        
        # Execute default path: A -> B -> C
        result1 = branching_compiled_graph.invoke(initial_state, config)
        assert result1["path"] == ["node_a", "node_b", "node_c"]
        
        # Get checkpoint at B (after node_b completed, before C/D)
        history = list(branching_compiled_graph.get_state_history(config))
        checkpoint_b = None
        for snap in history:
            # After node_b, path should include node_b and next should be node_c or node_d
            if "node_b" in snap.values.get("path", []) and snap.next:
                checkpoint_b = snap
                break
        
        if checkpoint_b:
            # Modify to take alternate path
            config_at_b = checkpoint_b.config
            branching_compiled_graph.update_state(config_at_b, {"switch": True})
            
            # Resume
            result2 = branching_compiled_graph.invoke(None, config)
            
            # Should have D instead of C
            assert "node_d" in result2["path"]
            assert "node_c" not in result2["path"][-1:]  # Last element should be node_d
    
    def test_branch_isolation_no_leak(self, branching_compiled_graph):
        """Test that branching doesn't leak future state."""
        initial_state = create_initial_branch_state(switch=False)
        initial_state["run_ids"] = ["RUN_1"]
        config = {"configurable": {"thread_id": "branch-isolation-1"}}
        
        # Execute: A -> B -> C with RUN_1
        result1 = branching_compiled_graph.invoke(initial_state, config)
        assert result1["messages"] == ["A", "B", "C"]
        
        # Get checkpoint at B (after node_b completed, before C/D)
        history = list(branching_compiled_graph.get_state_history(config))
        checkpoint_b = None
        for snap in history:
            # After node_b, path should include node_b and next should be node_c or node_d
            if "node_b" in snap.values.get("path", []) and snap.next:
                checkpoint_b = snap
                break
        
        if checkpoint_b:
            # Branch with RUN_2
            config_at_b = checkpoint_b.config
            branching_compiled_graph.update_state(config_at_b, {"run_ids": ["RUN_2"]})
            
            # Resume
            result2 = branching_compiled_graph.invoke(None, config)
            
            # CRITICAL: Should not duplicate C from first run
            assert result2["messages"].count("C") == 1
    
    def test_multiple_branches_from_same_checkpoint(self, branching_graph_def):
        """Test creating multiple branches from same checkpoint."""
        checkpointer = MemorySaver()
        graph = branching_graph_def.compile(checkpointer=checkpointer)
        
        # Execute base path
        base_config = {"configurable": {"thread_id": "multi-branch-base"}}
        graph.invoke(create_initial_branch_state(switch=False), base_config)
        
        # Get checkpoint at B
        history = list(graph.get_state_history(base_config))
        checkpoint_b = None
        for snap in history:
            # After node_b, path should include node_b and next should be node_c/d
            if "node_b" in snap.values.get("path", []) and snap.next:
                checkpoint_b = snap
                break
        
        if checkpoint_b:
            # Create branch 1: go to C (default)
            branch1_config = {"configurable": {"thread_id": "multi-branch-1"}}
            graph.update_state(
                branch1_config,
                checkpoint_b.values,
            )
            result1 = graph.invoke(None, branch1_config)
            
            # Create branch 2: go to D
            branch2_config = {"configurable": {"thread_id": "multi-branch-2"}}
            modified_state = {**checkpoint_b.values, "switch": True}
            graph.update_state(
                branch2_config,
                modified_state,
            )
            result2 = graph.invoke(None, branch2_config)
            
            # Verify branches took different paths
            assert "C" in result1["messages"]
            assert "D" in result2["messages"]


# ═══════════════════════════════════════════════════════════════════════════════
# Pause and Resume Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestPauseResume:
    """Tests for pause and resume operations."""
    
    def test_interrupt_before_node(self):
        """Test interrupting execution before a node."""
        from typing import TypedDict, Annotated
        import operator
        
        class InterruptState(TypedDict):
            messages: Annotated[list, operator.add]
            should_interrupt: bool
        
        workflow = StateGraph(InterruptState)
        
        workflow.add_node("node_a", lambda s: {"messages": ["A"]})
        workflow.add_node("node_b", lambda s: {"messages": ["B"]})
        workflow.add_node("node_c", lambda s: {"messages": ["C"]})
        
        workflow.set_entry_point("node_a")
        workflow.add_edge("node_a", "node_b")
        workflow.add_edge("node_b", "node_c")
        workflow.add_edge("node_c", END)
        
        checkpointer = MemorySaver()
        # Compile with interrupt_before
        graph = workflow.compile(
            checkpointer=checkpointer,
            interrupt_before=["node_b"]
        )
        
        config = {"configurable": {"thread_id": "interrupt-before-1"}}
        
        # First invocation should stop before node_b
        result1 = graph.invoke(
            {"messages": [], "should_interrupt": True},
            config
        )
        
        # Should have executed node_a only
        assert result1["messages"] == ["A"]
        
        # Check state shows node_b is next
        state = graph.get_state(config)
        assert "node_b" in state.next
        
        # Resume execution
        result2 = graph.invoke(None, config)
        
        # Should complete with all messages
        assert result2["messages"] == ["A", "B", "C"]
    
    def test_interrupt_after_node(self):
        """Test interrupting execution after a node."""
        from typing import TypedDict, Annotated
        import operator
        
        class InterruptState(TypedDict):
            messages: Annotated[list, operator.add]
        
        workflow = StateGraph(InterruptState)
        
        workflow.add_node("node_a", lambda s: {"messages": ["A"]})
        workflow.add_node("node_b", lambda s: {"messages": ["B"]})
        workflow.add_node("node_c", lambda s: {"messages": ["C"]})
        
        workflow.set_entry_point("node_a")
        workflow.add_edge("node_a", "node_b")
        workflow.add_edge("node_b", "node_c")
        workflow.add_edge("node_c", END)
        
        checkpointer = MemorySaver()
        # Compile with interrupt_after
        graph = workflow.compile(
            checkpointer=checkpointer,
            interrupt_after=["node_b"]
        )
        
        config = {"configurable": {"thread_id": "interrupt-after-1"}}
        
        # First invocation should stop after node_b
        result1 = graph.invoke({"messages": []}, config)
        
        # Should have executed node_a and node_b
        assert result1["messages"] == ["A", "B"]
        
        # Resume execution
        result2 = graph.invoke(None, config)
        
        # Should complete
        assert result2["messages"] == ["A", "B", "C"]
    
    def test_pause_modify_resume(self):
        """Test pausing, modifying state, and resuming."""
        from typing import TypedDict, Annotated
        import operator
        
        class ModifyState(TypedDict):
            messages: Annotated[list, operator.add]
            multiplier: int
        
        workflow = StateGraph(ModifyState)
        
        workflow.add_node("node_a", lambda s: {"messages": [f"A*{s['multiplier']}"]})
        workflow.add_node("node_b", lambda s: {"messages": [f"B*{s['multiplier']}"]})
        
        workflow.set_entry_point("node_a")
        workflow.add_edge("node_a", "node_b")
        workflow.add_edge("node_b", END)
        
        checkpointer = MemorySaver()
        graph = workflow.compile(
            checkpointer=checkpointer,
            interrupt_before=["node_b"]
        )
        
        config = {"configurable": {"thread_id": "pause-modify-1"}}
        
        # Execute to pause point
        graph.invoke({"messages": [], "multiplier": 1}, config)
        
        # Modify multiplier
        graph.update_state(config, {"multiplier": 10})
        
        # Resume
        result = graph.invoke(None, config)
        
        # node_b should see multiplier=10
        assert "B*10" in result["messages"]


# ═══════════════════════════════════════════════════════════════════════════════
# Single Node Execution Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSingleNodeExecution:
    """Tests for executing specific nodes."""
    
    def test_execute_specific_node_via_interrupt(self):
        """Test executing only a specific node using interrupts."""
        from typing import TypedDict, Annotated
        import operator
        
        class NodeState(TypedDict):
            messages: Annotated[list, operator.add]
            target_node: str
        
        workflow = StateGraph(NodeState)
        
        workflow.add_node("node_a", lambda s: {"messages": ["A"]})
        workflow.add_node("node_b", lambda s: {"messages": ["B"]})
        workflow.add_node("node_c", lambda s: {"messages": ["C"]})
        
        workflow.set_entry_point("node_a")
        workflow.add_edge("node_a", "node_b")
        workflow.add_edge("node_b", "node_c")
        workflow.add_edge("node_c", END)
        
        checkpointer = MemorySaver()
        
        # Compile with interrupt after each node to control execution
        graph = workflow.compile(
            checkpointer=checkpointer,
            interrupt_after=["node_a", "node_b", "node_c"]
        )
        
        config = {"configurable": {"thread_id": "single-node-1"}}
        
        # Execute only node_a
        result_a = graph.invoke({"messages": [], "target_node": "a"}, config)
        assert result_a["messages"] == ["A"]
        
        # Execute only node_b
        result_b = graph.invoke(None, config)
        assert result_b["messages"] == ["A", "B"]
        
        # Execute only node_c
        result_c = graph.invoke(None, config)
        assert result_c["messages"] == ["A", "B", "C"]
    
    def test_skip_to_node_via_state_update(self, simple_graph_def):
        """Test skipping to a specific node via state update."""
        # This tests manual state manipulation to simulate node skipping
        checkpointer = MemorySaver()
        graph = simple_graph_def.compile(
            checkpointer=checkpointer,
            interrupt_before=["node_a", "node_b", "node_c"]
        )
        
        config = {"configurable": {"thread_id": "skip-node-1"}}
        
        # Start execution
        graph.invoke(create_initial_simple_state(), config)
        
        # Skip node_a by updating state as if it ran
        graph.update_state(
            config,
            {"messages": ["A_SKIPPED"], "count": 1},
            as_node="node_a"
        )
        
        # Continue execution
        result = graph.invoke(None, config)
        
        # Should have skipped marker and continue through B, C
        assert "A_SKIPPED" in result["messages"]


# ═══════════════════════════════════════════════════════════════════════════════
# Time Travel Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestTimeTravel:
    """Tests for time travel operations."""
    
    def test_replay_from_checkpoint(self, simple_compiled_graph):
        """Test replaying execution from a checkpoint."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "replay-1"}}
        
        # Execute full graph
        simple_compiled_graph.invoke(initial_state, config)
        
        # Get history
        history = list(simple_compiled_graph.get_state_history(config))
        
        # Replay from step 1 (after node_a)
        step1_checkpoint = None
        for snap in history:
            if snap.metadata.get("step") == 1:
                step1_checkpoint = snap
                break
        
        if step1_checkpoint:
            # Get state at step 1
            old_state = simple_compiled_graph.get_state(step1_checkpoint.config)
            
            # Should show state after node_a
            assert old_state.values["messages"] == ["A"]
            assert old_state.values["count"] == 1
    
    def test_fork_execution_from_checkpoint(self, branching_compiled_graph):
        """Test forking a new execution from a checkpoint."""
        initial_state = create_initial_branch_state(switch=False)
        original_config = {"configurable": {"thread_id": "fork-original"}}
        
        # Execute original
        branching_compiled_graph.invoke(initial_state, original_config)
        
        # Get checkpoint at B
        history = list(branching_compiled_graph.get_state_history(original_config))
        checkpoint_b = None
        for snap in history:
            # After node_b, path should include node_b
            if "node_b" in snap.values.get("path", []) and snap.next:
                checkpoint_b = snap
                break
        
        if checkpoint_b:
            # Create fork with new thread
            fork_config = {"configurable": {"thread_id": "fork-new"}}
            
            # Copy state to new thread
            branching_compiled_graph.update_state(
                fork_config,
                checkpoint_b.values
            )
            
            # Modify fork state
            branching_compiled_graph.update_state(
                fork_config,
                {"switch": True, "run_ids": ["FORK"]}
            )
            
            # Execute fork
            fork_result = branching_compiled_graph.invoke(None, fork_config)
            
            # Get original final state
            original_state = branching_compiled_graph.get_state(original_config)
            
            # Fork should have different path
            assert "D" in fork_result["messages"]
            assert "C" in original_state.values["messages"]
            assert "FORK" in fork_result["run_ids"]
    
    def test_view_state_at_any_checkpoint(self, simple_compiled_graph):
        """Test viewing state at any historical checkpoint."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "view-history-1"}}
        
        simple_compiled_graph.invoke(initial_state, config)
        
        history = list(simple_compiled_graph.get_state_history(config))
        
        # Should be able to view state at each checkpoint
        for snapshot in history:
            state = simple_compiled_graph.get_state(snapshot.config)
            assert state is not None
            assert "messages" in state.values
    
    def test_checkpoint_metadata_contains_step(self, simple_compiled_graph):
        """Test that checkpoint metadata contains step number."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "metadata-step-1"}}
        
        simple_compiled_graph.invoke(initial_state, config)
        
        history = list(simple_compiled_graph.get_state_history(config))
        
        # All checkpoints should have step in metadata
        for snapshot in history:
            assert "step" in snapshot.metadata


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestACIDCompliance:
    """Tests verifying ACID compliance in execution control."""
    
    def test_atomicity_rollback_all_or_nothing(self, branching_compiled_graph):
        """Test that rollback operations are atomic."""
        initial_state = create_initial_branch_state(switch=False)
        config = {"configurable": {"thread_id": "acid-atomic-1"}}
        
        # Execute
        branching_compiled_graph.invoke(initial_state, config)
        
        # Get checkpoint
        history = list(branching_compiled_graph.get_state_history(config))
        checkpoint = history[1]  # Get a mid-point
        
        # Verify we can atomically access that state
        state = branching_compiled_graph.get_state(checkpoint.config)
        
        # State should be complete (all channels have values)
        assert "messages" in state.values
        assert "count" in state.values
        assert "path" in state.values
    
    def test_consistency_state_invariants_after_rollback(self, simple_compiled_graph):
        """Test state invariants are maintained after rollback."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "acid-consist-1"}}
        
        simple_compiled_graph.invoke(initial_state, config)
        
        # Rollback to any checkpoint
        history = list(simple_compiled_graph.get_state_history(config))
        
        for snapshot in history:
            state = simple_compiled_graph.get_state(snapshot.config)
            
            # Skip input checkpoint that may not have all fields initialized
            if "count" not in state.values:
                continue
            
            # Invariant: count should equal length of messages
            # (each node adds 1 message and increments count by 1)
            assert state.values["count"] == len(state.values["messages"])
    
    def test_isolation_branch_execution(self, branching_compiled_graph):
        """Test that branch executions are isolated."""
        config1 = {"configurable": {"thread_id": "acid-iso-1"}}
        config2 = {"configurable": {"thread_id": "acid-iso-2"}}
        
        # Execute on thread 1: path C
        branching_compiled_graph.invoke(
            create_initial_branch_state(switch=False),
            config1
        )
        
        # Execute on thread 2: path D
        branching_compiled_graph.invoke(
            create_initial_branch_state(switch=True),
            config2
        )
        
        state1 = branching_compiled_graph.get_state(config1)
        state2 = branching_compiled_graph.get_state(config2)
        
        # Thread 1 took C path, Thread 2 took D path
        assert "C" in state1.values["messages"]
        assert "D" in state2.values["messages"]
        
        # Neither polluted the other
        assert "D" not in state1.values["messages"]
        assert "C" not in state2.values["messages"]
    
    def test_durability_checkpoint_persists(self, simple_compiled_graph):
        """Test that checkpoints are durably stored."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "acid-durable-1"}}
        
        # Execute
        simple_compiled_graph.invoke(initial_state, config)
        
        # Get checkpoint IDs
        history = list(simple_compiled_graph.get_state_history(config))
        checkpoint_ids = [
            snap.config["configurable"]["checkpoint_id"]
            for snap in history
        ]
        
        # All checkpoints should be retrievable
        for cp_id in checkpoint_ids:
            cp_config = {
                "configurable": {
                    "thread_id": config["configurable"]["thread_id"],
                    "checkpoint_id": cp_id
                }
            }
            state = simple_compiled_graph.get_state(cp_config)
            assert state is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Complex Execution Control Scenarios
# ═══════════════════════════════════════════════════════════════════════════════


class TestComplexScenarios:
    """Tests for complex execution control scenarios."""
    
    def test_multiple_rollbacks(self, simple_compiled_graph):
        """Test performing multiple rollbacks in sequence."""
        initial_state = create_initial_simple_state()
        config = {"configurable": {"thread_id": "multi-rollback-1"}}
        
        # Execute
        simple_compiled_graph.invoke(initial_state, config)
        
        history = list(simple_compiled_graph.get_state_history(config))
        
        # Access each checkpoint in reverse order (simulating rollbacks)
        states = []
        for snapshot in history:
            state = simple_compiled_graph.get_state(snapshot.config)
            states.append(len(state.values["messages"]))
        
        # States should be accessible in any order
        assert len(states) == len(history)
    
    def test_interleaved_pause_resume(self):
        """Test interleaved pause and resume on multiple threads."""
        from typing import TypedDict, Annotated
        import operator
        
        class InterleaveState(TypedDict):
            messages: Annotated[list, operator.add]
            thread_marker: str
        
        workflow = StateGraph(InterleaveState)
        
        workflow.add_node("node_a", lambda s: {"messages": [f"A_{s['thread_marker']}"]})
        workflow.add_node("node_b", lambda s: {"messages": [f"B_{s['thread_marker']}"]})
        
        workflow.set_entry_point("node_a")
        workflow.add_edge("node_a", "node_b")
        workflow.add_edge("node_b", END)
        
        checkpointer = MemorySaver()
        graph = workflow.compile(
            checkpointer=checkpointer,
            interrupt_after=["node_a"]
        )
        
        config1 = {"configurable": {"thread_id": "interleave-1"}}
        config2 = {"configurable": {"thread_id": "interleave-2"}}
        
        # Start thread 1
        graph.invoke({"messages": [], "thread_marker": "T1"}, config1)
        
        # Start thread 2
        graph.invoke({"messages": [], "thread_marker": "T2"}, config2)
        
        # Resume thread 2 first
        result2 = graph.invoke(None, config2)
        
        # Resume thread 1
        result1 = graph.invoke(None, config1)
        
        # Both should complete with their markers
        assert "A_T1" in result1["messages"]
        assert "B_T1" in result1["messages"]
        assert "A_T2" in result2["messages"]
        assert "B_T2" in result2["messages"]
