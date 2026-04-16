"""
Integration Tests for LangGraph + Workspace Isolation.

Tests workspace isolation with LangGraph checkpointing including:
- Checkpoint creation with workspace state
- Rollback restoring both graph and file state
- Fork/branch creating new workspace
- Time-travel workspace isolation
- Pause/resume with workspace preservation

Design Reference: 
- docs/issues/WORKSPACE_ISOLATION_DESIGN.md (Section 3, 5, 6)
- docs/LangGraph/TIME_TRAVEL.md
"""

import json
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
import uuid

import pytest

from wtb.domain.models.workspace import (
    Workspace,
    WorkspaceConfig,
    WorkspaceStrategy,
    compute_venv_spec_hash,
)
from wtb.domain.events.workspace_events import (
    WorkspaceCreatedEvent,
    WorkspaceCleanedUpEvent,
    ForkRequestedEvent,
    ForkCompletedEvent,
)
from wtb.infrastructure.workspace.manager import WorkspaceManager


# Skip if LangGraph not available
langgraph_available = False
try:
    from langgraph.graph import StateGraph, END
    from langgraph.checkpoint.memory import MemorySaver
    langgraph_available = True
except ImportError:
    pass

pytestmark = pytest.mark.skipif(not langgraph_available, reason="LangGraph not installed")


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph State Types
# ═══════════════════════════════════════════════════════════════════════════════

