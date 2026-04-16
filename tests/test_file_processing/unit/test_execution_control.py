"""
Unit Tests for Advanced Execution Control with File Processing.

Tests advanced workflow execution features:
- Rollback to checkpoints
- Checkpoint creation and restoration
- Branching execution paths
- Single node execution
- Pause and resume execution

Design Principles:
- SOLID: Tests verify single responsibility components
- ACID: Transaction consistency during control operations
- Pattern: State machine pattern for execution control
"""

import pytest
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
import uuid
import copy

from wtb.domain.models.file_processing import (
    BlobId,
    CommitId,
    FileMemento,
    FileCommit,
    CheckpointFileLink,
    CommitStatus,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Execution Control Simulation Classes
# ═══════════════════════════════════════════════════════════════════════════════


class ExecutionControlStatus(Enum):
    """Execution status for file processing tests."""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"


@dataclass
class FileCheckpoint:
    """Checkpoint capturing file state at a point in execution."""
    checkpoint_id: int
    step: int
    node_id: str
    commit: FileCommit
    state_snapshot: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now)
    
    @classmethod
    def create(
        cls,
        checkpoint_id: int,
        step: int,
        node_id: str,
        commit: FileCommit,
        state: Dict[str, Any],
    ) -> "FileCheckpoint":
        return cls(
            checkpoint_id=checkpoint_id,
            step=step,
            node_id=node_id,
            commit=commit,
            state_snapshot=copy.deepcopy(state),
        )


@dataclass
class FileExecutionState:
    """State tracking for file-aware workflow execution."""
    execution_id: str
    status: ExecutionControlStatus = ExecutionControlStatus.PENDING
    current_step: int = 0
    current_node: Optional[str] = None
    checkpoints: List[FileCheckpoint] = field(default_factory=list)
    file_commits: Dict[str, CommitId] = field(default_factory=dict)
    workflow_state: Dict[str, Any] = field(default_factory=dict)
    execution_path: List[str] = field(default_factory=list)
    branch_history: List[Dict[str, Any]] = field(default_factory=list)
    
    def add_checkpoint(self, checkpoint: FileCheckpoint):
        """Add checkpoint to history."""
        self.checkpoints.append(checkpoint)
    
    def get_checkpoint(self, checkpoint_id: int) -> Optional[FileCheckpoint]:
        """Get checkpoint by ID."""
        for cp in self.checkpoints:
            if cp.checkpoint_id == checkpoint_id:
                return cp
        return None
    
    def get_latest_checkpoint(self) -> Optional[FileCheckpoint]:
        """Get most recent checkpoint."""
        if self.checkpoints:
            return max(self.checkpoints, key=lambda c: c.step)
        return None


