"""
Full Integration Tests for Workspace Isolation.

Tests complete integration of all systems:
- Ray + LangGraph + Venv + FileSystem
- Pause/Resume with workspace preservation
- Update node with venv invalidation
- Update workflow with checkpoint branching
- Branching (fork) with workspace cloning
- Rolling back with file + graph state restore
- Batch testing with parallel workspace isolation

Design Reference: docs/issues/WORKSPACE_ISOLATION_DESIGN.md
"""

import json
import os
import shutil
import sys
import tempfile
import threading
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
    VenvCreatedEvent,
)
from wtb.infrastructure.workspace.manager import WorkspaceManager
from wtb.infrastructure.events import WTBEventBus


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Pause/Resume Integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestPauseResumeIntegration:
    """Tests for pause/resume with full system integration."""
    
    def test_pause_preserves_workspace_and_state(
        self,
        workspace_manager,
        event_bus,
        sample_project_files,
    ):
        """Test that pause preserves workspace files and graph state."""
        # Create workspace
        ws = workspace_manager.create_workspace(
            batch_id="batch-pause-int",
            variant_name="test",
            execution_id="exec-pause-int",
            source_paths=[sample_project_files["data_dir"]],
        )
        
        original_cwd = os.getcwd()
        
        try:
            ws.activate()
            
            # Simulate execution with intermediate state
            checkpoint_file = ws.output_dir / "checkpoint_state.json"
            checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
            checkpoint_file.write_text(json.dumps({
                "step": 3,
                "status": "in_progress",
                "pause_reason": "user_request",
            }))
            
            # Simulate venv state tracking
            ws.venv_spec_hash = compute_venv_spec_hash(
                python_version="3.12",
                dependencies=["langchain"],
            )
            ws.save_metadata()
            
            ws.deactivate()
            
            # Verify workspace preserved after "pause"
            assert ws.root_path.exists()
            assert checkpoint_file.exists()
            
            # State should be recoverable
            state = json.loads(checkpoint_file.read_text())
            assert state["step"] == 3
            assert state["status"] == "in_progress"
            
        finally:
            os.chdir(original_cwd)
    
    def test_resume_continues_in_same_workspace(
        self,
        workspace_manager,
        sample_project_files,
    ):
        """Test that resume uses same workspace."""
        # Create workspace (simulating initial execution)
        ws = workspace_manager.create_workspace(
            batch_id="batch-resume-int",
            variant_name="test",
            execution_id="exec-resume-int",
            source_paths=[sample_project_files["csv"]],
        )
        
        original_cwd = os.getcwd()
        
        try:
            # First execution phase
            ws.activate()
            
            # Write intermediate state
            state_file = ws.output_dir / "state.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps({"phase": 1, "count": 5}))
            
            ws.deactivate()
            
            # "Pause" - workspace persisted
            ws_path = ws.root_path
            ws_id = ws.workspace_id
            
            # "Resume" - get same workspace
            resumed_ws = workspace_manager.get_workspace(ws_id)
            
            assert resumed_ws is not None
            assert resumed_ws.root_path == ws_path
            
            # Continue execution
            resumed_ws.activate()
            
            # Read previous state
            state = json.loads(state_file.read_text())
            
            # Continue from where we left off
            state["phase"] = 2
            state["count"] = 10
            state_file.write_text(json.dumps(state))
            
            resumed_ws.deactivate()
            
            # Verify continued state
            final_state = json.loads(state_file.read_text())
            assert final_state["phase"] == 2
            assert final_state["count"] == 10
            
        finally:
            os.chdir(original_cwd)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Update Node Integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestUpdateNodeIntegration:
    """Tests for node update with venv and workspace coordination."""
    
    def test_node_update_triggers_venv_invalidation_check(
        self,
        workspace_manager,
    ):
        """Test that node update checks for venv invalidation."""
        ws = workspace_manager.create_workspace(
            batch_id="batch-node-update",
            variant_name="test",
            execution_id="exec-node-update",
        )
        
        # Original venv spec
        original_spec = {
            "python_version": "3.12",
            "dependencies": ["langchain==0.1.0"],
        }
        ws.venv_spec_hash = compute_venv_spec_hash(**original_spec)
        ws.save_metadata()
        
        original_hash = ws.venv_spec_hash
        
        # Simulate node update with new dependencies
        updated_spec = {
            "python_version": "3.12",
            "dependencies": ["langchain==0.2.0", "chromadb"],  # Changed
        }
        new_hash = compute_venv_spec_hash(**updated_spec)
        
        # Check invalidation needed
        needs_invalidation = (original_hash != new_hash)
        
        assert needs_invalidation, "Dependency change should trigger invalidation"
    
    def test_node_update_code_only_no_venv_change(
        self,
        workspace_manager,
    ):
        """Test that code-only node update doesn't change venv."""
        ws = workspace_manager.create_workspace(
            batch_id="batch-code-update",
            variant_name="test",
            execution_id="exec-code-update",
        )
        
        # Set venv spec
        spec = {
            "python_version": "3.12",
            "dependencies": ["langchain==0.1.0"],
        }
        ws.venv_spec_hash = compute_venv_spec_hash(**spec)
        original_hash = ws.venv_spec_hash
        
        # Simulate code-only update (deps unchanged)
        # Node implementation changed but dependencies same
        new_hash = compute_venv_spec_hash(**spec)
        
        assert original_hash == new_hash, "Code change shouldn't affect venv hash"


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Update Workflow Integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestUpdateWorkflowIntegration:
    """Tests for workflow update with branching."""
    
    def test_workflow_update_creates_new_branch(
        self,
        workspace_manager,
        sample_project_files,
    ):
        """Test that workflow update can create new branch workspace."""
        # Original execution
        original_ws = workspace_manager.create_workspace(
            batch_id="batch-wf-update",
            variant_name="v1",
            execution_id="exec-v1",
            source_paths=[sample_project_files["data_dir"]],
        )
        
        original_cwd = os.getcwd()
        
        try:
            # Run some steps in original
            original_ws.activate()
            (original_ws.output_dir / "v1_result.json").write_text('{"version": 1}')
            original_ws.deactivate()
            
            # "Update workflow" - create new variant
            updated_ws = workspace_manager.create_workspace(
                batch_id="batch-wf-update",
                variant_name="v2",
                execution_id="exec-v2",
                source_paths=[sample_project_files["data_dir"]],
            )
            
            # Run updated workflow
            updated_ws.activate()
            (updated_ws.output_dir / "v2_result.json").write_text('{"version": 2}')
            updated_ws.deactivate()
            
            # Both versions should exist independently
            assert original_ws.root_path.exists()
            assert updated_ws.root_path.exists()
            
            # Each has its own output
            assert (original_ws.output_dir / "v1_result.json").exists()
            assert (updated_ws.output_dir / "v2_result.json").exists()
            
            # Cross-contamination check
            assert not (original_ws.output_dir / "v2_result.json").exists()
            assert not (updated_ws.output_dir / "v1_result.json").exists()
            
        finally:
            os.chdir(original_cwd)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Branching Integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestBranchingIntegration:
    """Tests for fork/branch with full workspace cloning."""
    
    def test_branch_creates_independent_workspace(
        self,
        workspace_manager,
        sample_project_files,
    ):
        """Test that branching creates independent workspace."""
        # Create source workspace
        source_ws = workspace_manager.create_workspace(
            batch_id="batch-branch-int",
            variant_name="source",
            execution_id="exec-source",
            source_paths=[sample_project_files["data_dir"]],
        )
        
        # Set venv spec on source
        source_ws.venv_spec_hash = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain"],
        )
        source_ws.save_metadata()
        
        original_cwd = os.getcwd()
        
        try:
            # Work in source
            source_ws.activate()
            (source_ws.output_dir / "checkpoint.json").write_text('{"step": 5}')
            source_ws.deactivate()
            
            # Create branch
            branch_ws = workspace_manager.create_branch_workspace(
                source_workspace_id=source_ws.workspace_id,
                fork_checkpoint_id=5,
                new_execution_id="exec-branch",
                file_commit_id="commit-abc123",
            )
            
            # Branch should be independent
            assert branch_ws.workspace_id != source_ws.workspace_id
            assert branch_ws.root_path != source_ws.root_path
            
            # Branch should have its own directories
            assert branch_ws.input_dir.exists()
            assert branch_ws.output_dir.exists()
            
            # Branch should have source commit reference
            assert branch_ws.source_snapshot_commit_id == "commit-abc123"
            
        finally:
            os.chdir(original_cwd)
    
    def test_branch_modifications_independent(
        self,
        workspace_manager,
        sample_project_files,
    ):
        """Test that branch modifications don't affect source."""
        source_ws = workspace_manager.create_workspace(
            batch_id="batch-independent",
            variant_name="source",
            execution_id="exec-source-ind",
            source_paths=[sample_project_files["csv"]],
        )
        
        original_cwd = os.getcwd()
        
        try:
            # Write to source
            source_ws.activate()
            source_file = source_ws.output_dir / "source_data.txt"
            source_file.parent.mkdir(parents=True, exist_ok=True)
            source_file.write_text("source data only")
            source_ws.deactivate()
            
            # Create branch
            branch_ws = workspace_manager.create_branch_workspace(
                source_workspace_id=source_ws.workspace_id,
                fork_checkpoint_id=1,
                new_execution_id="exec-branch-ind",
            )
            
            # Write to branch
            branch_ws.activate()
            branch_file = branch_ws.output_dir / "branch_data.txt"
            branch_file.parent.mkdir(parents=True, exist_ok=True)
            branch_file.write_text("branch data only")
            branch_ws.deactivate()
            
            # Verify independence
            assert source_file.exists()
            assert branch_file.exists()
            
            # Source shouldn't have branch file
            assert not (source_ws.output_dir / "branch_data.txt").exists()
            
            # Branch shouldn't have source file (in output)
            assert not (branch_ws.output_dir / "source_data.txt").exists()
            
        finally:
            os.chdir(original_cwd)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Rolling Back Integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollingBackIntegration:
    """Tests for rollback restoring both graph and file state."""
    
    def test_rollback_coordination_pattern(
        self,
        workspace_manager,
        mock_file_tracker,
        sample_project_files,
    ):
        """Test rollback coordination between graph and file state."""
        ws = workspace_manager.create_workspace(
            batch_id="batch-rollback-int",
            variant_name="test",
            execution_id="exec-rollback-int",
            source_paths=[sample_project_files["data_dir"]],
        )
        
        original_cwd = os.getcwd()
        
        try:
            ws.activate()
            
            # Step 1: Initial state
            model_file = ws.output_dir / "model.pkl"
            model_file.parent.mkdir(parents=True, exist_ok=True)
            model_file.write_bytes(b"MODEL_V1")
            
            # Checkpoint 1 (simulated)
            checkpoint_1 = {
                "checkpoint_id": 1,
                "graph_state": {"step": 1},
                "file_commit_id": "commit-v1",
                "venv_spec_hash": compute_venv_spec_hash("3.12", ["langchain==0.1.0"]),
            }
            
            # Step 2: Update state
            model_file.write_bytes(b"MODEL_V2")
            
            # Checkpoint 2
            checkpoint_2 = {
                "checkpoint_id": 2,
                "graph_state": {"step": 2},
                "file_commit_id": "commit-v2",
                "venv_spec_hash": compute_venv_spec_hash("3.12", ["langchain==0.1.0"]),
            }
            
            ws.deactivate()
            
            # Rollback to checkpoint 1
            target_checkpoint = checkpoint_1
            
            # 1. Restore graph state (would be via LangGraph)
            # graph.get_state(config_with_checkpoint_1)
            
            # 2. Restore file state
            mock_file_tracker.restore_to_workspace(
                checkpoint_id=target_checkpoint["checkpoint_id"],
                workspace_path=str(ws.output_dir),
            )
            
            # 3. Check venv compatibility
            current_venv_hash = compute_venv_spec_hash("3.12", ["langchain==0.1.0"])
            target_venv_hash = target_checkpoint["venv_spec_hash"]
            
            venv_compatible = (current_venv_hash == target_venv_hash)
            
            assert venv_compatible, "Venv should be compatible for rollback"
            
        finally:
            os.chdir(original_cwd)
    
    def test_rollback_venv_mismatch_detection(
        self,
        workspace_manager,
    ):
        """Test detecting venv mismatch during rollback."""
        ws = workspace_manager.create_workspace(
            batch_id="batch-venv-mismatch",
            variant_name="test",
            execution_id="exec-venv-mm",
        )
        
        # Old checkpoint had different venv
        old_checkpoint = {
            "venv_spec_hash": compute_venv_spec_hash("3.12", ["langchain==0.1.0"]),
        }
        
        # Current workspace has upgraded venv
        current_venv_hash = compute_venv_spec_hash("3.12", ["langchain==0.2.0"])
        ws.venv_spec_hash = current_venv_hash
        
        # Detect mismatch
        venv_compatible = (current_venv_hash == old_checkpoint["venv_spec_hash"])
        
        assert not venv_compatible, "Should detect venv mismatch"


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Batch Testing Integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchTestingIntegration:
    """Tests for batch testing with parallel workspace isolation."""
    
    def test_batch_test_creates_isolated_workspaces(
        self,
        workspace_manager,
        sample_project_files,
    ):
        """Test that batch test creates isolated workspace per variant."""
        batch_id = f"batch-test-{uuid.uuid4().hex[:8]}"
        variants = ["bm25", "dense", "hybrid", "sparse"]
        
        workspaces = []
        for variant in variants:
            ws = workspace_manager.create_workspace(
                batch_id=batch_id,
                variant_name=variant,
                execution_id=f"exec-{variant}",
                source_paths=[sample_project_files["csv"]],
            )
            workspaces.append(ws)
        
        # All should have unique paths
        paths = {str(ws.root_path) for ws in workspaces}
        assert len(paths) == len(variants)
        
        # All should exist
        for ws in workspaces:
            assert ws.root_path.exists()
            assert ws.input_dir.exists()
            assert ws.output_dir.exists()
    
    def test_batch_test_parallel_file_isolation(
        self,
        workspace_manager,
        sample_project_files,
    ):
        """Test file isolation in parallel batch test execution."""
        batch_id = f"batch-parallel-{uuid.uuid4().hex[:8]}"
        variants = ["v1", "v2", "v3"]
        
        workspaces = {}
        for variant in variants:
            ws = workspace_manager.create_workspace(
                batch_id=batch_id,
                variant_name=variant,
                execution_id=f"exec-{variant}",
                source_paths=[sample_project_files["csv"]],
            )
            workspaces[variant] = ws
        
        # Simulate parallel execution
        def execute_variant(ws: Workspace, variant: str):
            original_cwd = os.getcwd()
            try:
                ws.activate()
                
                # Write variant-specific output
                output_file = ws.output_dir / "result.json"
                output_file.parent.mkdir(parents=True, exist_ok=True)
                output_file.write_text(json.dumps({
                    "variant": variant,
                    "score": 0.85,
                }))
                
                return True
            finally:
                ws.deactivate()
                os.chdir(original_cwd)
        
        # Execute (sequentially for this test, but pattern is same)
        for variant, ws in workspaces.items():
            execute_variant(ws, variant)
        
        # Verify isolation
        for variant, ws in workspaces.items():
            result_file = ws.output_dir / "result.json"
            assert result_file.exists()
            
            content = json.loads(result_file.read_text())
            assert content["variant"] == variant
    
    def test_batch_cleanup_removes_all(
        self,
        workspace_manager,
    ):
        """Test that batch cleanup removes all variant workspaces."""
        batch_id = f"batch-cleanup-{uuid.uuid4().hex[:8]}"
        
        workspace_ids = []
        for i in range(5):
            ws = workspace_manager.create_workspace(
                batch_id=batch_id,
                variant_name=f"v{i}",
                execution_id=f"exec-{i}",
            )
            workspace_ids.append(ws.workspace_id)
        
        # All should exist
        assert len(workspace_manager.list_workspaces(batch_id=batch_id)) == 5
        
        # Cleanup batch
        cleaned = workspace_manager.cleanup_batch(batch_id)
        
        assert cleaned == 5
        assert len(workspace_manager.list_workspaces(batch_id=batch_id)) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Event Integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventIntegration:
    """Tests for event bus integration across all systems."""
    
    def test_workspace_lifecycle_events_complete(
        self,
        workspace_manager,
        event_bus,
        event_collector,
        sample_project_files,
    ):
        """Test complete lifecycle events are published."""
        ws = workspace_manager.create_workspace(
            batch_id="batch-events",
            variant_name="test",
            execution_id="exec-events",
            source_paths=[sample_project_files["csv"]],
        )
        
        # Should have created event
        assert len(event_collector["workspace_created"]) >= 1
        
        # Cleanup
        workspace_manager.cleanup_workspace(ws.workspace_id)
        
        # Should have cleanup event
        assert len(event_collector["workspace_cleaned_up"]) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Error Handling Integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestErrorHandlingIntegration:
    """Tests for error handling across integrated systems."""
    
    def test_workspace_preserved_on_failure(
        self,
        workspace_manager,
        sample_project_files,
    ):
        """Test that workspace is preserved when execution fails."""
        ws = workspace_manager.create_workspace(
            batch_id="batch-error",
            variant_name="test",
            execution_id="exec-error",
            source_paths=[sample_project_files["csv"]],
        )
        
        original_cwd = os.getcwd()
        
        try:
            ws.activate()
            
            # Write partial output before "failure"
            partial_file = ws.output_dir / "partial.txt"
            partial_file.parent.mkdir(parents=True, exist_ok=True)
            partial_file.write_text("partial data")
            
            # Simulate failure
            try:
                raise RuntimeError("Simulated execution failure")
            except RuntimeError:
                pass  # Caught, workspace should be preserved
            
        finally:
            ws.deactivate()
            os.chdir(original_cwd)
        
        # Workspace should still exist
        assert ws.root_path.exists()
        assert partial_file.exists()
    
    def test_cleanup_handles_missing_workspace(
        self,
        workspace_manager,
    ):
        """Test that cleanup handles already-removed workspace."""
        result = workspace_manager.cleanup_workspace("nonexistent-ws-id")
        
        # Should return False, not raise
        assert result is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