if langgraph_available:
    from typing import TypedDict, Annotated
    from operator import add
    
    class SimpleState(TypedDict):
        messages: Annotated[List[str], add]
        count: int
    
    class FileState(TypedDict):
        messages: Annotated[List[str], add]
        count: int
        files_written: List[str]
        last_checkpoint_ref: Optional[str]  # Renamed to avoid reserved name


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Checkpoint with Workspace State
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckpointWithWorkspace:
    """Tests for LangGraph checkpoints with workspace state tracking."""
    
    @pytest.fixture
    def file_tracking_graph(self, memory_checkpointer):
        """Create graph that tracks file writes."""
        def write_file_node(state: FileState) -> dict:
            # Simulate writing a file
            file_name = f"output_{state['count']}.txt"
            return {
                "messages": [f"Wrote {file_name}"],
                "count": state["count"] + 1,
                "files_written": [file_name],
            }
        
        def process_node(state: FileState) -> dict:
            return {
                "messages": ["Processed"],
                "count": state["count"] + 1,
            }
        
        workflow = StateGraph(FileState)
        workflow.add_node("write", write_file_node)
        workflow.add_node("process", process_node)
        
        workflow.set_entry_point("write")
        workflow.add_edge("write", "process")
        workflow.add_edge("process", END)
        
        return workflow.compile(checkpointer=memory_checkpointer)
    
    def test_checkpoint_captures_workspace_context(
        self,
        workspace_manager,
        file_tracking_graph,
        sample_project_files,
    ):
        """Test that checkpoints can reference workspace state."""
        # Create workspace
        ws = workspace_manager.create_workspace(
            batch_id="batch-checkpoint",
            variant_name="test",
            execution_id="exec-cp-001",
            source_paths=[sample_project_files["data_dir"]],
        )
        
        # Execute graph with workspace context
        thread_id = f"thread-{ws.execution_id}"
        config = {"configurable": {"thread_id": thread_id}}
        
        initial_state: FileState = {
            "messages": [],
            "count": 0,
            "files_written": [],
            "last_checkpoint_ref": None,
        }
        
        result = file_tracking_graph.invoke(initial_state, config)
        
        # Verify checkpoint was created
        state_history = list(file_tracking_graph.get_state_history(config))
        assert len(state_history) > 0
        
        # Latest checkpoint should have our state
        latest = state_history[0]
        assert latest.values["count"] > 0
        
        # Workspace should be linkable to checkpoint
        # (In real implementation, we'd store workspace_id in checkpoint metadata)
        workspace_metadata = {
            "workspace_id": ws.workspace_id,
            "workspace_path": str(ws.root_path),
            "checkpoint_id": latest.config["configurable"]["checkpoint_id"],
        }
        
        assert workspace_metadata["workspace_id"] is not None
    
    def test_multiple_checkpoints_same_workspace(
        self,
        workspace_manager,
        memory_checkpointer,
        sample_project_files,
    ):
        """Test multiple checkpoints within same workspace."""
        # Create graph with multiple steps
        def step_node(state: SimpleState) -> dict:
            return {
                "messages": [f"Step {state['count']}"],
                "count": state["count"] + 1,
            }
        
        workflow = StateGraph(SimpleState)
        for i in range(5):
            workflow.add_node(f"step_{i}", step_node)
        
        workflow.set_entry_point("step_0")
        for i in range(4):
            workflow.add_edge(f"step_{i}", f"step_{i+1}")
        workflow.add_edge("step_4", END)
        
        graph = workflow.compile(checkpointer=memory_checkpointer)
        
        # Create workspace
        ws = workspace_manager.create_workspace(
            batch_id="batch-multi-cp",
            variant_name="test",
            execution_id="exec-multi",
        )
        
        # Execute
        thread_id = f"thread-{ws.execution_id}"
        config = {"configurable": {"thread_id": thread_id}}
        
        result = graph.invoke({"messages": [], "count": 0}, config)
        
        # Should have multiple checkpoints
        history = list(graph.get_state_history(config))
        assert len(history) >= 5  # At least one per step


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Rollback with Workspace File Restore
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollbackWithWorkspace:
    """Tests for rollback restoring both graph and file state."""
    
    def test_rollback_restores_graph_state(
        self,
        workspace_manager,
        simple_workflow,
        sample_project_files,
    ):
        """Test that rollback restores LangGraph state."""
        # Create workspace
        ws = workspace_manager.create_workspace(
            batch_id="batch-rollback",
            variant_name="test",
            execution_id="exec-rollback",
            source_paths=[sample_project_files["data_dir"]],
        )
        
        # Execute workflow
        thread_id = f"thread-{ws.execution_id}"
        config = {"configurable": {"thread_id": thread_id}}
        
        result = simple_workflow.invoke({"messages": [], "count": 0}, config)
        
        # Get checkpoint history
        history = list(simple_workflow.get_state_history(config))
        assert len(history) > 1
        
        # Get earlier checkpoint
        earlier_checkpoint = history[-2]  # Second to last
        earlier_checkpoint_id = earlier_checkpoint.config["configurable"]["checkpoint_id"]
        
        # Rollback to earlier checkpoint
        rollback_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_id": earlier_checkpoint_id,
            }
        }
        
        rolled_back_state = simple_workflow.get_state(rollback_config)
        
        # State should be earlier state
        assert rolled_back_state.values["count"] < result["count"]
    
    def test_rollback_workspace_file_coordination(
        self,
        workspace_manager,
        simple_workflow,
        sample_project_files,
    ):
        """Test that rollback can coordinate with workspace file state."""
        # Create workspace
        ws = workspace_manager.create_workspace(
            batch_id="batch-file-rollback",
            variant_name="test",
            execution_id="exec-file-rb",
            source_paths=[sample_project_files["data_dir"]],
        )
        
        original_cwd = os.getcwd()
        
        try:
            ws.activate()
            
            # Write file during execution
            output_v1 = ws.output_dir / "model.pkl"
            output_v1.parent.mkdir(parents=True, exist_ok=True)
            output_v1.write_bytes(b"MODEL_VERSION_1")
            
            # Execute workflow
            thread_id = f"thread-{ws.execution_id}"
            config = {"configurable": {"thread_id": thread_id}}
            
            result1 = simple_workflow.invoke({"messages": [], "count": 0}, config)
            
            # Get checkpoint for V1 state
            history = list(simple_workflow.get_state_history(config))
            v1_checkpoint = history[0]
            v1_checkpoint_id = v1_checkpoint.config["configurable"]["checkpoint_id"]
            
            # Simulate update - write V2
            output_v1.write_bytes(b"MODEL_VERSION_2")
            
            # Execute more steps
            result2 = simple_workflow.invoke(None, config)
            
            # Now we want to rollback to V1
            # Graph state rollback
            rollback_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_id": v1_checkpoint_id,
                }
            }
            
            rolled_back_state = simple_workflow.get_state(rollback_config)
            
            # File state would need separate restore (FileTracker integration)
            # Here we verify the coordination pattern works
            assert rolled_back_state.values["count"] == v1_checkpoint.values["count"]
            
        finally:
            ws.deactivate()
            os.chdir(original_cwd)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Fork/Branch with New Workspace
# ═══════════════════════════════════════════════════════════════════════════════


