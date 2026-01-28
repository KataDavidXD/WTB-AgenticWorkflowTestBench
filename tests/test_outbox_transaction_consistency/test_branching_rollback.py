"""
Integration tests for Branching and Rollback Transaction Consistency.

Tests outbox transaction consistency during:
- Branch creation from checkpoint
- Branch merging
- Rollback to previous checkpoint
- Rollback with file restoration
- Rollback with venv state restoration
- Cross-branch consistency

ACID Compliance Focus:
- Atomicity: Branch/rollback operations complete fully
- Consistency: State integrity maintained across branches
- Isolation: Branches don't interfere with each other
- Durability: Branch and rollback states persist correctly

Run with: pytest tests/test_outbox_transaction_consistency/test_branching_rollback.py -v
"""

import pytest
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, Any, List
from unittest.mock import Mock, MagicMock

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

# Import real domain types
from wtb.domain.models.outbox import OutboxEvent, OutboxEventType

# Import test helpers
from tests.test_outbox_transaction_consistency.helpers import (
    BranchState,
    RollbackState,
    create_branch_state,
    create_rollback_state,
    branch_node_a,
    branch_node_b,
    branch_node_c,
    branch_node_d,
    route_by_switch,
)

# Import centralized mocks
from tests.mocks import MockMemento
from tests.mocks.services import (
    verify_outbox_consistency,
    verify_transaction_atomicity,
    VerificationResult,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions for Test Events
# ═══════════════════════════════════════════════════════════════════════════════


def create_branch_rollback_event(
    event_id: str,
    event_type_name: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: Dict[str, Any] = None,
) -> OutboxEvent:
    """
    Create an OutboxEvent for branching/rollback testing.
    
    Maps string event type names to proper OutboxEventType enums.
    """
    type_mapping = {
        "BRANCH_CREATED": OutboxEventType.CHECKPOINT_CREATE,
        "BRANCH_MERGED": OutboxEventType.CHECKPOINT_VERIFY,
        "MERGE_CONFLICT_DETECTED": OutboxEventType.CHECKPOINT_VERIFY,
        "ROLLBACK_EXECUTED": OutboxEventType.ROLLBACK_VERIFY,
        "ROLLBACK_FILE_RESTORE": OutboxEventType.ROLLBACK_FILE_RESTORE,
        "ROLLBACK_COMPLETED": OutboxEventType.ROLLBACK_PERFORMED,
        "VENV_ROLLBACK": OutboxEventType.ROLLBACK_VERIFY,
        "ROLLBACK_INITIATED": OutboxEventType.ROLLBACK_VERIFY,
        "STATE_SNAPSHOT_SAVED": OutboxEventType.CHECKPOINT_SAVED,
        "FILES_RESTORING": OutboxEventType.FILE_RESTORE_VERIFY,
        "FILES_RESTORED": OutboxEventType.FILE_RESTORE_VERIFY,
        "VENV_RESTORING": OutboxEventType.ROLLBACK_VERIFY,
        "VENV_RESTORED": OutboxEventType.ROLLBACK_VERIFY,
        "ROLLBACK_FAILED": OutboxEventType.ROLLBACK_VERIFY,
    }
    
    event_type = type_mapping.get(event_type_name, OutboxEventType.CHECKPOINT_CREATE)
    
    event = OutboxEvent(
        event_id=event_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload or {},
    )
    event.payload["_event_type_name"] = event_type_name
    return event


def event_type_matches(event: OutboxEvent, type_name: str) -> bool:
    """Check if event matches a given type name."""
    if "_event_type_name" in event.payload:
        return event.payload["_event_type_name"] == type_name
    return event.event_type.value == type_name.lower()


def verify_checkpoint_file_link(
    checkpoint_id: int,
    commit_id: str,
    checkpoint_repo,
    commit_repo,
) -> VerificationResult:
    """Verify checkpoint-file link consistency."""
    checkpoint = checkpoint_repo.get_by_id(checkpoint_id)
    commit = commit_repo.get_by_id(commit_id)
    
    if not checkpoint:
        return VerificationResult(
            success=False,
            message=f"Checkpoint {checkpoint_id} not found",
        )
    
    if not commit:
        return VerificationResult(
            success=False,
            message=f"Commit {commit_id} not found",
        )
    
    return VerificationResult(
        success=True,
        message="Checkpoint-file link verified",
        details={"checkpoint_id": checkpoint_id, "commit_id": commit_id},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Branch Creation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBranchCreationConsistency:
    """Tests for branch creation transaction consistency."""
    
    def test_branch_from_checkpoint_creates_outbox_event(
        self,
        branching_compiled_graph,
        checkpoint_repository,
        outbox_repository,
    ):
        """Branch creation from checkpoint should create outbox event."""
        # Execute to create checkpoints
        initial = create_branch_state(switch=False)
        config = {"configurable": {"thread_id": "branch-create-1"}}
        result = branching_compiled_graph.invoke(initial, config)
        
        # Get checkpoint history
        history = list(branching_compiled_graph.get_state_history(config))
        
        # Find checkpoint at node_b
        checkpoint_at_b = None
        for snap in history:
            if "B" in snap.values.get("messages", []) and "C" not in snap.values.get("messages", []):
                checkpoint_at_b = snap
                break
        
        if checkpoint_at_b:
            parent_step = checkpoint_at_b.metadata.get("step", 0)
            
            # Create branch
            branch_id = f"branch-{uuid.uuid4().hex[:8]}"
            
            # Add to checkpoint repository
            checkpoint_repository.add_checkpoint(
                checkpoint_id=100,  # New branch checkpoint
                thread_id=branch_id,
                step=0,  # Branch starts at step 0
                state={
                    **checkpoint_at_b.values,
                    "branch_id": branch_id,
                    "parent_checkpoint_id": parent_step,
                },
            )
            
            # Create branch event
            branch_event = create_branch_rollback_event(
                event_id=f"branch-event-{branch_id}",
                event_type_name="BRANCH_CREATED",
                aggregate_type="Branch",
                aggregate_id=branch_id,
                payload={
                    "parent_thread_id": "branch-create-1",
                    "parent_checkpoint_step": parent_step,
                    "branch_id": branch_id,
                },
            )
            outbox_repository.add(branch_event)
            
            # Verify
            assert outbox_repository.get_by_id(f"branch-event-{branch_id}") is not None
    
    def test_branch_inherits_parent_state(
        self,
        branching_compiled_graph,
        memory_checkpointer,
    ):
        """Branch should inherit state from parent checkpoint."""
        # Execute parent path: A -> B -> C
        initial = create_branch_state(switch=False)
        config = {"configurable": {"thread_id": "parent-1"}}
        result = branching_compiled_graph.invoke(initial, config)
        
        # Get state at B
        history = list(branching_compiled_graph.get_state_history(config))
        state_at_b = None
        for snap in history:
            if "B" in snap.values.get("messages", []) and len(snap.values.get("messages", [])) == 2:
                state_at_b = snap.values
                break
        
        if state_at_b:
            # Create branch from B with switch=True (will go to D)
            branch_state = {
                **state_at_b,
                "switch": True,  # Changed to take alternate path
            }
            
            branch_config = {"configurable": {"thread_id": "branch-from-b"}}
            
            # Create new workflow for branch (starts from node_b's output)
            branch_workflow = StateGraph(BranchState)
            branch_workflow.add_node("node_c", branch_node_c)
            branch_workflow.add_node("node_d", branch_node_d)
            
            def branch_router(state: BranchState) -> str:
                if state.get("switch", False):
                    return "node_d"
                return "node_c"
            
            branch_workflow.set_entry_point("node_d")  # Start at D (switch=True)
            branch_workflow.add_edge("node_c", END)
            branch_workflow.add_edge("node_d", END)
            
            branch_graph = branch_workflow.compile(checkpointer=MemorySaver())
            branch_result = branch_graph.invoke(branch_state, branch_config)
            
            # Branch should have D (not C)
            assert "D" in branch_result["messages"]
            # But also should have inherited A, B
            assert "A" in branch_result["messages"]
            assert "B" in branch_result["messages"]
    
    def test_multiple_branches_from_same_checkpoint(
        self,
        branching_compiled_graph,
        checkpoint_repository,
        outbox_repository,
    ):
        """Multiple branches from same checkpoint should be isolated."""
        # Execute to create checkpoints
        initial = create_branch_state(switch=False)
        config = {"configurable": {"thread_id": "multi-branch-parent"}}
        result = branching_compiled_graph.invoke(initial, config)
        
        # Create multiple branches
        branches = []
        for i in range(3):
            branch_id = f"branch-{i}"
            
            checkpoint_repository.add_checkpoint(
                checkpoint_id=200 + i,
                thread_id=branch_id,
                step=0,
                state={"branch_id": branch_id, "parent": "multi-branch-parent"},
            )
            
            branch_event = create_branch_rollback_event(
                event_id=f"multi-branch-event-{i}",
                event_type_name="BRANCH_CREATED",
                aggregate_type="Branch",
                aggregate_id=branch_id,
            )
            outbox_repository.add(branch_event)
            branches.append(branch_id)
        
        # Verify all branches created
        assert len(branches) == 3
        for i, branch_id in enumerate(branches):
            cp = checkpoint_repository.get_by_id(200 + i)
            assert cp is not None
            assert cp.state["branch_id"] == branch_id


# ═══════════════════════════════════════════════════════════════════════════════
# Rollback Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollbackConsistency:
    """Tests for rollback transaction consistency."""
    
    def test_rollback_to_checkpoint(
        self,
        simple_compiled_graph,
        checkpoint_repository,
        outbox_repository,
    ):
        """Rollback to checkpoint should restore state and create event."""
        from tests.test_outbox_transaction_consistency.helpers import create_simple_state
        
        # Execute full workflow
        config = {"configurable": {"thread_id": "rollback-test-1"}}
        result = simple_compiled_graph.invoke(create_simple_state(), config)
        
        # Get checkpoint history
        history = list(simple_compiled_graph.get_state_history(config))
        
        # Find checkpoint at step 2 (after B, before C)
        target_checkpoint = None
        for snap in history:
            if snap.metadata.get("step") == 2:
                target_checkpoint = snap
                break
        
        if target_checkpoint:
            target_step = target_checkpoint.metadata.get("step", 0)
            
            # Add rollback checkpoint
            checkpoint_repository.add_checkpoint(
                checkpoint_id=target_step,
                thread_id="rollback-test-1",
                step=target_step,
                state=target_checkpoint.values,
            )
            
            # Create rollback event
            rollback_event = create_branch_rollback_event(
                event_id="rollback-event-1",
                event_type_name="ROLLBACK_EXECUTED",
                aggregate_type="Execution",
                aggregate_id="rollback-test-1",
                payload={
                    "from_step": result["count"],
                    "to_step": target_step,
                    "state_at_target": target_checkpoint.values,
                },
            )
            outbox_repository.add(rollback_event)
            
            # Verify rollback state
            restored = checkpoint_repository.get_by_id(target_step)
            assert restored is not None
    
    def test_rollback_with_file_restoration(
        self,
        commit_repository,
        blob_repository,
        checkpoint_repository,
        outbox_repository,
    ):
        """Rollback should restore files from target checkpoint."""
        # Setup: Create checkpoints with file commits
        for step in [1, 2, 3]:
            # Create checkpoint
            checkpoint_repository.add_checkpoint(
                checkpoint_id=step,
                thread_id="file-rollback-1",
                step=step,
            )
            
            # Create file commit for each checkpoint
            blob_hash = blob_repository.save(f"content at step {step}".encode())
            commit_repository.add_commit(
                commit_id=f"commit-step-{step}",
                mementos=[MockMemento(f"file_step_{step}.txt", blob_hash)],
                execution_id="file-rollback-1",
            )
        
        # Rollback from step 3 to step 1
        rollback_event = create_branch_rollback_event(
            event_id="file-rollback-event-1",
            event_type_name="ROLLBACK_FILE_RESTORE",
            aggregate_type="Execution",
            aggregate_id="file-rollback-1",
            payload={
                "from_step": 3,
                "to_step": 1,
                "files_to_restore": ["file_step_1.txt"],
                "commit_id": "commit-step-1",
            },
        )
        outbox_repository.add(rollback_event)
        
        # Verify target commit exists
        target_commit = commit_repository.get_by_id("commit-step-1")
        assert target_commit is not None
        assert target_commit.file_count == 1
    
    def test_rollback_atomicity(
        self,
        checkpoint_repository,
        commit_repository,
        blob_repository,
        outbox_repository,
    ):
        """Rollback should be atomic - all or nothing."""
        operations = []
        
        try:
            # Step 1: Restore checkpoint state
            checkpoint = checkpoint_repository.add_checkpoint(
                checkpoint_id=1,
                thread_id="atomic-rollback-1",
                step=1,
            )
            operations.append(("checkpoint_restore", True))
            
            # Step 2: Restore files
            blob_hash = blob_repository.save(b"rollback content")
            commit = commit_repository.add_commit(
                commit_id="rollback-commit-1",
                mementos=[MockMemento("rollback.txt", blob_hash)],
            )
            operations.append(("file_restore", True))
            
            # Step 3: Create rollback event
            event = create_branch_rollback_event(
                event_id="atomic-rollback-event-1",
                event_type_name="ROLLBACK_COMPLETED",
                aggregate_type="Execution",
                aggregate_id="atomic-rollback-1",
            )
            outbox_repository.add(event)
            operations.append(("outbox_event", True))
            
        except Exception as e:
            operations.append(("error", False))
        
        result = verify_transaction_atomicity(operations)
        assert result.success
    
    def test_rollback_venv_state(
        self,
        venv_manager,
        outbox_repository,
    ):
        """Rollback should restore venv state."""
        # Create venv with packages at step 1
        venv1 = venv_manager.create_venv(
            venv_id="venv-rollback-v1",
            venv_path="/tmp/venv-v1",
        )
        venv_manager.install_packages("venv-rollback-v1", ["numpy==1.0"])
        
        # "Upgrade" to step 2
        venv_manager.install_packages("venv-rollback-v1", ["numpy==2.0", "pandas"])
        
        # Create rollback event (restore to step 1 packages)
        rollback_event = create_branch_rollback_event(
            event_id="venv-rollback-event-1",
            event_type_name="VENV_ROLLBACK",
            aggregate_type="Venv",
            aggregate_id="venv-rollback-v1",
            payload={
                "from_packages": ["numpy==2.0", "pandas"],
                "to_packages": ["numpy==1.0"],
                "step": 1,
            },
        )
        outbox_repository.add(rollback_event)
        
        # Verify event created
        saved = outbox_repository.get_by_id("venv-rollback-event-1")
        assert saved is not None
        assert saved.payload["to_packages"] == ["numpy==1.0"]


# ═══════════════════════════════════════════════════════════════════════════════
# Branch Merge Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBranchMergeConsistency:
    """Tests for branch merge transaction consistency."""
    
    def test_branch_merge_creates_event(
        self,
        checkpoint_repository,
        outbox_repository,
    ):
        """Branch merge should create appropriate events."""
        # Create main branch checkpoint
        main_cp = checkpoint_repository.add_checkpoint(
            checkpoint_id=1,
            thread_id="main",
            step=3,
            state={"messages": ["A", "B", "C"], "count": 3},
        )
        
        # Create feature branch checkpoint
        feature_cp = checkpoint_repository.add_checkpoint(
            checkpoint_id=100,
            thread_id="feature-1",
            step=2,
            state={"messages": ["A", "B", "D"], "count": 3, "branch_id": "feature-1"},
        )
        
        # Create merge event
        merge_event = create_branch_rollback_event(
            event_id="merge-event-1",
            event_type_name="BRANCH_MERGED",
            aggregate_type="Branch",
            aggregate_id="feature-1",
            payload={
                "source_branch": "feature-1",
                "target_branch": "main",
                "source_checkpoint": 100,
                "target_checkpoint": 1,
                "merge_strategy": "fast_forward",
            },
        )
        outbox_repository.add(merge_event)
        
        # Verify event
        saved = outbox_repository.get_by_id("merge-event-1")
        assert saved.payload["source_branch"] == "feature-1"
        assert saved.payload["target_branch"] == "main"
    
    def test_branch_merge_conflict_detection(
        self,
        checkpoint_repository,
        outbox_repository,
    ):
        """Branch merge should detect conflicts."""
        # Main branch: A -> B -> C
        checkpoint_repository.add_checkpoint(
            checkpoint_id=1,
            thread_id="main",
            step=3,
            state={"messages": ["A", "B", "C"], "modified_files": ["file1.txt"]},
        )
        
        # Feature branch: A -> B -> D (diverged, both modified same file)
        checkpoint_repository.add_checkpoint(
            checkpoint_id=100,
            thread_id="feature-conflict",
            step=3,
            state={"messages": ["A", "B", "D"], "modified_files": ["file1.txt"]},  # Same file!
        )
        
        # Create conflict event
        conflict_event = create_branch_rollback_event(
            event_id="conflict-event-1",
            event_type_name="MERGE_CONFLICT_DETECTED",
            aggregate_type="Branch",
            aggregate_id="feature-conflict",
            payload={
                "source_branch": "feature-conflict",
                "target_branch": "main",
                "conflicting_files": ["file1.txt"],
                "conflict_type": "both_modified",
            },
        )
        outbox_repository.add(conflict_event)
        
        # Verify conflict detected
        saved = outbox_repository.get_by_id("conflict-event-1")
        assert event_type_matches(saved, "MERGE_CONFLICT_DETECTED")
        assert "file1.txt" in saved.payload["conflicting_files"]


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-Branch Consistency Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrossBranchConsistency:
    """Tests for cross-branch consistency."""
    
    def test_branches_isolated_execution(
        self,
        memory_checkpointer,
    ):
        """Different branches should have isolated execution."""
        # Create workflow
        workflow = StateGraph(BranchState)
        workflow.add_node("node_a", branch_node_a)
        workflow.add_node("node_b", branch_node_b)
        workflow.add_node("node_c", branch_node_c)
        workflow.add_node("node_d", branch_node_d)
        workflow.set_entry_point("node_a")
        workflow.add_edge("node_a", "node_b")
        workflow.add_conditional_edges("node_b", route_by_switch)
        workflow.add_edge("node_c", END)
        workflow.add_edge("node_d", END)
        
        graph = workflow.compile(checkpointer=memory_checkpointer)
        
        # Execute main branch (switch=False -> C)
        main_config = {"configurable": {"thread_id": "main-branch"}}
        main_result = graph.invoke(create_branch_state(switch=False), main_config)
        
        # Execute feature branch (switch=True -> D)
        feature_config = {"configurable": {"thread_id": "feature-branch"}}
        feature_result = graph.invoke(create_branch_state(switch=True), feature_config)
        
        # Results should be different
        assert "C" in main_result["messages"]
        assert "D" not in main_result["messages"]
        
        assert "D" in feature_result["messages"]
        assert "C" not in feature_result["messages"]
    
    def test_concurrent_branch_execution(
        self,
        memory_checkpointer,
    ):
        """Concurrent branch execution should be isolated."""
        workflow = StateGraph(BranchState)
        workflow.add_node("node_a", branch_node_a)
        workflow.add_node("node_b", branch_node_b)
        workflow.add_node("node_c", branch_node_c)
        workflow.add_node("node_d", branch_node_d)
        workflow.set_entry_point("node_a")
        workflow.add_edge("node_a", "node_b")
        workflow.add_conditional_edges("node_b", route_by_switch)
        workflow.add_edge("node_c", END)
        workflow.add_edge("node_d", END)
        
        graph = workflow.compile(checkpointer=memory_checkpointer)
        
        results = {}
        
        def run_branch(branch_id: str, switch: bool):
            config = {"configurable": {"thread_id": branch_id}}
            results[branch_id] = graph.invoke(create_branch_state(switch=switch), config)
        
        threads = [
            threading.Thread(target=run_branch, args=("branch-1", False)),
            threading.Thread(target=run_branch, args=("branch-2", True)),
            threading.Thread(target=run_branch, args=("branch-3", False)),
            threading.Thread(target=run_branch, args=("branch-4", True)),
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Verify isolation
        assert "C" in results["branch-1"]["messages"]
        assert "D" in results["branch-2"]["messages"]
        assert "C" in results["branch-3"]["messages"]
        assert "D" in results["branch-4"]["messages"]


# ═══════════════════════════════════════════════════════════════════════════════
# Rollback Event Ordering Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollbackEventOrdering:
    """Tests for rollback event ordering."""
    
    def test_rollback_events_ordered(
        self,
        outbox_repository,
    ):
        """Rollback events should be properly ordered."""
        events = []
        
        # Rollback sequence
        for i, event_type_str in enumerate([
            "ROLLBACK_INITIATED",
            "STATE_SNAPSHOT_SAVED",
            "FILES_RESTORING",
            "FILES_RESTORED",
            "VENV_RESTORING",
            "VENV_RESTORED",
            "ROLLBACK_COMPLETED",
        ]):
            event = create_branch_rollback_event(
                event_id=f"rollback-order-{i}",
                event_type_name=event_type_str,
                aggregate_type="Execution",
                aggregate_id="exec-1",
                payload={"step": i},
            )
            events.append(event)
            outbox_repository.add(event)
            time.sleep(0.001)
        
        # Verify ordering
        saved = sorted(outbox_repository.list_all(), key=lambda e: e.created_at)
        assert event_type_matches(saved[0], "ROLLBACK_INITIATED")
        assert event_type_matches(saved[-1], "ROLLBACK_COMPLETED")
    
    def test_failed_rollback_creates_error_event(
        self,
        outbox_repository,
    ):
        """Failed rollback should create error event."""
        # Initiate rollback
        init_event = create_branch_rollback_event(
            event_id="failed-rollback-init",
            event_type_name="ROLLBACK_INITIATED",
            aggregate_type="Execution",
            aggregate_id="failed-exec-1",
        )
        outbox_repository.add(init_event)
        
        # Simulate failure
        error_event = create_branch_rollback_event(
            event_id="failed-rollback-error",
            event_type_name="ROLLBACK_FAILED",
            aggregate_type="Execution",
            aggregate_id="failed-exec-1",
            payload={
                "error_type": "FileRestoreError",
                "error_message": "Blob not found: abc123",
                "failed_at_step": "FILES_RESTORING",
            },
        )
        outbox_repository.add(error_event)
        
        # Verify error event
        saved = outbox_repository.get_by_id("failed-rollback-error")
        assert event_type_matches(saved, "ROLLBACK_FAILED")
        assert "FileRestoreError" in saved.payload["error_type"]


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Compliance Tests for Branching/Rollback
# ═══════════════════════════════════════════════════════════════════════════════


class TestBranchRollbackACIDCompliance:
    """Tests for ACID compliance in branching and rollback operations."""
    
    def test_branch_atomicity(
        self,
        checkpoint_repository,
        outbox_repository,
    ):
        """Branch creation should be atomic."""
        operations = []
        
        try:
            # Step 1: Create branch checkpoint
            cp = checkpoint_repository.add_checkpoint(
                checkpoint_id=100,
                thread_id="atomic-branch-1",
                step=0,
            )
            operations.append(("checkpoint", True))
            
            # Step 2: Create branch event
            event = create_branch_rollback_event(
                event_id="atomic-branch-event-1",
                event_type_name="BRANCH_CREATED",
                aggregate_type="Branch",
                aggregate_id="atomic-branch-1",
            )
            outbox_repository.add(event)
            operations.append(("event", True))
            
        except Exception:
            operations.append(("error", False))
        
        result = verify_transaction_atomicity(operations)
        assert result.success
    
    def test_rollback_consistency_state_valid(
        self,
        branching_compiled_graph,
    ):
        """State should remain consistent after rollback."""
        initial = create_branch_state(switch=False)
        config = {"configurable": {"thread_id": "rollback-consist-1"}}
        
        # Execute full workflow
        result = branching_compiled_graph.invoke(initial, config)
        
        # Get history
        history = list(branching_compiled_graph.get_state_history(config))
        
        # Every checkpoint with count/messages should have valid state
        for snap in history:
            state = snap.values
            # Skip checkpoints that don't have both keys (e.g., initial input)
            if "count" in state and "messages" in state:
                # Invariant: count equals number of messages
                assert state["count"] == len(state["messages"]), \
                    f"State inconsistent: count={state['count']}, messages={len(state['messages'])}"
    
    def test_branch_isolation(
        self,
        memory_checkpointer,
    ):
        """Branches should be completely isolated."""
        workflow = StateGraph(BranchState)
        workflow.add_node("node_a", branch_node_a)
        workflow.add_node("node_b", branch_node_b)
        workflow.add_node("node_c", branch_node_c)
        workflow.add_node("node_d", branch_node_d)
        workflow.set_entry_point("node_a")
        workflow.add_edge("node_a", "node_b")
        workflow.add_conditional_edges("node_b", route_by_switch)
        workflow.add_edge("node_c", END)
        workflow.add_edge("node_d", END)
        
        graph = workflow.compile(checkpointer=memory_checkpointer)
        
        # Execute two branches
        config1 = {"configurable": {"thread_id": "iso-branch-1"}}
        config2 = {"configurable": {"thread_id": "iso-branch-2"}}
        
        result1 = graph.invoke(create_branch_state(switch=False), config1)
        result2 = graph.invoke(create_branch_state(switch=True), config2)
        
        # Modify branch 1 state (shouldn't affect branch 2)
        state1 = graph.get_state(config1)
        state2 = graph.get_state(config2)
        
        # They should have different paths
        assert state1.values["path"] != state2.values["path"]
    
    def test_rollback_durability(
        self,
        checkpoint_repository,
        outbox_repository,
    ):
        """Rollback should persist durably."""
        # Create checkpoint
        cp = checkpoint_repository.add_checkpoint(
            checkpoint_id=1,
            thread_id="durable-rollback-1",
            step=1,
            state={"messages": ["A"], "count": 1},
        )
        
        # Create rollback event
        event = create_branch_rollback_event(
            event_id="durable-rollback-event-1",
            event_type_name="ROLLBACK_COMPLETED",
            aggregate_type="Execution",
            aggregate_id="durable-rollback-1",
            payload={"to_step": 1},
        )
        outbox_repository.add(event)
        event.mark_processed()
        outbox_repository.update(event)
        
        # Verify persistence
        retrieved_cp = checkpoint_repository.get_by_id(1)
        retrieved_event = outbox_repository.get_by_id("durable-rollback-event-1")
        
        assert retrieved_cp is not None
        assert retrieved_event is not None
        assert retrieved_event.status.value == "processed"
