"""
Integration tests for cross-database consistency.

Tests the Outbox Pattern and IntegrityChecker working together
to ensure consistency across WTB, AgentGit, and FileTracker databases.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock

from wtb.domain.models import (
    Execution,
    ExecutionStatus,
    ExecutionState,
    NodeBoundary,
    CheckpointFile,
    InvalidStateTransition,
)
from wtb.domain.models.outbox import (
    OutboxEvent,
    OutboxEventType,
    OutboxStatus,
)
from wtb.domain.models.integrity import (
    IntegrityReport,
    IntegrityIssueType,
    IntegritySeverity,
)
from wtb.infrastructure.database.inmemory_unit_of_work import InMemoryUnitOfWork
from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork


class TestOutboxPatternIntegration:
    """Integration tests for the Outbox Pattern."""
    
    def test_atomic_boundary_and_outbox_creation(self):
        """Test that node boundary and outbox event are created atomically."""
        uow = InMemoryUnitOfWork()
        
        with uow:
            # Create node boundary
            boundary = NodeBoundary(
                execution_id="exec-1",
                internal_session_id=1,
                node_id="train",
                entry_checkpoint_id=42
            )
            uow.node_boundaries.add(boundary)
            
            # Create corresponding outbox event
            event = OutboxEvent.create_checkpoint_verify(
                execution_id="exec-1",
                checkpoint_id=42,
                node_id="train",
                internal_session_id=1,
                is_entry=True
            )
            uow.outbox.add(event)
            
            uow.commit()
        
        # Verify both exist
        boundaries = uow.node_boundaries.list()
        events = uow.outbox.get_pending()
        
        assert len(boundaries) == 1
        assert len(events) == 1
        
        # Verify relationship
        assert events[0].payload["checkpoint_id"] == boundaries[0].entry_checkpoint_id
    
    def test_file_commit_link_with_outbox(self):
        """Test file commit linking with outbox verification event."""
        uow = InMemoryUnitOfWork()
        
        with uow:
            # Create checkpoint file link
            link = CheckpointFile(
                checkpoint_id=42,
                file_commit_id="fc-abc-123",
                file_count=3,
                total_size_bytes=1024
            )
            uow.checkpoint_files.add(link)
            
            # Create verification outbox event
            event = OutboxEvent.create_checkpoint_file_link_verify(
                execution_id="exec-1",
                checkpoint_id=42,
                file_commit_id="fc-abc-123"
            )
            uow.outbox.add(event)
            
            uow.commit()
        
        # Verify both exist
        links = uow.checkpoint_files.list()
        events = uow.outbox.get_pending()
        
        assert len(links) == 1
        assert len(events) == 1
        assert events[0].event_type == OutboxEventType.CHECKPOINT_FILE_LINK_VERIFY


class TestExecutionRichDomainModel:
    """Integration tests for the Rich Domain Model Execution entity."""
    
    def test_execution_lifecycle_with_breakpoints(self):
        """Test full execution lifecycle with breakpoints."""
        execution = Execution(workflow_id="wf-1")
        
        # Add breakpoint
        execution.add_breakpoint("evaluate")
        
        # Start execution
        execution.start()
        assert execution.status == ExecutionStatus.RUNNING
        
        # Execute first node
        execution.state.current_node_id = "load_data"
        execution.record_node_result("load_data", {"data_loaded": True})
        
        # Advance to next node (breakpoint)
        execution.advance_to_node("evaluate")
        assert execution.is_at_breakpoint()
        
        # Pause at breakpoint
        execution.pause()
        assert execution.status == ExecutionStatus.PAUSED
        
        # Modify state while paused
        execution.apply_state_modification({"threshold": 0.9})
        assert execution.get_variable("threshold") == 0.9
        
        # Resume execution
        execution.resume()
        assert execution.status == ExecutionStatus.RUNNING
        
        # Complete
        execution.advance_to_node(None)
        assert execution.status == ExecutionStatus.COMPLETED
    
    def test_execution_rollback_from_checkpoint(self):
        """Test rolling back execution to a checkpoint state."""
        execution = Execution(workflow_id="wf-1")
        
        # Run for a bit
        execution.start()
        execution.record_node_result("node1", {"x": 1})
        execution.record_node_result("node2", {"y": 2})
        execution.advance_to_node("node3")
        
        # Save checkpoint state
        checkpoint_state = execution.state.clone()
        
        # Continue and fail
        execution.record_node_result("node3", {"z": 3})
        execution.advance_to_node("node4")
        execution.fail("Error in node4", "node4")
        
        assert execution.status == ExecutionStatus.FAILED
        
        # Rollback to checkpoint
        execution.restore_from_checkpoint(checkpoint_state)
        
        assert execution.status == ExecutionStatus.PAUSED
        assert execution.state.current_node_id == "node3"
        assert execution.error_message is None
        assert len(execution.state.execution_path) == 2  # node1, node2
    
    def test_execution_state_transitions_validation(self):
        """Test that invalid state transitions are rejected."""
        execution = Execution(workflow_id="wf-1")
        
        # Cannot pause when not running
        with pytest.raises(InvalidStateTransition) as exc_info:
            execution.pause()
        assert exc_info.value.attempted_action == "pause"
        
        # Start execution
        execution.start()
        
        # Cannot start when already running
        with pytest.raises(InvalidStateTransition) as exc_info:
            execution.start()
        assert exc_info.value.current_status == ExecutionStatus.RUNNING
        
        # Complete execution
        execution.complete()
        
        # Cannot resume when completed
        with pytest.raises(InvalidStateTransition) as exc_info:
            execution.resume()
        assert exc_info.value.attempted_action == "resume"
    
    def test_execution_state_modification_only_when_paused(self):
        """Test that state can only be modified when paused."""
        execution = Execution(workflow_id="wf-1")
        
        # Cannot modify in PENDING state
        with pytest.raises(InvalidStateTransition):
            execution.apply_state_modification({"key": "value"})
        
        # Start and pause
        execution.start()
        execution.pause()
        
        # Can modify when PAUSED
        execution.apply_state_modification({"key": "value"})
        assert execution.get_variable("key") == "value"


class TestIntegrityCheckerIntegration:
    """Integration tests for IntegrityChecker with real database."""
    
    @pytest.fixture
    def db_url(self, tmp_path):
        """Create a temporary SQLite database."""
        db_file = tmp_path / "test_wtb.db"
        return f"sqlite:///{db_file}"
    
    def test_check_healthy_database(self, db_url):
        """Test checking a healthy database with no issues."""
        from wtb.infrastructure.integrity.checker import IntegrityChecker
        
        # Create empty database (healthy)
        with SQLAlchemyUnitOfWork(db_url) as uow:
            uow.commit()
        
        checker = IntegrityChecker(wtb_db_url=db_url)
        report = checker.check()
        
        assert report.is_healthy
    
    def test_detect_stuck_outbox_events(self, db_url):
        """Test detecting stuck outbox events."""
        from wtb.infrastructure.integrity.checker import IntegrityChecker
        
        # Create database with old pending events
        with SQLAlchemyUnitOfWork(db_url) as uow:
            # Old event (stuck)
            old_event = OutboxEvent(
                event_type=OutboxEventType.CHECKPOINT_VERIFY,
                aggregate_type="Execution",
                aggregate_id="exec-1"
            )
            old_event.created_at = datetime.now() - timedelta(hours=3)
            uow.outbox.add(old_event)
            
            # Recent event (not stuck)
            recent_event = OutboxEvent(
                event_type=OutboxEventType.CHECKPOINT_VERIFY,
                aggregate_type="Execution",
                aggregate_id="exec-2"
            )
            uow.outbox.add(recent_event)
            
            uow.commit()
        
        checker = IntegrityChecker(
            wtb_db_url=db_url,
            outbox_stuck_threshold_hours=1.0
        )
        
        report = checker.check()
        
        stuck_issues = report.get_issues_by_type(IntegrityIssueType.OUTBOX_STUCK)
        assert len(stuck_issues) == 1  # Only the old event
    
    def test_detect_failed_outbox_events(self, db_url):
        """Test detecting failed outbox events that exhausted retries."""
        from wtb.infrastructure.integrity.checker import IntegrityChecker
        
        with SQLAlchemyUnitOfWork(db_url) as uow:
            # Exhausted event
            exhausted = OutboxEvent(
                event_type=OutboxEventType.FILE_COMMIT_VERIFY,
                aggregate_type="Execution",
                aggregate_id="exec-1",
                status=OutboxStatus.FAILED,
                retry_count=5,
                max_retries=5,
                last_error="Connection refused"
            )
            uow.outbox.add(exhausted)
            uow.commit()
        
        checker = IntegrityChecker(wtb_db_url=db_url)
        report = checker.check()
        
        stuck = report.get_issues_by_type(IntegrityIssueType.OUTBOX_STUCK)
        critical_stuck = [i for i in stuck if i.severity == IntegritySeverity.CRITICAL]
        
        assert len(critical_stuck) >= 1


class TestEndToEndConsistency:
    """End-to-end tests for cross-database consistency."""
    
    def test_complete_node_execution_flow(self):
        """Test complete flow: execution → checkpoint → outbox → verify."""
        uow = InMemoryUnitOfWork()
        
        # Step 1: Create execution
        execution = Execution(
            workflow_id="wf-ml-pipeline",
            state=ExecutionState(current_node_id="train")
        )
        
        with uow:
            uow.executions.add(execution)
            uow.commit()
        
        # Step 2: Simulate node execution with checkpoint
        checkpoint_id = 42  # Would come from AgentGit
        
        with uow:
            # Create node boundary
            boundary = NodeBoundary(
                execution_id=execution.id,
                internal_session_id=1,
                node_id="train",
                entry_checkpoint_id=checkpoint_id,
                node_status="started"
            )
            uow.node_boundaries.add(boundary)
            
            # Create outbox event for verification
            event = OutboxEvent.create_checkpoint_verify(
                execution_id=execution.id,
                checkpoint_id=checkpoint_id,
                node_id="train",
                internal_session_id=1,
                is_entry=True
            )
            uow.outbox.add(event)
            
            uow.commit()
        
        # Step 3: Update domain model
        execution.start()
        execution.record_node_result("train", {"model_path": "/models/v1"})
        
        # Step 4: Complete node with exit checkpoint
        exit_checkpoint_id = 43
        
        with uow:
            boundary = uow.node_boundaries.find_by_node(1, "train")
            boundary.exit_checkpoint_id = exit_checkpoint_id
            boundary.node_status = "completed"
            boundary.completed_at = datetime.now()
            uow.node_boundaries.update(boundary)
            
            # Another outbox event for exit
            exit_event = OutboxEvent.create_checkpoint_verify(
                execution_id=execution.id,
                checkpoint_id=exit_checkpoint_id,
                node_id="train",
                internal_session_id=1,
                is_exit=True
            )
            uow.outbox.add(exit_event)
            
            uow.commit()
        
        # Verify final state
        boundaries = uow.node_boundaries.find_by_session(1)
        events = uow.outbox.list_all()
        
        assert len(boundaries) == 1
        assert boundaries[0].node_status == "completed"
        assert boundaries[0].exit_checkpoint_id == exit_checkpoint_id
        assert len(events) == 2  # entry + exit
    
    def test_file_tracking_with_checkpoint(self):
        """Test file tracking integration with checkpoints."""
        uow = InMemoryUnitOfWork()
        
        # Simulate saving model file with checkpoint
        checkpoint_id = 100
        file_commit_id = "commit-model-v1"
        
        with uow:
            # Link checkpoint to file commit
            link = CheckpointFile(
                checkpoint_id=checkpoint_id,
                file_commit_id=file_commit_id,
                file_count=1,
                total_size_bytes=50 * 1024 * 1024  # 50MB
            )
            uow.checkpoint_files.add(link)
            
            # Outbox event for file verification
            event = OutboxEvent.create_file_commit_verify(
                execution_id="exec-1",
                checkpoint_id=checkpoint_id,
                file_commit_id=file_commit_id,
                node_id="save_model"
            )
            uow.outbox.add(event)
            
            uow.commit()
        
        # Verify
        link = uow.checkpoint_files.find_by_checkpoint(checkpoint_id)
        assert link is not None
        assert link.file_commit_id == file_commit_id
        
        events = uow.outbox.get_pending()
        assert len(events) == 1
        assert events[0].event_type == OutboxEventType.FILE_COMMIT_VERIFY

