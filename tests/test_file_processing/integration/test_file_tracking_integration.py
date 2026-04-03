"""
Integration Tests for File Tracking Service.

Tests the integration between:
- IFileTrackingService implementations
- Domain model CheckpointFileLink
- Interface FileTrackingLink
- Repository layer

Design Decision (2026-01-16):
- FileTrackingLink: Interface VO for ACL boundary (primitive types)
- CheckpointFileLink: Domain model with rich value objects

Test Categories:
1. Mock Service Integration
2. ACL Boundary Conversion
3. Repository Integration
4. Transaction Consistency
"""

import pytest
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
from typing import Dict, List

from wtb.domain.interfaces.file_tracking import (
    IFileTrackingService,
    FileTrackingLink,
    TrackedFile,
    FileTrackingResult,
    FileRestoreResult,
)
from wtb.domain.models.file_processing import (
    CheckpointFileLink,
    FileCommit,
    FileMemento,
    CommitId,
    BlobId,
)
from wtb.infrastructure.file_tracking import MockFileTrackingService
from wtb.infrastructure.database.repositories.file_processing_repository import (
    InMemoryCheckpointFileLinkRepository,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def temp_workspace():
    """Create temporary workspace for file operations."""
    temp_path = tempfile.mkdtemp(prefix="wtb_integration_")
    yield Path(temp_path)
    shutil.rmtree(temp_path, ignore_errors=True)


@pytest.fixture
def sample_output_files(temp_workspace) -> Dict[str, Path]:
    """Create sample output files in workspace."""
    files = {}
    
    # Simulated ML pipeline outputs
    output_dir = temp_workspace / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Model file
    model_path = output_dir / "model.pkl"
    model_path.write_bytes(b"\x80\x04\x95model_data_here")
    files["model"] = model_path
    
    # Predictions CSV
    pred_path = output_dir / "predictions.csv"
    pred_path.write_text("id,prediction\n1,0.95\n2,0.87\n", encoding="utf-8")
    files["predictions"] = pred_path
    
    # Metrics JSON
    metrics_path = output_dir / "metrics.json"
    metrics_path.write_text('{"accuracy": 0.92, "f1": 0.89}', encoding="utf-8")
    files["metrics"] = metrics_path
    
    return files


@pytest.fixture
def mock_file_tracking():
    """Create mock file tracking service."""
    return MockFileTrackingService(simulate_file_existence=True)


@pytest.fixture
def checkpoint_link_repo():
    """Create in-memory checkpoint link repository."""
    return InMemoryCheckpointFileLinkRepository()


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Mock Service Integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestMockServiceIntegration:
    """Integration tests for MockFileTrackingService with real files."""
    
    def test_track_real_files(self, mock_file_tracking, sample_output_files):
        """Test tracking real files from workspace."""
        file_paths = [str(f) for f in sample_output_files.values()]
        
        result = mock_file_tracking.track_files(
            file_paths=file_paths,
            message="Track ML outputs",
        )
        
        assert result.files_tracked == 3
        assert result.total_size_bytes > 0
        assert len(result.file_hashes) == 3
    
    def test_track_and_link_workflow(self, mock_file_tracking, sample_output_files):
        """Test complete track-and-link workflow."""
        # Simulate checkpoint IDs from LangGraph
        checkpoint_ids = [1, 2, 3]
        
        for i, cp_id in enumerate(checkpoint_ids):
            # Track different files at each checkpoint
            file_key = list(sample_output_files.keys())[i]
            file_path = str(sample_output_files[file_key])
            
            result = mock_file_tracking.track_and_link(
                checkpoint_id=cp_id,
                file_paths=[file_path],
                message=f"Checkpoint {cp_id}",
            )
            
            assert result.files_tracked == 1
            
            # Verify link
            commit_id = mock_file_tracking.get_commit_for_checkpoint(cp_id)
            assert commit_id == result.commit_id
    
    def test_restore_workflow(self, mock_file_tracking, sample_output_files):
        """Test restore from checkpoint workflow."""
        # Track and link
        file_paths = [str(sample_output_files["model"])]
        mock_file_tracking.track_and_link(
            checkpoint_id=42,
            file_paths=file_paths,
        )
        
        # Restore
        result = mock_file_tracking.restore_from_checkpoint(42)
        
        assert result.success is True
        assert result.files_restored == 1
        assert str(sample_output_files["model"]) in result.restored_paths


# ═══════════════════════════════════════════════════════════════════════════════
# Test: ACL Boundary Conversion
# ═══════════════════════════════════════════════════════════════════════════════


class TestACLBoundaryConversion:
    """Tests for Anti-Corruption Layer boundary conversions."""
    
    def test_interface_to_domain_conversion(self, mock_file_tracking, sample_output_files):
        """Test converting FileTrackingLink to domain CheckpointFileLink."""
        # Get interface link from service
        result = mock_file_tracking.track_files([str(sample_output_files["model"])])
        interface_link = mock_file_tracking.link_to_checkpoint(
            checkpoint_id=100,
            commit_id=result.commit_id,
        )
        
        # Convert at ACL boundary (entering domain)
        domain_link = CheckpointFileLink(
            checkpoint_id=interface_link.checkpoint_id,
            commit_id=CommitId(interface_link.commit_id),
            linked_at=interface_link.linked_at,
            file_count=interface_link.file_count,
            total_size_bytes=interface_link.total_size_bytes,
        )
        
        # Verify conversion
        assert domain_link.checkpoint_id == interface_link.checkpoint_id
        assert domain_link.commit_id.value == interface_link.commit_id
        assert isinstance(domain_link.commit_id, CommitId)
    
    def test_domain_to_interface_conversion(self, sample_output_files):
        """Test converting domain CheckpointFileLink to FileTrackingLink."""
        # Create domain model
        commit = FileCommit.create(message="Domain commit")
        memento, _ = FileMemento.from_file(str(sample_output_files["model"]))
        commit.add_memento(memento)
        commit.finalize()
        
        domain_link = CheckpointFileLink.create(checkpoint_id=200, commit=commit)
        
        # Convert at ACL boundary (leaving domain)
        interface_link = FileTrackingLink(
            checkpoint_id=domain_link.checkpoint_id,
            commit_id=domain_link.commit_id.value,
            linked_at=domain_link.linked_at,
            file_count=domain_link.file_count,
            total_size_bytes=domain_link.total_size_bytes,
        )
        
        # Verify conversion
        assert interface_link.checkpoint_id == domain_link.checkpoint_id
        assert interface_link.commit_id == domain_link.commit_id.value
        assert isinstance(interface_link.commit_id, str)
    
    def test_round_trip_conversion(self, mock_file_tracking, sample_output_files):
        """Test round-trip conversion preserves data."""
        # Start with interface link
        result = mock_file_tracking.track_files([str(sample_output_files["metrics"])])
        original_interface = mock_file_tracking.link_to_checkpoint(
            checkpoint_id=300,
            commit_id=result.commit_id,
        )
        
        # Convert to domain
        domain = CheckpointFileLink(
            checkpoint_id=original_interface.checkpoint_id,
            commit_id=CommitId(original_interface.commit_id),
            linked_at=original_interface.linked_at,
            file_count=original_interface.file_count,
            total_size_bytes=original_interface.total_size_bytes,
        )
        
        # Convert back to interface
        restored_interface = FileTrackingLink(
            checkpoint_id=domain.checkpoint_id,
            commit_id=domain.commit_id.value,
            linked_at=domain.linked_at,
            file_count=domain.file_count,
            total_size_bytes=domain.total_size_bytes,
        )
        
        # Verify round-trip
        assert restored_interface.checkpoint_id == original_interface.checkpoint_id
        assert restored_interface.commit_id == original_interface.commit_id
        assert restored_interface.file_count == original_interface.file_count


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Repository Integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestRepositoryIntegration:
    """Tests for repository integration with both link types."""
    
    def test_save_domain_link_to_repository(
        self,
        sample_output_files,
        checkpoint_link_repo,
    ):
        """Test saving domain CheckpointFileLink to repository."""
        # Create domain link
        commit = FileCommit.create(message="Repo test")
        memento, _ = FileMemento.from_file(str(sample_output_files["predictions"]))
        commit.add_memento(memento)
        commit.finalize()
        
        link = CheckpointFileLink.create(checkpoint_id=500, commit=commit)
        
        # Save to repository
        checkpoint_link_repo.add(link)
        
        # Retrieve and verify
        retrieved = checkpoint_link_repo.get_by_checkpoint(500)
        assert retrieved is not None
        assert retrieved.commit_id.value == link.commit_id.value
    
    def test_convert_interface_link_for_repository(
        self,
        mock_file_tracking,
        sample_output_files,
        checkpoint_link_repo,
    ):
        """Test converting interface link before saving to repository."""
        # Get interface link from service
        result = mock_file_tracking.track_files([str(sample_output_files["model"])])
        interface_link = mock_file_tracking.link_to_checkpoint(
            checkpoint_id=600,
            commit_id=result.commit_id,
        )
        
        # Convert to domain for repository
        domain_link = CheckpointFileLink(
            checkpoint_id=interface_link.checkpoint_id,
            commit_id=CommitId(interface_link.commit_id),
            linked_at=interface_link.linked_at,
            file_count=interface_link.file_count,
            total_size_bytes=interface_link.total_size_bytes,
        )
        
        # Save to repository
        checkpoint_link_repo.add(domain_link)
        
        # Verify
        retrieved = checkpoint_link_repo.get_by_checkpoint(600)
        assert retrieved is not None
        assert isinstance(retrieved.commit_id, CommitId)
    
    def test_find_links_by_commit(
        self,
        sample_output_files,
        checkpoint_link_repo,
    ):
        """Test finding all links for a commit."""
        # Create commit
        commit = FileCommit.create(message="Multi-link")
        memento, _ = FileMemento.from_file(str(sample_output_files["metrics"]))
        commit.add_memento(memento)
        commit.finalize()
        
        # Create multiple links to same commit
        for cp_id in [700, 701, 702]:
            link = CheckpointFileLink.create(checkpoint_id=cp_id, commit=commit)
            checkpoint_link_repo.add(link)
        
        # Find all links
        links = checkpoint_link_repo.get_by_commit(commit.commit_id)
        
        assert len(links) == 3
        assert all(l.commit_id.value == commit.commit_id.value for l in links)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Transaction Consistency
# ═══════════════════════════════════════════════════════════════════════════════


class TestTransactionConsistency:
    """Tests for transaction consistency across operations."""
    
    def test_track_and_link_atomic(self, mock_file_tracking, sample_output_files):
        """Test that track_and_link is atomic."""
        file_path = str(sample_output_files["model"])
        
        result = mock_file_tracking.track_and_link(
            checkpoint_id=800,
            file_paths=[file_path],
        )
        
        # Both commit and link should exist
        assert mock_file_tracking.get_commit_count() >= 1
        assert mock_file_tracking.get_commit_for_checkpoint(800) == result.commit_id
    
    def test_multiple_checkpoints_same_commit(
        self,
        mock_file_tracking,
        sample_output_files,
    ):
        """Test linking multiple checkpoints to same commit."""
        # Create one commit
        result = mock_file_tracking.track_files([str(sample_output_files["model"])])
        
        # Link to multiple checkpoints
        for cp_id in [900, 901, 902]:
            link = mock_file_tracking.link_to_checkpoint(cp_id, result.commit_id)
            assert link.commit_id == result.commit_id
        
        # All checkpoints should resolve to same commit
        for cp_id in [900, 901, 902]:
            assert mock_file_tracking.get_commit_for_checkpoint(cp_id) == result.commit_id
    
    def test_overwrite_checkpoint_link(self, mock_file_tracking, sample_output_files):
        """Test that linking to existing checkpoint overwrites."""
        # First link
        result1 = mock_file_tracking.track_and_link(
            checkpoint_id=1000,
            file_paths=[str(sample_output_files["model"])],
        )
        
        # Second link to same checkpoint
        result2 = mock_file_tracking.track_and_link(
            checkpoint_id=1000,
            file_paths=[str(sample_output_files["predictions"])],
        )
        
        # Should have the second commit
        current = mock_file_tracking.get_commit_for_checkpoint(1000)
        assert current == result2.commit_id
        assert current != result1.commit_id


# ═══════════════════════════════════════════════════════════════════════════════
# Test: End-to-End Workflow
# ═══════════════════════════════════════════════════════════════════════════════


class TestEndToEndWorkflow:
    """End-to-end workflow tests simulating real usage."""
    
    def test_ml_pipeline_file_tracking(
        self,
        mock_file_tracking,
        sample_output_files,
        checkpoint_link_repo,
    ):
        """Test file tracking through ML pipeline execution."""
        # Simulate pipeline steps with checkpoints
        pipeline_steps = [
            ("preprocess", [str(sample_output_files["predictions"])]),
            ("train", [str(sample_output_files["model"])]),
            ("evaluate", [str(sample_output_files["metrics"])]),
        ]
        
        step_commits = {}
        
        for i, (step_name, files) in enumerate(pipeline_steps):
            checkpoint_id = i + 1
            
            # Track files at checkpoint
            result = mock_file_tracking.track_and_link(
                checkpoint_id=checkpoint_id,
                file_paths=files,
                message=f"Step: {step_name}",
            )
            
            step_commits[step_name] = result.commit_id
            
            # Convert to domain and save to repository
            interface_link = mock_file_tracking.link_to_checkpoint(
                checkpoint_id=checkpoint_id,
                commit_id=result.commit_id,
            )
            
            domain_link = CheckpointFileLink(
                checkpoint_id=interface_link.checkpoint_id,
                commit_id=CommitId(interface_link.commit_id),
                linked_at=interface_link.linked_at,
                file_count=interface_link.file_count,
                total_size_bytes=interface_link.total_size_bytes,
            )
            checkpoint_link_repo.add(domain_link)
        
        # Verify all steps tracked
        assert len(step_commits) == 3
        
        # Verify can restore from any checkpoint
        for cp_id in [1, 2, 3]:
            result = mock_file_tracking.restore_from_checkpoint(cp_id)
            assert result.success is True
        
        # Verify repository has all links
        for cp_id in [1, 2, 3]:
            link = checkpoint_link_repo.get_by_checkpoint(cp_id)
            assert link is not None
    
    def test_rollback_scenario(
        self,
        mock_file_tracking,
        sample_output_files,
    ):
        """Test rollback scenario - restore to earlier checkpoint."""
        # Create checkpoints
        mock_file_tracking.track_and_link(
            checkpoint_id=1,
            file_paths=[str(sample_output_files["model"])],
            message="Initial model",
        )
        
        mock_file_tracking.track_and_link(
            checkpoint_id=2,
            file_paths=[str(sample_output_files["predictions"])],
            message="After predictions",
        )
        
        mock_file_tracking.track_and_link(
            checkpoint_id=3,
            file_paths=[str(sample_output_files["metrics"])],
            message="After evaluation",
        )
        
        # Simulate rollback to checkpoint 1
        result = mock_file_tracking.restore_from_checkpoint(1)
        
        assert result.success is True
        assert str(sample_output_files["model"]) in result.restored_paths
        # Should only have model file, not predictions or metrics
        assert len(result.restored_paths) == 1