class FileExecutionController:
    """Controller for file-aware workflow execution with advanced control."""
    
    def __init__(
        self,
        blob_repository,
        commit_repository,
        link_repository,
    ):
        self._blob_repo = blob_repository
        self._commit_repo = commit_repository
        self._link_repo = link_repository
        self._executions: Dict[str, FileExecutionState] = {}
    
    def create_execution(self, execution_id: str = None) -> FileExecutionState:
        """Create new execution state."""
        exec_id = execution_id or f"exec-{uuid.uuid4().hex[:8]}"
        state = FileExecutionState(execution_id=exec_id)
        self._executions[exec_id] = state
        return state
    
    def get_execution(self, execution_id: str) -> Optional[FileExecutionState]:
        """Get execution state."""
        return self._executions.get(execution_id)
    
    def start_execution(self, execution_id: str) -> FileExecutionState:
        """Start execution."""
        state = self._executions[execution_id]
        state.status = ExecutionControlStatus.RUNNING
        return state
    
    def pause_execution(self, execution_id: str) -> FileExecutionState:
        """Pause execution."""
        state = self._executions[execution_id]
        state.status = ExecutionControlStatus.PAUSED
        return state
    
    def resume_execution(self, execution_id: str) -> FileExecutionState:
        """Resume paused execution."""
        state = self._executions[execution_id]
        if state.status == ExecutionControlStatus.PAUSED:
            state.status = ExecutionControlStatus.RUNNING
        return state
    
    def complete_execution(self, execution_id: str) -> FileExecutionState:
        """Mark execution as completed."""
        state = self._executions[execution_id]
        state.status = ExecutionControlStatus.COMPLETED
        return state
    
    def execute_node(
        self,
        execution_id: str,
        node_id: str,
        files_to_track: List[str] = None,
    ) -> FileCheckpoint:
        """
        Execute a single node with file tracking.
        
        Creates checkpoint with file commit if files provided.
        """
        state = self._executions[execution_id]
        state.current_node = node_id
        state.current_step += 1
        state.execution_path.append(node_id)
        
        # Create commit for tracked files
        commit = FileCommit.create(
            message=f"Node {node_id} execution",
            execution_id=execution_id,
        )
        
        if files_to_track:
            for file_path in files_to_track:
                memento, content = FileMemento.from_file(file_path)
                self._blob_repo.save(content)  # Content-addressable storage
                commit.add_memento(memento)
            commit.finalize()
            self._commit_repo.save(commit)
            state.file_commits[node_id] = commit.commit_id
        else:
            # Empty commit for nodes without files
            dummy_content = f"node_{node_id}_step_{state.current_step}".encode()
            blob_id = self._blob_repo.save(dummy_content)  # Returns BlobId
            memento = FileMemento.from_path_and_hash(
                f"/execution/{execution_id}/{node_id}/marker",
                blob_id,
                len(dummy_content),
            )
            commit.add_memento(memento)
            commit.finalize()
            self._commit_repo.save(commit)
        
        # Create checkpoint
        checkpoint = FileCheckpoint.create(
            checkpoint_id=state.current_step,
            step=state.current_step,
            node_id=node_id,
            commit=commit,
            state=state.workflow_state,
        )
        state.add_checkpoint(checkpoint)
        
        # Link checkpoint to commit
        link = CheckpointFileLink.create(checkpoint.checkpoint_id, commit)
        self._link_repo.add(link)
        
        return checkpoint
    
    def rollback_to_checkpoint(
        self,
        execution_id: str,
        checkpoint_id: int,
    ) -> FileExecutionState:
        """
        Rollback execution to a previous checkpoint.
        
        Restores state and file commits from checkpoint.
        """
        state = self._executions[execution_id]
        checkpoint = state.get_checkpoint(checkpoint_id)
        
        if checkpoint is None:
            raise ValueError(f"Checkpoint {checkpoint_id} not found")
        
        # Record branch point
        state.branch_history.append({
            "branch_point": checkpoint_id,
            "from_step": state.current_step,
            "timestamp": datetime.now().isoformat(),
        })
        
        # Restore state
        state.current_step = checkpoint.step
        state.current_node = checkpoint.node_id
        state.workflow_state = copy.deepcopy(checkpoint.state_snapshot)
        state.status = ExecutionControlStatus.ROLLED_BACK
        
        # Truncate execution path to checkpoint
        cp_index = next(
            (i for i, cp in enumerate(state.checkpoints) if cp.checkpoint_id == checkpoint_id),
            len(state.checkpoints) - 1
        )
        state.execution_path = state.execution_path[:cp_index + 1]
        
        return state
    
    def restore_files_from_checkpoint(
        self,
        execution_id: str,
        checkpoint_id: int,
        output_dir: str,
    ) -> List[str]:
        """
        Restore files from a checkpoint's commit.
        
        Returns list of restored file paths.
        """
        state = self._executions[execution_id]
        checkpoint = state.get_checkpoint(checkpoint_id)
        
        if checkpoint is None:
            raise ValueError(f"Checkpoint {checkpoint_id} not found")
        
        restored = []
        for memento in checkpoint.commit.mementos:
            content = self._blob_repo.get(memento.file_hash)
            if content:
                # Write to output directory
                output_path = Path(output_dir) / Path(memento.file_path).name
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(content)
                restored.append(str(output_path))
        
        return restored
    
    def branch_execution(
        self,
        execution_id: str,
        checkpoint_id: int,
        branch_name: str,
    ) -> str:
        """
        Create a new execution branch from a checkpoint.
        
        Returns new execution ID.
        """
        source_state = self._executions[execution_id]
        checkpoint = source_state.get_checkpoint(checkpoint_id)
        
        if checkpoint is None:
            raise ValueError(f"Checkpoint {checkpoint_id} not found")
        
        # Create new execution
        new_exec_id = f"{execution_id}_{branch_name}"
        new_state = FileExecutionState(
            execution_id=new_exec_id,
            current_step=checkpoint.step,
            current_node=checkpoint.node_id,
            workflow_state=copy.deepcopy(checkpoint.state_snapshot),
            execution_path=source_state.execution_path[:checkpoint.step + 1].copy(),
        )
        
        # Copy checkpoints up to branch point
        for cp in source_state.checkpoints:
            if cp.step <= checkpoint.step:
                new_state.add_checkpoint(cp)
        
        # Record branch
        new_state.branch_history.append({
            "branched_from": execution_id,
            "branch_point": checkpoint_id,
            "branch_name": branch_name,
            "timestamp": datetime.now().isoformat(),
        })
        
        self._executions[new_exec_id] = new_state
        return new_exec_id


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Checkpoint Creation
# ═══════════════════════════════════════════════════════════════════════════════


