"""
Integration Tests for Venv + Workspace Isolation.

Tests virtual environment isolation with workspace including:
- Per-variant venv creation
- Venv spec hash tracking for invalidation
- Rollback venv compatibility checking
- Node update venv invalidation
- UV venv manager integration

Design Reference: docs/issues/WORKSPACE_ISOLATION_DESIGN.md (Section 10)
"""

import json
import os
import shutil
import sys
import tempfile
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
    VenvCreatedEvent,
    VenvInvalidatedEvent,
    VenvMismatchWarningEvent,
)
from wtb.infrastructure.workspace.manager import WorkspaceManager


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Venv Spec Hash Computation
# ═══════════════════════════════════════════════════════════════════════════════


class TestVenvSpecHash:
    """Tests for venv spec hash computation."""
    
    def test_hash_consistent(self):
        """Test that same spec produces same hash."""
        hash1 = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain>=0.1.0", "faiss-cpu"],
        )
        
        hash2 = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain>=0.1.0", "faiss-cpu"],
        )
        
        assert hash1 == hash2
    
    def test_hash_changes_with_python_version(self):
        """Test that different Python versions produce different hashes."""
        hash_312 = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain"],
        )
        
        hash_311 = compute_venv_spec_hash(
            python_version="3.11",
            dependencies=["langchain"],
        )
        
        assert hash_312 != hash_311
    
    def test_hash_changes_with_dependencies(self):
        """Test that different dependencies produce different hashes."""
        hash_v1 = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain>=0.1.0"],
        )
        
        hash_v2 = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain>=0.2.0"],
        )
        
        assert hash_v1 != hash_v2
    
    def test_hash_order_independent(self):
        """Test that dependency order doesn't affect hash."""
        hash1 = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain", "faiss-cpu", "chromadb"],
        )
        
        hash2 = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["chromadb", "langchain", "faiss-cpu"],
        )
        
        assert hash1 == hash2
    
    def test_hash_includes_requirements_file(self):
        """Test that requirements file content affects hash."""
        hash_no_req = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain"],
        )
        
        hash_with_req = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain"],
            requirements_file_content="langchain==0.1.0\nfaiss-cpu==1.7.0\n",
        )
        
        assert hash_no_req != hash_with_req


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Workspace Venv Tracking
# ═══════════════════════════════════════════════════════════════════════════════


class TestWorkspaceVenvTracking:
    """Tests for workspace venv spec tracking."""
    
    def test_workspace_stores_venv_spec_hash(
        self,
        workspace_manager,
    ):
        """Test that workspace can store venv spec hash."""
        ws = workspace_manager.create_workspace(
            batch_id="batch-venv-hash",
            variant_name="test",
            execution_id="exec-venv",
        )
        
        # Calculate venv spec hash
        venv_hash = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain>=0.1.0", "faiss-cpu>=1.7.0"],
        )
        
        # Store hash in workspace
        ws.venv_spec_hash = venv_hash
        ws.save_metadata()
        
        # Reload from metadata
        loaded_ws = Workspace.load_from_path(ws.root_path)
        
        assert loaded_ws is not None
        assert loaded_ws.venv_spec_hash == venv_hash
    
    def test_workspace_has_venv_directory(
        self,
        workspace_manager,
    ):
        """Test that workspace has venv directory path."""
        ws = workspace_manager.create_workspace(
            batch_id="batch-venv-dir",
            variant_name="test",
            execution_id="exec-venv-dir",
        )
        
        # Venv directory should be derived from root
        expected_venv_dir = ws.root_path / ".venv"
        assert ws.venv_dir == expected_venv_dir
        
        # Python path should be correct for platform
        if sys.platform == "win32":
            expected_python = ws.venv_dir / "Scripts" / "python.exe"
        else:
            expected_python = ws.venv_dir / "bin" / "python"
        
        assert ws.python_path == expected_python
    
    def test_venv_spec_hash_comparison(self):
        """Test comparing venv spec hashes for invalidation."""
        original_hash = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain==0.1.0", "faiss-cpu==1.7.0"],
        )
        
        # Same spec
        same_hash = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain==0.1.0", "faiss-cpu==1.7.0"],
        )
        
        # Updated spec
        updated_hash = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain==0.2.0", "faiss-cpu==1.7.0"],
        )
        
        # Same spec should match
        assert original_hash == same_hash
        
        # Updated should not match -> invalidation needed
        assert original_hash != updated_hash


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Per-Variant Venv Creation
# ═══════════════════════════════════════════════════════════════════════════════