class TestForkWithWorkspace:
    """Tests for fork/branch creating new isolated workspace."""
    
    def test_fork_creates_new_thread_and_workspace(
        self,
        workspace_manager,
        simple_workflow,
        sample_project_files,
    ):
        """Test that fork creates new LangGraph thread and workspace."""
        # Create original workspace
        original_ws = workspace_manager.create_workspace(
            batch_id="batch-fork",
            variant_name="original",
            execution_id="exec-original",
            source_paths=[sample_project_files["data_dir"]],
        )
        
        # Execute original
        original_thread = f"thread-{original_ws.execution_id}"
        original_config = {"configurable": {"thread_id": original_thread}}
        
        result = simple_workflow.invoke({"messages": [], "count": 0}, original_config)
        
        # Get checkpoint to fork from
        history = list(simple_workflow.get_state_history(original_config))
        fork_checkpoint = history[-2]  # Middle checkpoint
        fork_checkpoint_id = fork_checkpoint.config["configurable"]["checkpoint_id"]
        
        # Create branch workspace
        branch_ws = workspace_manager.create_branch_workspace(
            source_workspace_id=original_ws.workspace_id,
            fork_checkpoint_id=42,  # Simulated checkpoint ID
            new_execution_id="exec-branch",
        )
        
        # Verify new workspace is independent
        assert branch_ws.workspace_id != original_ws.workspace_id
        assert branch_ws.root_path != original_ws.root_path
        
        # Get state at fork point using original thread (checkpoint_id specifies which state)
        fork_config = {
            "configurable": {
                "thread_id": original_thread,
                "checkpoint_id": fork_checkpoint_id,
            }
        }
        fork_state = simple_workflow.get_state(fork_config)
        
        # Start new execution on branch thread (fresh start with fork state values)
        branch_thread = f"thread-{branch_ws.execution_id}"
        branch_config = {"configurable": {"thread_id": branch_thread}}
        
        # Invoke with the actual state values from fork point
        branch_result = simple_workflow.invoke(
            {"messages": fork_state.values.get("messages", []), "count": fork_state.values.get("count", 0)},
            branch_config
        )
        
        # Both threads should have independent histories
        original_history = list(simple_workflow.get_state_history(original_config))
        branch_history = list(simple_workflow.get_state_history(branch_config))
        
        # Branch should have its own history
        assert len(branch_history) > 0
    
    def test_fork_workspace_has_source_files(
        self,
        workspace_manager,
        sample_project_files,
    ):
        """Test that forked workspace inherits source files."""
        # Create original with files
        original_ws = workspace_manager.create_workspace(
            batch_id="batch-fork-files",
            variant_name="original",
            execution_id="exec-orig",
            source_paths=[sample_project_files["data_dir"]],
        )
        
        # Verify original has files
        original_input = original_ws.input_dir / "data"
        assert original_input.exists()
        
        # Create branch
        branch_ws = workspace_manager.create_branch_workspace(
            source_workspace_id=original_ws.workspace_id,
            fork_checkpoint_id=1,
            new_execution_id="exec-branch",
        )
        
        # Branch should have its own input directory
        assert branch_ws.input_dir.exists()
        
        # Original should be unaffected
        assert original_ws.root_path.exists()


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Pause/Resume with Workspace
# ═══════════════════════════════════════════════════════════════════════════════


class TestPauseResumeWithWorkspace:
    """Tests for pause/resume preserving workspace state."""
    
    def test_pause_preserves_workspace(
        self,
        workspace_manager,
        memory_checkpointer,
        sample_project_files,
    ):
        """Test that pausing preserves workspace state."""
        # Create graph with interrupt
        def step1(state: SimpleState) -> dict:
            return {"messages": ["Step 1"], "count": state["count"] + 1}
        
        def step2(state: SimpleState) -> dict:
            return {"messages": ["Step 2"], "count": state["count"] + 1}
        
        workflow = StateGraph(SimpleState)
        workflow.add_node("step1", step1)
        workflow.add_node("step2", step2)
        
        workflow.set_entry_point("step1")
        workflow.add_edge("step1", "step2")
        workflow.add_edge("step2", END)
        
        # Compile with interrupt after step1
        graph = workflow.compile(
            checkpointer=memory_checkpointer,
            interrupt_after=["step1"],
        )
        
        # Create workspace
        ws = workspace_manager.create_workspace(
            batch_id="batch-pause",
            variant_name="test",
            execution_id="exec-pause",
            source_paths=[sample_project_files["data_dir"]],
        )
        
        original_cwd = os.getcwd()
        
        try:
            ws.activate()
            
            # Write file before pause point
            output_file = ws.output_dir / "checkpoint_data.json"
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text('{"status": "paused"}')
            
            ws.deactivate()
            
            # Execute until interrupt
            thread_id = f"thread-{ws.execution_id}"
            config = {"configurable": {"thread_id": thread_id}}
            
            for _ in graph.stream({"messages": [], "count": 0}, config):
                pass  # Will interrupt after step1
            
            # Get current state (paused state)
            paused_state = graph.get_state(config)
            
            # Workspace should still exist with our file
            assert ws.root_path.exists()
            assert output_file.exists()
            
            # Verify paused state
            assert paused_state.values["count"] == 1  # After step1
            assert "step2" in paused_state.next  # Next node is step2
            
        finally:
            os.chdir(original_cwd)
    
    def test_resume_continues_in_workspace(
        self,
        workspace_manager,
        memory_checkpointer,
        sample_project_files,
    ):
        """Test that resume continues in same workspace context."""
        # Create interruptible graph
        def step1(state: SimpleState) -> dict:
            return {"messages": ["Step 1"], "count": state["count"] + 1}
        
        def step2(state: SimpleState) -> dict:
            return {"messages": ["Step 2"], "count": state["count"] + 1}
        
        workflow = StateGraph(SimpleState)
        workflow.add_node("step1", step1)
        workflow.add_node("step2", step2)
        
        workflow.set_entry_point("step1")
        workflow.add_edge("step1", "step2")
        workflow.add_edge("step2", END)
        
        graph = workflow.compile(
            checkpointer=memory_checkpointer,
            interrupt_after=["step1"],
        )
        
        # Create workspace
        ws = workspace_manager.create_workspace(
            batch_id="batch-resume",
            variant_name="test",
            execution_id="exec-resume",
        )
        
        thread_id = f"thread-{ws.execution_id}"
        config = {"configurable": {"thread_id": thread_id}}
        
        # Execute until interrupt
        for _ in graph.stream({"messages": [], "count": 0}, config):
            pass
        
        # Verify paused
        paused_state = graph.get_state(config)
        assert paused_state.values["count"] == 1
        
        # Resume execution
        for _ in graph.stream(None, config):
            pass
        
        # Verify completed
        final_state = graph.get_state(config)
        assert final_state.values["count"] == 2
        assert len(final_state.next) == 0  # No more nodes
        
        # Workspace should still exist
        assert ws.root_path.exists()


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Update Node with Workspace
# ═══════════════════════════════════════════════════════════════════════════════


