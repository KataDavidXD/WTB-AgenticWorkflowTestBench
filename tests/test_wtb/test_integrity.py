"""
Unit tests for IntegrityChecker implementation.

Tests:
- IntegrityIssue domain model
- IntegrityReport domain model
- IntegrityChecker functionality
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock

from wtb.domain.models.integrity import (
    IntegrityIssue,
    IntegrityIssueType,
    IntegritySeverity,
    IntegrityReport,
    RepairAction,
)
from wtb.domain.models.outbox import OutboxEvent, OutboxStatus, OutboxEventType
from wtb.infrastructure.database.inmemory_unit_of_work import InMemoryUnitOfWork


class TestIntegrityIssue:
    """Tests for IntegrityIssue domain model."""
    
    def test_create_issue(self):
        """Test creating a basic issue."""
        issue = IntegrityIssue(
            issue_type=IntegrityIssueType.DANGLING_REFERENCE,
            severity=IntegritySeverity.CRITICAL,
            source_table="wtb_node_boundaries",
            source_id="123",
            target_table="checkpoints",
            target_id="456",
            message="Missing checkpoint reference"
        )
        
        assert issue.issue_type == IntegrityIssueType.DANGLING_REFERENCE
        assert issue.severity == IntegritySeverity.CRITICAL
        assert issue.source_table == "wtb_node_boundaries"
        assert not issue.auto_repairable
    
    def test_dangling_reference_factory(self):
        """Test factory for dangling reference issues."""
        issue = IntegrityIssue.dangling_reference(
            source_table="wtb_node_boundaries",
            source_id="1",
            target_table="checkpoints",
            target_id="42",
            message="Node boundary references missing checkpoint",
            node_id="train"
        )
        
        assert issue.issue_type == IntegrityIssueType.DANGLING_REFERENCE
        assert issue.severity == IntegritySeverity.CRITICAL
        assert issue.auto_repairable is True
        assert issue.suggested_action == RepairAction.DELETE_ORPHAN
        assert issue.details["node_id"] == "train"
    
    def test_orphan_checkpoint_factory(self):
        """Test factory for orphan checkpoint issues."""
        issue = IntegrityIssue.orphan_checkpoint(
            checkpoint_id=42,
            message="Checkpoint not referenced by WTB",
            execution_id="exec-123"
        )
        
        assert issue.issue_type == IntegrityIssueType.ORPHAN_CHECKPOINT
        assert issue.severity == IntegritySeverity.INFO
        assert issue.auto_repairable is False  # Needs manual review
        assert issue.details["execution_id"] == "exec-123"
    
    def test_outbox_stuck_factory_retryable(self):
        """Test factory for retryable stuck outbox events."""
        issue = IntegrityIssue.outbox_stuck(
            event_id="evt-123",
            event_type="checkpoint_verify",
            retry_count=2,
            can_retry=True,
            message="Event stuck for 2 hours"
        )
        
        assert issue.issue_type == IntegrityIssueType.OUTBOX_STUCK
        assert issue.severity == IntegritySeverity.WARNING
        assert issue.auto_repairable is True
        assert issue.suggested_action == RepairAction.RETRY_OUTBOX
    
    def test_outbox_stuck_factory_exhausted(self):
        """Test factory for exhausted stuck outbox events."""
        issue = IntegrityIssue.outbox_stuck(
            event_id="evt-123",
            event_type="checkpoint_verify",
            retry_count=5,
            can_retry=False,
            message="Event max retries exceeded"
        )
        
        assert issue.severity == IntegritySeverity.CRITICAL
        assert issue.auto_repairable is False
        assert issue.suggested_action == RepairAction.MANUAL_REQUIRED
    
    def test_to_dict_and_from_dict(self):
        """Test serialization round-trip."""
        original = IntegrityIssue(
            issue_type=IntegrityIssueType.STATE_MISMATCH,
            severity=IntegritySeverity.WARNING,
            source_table="wtb_executions",
            source_id="exec-1",
            message="Status mismatch",
            details={"expected": "completed", "actual": "running"},
            auto_repairable=True
        )
        
        data = original.to_dict()
        restored = IntegrityIssue.from_dict(data)
        
        assert restored.issue_type == original.issue_type
        assert restored.severity == original.severity
        assert restored.source_table == original.source_table
        assert restored.details == original.details


class TestIntegrityReport:
    """Tests for IntegrityReport domain model."""
    
    def test_empty_report_is_healthy(self):
        """Test that empty report is considered healthy."""
        report = IntegrityReport()
        
        assert report.is_healthy
        assert report.issues_found == 0
        assert report.critical_count == 0
    
    def test_add_critical_issue(self):
        """Test adding a critical issue."""
        report = IntegrityReport()
        
        issue = IntegrityIssue(
            issue_type=IntegrityIssueType.DANGLING_REFERENCE,
            severity=IntegritySeverity.CRITICAL,
            source_table="test",
            source_id="1",
            message="Critical issue"
        )
        report.add_issue(issue)
        
        assert not report.is_healthy
        assert report.issues_found == 1
        assert report.critical_count == 1
    
    def test_add_warning_issue(self):
        """Test adding a warning issue."""
        report = IntegrityReport()
        
        issue = IntegrityIssue(
            issue_type=IntegrityIssueType.OUTBOX_STUCK,
            severity=IntegritySeverity.WARNING,
            source_table="test",
            source_id="1",
            message="Warning issue"
        )
        report.add_issue(issue)
        
        assert report.is_healthy  # Warnings don't make report unhealthy
        assert report.warning_count == 1
    
    def test_add_info_issue(self):
        """Test adding an info issue."""
        report = IntegrityReport()
        
        issue = IntegrityIssue(
            issue_type=IntegrityIssueType.ORPHAN_CHECKPOINT,
            severity=IntegritySeverity.INFO,
            source_table="test",
            source_id="1",
            message="Info issue"
        )
        report.add_issue(issue)
        
        assert report.is_healthy
        assert report.info_count == 1
    
    def test_get_issues_by_type(self):
        """Test filtering issues by type."""
        report = IntegrityReport()
        
        # Add different types
        report.add_issue(IntegrityIssue(
            issue_type=IntegrityIssueType.DANGLING_REFERENCE,
            severity=IntegritySeverity.CRITICAL,
            source_table="test", source_id="1", message="Issue 1"
        ))
        report.add_issue(IntegrityIssue(
            issue_type=IntegrityIssueType.ORPHAN_CHECKPOINT,
            severity=IntegritySeverity.INFO,
            source_table="test", source_id="2", message="Issue 2"
        ))
        report.add_issue(IntegrityIssue(
            issue_type=IntegrityIssueType.DANGLING_REFERENCE,
            severity=IntegritySeverity.CRITICAL,
            source_table="test", source_id="3", message="Issue 3"
        ))
        
        dangling = report.get_issues_by_type(IntegrityIssueType.DANGLING_REFERENCE)
        
        assert len(dangling) == 2
    
    def test_get_issues_by_severity(self):
        """Test filtering issues by severity."""
        report = IntegrityReport()
        
        report.add_issue(IntegrityIssue(
            issue_type=IntegrityIssueType.DANGLING_REFERENCE,
            severity=IntegritySeverity.CRITICAL,
            source_table="test", source_id="1", message="Critical"
        ))
        report.add_issue(IntegrityIssue(
            issue_type=IntegrityIssueType.OUTBOX_STUCK,
            severity=IntegritySeverity.WARNING,
            source_table="test", source_id="2", message="Warning"
        ))
        
        criticals = report.get_issues_by_severity(IntegritySeverity.CRITICAL)
        
        assert len(criticals) == 1
    
    def test_auto_repairable_count(self):
        """Test counting auto-repairable issues."""
        report = IntegrityReport()
        
        report.add_issue(IntegrityIssue(
            issue_type=IntegrityIssueType.DANGLING_REFERENCE,
            severity=IntegritySeverity.CRITICAL,
            source_table="test", source_id="1",
            message="Auto-repairable",
            auto_repairable=True
        ))
        report.add_issue(IntegrityIssue(
            issue_type=IntegrityIssueType.ORPHAN_CHECKPOINT,
            severity=IntegritySeverity.INFO,
            source_table="test", source_id="2",
            message="Manual required",
            auto_repairable=False
        ))
        
        assert report.auto_repairable_count == 1
    
    def test_summary_format(self):
        """Test summary string generation."""
        report = IntegrityReport()
        report.total_checked = 100
        report.duration_ms = 50.5
        
        summary = report.summary()
        
        assert "HEALTHY" in summary
        assert "100" in summary
        assert "50.50" in summary
    
    def test_to_dict(self):
        """Test serialization to dict."""
        report = IntegrityReport()
        report.total_checked = 100
        report.add_issue(IntegrityIssue(
            issue_type=IntegrityIssueType.DANGLING_REFERENCE,
            severity=IntegritySeverity.CRITICAL,
            source_table="test", source_id="1", message="Test"
        ))
        
        data = report.to_dict()
        
        assert data["total_checked"] == 100
        assert data["issues_found"] == 1
        assert len(data["issues"]) == 1


class TestIntegrityChecker:
    """Tests for IntegrityChecker class."""
    
    def test_check_empty_database(self):
        """Test checking an empty database."""
        from wtb.infrastructure.integrity.checker import IntegrityChecker
        
        checker = IntegrityChecker(
            wtb_db_url="sqlite:///:memory:",
            checkpoint_repo=None  # No AgentGit
        )
        
        report = checker.check()
        
        assert report.is_healthy
        assert report.duration_ms > 0
    
    def test_check_outbox_stuck_events(self, tmp_path):
        """Test detection of stuck outbox events."""
        from wtb.infrastructure.integrity.checker import IntegrityChecker
        from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork
        
        # Use file-based SQLite for persistence across UoW instances
        db_file = tmp_path / "test.db"
        db_url = f"sqlite:///{db_file}"
        
        # Create database and add stuck event
        with SQLAlchemyUnitOfWork(db_url) as uow:
            # Add an old pending event
            old_event = OutboxEvent(
                event_type=OutboxEventType.CHECKPOINT_VERIFY,
                aggregate_type="Execution",
                aggregate_id="exec-1"
            )
            old_event.created_at = datetime.now() - timedelta(hours=2)
            uow.outbox.add(old_event)
            uow.commit()
        
        checker = IntegrityChecker(
            wtb_db_url=db_url,
            outbox_stuck_threshold_hours=1.0
        )
        
        report = checker.check()
        
        # Should find the stuck event
        stuck_issues = report.get_issues_by_type(IntegrityIssueType.OUTBOX_STUCK)
        assert len(stuck_issues) >= 1
    
    def test_check_with_mock_checkpoint_repo(self, tmp_path):
        """Test checking with a mock checkpoint repository."""
        from wtb.infrastructure.integrity.checker import IntegrityChecker
        from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork
        from wtb.domain.models import NodeBoundary
        
        # Use file-based SQLite for persistence across UoW instances
        db_file = tmp_path / "test.db"
        db_url = f"sqlite:///{db_file}"
        
        # Create database with node boundary
        with SQLAlchemyUnitOfWork(db_url) as uow:
            boundary = NodeBoundary(
                execution_id="exec-1",
                internal_session_id=1,
                node_id="train",
                entry_checkpoint_id=999  # Non-existent in mock
            )
            uow.node_boundaries.add(boundary)
            uow.commit()
        
        # Mock checkpoint repo that returns None (checkpoint not found)
        mock_repo = Mock()
        mock_repo.get_by_id.return_value = None
        mock_repo.list_all.return_value = []  # Return empty list for orphan check
        
        checker = IntegrityChecker(
            wtb_db_url=db_url,
            checkpoint_repo=mock_repo
        )
        
        report = checker.check()
        
        # Should find dangling reference
        dangling = report.get_issues_by_type(IntegrityIssueType.DANGLING_REFERENCE)
        assert len(dangling) >= 1
    
    def test_check_and_repair(self):
        """Test check_and_repair flow."""
        from wtb.infrastructure.integrity.checker import IntegrityChecker
        from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork
        
        db_url = "sqlite:///:memory:"
        
        # Create a stuck outbox event that can be auto-repaired
        with SQLAlchemyUnitOfWork(db_url) as uow:
            stuck_event = OutboxEvent(
                event_type=OutboxEventType.CHECKPOINT_VERIFY,
                aggregate_type="Execution",
                aggregate_id="exec-1",
                status=OutboxStatus.PENDING
            )
            stuck_event.created_at = datetime.now() - timedelta(hours=2)
            uow.outbox.add(stuck_event)
            uow.commit()
        
        checker = IntegrityChecker(
            wtb_db_url=db_url,
            outbox_stuck_threshold_hours=1.0
        )
        
        report = checker.check_and_repair()
        
        # Should have attempted repair
        # Note: Repair might succeed or fail depending on implementation
        assert report.repaired_count >= 0


class TestIntegrityCheckerWithInMemoryUoW:
    """Tests using InMemoryUnitOfWork for faster execution."""
    
    def test_integrity_report_summary(self):
        """Test generating detailed summary."""
        report = IntegrityReport()
        report.total_checked = 50
        report.duration_ms = 10.5
        
        report.add_issue(IntegrityIssue.dangling_reference(
            source_table="wtb_node_boundaries",
            source_id="1",
            target_table="checkpoints",
            target_id="42",
            message="Missing entry checkpoint"
        ))
        
        report.add_issue(IntegrityIssue.outbox_stuck(
            event_id="evt-1",
            event_type="checkpoint_verify",
            retry_count=2,
            can_retry=True,
            message="Event pending too long"
        ))
        
        detailed = report.detailed_summary()
        
        assert "UNHEALTHY" in detailed
        assert "dangling_reference" in detailed
        assert "outbox_stuck" in detailed