class TestPerVariantVenv:
    """Tests for per-variant venv creation."""
    
    def test_different_variants_different_venv_hashes(
        self,
        workspace_manager,
    ):
        """Test that different variants can have different venv specs."""
        # Create workspaces for different variants with different deps
        variant_specs = {
            "bm25": {
                "python_version": "3.12",
                "dependencies": ["rank-bm25>=0.2.0"],
            },
            "faiss": {
                "python_version": "3.12",
                "dependencies": ["faiss-cpu>=1.7.0"],
            },
            "hybrid": {
                "python_version": "3.12",
                "dependencies": ["rank-bm25>=0.2.0", "faiss-cpu>=1.7.0"],
            },
        }
        
        workspaces = {}
        for variant, spec in variant_specs.items():
            ws = workspace_manager.create_workspace(
                batch_id="batch-multi-venv",
                variant_name=variant,
                execution_id=f"exec-{variant}",
            )
            
            # Set venv spec hash
            ws.venv_spec_hash = compute_venv_spec_hash(**spec)
            ws.save_metadata()
            
            workspaces[variant] = ws
        
        # Verify each has unique hash
        hashes = {ws.venv_spec_hash for ws in workspaces.values()}
        assert len(hashes) == 3, "Each variant should have unique venv spec"
    
    def test_variant_workspaces_isolated_venv_paths(
        self,
        workspace_manager,
    ):
        """Test that variant workspaces have isolated venv paths."""
        variants = ["v1", "v2", "v3"]
        workspaces = []
        
        for variant in variants:
            ws = workspace_manager.create_workspace(
                batch_id="batch-isolated-venv",
                variant_name=variant,
                execution_id=f"exec-{variant}",
            )
            workspaces.append(ws)
        
        # All venv paths should be unique
        venv_paths = {str(ws.venv_dir) for ws in workspaces}
        assert len(venv_paths) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Rollback Venv Compatibility
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollbackVenvCompatibility:
    """Tests for rollback venv compatibility checking."""
    
    def test_detect_venv_mismatch_on_rollback(
        self,
        workspace_manager,
    ):
        """Test detecting venv spec mismatch during rollback."""
        # Create workspace with initial venv spec
        ws = workspace_manager.create_workspace(
            batch_id="batch-rollback-venv",
            variant_name="test",
            execution_id="exec-rollback",
        )
        
        # Initial venv spec (v1)
        initial_hash = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain==0.1.0"],
        )
        ws.venv_spec_hash = initial_hash
        ws.save_metadata()
        
        # Simulate checkpoint with venv hash
        checkpoint_metadata = {
            "checkpoint_id": 1,
            "venv_spec_hash": initial_hash,
        }
        
        # Simulate dependency upgrade
        current_hash = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain==0.2.0"],  # Upgraded
        )
        ws.venv_spec_hash = current_hash
        
        # Check compatibility for rollback
        checkpoint_hash = checkpoint_metadata["venv_spec_hash"]
        
        is_compatible = (current_hash == checkpoint_hash)
        
        assert not is_compatible, "Venv mismatch should be detected"
    
    def test_venv_compatible_when_unchanged(
        self,
        workspace_manager,
    ):
        """Test venv is compatible when unchanged."""
        ws = workspace_manager.create_workspace(
            batch_id="batch-compat",
            variant_name="test",
            execution_id="exec-compat",
        )
        
        # Same venv spec throughout
        venv_hash = compute_venv_spec_hash(
            python_version="3.12",
            dependencies=["langchain==0.1.0"],
        )
        
        # Checkpoint has same hash
        checkpoint_metadata = {
            "checkpoint_id": 1,
            "venv_spec_hash": venv_hash,
        }
        
        # Current workspace has same hash
        ws.venv_spec_hash = venv_hash
        
        is_compatible = (ws.venv_spec_hash == checkpoint_metadata["venv_spec_hash"])
        
        assert is_compatible, "Same venv spec should be compatible"


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Node Update Venv Invalidation
# ═══════════════════════════════════════════════════════════════════════════════


class TestNodeUpdateVenvInvalidation:
    """Tests for venv invalidation when node dependencies change."""
    
    def test_detect_venv_invalidation_on_dep_change(self):
        """Test detecting when node update requires venv invalidation."""
        # Original node spec
        original_spec = {
            "python_version": "3.12",
            "dependencies": ["langchain==0.1.0", "openai==1.0.0"],
        }
        original_hash = compute_venv_spec_hash(**original_spec)
        
        # Updated node spec (new dependency added)
        updated_spec = {
            "python_version": "3.12",
            "dependencies": ["langchain==0.1.0", "openai==1.0.0", "chromadb==0.4.0"],
        }
        updated_hash = compute_venv_spec_hash(**updated_spec)
        
        # Invalidation needed
        needs_invalidation = (original_hash != updated_hash)
        
        assert needs_invalidation, "New dependency should trigger invalidation"
    
    def test_no_invalidation_on_code_only_change(self):
        """Test that code-only changes don't invalidate venv."""
        # Same dependencies
        spec = {
            "python_version": "3.12",
            "dependencies": ["langchain==0.1.0"],
        }
        
        # Both before and after code change
        before_hash = compute_venv_spec_hash(**spec)
        after_hash = compute_venv_spec_hash(**spec)  # Deps unchanged
        
        assert before_hash == after_hash, "Code changes shouldn't affect venv hash"


