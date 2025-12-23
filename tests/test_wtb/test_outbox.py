"""
Unit tests for Outbox Pattern implementation.

Tests:
- OutboxEvent domain model
- InMemoryOutboxRepository
- OutboxProcessor
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from wtb.domain.models.outbox import (
    OutboxEvent,
    OutboxEventType,
    OutboxStatus,
)
from wtb.infrastructure.database.inmemory_unit_of_work import (
    InMemoryUnitOfWork,
    InMemoryOutboxRepository,
)


class TestOutboxEvent:
    """Tests for OutboxEvent domain model."""
    
    def test_create_default_event(self):
        """Test creating event with defaults."""
        event = OutboxEvent()
        
        assert event.id is None
        assert event.event_id is not None  # UUID generated
        assert event.event_type == OutboxEventType.CHECKPOINT_CREATE
        assert event.status == OutboxStatus.PENDING
        assert event.retry_count == 0
        assert event.max_retries == 5
    
    def test_create_checkpoint_verify_event(self):
        """Test factory method for checkpoint verification."""
        event = OutboxEvent.create_checkpoint_verify(
            execution_id="exec-123",
            checkpoint_id=42,
            node_id="train",
            internal_session_id=1,
            is_entry=True,
            is_exit=False
        )
        
        assert event.event_type == OutboxEventType.CHECKPOINT_VERIFY
        assert event.aggregate_type == "Execution"
        assert event.aggregate_id == "exec-123"
        assert event.payload["checkpoint_id"] == 42
        assert event.payload["node_id"] == "train"
        assert event.payload["is_entry"] is True
    
    def test_create_file_commit_verify_event(self):
        """Test factory method for file commit verification."""
        event = OutboxEvent.create_file_commit_verify(
            execution_id="exec-123",
            checkpoint_id=42,
            file_commit_id="fc-abc",
            node_id="save_model"
        )
        
        assert event.event_type == OutboxEventType.FILE_COMMIT_VERIFY
        assert event.payload["file_commit_id"] == "fc-abc"
    
    def test_can_retry_pending(self):
        """Test can_retry for pending event."""
        event = OutboxEvent(status=OutboxStatus.PENDING, retry_count=0)
        assert event.can_retry() is True
    
    def test_can_retry_failed_under_max(self):
        """Test can_retry for failed event under max retries."""
        event = OutboxEvent(
            status=OutboxStatus.FAILED,
            retry_count=3,
            max_retries=5
        )
        assert event.can_retry() is True
    
    def test_cannot_retry_max_exceeded(self):
        """Test can_retry returns False when max exceeded."""
        event = OutboxEvent(
            status=OutboxStatus.FAILED,
            retry_count=5,
            max_retries=5
        )
        assert event.can_retry() is False
    
    def test_cannot_retry_processed(self):
        """Test can_retry returns False for processed events."""
        event = OutboxEvent(status=OutboxStatus.PROCESSED)
        assert event.can_retry() is False
    
    def test_mark_processing(self):
        """Test marking event as processing."""
        event = OutboxEvent(status=OutboxStatus.PENDING)
        event.mark_processing()
        
        assert event.status == OutboxStatus.PROCESSING
    
    def test_mark_processed(self):
        """Test marking event as processed."""
        event = OutboxEvent(status=OutboxStatus.PROCESSING)
        event.mark_processed()
        
        assert event.status == OutboxStatus.PROCESSED
        assert event.processed_at is not None
    
    def test_mark_failed(self):
        """Test marking event as failed."""
        event = OutboxEvent(status=OutboxStatus.PROCESSING, retry_count=0)
        event.mark_failed("Connection timeout")
        
        assert event.status == OutboxStatus.FAILED
        assert event.retry_count == 1
        assert event.last_error == "Connection timeout"
    
    def test_reset_for_retry(self):
        """Test resetting failed event for manual retry."""
        event = OutboxEvent(
            status=OutboxStatus.FAILED,
            retry_count=5,
            last_error="Some error"
        )
        event.reset_for_retry()
        
        assert event.status == OutboxStatus.PENDING
        assert event.retry_count == 0
        assert event.last_error is None
    
    def test_to_dict_and_from_dict(self):
        """Test serialization round-trip."""
        original = OutboxEvent(
            event_type=OutboxEventType.NODE_BOUNDARY_SYNC,
            aggregate_type="NodeBoundary",
            aggregate_id="nb-123",
            payload={"node_id": "train", "exit_checkpoint_id": 42},
            status=OutboxStatus.PENDING
        )
        
        data = original.to_dict()
        restored = OutboxEvent.from_dict(data)
        
        assert restored.event_type == original.event_type
        assert restored.aggregate_type == original.aggregate_type
        assert restored.aggregate_id == original.aggregate_id
        assert restored.payload == original.payload
        assert restored.status == original.status


class TestInMemoryOutboxRepository:
    """Tests for InMemoryOutboxRepository."""
    
    @pytest.fixture
    def repo(self):
        """Fresh repository for each test."""
        return InMemoryOutboxRepository()
    
    def test_add_event(self, repo):
        """Test adding an event."""
        event = OutboxEvent(
            event_type=OutboxEventType.CHECKPOINT_VERIFY,
            aggregate_type="Execution",
            aggregate_id="exec-1"
        )
        
        saved = repo.add(event)
        
        assert saved.id is not None
        assert saved.id == 1
    
    def test_get_by_id(self, repo):
        """Test getting event by event_id."""
        event = OutboxEvent(event_id="test-uuid-123")
        repo.add(event)
        
        found = repo.get_by_id("test-uuid-123")
        
        assert found is not None
        assert found.event_id == "test-uuid-123"
    
    def test_get_by_pk(self, repo):
        """Test getting event by database ID."""
        event = OutboxEvent()
        saved = repo.add(event)
        
        found = repo.get_by_pk(saved.id)
        
        assert found is not None
        assert found.id == saved.id
    
    def test_get_pending(self, repo):
        """Test getting pending events."""
        # Add events with different statuses
        pending1 = OutboxEvent(status=OutboxStatus.PENDING)
        pending2 = OutboxEvent(status=OutboxStatus.PENDING)
        processed = OutboxEvent(status=OutboxStatus.PROCESSED)
        
        repo.add(pending1)
        repo.add(pending2)
        repo.add(processed)
        
        pending = repo.get_pending()
        
        assert len(pending) == 2
        assert all(e.status == OutboxStatus.PENDING for e in pending)
    
    def test_get_pending_ordered_by_created_at(self, repo):
        """Test pending events are ordered by creation time."""
        now = datetime.now()
        
        event1 = OutboxEvent(status=OutboxStatus.PENDING)
        event1.created_at = now - timedelta(hours=2)
        
        event2 = OutboxEvent(status=OutboxStatus.PENDING)
        event2.created_at = now - timedelta(hours=1)
        
        event3 = OutboxEvent(status=OutboxStatus.PENDING)
        event3.created_at = now
        
        # Add in wrong order
        repo.add(event3)
        repo.add(event1)
        repo.add(event2)
        
        pending = repo.get_pending()
        
        # Should be ordered oldest first
        assert pending[0].id == event1.id
        assert pending[1].id == event2.id
        assert pending[2].id == event3.id
    
    def test_get_failed_for_retry(self, repo):
        """Test getting failed events that can be retried."""
        # Retryable failed event
        retryable = OutboxEvent(
            status=OutboxStatus.FAILED,
            retry_count=2,
            max_retries=5
        )
        
        # Non-retryable failed event (max reached)
        exhausted = OutboxEvent(
            status=OutboxStatus.FAILED,
            retry_count=5,
            max_retries=5
        )
        
        repo.add(retryable)
        repo.add(exhausted)
        
        failed = repo.get_failed_for_retry()
        
        assert len(failed) == 1
        assert failed[0].id == retryable.id
    
    def test_update_event(self, repo):
        """Test updating event status."""
        event = OutboxEvent(status=OutboxStatus.PENDING)
        saved = repo.add(event)
        
        # Update status
        saved.status = OutboxStatus.PROCESSED
        saved.processed_at = datetime.now()
        repo.update(saved)
        
        # Verify update
        found = repo.get_by_pk(saved.id)
        assert found.status == OutboxStatus.PROCESSED
    
    def test_delete_processed(self, repo):
        """Test deleting old processed events."""
        now = datetime.now()
        
        # Old processed event
        old_processed = OutboxEvent(status=OutboxStatus.PROCESSED)
        old_processed.processed_at = now - timedelta(days=10)
        
        # Recent processed event
        recent_processed = OutboxEvent(status=OutboxStatus.PROCESSED)
        recent_processed.processed_at = now - timedelta(hours=1)
        
        # Pending event (should not be deleted)
        pending = OutboxEvent(status=OutboxStatus.PENDING)
        
        repo.add(old_processed)
        repo.add(recent_processed)
        repo.add(pending)
        
        # Delete events processed before 7 days ago
        cutoff = now - timedelta(days=7)
        deleted = repo.delete_processed(before=cutoff)
        
        assert deleted == 1
        assert len(repo.list_all()) == 2
    
    def test_list_all(self, repo):
        """Test listing all events."""
        for i in range(5):
            repo.add(OutboxEvent())
        
        all_events = repo.list_all()
        
        assert len(all_events) == 5


class TestOutboxWithUnitOfWork:
    """Tests for Outbox integration with UnitOfWork."""
    
    def test_uow_has_outbox_repository(self):
        """Test that UoW includes outbox repository."""
        uow = InMemoryUnitOfWork()
        
        assert hasattr(uow, 'outbox')
        assert uow.outbox is not None
    
    def test_atomic_business_data_and_outbox(self):
        """Test that business data and outbox are written atomically."""
        uow = InMemoryUnitOfWork()
        
        with uow:
            # Simulate adding business data
            from wtb.domain.models import NodeBoundary
            boundary = NodeBoundary(
                execution_id="exec-1",
                internal_session_id=1,
                node_id="train",
                entry_checkpoint_id=42
            )
            uow.node_boundaries.add(boundary)
            
            # Add outbox event in same transaction
            event = OutboxEvent.create_checkpoint_verify(
                execution_id="exec-1",
                checkpoint_id=42,
                node_id="train",
                internal_session_id=1,
                is_entry=True
            )
            uow.outbox.add(event)
            
            uow.commit()
        
        # Verify both were saved
        boundaries = uow.node_boundaries.list()
        events = uow.outbox.list_all()
        
        assert len(boundaries) == 1
        assert len(events) == 1


class TestOutboxProcessor:
    """Tests for OutboxProcessor."""
    
    def test_processor_init(self):
        """Test processor initialization."""
        from wtb.infrastructure.outbox.processor import OutboxProcessor
        
        processor = OutboxProcessor(
            wtb_db_url="sqlite:///:memory:",
            poll_interval_seconds=0.1,
            batch_size=10
        )
        
        assert processor._poll_interval == 0.1
        assert processor._batch_size == 10
        assert not processor.is_running()
    
    def test_processor_lifecycle(self):
        """Test processor start/stop lifecycle."""
        from wtb.infrastructure.outbox.processor import OutboxProcessor
        
        processor = OutboxProcessor(
            wtb_db_url="sqlite:///:memory:",
            poll_interval_seconds=0.01
        )
        
        # Start
        processor.start()
        assert processor.is_running()
        
        # Stop
        processor.stop(timeout=1.0)
        assert not processor.is_running()
    
    def test_processor_double_start(self):
        """Test that double start is handled gracefully."""
        from wtb.infrastructure.outbox.processor import OutboxProcessor
        
        processor = OutboxProcessor(wtb_db_url="sqlite:///:memory:")
        
        processor.start()
        processor.start()  # Should not raise
        
        processor.stop()