class TestCheckpointCreation:
    """Tests for checkpoint creation during file-aware execution."""
    
    @pytest.fixture
    def controller(self, blob_repository, commit_repository, checkpoint_link_repository):
        """Create controller with repositories."""
        return FileExecutionController(
            blob_repository,
            commit_repository,
            checkpoint_link_repository,
        )
    
    def test_checkpoint_created_on_node_execution(self, controller, sample_files):
        """Test that checkpoint is created when node executes."""
        state = controller.create_execution("exec-1")
        controller.start_execution("exec-1")
        
        checkpoint = controller.execute_node(
            "exec-1",
            "node_a",
            [str(sample_files["csv"])],
        )
        
        assert checkpoint is not None
        assert checkpoint.node_id == "node_a"
        assert checkpoint.step == 1
        assert checkpoint.commit.is_finalized
    
    def test_checkpoint_contains_file_commit(self, controller, sample_files):
        """Test that checkpoint contains correct file commit."""
        controller.create_execution("exec-2")
        controller.start_execution("exec-2")
        
        checkpoint = controller.execute_node(
            "exec-2",
            "preprocess",
            [str(sample_files["csv"]), str(sample_files["json"])],
        )
        
        assert checkpoint.commit.file_count == 2
        assert checkpoint.commit.status == CommitStatus.FINALIZED
    
    def test_multiple_checkpoints_in_sequence(self, controller, sample_files):
        """Test creating checkpoints in sequence."""
        state = controller.create_execution("exec-3")
        controller.start_execution("exec-3")
        
        # Execute multiple nodes
        nodes = ["load", "transform", "save"]
        for node in nodes:
            controller.execute_node("exec-3", node, [str(sample_files["csv"])])
        
        # Verify all checkpoints created
        assert len(state.checkpoints) == 3
        assert state.checkpoints[0].node_id == "load"
        assert state.checkpoints[1].node_id == "transform"
        assert state.checkpoints[2].node_id == "save"
    
    def test_checkpoint_preserves_workflow_state(self, controller, sample_files):
        """Test that checkpoint preserves workflow state."""
        state = controller.create_execution("exec-4")
        state.workflow_state = {"count": 10, "results": [1, 2, 3]}
        controller.start_execution("exec-4")
        
        checkpoint = controller.execute_node(
            "exec-4",
            "node_a",
            [str(sample_files["csv"])],
        )
        
        assert checkpoint.state_snapshot["count"] == 10
        assert checkpoint.state_snapshot["results"] == [1, 2, 3]
    
    def test_checkpoint_state_isolation(self, controller, sample_files):
        """Test that checkpoint state is isolated from execution state."""
        state = controller.create_execution("exec-5")
        state.workflow_state = {"value": 1}
        controller.start_execution("exec-5")
        
        checkpoint = controller.execute_node("exec-5", "node_a", [str(sample_files["csv"])])
        
        # Modify execution state
        state.workflow_state["value"] = 100
        
        # Checkpoint state should be unchanged
        assert checkpoint.state_snapshot["value"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Rollback Operations
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollbackOperations:
    """Tests for rollback to previous checkpoints."""
    
    @pytest.fixture
    def controller(self, blob_repository, commit_repository, checkpoint_link_repository):
        return FileExecutionController(
            blob_repository,
            commit_repository,
            checkpoint_link_repository,
        )
    
    def test_rollback_to_specific_checkpoint(self, controller, sample_files):
        """Test rolling back to a specific checkpoint."""
        state = controller.create_execution("exec-rollback")
        controller.start_execution("exec-rollback")
        
        # Execute several nodes
        controller.execute_node("exec-rollback", "step1", [str(sample_files["csv"])])
        controller.execute_node("exec-rollback", "step2", [str(sample_files["json"])])
        controller.execute_node("exec-rollback", "step3", [str(sample_files["txt"])])
        
        assert state.current_step == 3
        
        # Rollback to step 2
        controller.rollback_to_checkpoint("exec-rollback", 2)
        
        assert state.current_step == 2
        assert state.current_node == "step2"
        assert state.status == ExecutionControlStatus.ROLLED_BACK
    
    def test_rollback_restores_workflow_state(self, controller, sample_files):
        """Test that rollback restores workflow state."""
        state = controller.create_execution("exec-state")
        controller.start_execution("exec-state")
        
        # Step 1 with initial state
        state.workflow_state = {"counter": 1}
        controller.execute_node("exec-state", "step1", [str(sample_files["csv"])])
        
        # Step 2 modifies state
        state.workflow_state["counter"] = 2
        state.workflow_state["new_key"] = "value"
        controller.execute_node("exec-state", "step2", [str(sample_files["json"])])
        
        # Step 3 modifies more
        state.workflow_state["counter"] = 3
        controller.execute_node("exec-state", "step3", [str(sample_files["txt"])])
        
        # Rollback to step 1
        controller.rollback_to_checkpoint("exec-state", 1)
        
        assert state.workflow_state["counter"] == 1
        assert "new_key" not in state.workflow_state
    
    def test_rollback_records_branch_history(self, controller, sample_files):
        """Test that rollback records branch history."""
        state = controller.create_execution("exec-branch-hist")
        controller.start_execution("exec-branch-hist")
        
        controller.execute_node("exec-branch-hist", "step1", [str(sample_files["csv"])])
        controller.execute_node("exec-branch-hist", "step2", [str(sample_files["json"])])
        controller.execute_node("exec-branch-hist", "step3", [str(sample_files["txt"])])
        
        controller.rollback_to_checkpoint("exec-branch-hist", 1)
        
        assert len(state.branch_history) == 1
        assert state.branch_history[0]["branch_point"] == 1
        assert state.branch_history[0]["from_step"] == 3
    
    def test_rollback_truncates_execution_path(self, controller, sample_files):
        """Test that rollback truncates execution path."""
        state = controller.create_execution("exec-path")
        controller.start_execution("exec-path")
        
        controller.execute_node("exec-path", "node_a", [str(sample_files["csv"])])
        controller.execute_node("exec-path", "node_b", [str(sample_files["json"])])
        controller.execute_node("exec-path", "node_c", [str(sample_files["txt"])])
        
        assert state.execution_path == ["node_a", "node_b", "node_c"]
        
        controller.rollback_to_checkpoint("exec-path", 2)
        
        assert state.execution_path == ["node_a", "node_b"]
    
    def test_rollback_invalid_checkpoint_raises_error(self, controller):
        """Test that rollback to non-existent checkpoint raises error."""
        controller.create_execution("exec-invalid")
        
        with pytest.raises(ValueError, match="not found"):
            controller.rollback_to_checkpoint("exec-invalid", 999)
    
    def test_continue_after_rollback(self, controller, sample_files):
        """Test continuing execution after rollback."""
        state = controller.create_execution("exec-continue")
        controller.start_execution("exec-continue")
        
        controller.execute_node("exec-continue", "step1", [str(sample_files["csv"])])
        controller.execute_node("exec-continue", "step2", [str(sample_files["json"])])
        controller.execute_node("exec-continue", "step3", [str(sample_files["txt"])])
        
        # Rollback to step 2
        controller.rollback_to_checkpoint("exec-continue", 2)
        
        # Continue with different path
        controller.resume_execution("exec-continue")
        controller.execute_node("exec-continue", "step3_alt", [str(sample_files["csv"])])
        
        assert "step3_alt" in state.execution_path
        assert state.current_node == "step3_alt"


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Branching Execution
# ═══════════════════════════════════════════════════════════════════════════════


class TestBranchingExecution:
    """Tests for branching execution paths."""
    
    @pytest.fixture
    def controller(self, blob_repository, commit_repository, checkpoint_link_repository):
        return FileExecutionController(
            blob_repository,
            commit_repository,
            checkpoint_link_repository,
        )
    
    def test_create_branch_from_checkpoint(self, controller, sample_files):
        """Test creating a branch from a checkpoint."""
        controller.create_execution("exec-main")
        controller.start_execution("exec-main")
        
        controller.execute_node("exec-main", "step1", [str(sample_files["csv"])])
        controller.execute_node("exec-main", "step2", [str(sample_files["json"])])
        
        # Create branch from step 1
        branch_id = controller.branch_execution("exec-main", 1, "branch_a")
        
        assert branch_id == "exec-main_branch_a"
        branch_state = controller.get_execution(branch_id)
        assert branch_state is not None
        assert branch_state.current_step == 1
    
    def test_branch_isolation(self, controller, sample_files):
        """Test that branches are isolated from each other."""
        controller.create_execution("exec-iso")
        controller.start_execution("exec-iso")
        
        controller.execute_node("exec-iso", "step1", [str(sample_files["csv"])])
        controller.execute_node("exec-iso", "step2", [str(sample_files["json"])])
        
        # Create branch
        branch_id = controller.branch_execution("exec-iso", 1, "iso_branch")
        
        # Execute on main branch
        controller.execute_node("exec-iso", "step3_main", [str(sample_files["txt"])])
        
        # Execute on branch
        controller.start_execution(branch_id)
        controller.execute_node(branch_id, "step2_branch", [str(sample_files["txt"])])
        
        main_state = controller.get_execution("exec-iso")
        branch_state = controller.get_execution(branch_id)
        
        assert "step3_main" in main_state.execution_path
        assert "step2_branch" in branch_state.execution_path
        assert "step3_main" not in branch_state.execution_path
        assert "step2_branch" not in main_state.execution_path
    
    def test_branch_preserves_checkpoints(self, controller, sample_files):
        """Test that branch preserves checkpoints up to branch point."""
        controller.create_execution("exec-cp")
        controller.start_execution("exec-cp")
        
        controller.execute_node("exec-cp", "step1", [str(sample_files["csv"])])
        controller.execute_node("exec-cp", "step2", [str(sample_files["json"])])
        controller.execute_node("exec-cp", "step3", [str(sample_files["txt"])])
        
        # Branch from step 2
        branch_id = controller.branch_execution("exec-cp", 2, "cp_branch")
        branch_state = controller.get_execution(branch_id)
        
        # Branch should have checkpoints 1 and 2
        assert len(branch_state.checkpoints) == 2
        assert branch_state.checkpoints[0].step == 1
        assert branch_state.checkpoints[1].step == 2
    
    def test_branch_history_recorded(self, controller, sample_files):
        """Test that branch records its origin."""
        controller.create_execution("exec-hist")
        controller.start_execution("exec-hist")
        
        controller.execute_node("exec-hist", "step1", [str(sample_files["csv"])])
        
        branch_id = controller.branch_execution("exec-hist", 1, "hist_branch")
        branch_state = controller.get_execution(branch_id)
        
        assert len(branch_state.branch_history) == 1
        assert branch_state.branch_history[0]["branched_from"] == "exec-hist"
        assert branch_state.branch_history[0]["branch_point"] == 1
        assert branch_state.branch_history[0]["branch_name"] == "hist_branch"


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Pause and Resume
# ═══════════════════════════════════════════════════════════════════════════════


class TestPauseAndResume:
    """Tests for pausing and resuming execution."""
    
    @pytest.fixture
    def controller(self, blob_repository, commit_repository, checkpoint_link_repository):
        return FileExecutionController(
            blob_repository,
            commit_repository,
            checkpoint_link_repository,
        )
    
    def test_pause_execution(self, controller, sample_files):
        """Test pausing execution."""
        state = controller.create_execution("exec-pause")
        controller.start_execution("exec-pause")
        controller.execute_node("exec-pause", "step1", [str(sample_files["csv"])])
        
        controller.pause_execution("exec-pause")
        
        assert state.status == ExecutionControlStatus.PAUSED
    
    def test_resume_execution(self, controller, sample_files):
        """Test resuming paused execution."""
        state = controller.create_execution("exec-resume")
        controller.start_execution("exec-resume")
        controller.execute_node("exec-resume", "step1", [str(sample_files["csv"])])
        controller.pause_execution("exec-resume")
        
        controller.resume_execution("exec-resume")
        
        assert state.status == ExecutionControlStatus.RUNNING
    
    def test_resume_only_paused(self, controller, sample_files):
        """Test that resume only works on paused execution."""
        state = controller.create_execution("exec-running")
        controller.start_execution("exec-running")
        
        # Already running - resume should not change status
        controller.resume_execution("exec-running")
        
        assert state.status == ExecutionControlStatus.RUNNING
    
    def test_execute_after_resume(self, controller, sample_files):
        """Test executing nodes after resuming."""
        state = controller.create_execution("exec-continue")
        controller.start_execution("exec-continue")
        controller.execute_node("exec-continue", "step1", [str(sample_files["csv"])])
        
        controller.pause_execution("exec-continue")
        controller.resume_execution("exec-continue")
        
        controller.execute_node("exec-continue", "step2", [str(sample_files["json"])])
        
        assert state.current_step == 2
        assert "step2" in state.execution_path


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Single Node Execution
# ═══════════════════════════════════════════════════════════════════════════════


class TestSingleNodeExecution:
    """Tests for executing single nodes independently."""
    
    @pytest.fixture
    def controller(self, blob_repository, commit_repository, checkpoint_link_repository):
        return FileExecutionController(
            blob_repository,
            commit_repository,
            checkpoint_link_repository,
        )
    
    def test_execute_single_node(self, controller, sample_files):
        """Test executing a single node."""
        state = controller.create_execution("exec-single")
        controller.start_execution("exec-single")
        
        checkpoint = controller.execute_node(
            "exec-single",
            "single_node",
            [str(sample_files["csv"])],
        )
        
        assert checkpoint.node_id == "single_node"
        assert state.current_node == "single_node"
        assert state.file_commits.get("single_node") is not None
    
    def test_execute_node_without_files(self, controller):
        """Test executing node without file tracking."""
        state = controller.create_execution("exec-nofile")
        controller.start_execution("exec-nofile")
        
        checkpoint = controller.execute_node("exec-nofile", "compute_node")
        
        assert checkpoint is not None
        assert checkpoint.commit.file_count == 1  # Marker file
    
    def test_execute_node_creates_link(
        self, controller, sample_files, checkpoint_link_repository
    ):
        """Test that node execution creates checkpoint-file link."""
        state = controller.create_execution("exec-link")
        controller.start_execution("exec-link")
        
        controller.execute_node("exec-link", "linked_node", [str(sample_files["csv"])])
        
        # Verify link exists
        link = checkpoint_link_repository.get_by_checkpoint(1)
        assert link is not None
        assert link.file_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Test: File Restoration
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileRestoration:
    """Tests for restoring files from checkpoints."""
    
    @pytest.fixture
    def controller(self, blob_repository, commit_repository, checkpoint_link_repository):
        return FileExecutionController(
            blob_repository,
            commit_repository,
            checkpoint_link_repository,
        )
    
    def test_restore_files_from_checkpoint(self, controller, sample_files, temp_dir):
        """Test restoring files from a checkpoint."""
        state = controller.create_execution("exec-restore")
        controller.start_execution("exec-restore")
        
        controller.execute_node(
            "exec-restore",
            "tracked_node",
            [str(sample_files["csv"])],
        )
        
        output_dir = Path(temp_dir) / "restored"
        output_dir.mkdir()
        
        restored = controller.restore_files_from_checkpoint(
            "exec-restore",
            checkpoint_id=1,
            output_dir=str(output_dir),
        )
        
        assert len(restored) == 1
        assert Path(restored[0]).exists()
        
        # Verify content matches original
        original_content = sample_files["csv"].read_text()
        restored_content = Path(restored[0]).read_text()
        assert restored_content == original_content
    
    def test_restore_multiple_files(self, controller, sample_files, temp_dir):
        """Test restoring multiple files from checkpoint."""
        state = controller.create_execution("exec-multi-restore")
        controller.start_execution("exec-multi-restore")
        
        controller.execute_node(
            "exec-multi-restore",
            "multi_files",
            [str(sample_files["csv"]), str(sample_files["json"])],
        )
        
        output_dir = Path(temp_dir) / "multi_restored"
        output_dir.mkdir()
        
        restored = controller.restore_files_from_checkpoint(
            "exec-multi-restore",
            checkpoint_id=1,
            output_dir=str(output_dir),
        )
        
        assert len(restored) == 2
        assert all(Path(p).exists() for p in restored)
    
    def test_restore_from_earlier_checkpoint(self, controller, sample_files, temp_dir):
        """Test restoring from earlier checkpoint in execution."""
        state = controller.create_execution("exec-early")
        controller.start_execution("exec-early")
        
        # Execute multiple nodes with different files
        controller.execute_node("exec-early", "step1", [str(sample_files["csv"])])
        controller.execute_node("exec-early", "step2", [str(sample_files["json"])])
        controller.execute_node("exec-early", "step3", [str(sample_files["txt"])])
        
        # Restore from step 2
        output_dir = Path(temp_dir) / "early_restore"
        output_dir.mkdir()
        
        restored = controller.restore_files_from_checkpoint(
            "exec-early",
            checkpoint_id=2,
            output_dir=str(output_dir),
        )
        
        # Should restore json file from step 2
        assert len(restored) == 1
        assert "config.json" in restored[0]


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Transaction Consistency
# ═══════════════════════════════════════════════════════════════════════════════


class TestTransactionConsistency:
    """Tests for transaction consistency in execution control."""
    
    @pytest.fixture
    def controller(self, blob_repository, commit_repository, checkpoint_link_repository):
        return FileExecutionController(
            blob_repository,
            commit_repository,
            checkpoint_link_repository,
        )
    
    def test_checkpoint_commit_consistency(
        self, controller, sample_files, commit_repository, checkpoint_link_repository
    ):
        """Test that checkpoint and commit are created atomically."""
        state = controller.create_execution("exec-atomic")
        controller.start_execution("exec-atomic")
        
        controller.execute_node("exec-atomic", "node", [str(sample_files["csv"])])
        
        checkpoint = state.checkpoints[0]
        commit = checkpoint.commit
        link = checkpoint_link_repository.get_by_checkpoint(checkpoint.checkpoint_id)
        
        # All three should exist and be consistent
        assert commit_repository.exists(commit.commit_id)
        assert link is not None
        assert link.commit_id.value == commit.commit_id.value
    
    def test_rollback_preserves_commit_integrity(
        self, controller, sample_files, commit_repository
    ):
        """Test that rollback doesn't corrupt commits."""
        state = controller.create_execution("exec-integrity")
        controller.start_execution("exec-integrity")
        
        controller.execute_node("exec-integrity", "step1", [str(sample_files["csv"])])
        controller.execute_node("exec-integrity", "step2", [str(sample_files["json"])])
        
        original_commit_ids = [cp.commit.commit_id for cp in state.checkpoints]
        
        controller.rollback_to_checkpoint("exec-integrity", 1)
        
        # Original commits should still exist
        for commit_id in original_commit_ids:
            assert commit_repository.get_by_id(commit_id) is not None
    
    def test_branch_commit_isolation(self, controller, sample_files, commit_repository):
        """Test that branch doesn't affect main branch commits."""
        controller.create_execution("exec-branch-iso")
        controller.start_execution("exec-branch-iso")
        
        controller.execute_node("exec-branch-iso", "step1", [str(sample_files["csv"])])
        main_state = controller.get_execution("exec-branch-iso")
        original_commit = main_state.checkpoints[0].commit
        
        # Create and execute on branch
        branch_id = controller.branch_execution("exec-branch-iso", 1, "iso")
        controller.start_execution(branch_id)
        controller.execute_node(branch_id, "branch_step", [str(sample_files["json"])])
        
        # Original commit unchanged
        retrieved = commit_repository.get_by_id(original_commit.commit_id)
        assert retrieved is not None
        assert retrieved.file_count == original_commit.file_count