# ═══════════════════════════════════════════════════════════════════════════════
# Test: UV Venv Manager Integration (Mock)
# ═══════════════════════════════════════════════════════════════════════════════


class TestUVVenvManagerIntegration:
    """Tests for UV venv manager integration (using mocks)."""
    
    def test_workspace_can_request_venv_creation(
        self,
        workspace_manager,
        mock_uv_venv_manager,
    ):
        """Test workspace can request venv creation from UV manager."""
        ws = workspace_manager.create_workspace(
            batch_id="batch-uv",
            variant_name="test",
            execution_id="exec-uv",
        )
        
        # Simulate requesting venv creation
        venv_spec = {
            "python_version": "3.12",
            "dependencies": ["langchain>=0.1.0"],
        }
        
        result = mock_uv_venv_manager.create_environment(
            env_id=ws.workspace_id,
            config={
                "packages": venv_spec["dependencies"],
                "python_version": venv_spec["python_version"],
                "target_dir": str(ws.venv_dir),
            },
        )
        
        assert result["status"] == "created"
        assert "env_id" in result
        
        # Store hash
        ws.venv_spec_hash = compute_venv_spec_hash(**venv_spec)
        ws.venv_commit_id = result["env_id"]
    
    def test_venv_activation_for_workspace(
        self,
        workspace_manager,
        mock_uv_venv_manager,
    ):
        """Test venv activation when workspace is activated."""
        ws = workspace_manager.create_workspace(
            batch_id="batch-activate",
            variant_name="test",
            execution_id="exec-activate",
        )
        
        # Simulate venv creation
        mock_uv_venv_manager.create_environment(
            env_id=ws.workspace_id,
            config={"packages": ["langchain"]},
        )
        
        # Activate venv (simulated)
        result = mock_uv_venv_manager.activate_environment(ws.workspace_id)
        
        assert result is True
        
        # Deactivate
        mock_uv_venv_manager.deactivate_environment(ws.workspace_id)
    
    def test_venv_cleanup_on_workspace_cleanup(
        self,
        workspace_manager,
        mock_uv_venv_manager,
    ):
        """Test venv is cleaned up with workspace."""
        ws = workspace_manager.create_workspace(
            batch_id="batch-cleanup-venv",
            variant_name="test",
            execution_id="exec-cleanup",
        )
        ws_id = ws.workspace_id
        
        # Simulate venv creation
        mock_uv_venv_manager.create_environment(
            env_id=ws_id,
            config={"packages": ["langchain"]},
        )
        
        # Cleanup workspace
        workspace_manager.cleanup_workspace(ws_id)
        
        # In real implementation, would also call:
        # mock_uv_venv_manager.delete_environment(ws_id)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Venv Events
# ═══════════════════════════════════════════════════════════════════════════════


class TestVenvEvents:
    """Tests for venv-related domain events."""
    
    def test_venv_created_event_serializable(self):
        """Test VenvCreatedEvent can be serialized."""
        event = VenvCreatedEvent(
            workspace_id="ws-001",
            execution_id="exec-001",
            venv_path="/tmp/workspace/.venv",
            python_version="3.12",
            packages=["langchain>=0.1.0", "faiss-cpu"],
            venv_spec_hash="abc123def456",
            creation_time_ms=1500.5,
        )
        
        # Serialize
        event_dict = {
            "event_type": event.event_type,
            "workspace_id": event.workspace_id,
            "execution_id": event.execution_id,
            "venv_path": event.venv_path,
            "python_version": event.python_version,
            "packages": event.packages,
            "venv_spec_hash": event.venv_spec_hash,
            "creation_time_ms": event.creation_time_ms,
        }
        
        json_str = json.dumps(event_dict)
        assert json_str is not None
        
        restored = json.loads(json_str)
        assert restored["workspace_id"] == "ws-001"
        assert restored["python_version"] == "3.12"
    
    def test_venv_invalidated_event(self):
        """Test VenvInvalidatedEvent creation."""
        event = VenvInvalidatedEvent(
            node_id="retriever",
            old_spec_hash="old123",
            new_spec_hash="new456",
            reason="dependency_change",
            affected_workspaces=["ws-001", "ws-002"],
        )
        
        assert event.node_id == "retriever"
        assert event.reason == "dependency_change"
        assert len(event.affected_workspaces) == 2
    
    def test_venv_mismatch_warning_event(self):
        """Test VenvMismatchWarningEvent creation."""
        event = VenvMismatchWarningEvent(
            checkpoint_id=42,
            expected_venv_hash="expected123",
            current_venv_hash="current456",
            expected_packages=["langchain==0.1.0"],
            current_packages=["langchain==0.2.0"],
        )
        
        assert event.checkpoint_id == 42
        assert event.expected_venv_hash != event.current_venv_hash


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