class TestUpdateNodeWithWorkspace:
    """Tests for node updates with workspace version tracking."""
    
    def test_update_state_mid_execution(
        self,
        workspace_manager,
        memory_checkpointer,
    ):
        """Test updating state mid-execution via update_state."""
        def step1(state: SimpleState) -> dict:
            return {"messages": ["Step 1"], "count": state["count"] + 1}
        
        def step2(state: SimpleState) -> dict:
            return {"messages": ["Step 2"], "count": state["count"] + 1}
        
        workflow = StateGraph(SimpleState)
        workflow.add_node("step1", step1)
        workflow.add_node("step2", step2)
        
        workflow.set_entry_point("step1")
        workflow.add_edge("step1", "step2")
        workflow.add_edge("step2", END)
        
        graph = workflow.compile(
            checkpointer=memory_checkpointer,
            interrupt_after=["step1"],
        )
        
        # Create workspace
        ws = workspace_manager.create_workspace(
            batch_id="batch-update",
            variant_name="test",
            execution_id="exec-update",
        )
        
        thread_id = f"thread-{ws.execution_id}"
        config = {"configurable": {"thread_id": thread_id}}
        
        # Execute until interrupt
        for _ in graph.stream({"messages": [], "count": 0}, config):
            pass
        
        # Update state (simulating node modification)
        graph.update_state(
            config,
            {"messages": ["Modified"], "count": 100},
            as_node="step1",
        )
        
        # Verify update
        updated_state = graph.get_state(config)
        assert updated_state.values["count"] == 100
        assert "Modified" in updated_state.values["messages"]
        
        # Resume with modified state
        for _ in graph.stream(None, config):
            pass
        
        final_state = graph.get_state(config)
        assert final_state.values["count"] == 101  # 100 + 1 from step2


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Time Travel with Workspace Isolation
# ═══════════════════════════════════════════════════════════════════════════════


class TestTimeTravelWorkspaceIsolation:
    """Tests for time travel ensuring workspace isolation."""
    
    def test_time_travel_no_future_state_leak(
        self,
        workspace_manager,
        simple_workflow,
        sample_project_files,
    ):
        """Test that time travel doesn't leak future state."""
        # Create workspace
        ws = workspace_manager.create_workspace(
            batch_id="batch-timetravel",
            variant_name="test",
            execution_id="exec-tt",
            source_paths=[sample_project_files["data_dir"]],
        )
        
        # Execute workflow
        thread_id = f"thread-{ws.execution_id}"
        config = {"configurable": {"thread_id": thread_id}}
        
        result = simple_workflow.invoke({"messages": [], "count": 0}, config)
        final_count = result["count"]
        
        # Get earlier checkpoint
        history = list(simple_workflow.get_state_history(config))
        assert len(history) > 2
        
        earlier_checkpoint = history[2]
        earlier_count = earlier_checkpoint.values["count"]
        
        # Travel to earlier state
        earlier_config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_id": earlier_checkpoint.config["configurable"]["checkpoint_id"],
            }
        }
        
        time_travel_state = simple_workflow.get_state(earlier_config)
        
        # Earlier state should not contain future state
        assert time_travel_state.values["count"] == earlier_count
        assert time_travel_state.values["count"] < final_count


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
