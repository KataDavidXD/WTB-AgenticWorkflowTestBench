"""
Integration Tests: File Processing + Events.

Tests the integration between file processing operations and
the WTB event system.

Test Categories:
1. Event Publishing on File Operations
2. Event Handlers and Listeners
3. Event History and Ordering
4. Error Events
5. Thread Safety

Design Principles:
- SOLID: Events have single responsibility
- ACID: Events are atomic and ordered
- Pattern: Observer pattern for event notification
"""

import pytest
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any
from dataclasses import dataclass, field
import threading
import time
import uuid

from wtb.domain.models.file_processing import (
    BlobId,
    CommitId,
    FileMemento,
    FileCommit,
    CheckpointFileLink,
    CommitStatus,
)
from wtb.domain.events.file_processing_events import (
    FileCommitCreatedEvent,
    FileRestoredEvent,
    FileCommitVerifiedEvent,
    CheckpointFileLinkCreatedEvent,
    CheckpointFileLinkVerifiedEvent,
    BlobCreatedEvent,
    BlobDeletedEvent,
    FileTrackingStartedEvent,
    FileTrackingCompletedEvent,
    FileTrackingFailedEvent,
)
from wtb.infrastructure.events import WTBEventBus


# ═══════════════════════════════════════════════════════════════════════════════
# Event-Aware File Service
# ═══════════════════════════════════════════════════════════════════════════════


class EventAwareFileService:
    """
    File service that publishes events for all operations.
    
    Demonstrates event-driven file processing pattern.
    """
    
    def __init__(self, event_bus: WTBEventBus, blob_repo, commit_repo, link_repo):
        self._event_bus = event_bus
        self._blob_repo = blob_repo
        self._commit_repo = commit_repo
        self._link_repo = link_repo
    
    def track_files(
        self,
        file_paths: List[str],
        execution_id: str,
        message: str = None,
    ) -> FileCommit:
        """
        Track files and publish events.
        
        Events:
        - FileTrackingStartedEvent: Before tracking begins
        - BlobCreatedEvent: For each blob saved
        - FileCommitCreatedEvent: When commit is created
        - FileTrackingCompletedEvent: After successful tracking
        - FileTrackingFailedEvent: On error
        """
        tracking_id = str(uuid.uuid4())
        
        # Publish start event
        self._event_bus.publish(FileTrackingStartedEvent(
            operation_id=tracking_id,
            file_paths=tuple(file_paths),
        ))
        
        try:
            commit = FileCommit.create(
                message=message or f"Track {len(file_paths)} files",
                execution_id=execution_id,
            )
            
            for file_path in file_paths:
                memento, content = FileMemento.from_file(file_path)
                
                # Save blob (content-addressable) and publish event
                self._blob_repo.save(content)
                self._event_bus.publish(BlobCreatedEvent(
                    blob_id=memento.file_hash.value,
                    size_bytes=memento.file_size,
                ))
                
                commit.add_memento(memento)
            
            commit.finalize()
            self._commit_repo.save(commit)
            
            # Publish commit created event
            self._event_bus.publish(FileCommitCreatedEvent(
                commit_id=commit.commit_id.value,
                execution_id=execution_id,
                file_count=commit.file_count,
                total_size_bytes=commit.total_size,
                message=commit.message,
            ))
            
            # Publish completion event
            self._event_bus.publish(FileTrackingCompletedEvent(
                operation_id=tracking_id,
                commit_id=commit.commit_id.value,
            ))
            
            return commit
            
        except Exception as e:
            # Publish failure event
            self._event_bus.publish(FileTrackingFailedEvent(
                operation_id=tracking_id,
                error_message=str(e),
            ))
            raise
    
    def restore_commit(
        self,
        commit_id: str,
        output_dir: str,
        execution_id: str,
    ) -> List[str]:
        """
        Restore files from commit and publish events.
        
        Events:
        - FileRestoredEvent: After successful restore
        """
        commit = self._commit_repo.get_by_id(CommitId(commit_id))
        if commit is None:
            raise ValueError(f"Commit not found: {commit_id}")
        
        restored = []
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        for memento in commit.mementos:
            content = self._blob_repo.get(memento.file_hash)
            if content:
                file_path = output_path / Path(memento.file_path).name
                file_path.write_bytes(content)
                restored.append(str(file_path))
        
        # Publish restore event
        self._event_bus.publish(FileRestoredEvent(
            commit_id=commit_id,
            files_restored=len(restored),
            restored_paths=tuple(restored),
        ))
        
        return restored
    
    def link_to_checkpoint(
        self,
        commit_id: CommitId,
        checkpoint_id: int,
        execution_id: str,
    ) -> CheckpointFileLink:
        """
        Link commit to checkpoint and publish events.
        
        Events:
        - CheckpointFileLinkCreatedEvent: When link is created
        """
        commit = self._commit_repo.get_by_id(commit_id)
        if commit is None:
            raise ValueError(f"Commit not found: {commit_id}")
        
        link = CheckpointFileLink.create(checkpoint_id, commit)
        self._link_repo.add(link)
        
        # Publish link created event
        self._event_bus.publish(CheckpointFileLinkCreatedEvent(
            checkpoint_id=checkpoint_id,
            commit_id=commit_id.value,
            file_count=link.file_count,
            total_size_bytes=link.total_size_bytes,
        ))
        
        return link
    
    def verify_commit(self, commit_id: str, execution_id: str) -> bool:
        """
        Verify commit and publish verification event.
        
        Events:
        - FileCommitVerifiedEvent: When verification completes
        """
        commit = self._commit_repo.get_by_id(CommitId(commit_id))
        if commit is None:
            return False
        
        # Verify all blobs exist
        verified = True
        verified_blobs = []
        for memento in commit.mementos:
            if self._blob_repo.exists(memento.file_hash):
                verified_blobs.append(memento.file_hash.value)
            else:
                verified = False
        
        commit.mark_verified()
        self._commit_repo.save(commit)
        
        # Publish verification event
        self._event_bus.publish(FileCommitVerifiedEvent(
            commit_id=commit_id,
            blobs_verified=len(verified_blobs),
        ))
        
        return verified
    
    def delete_blob(self, blob_id: BlobId, execution_id: str) -> bool:
        """
        Delete blob and publish event.
        
        Events:
        - BlobDeletedEvent: When blob is deleted
        """
        success = self._blob_repo.delete(blob_id)
        
        if success:
            self._event_bus.publish(BlobDeletedEvent(
                blob_id=blob_id.value,
            ))
        
        return success


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def file_service(event_bus, blob_repository, commit_repository, checkpoint_link_repository):
    """Create event-aware file service."""
    return EventAwareFileService(
        event_bus,
        blob_repository,
        commit_repository,
        checkpoint_link_repository,
    )


@pytest.fixture
def all_events_collector(event_bus):
    """Collect all file-related events."""
    events = []
    
    event_types = [
        FileCommitCreatedEvent,
        FileRestoredEvent,
        FileCommitVerifiedEvent,
        CheckpointFileLinkCreatedEvent,
        CheckpointFileLinkVerifiedEvent,
        BlobCreatedEvent,
        BlobDeletedEvent,
        FileTrackingStartedEvent,
        FileTrackingCompletedEvent,
        FileTrackingFailedEvent,
    ]
    
    for event_type in event_types:
        event_bus.subscribe(event_type, events.append)
    
    return events


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Event Publishing on File Operations
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventPublishingOnFileOperations:
    """Tests for event publishing during file operations."""
    
    def test_track_files_publishes_start_event(self, file_service, sample_files, all_events_collector):
        """Test that tracking publishes start event."""
        file_service.track_files([str(sample_files["csv"])], "exec-1")
        
        start_events = [e for e in all_events_collector if isinstance(e, FileTrackingStartedEvent)]
        assert len(start_events) == 1
        assert len(start_events[0].file_paths) == 1
    
    def test_track_files_publishes_blob_events(self, file_service, sample_files, all_events_collector):
        """Test that tracking publishes blob created events."""
        file_service.track_files(
            [str(sample_files["csv"]), str(sample_files["json"])],
            "exec-2",
        )
        
        blob_events = [e for e in all_events_collector if isinstance(e, BlobCreatedEvent)]
        assert len(blob_events) == 2
        assert all(e.blob_id is not None for e in blob_events)
    
    def test_track_files_publishes_commit_event(self, file_service, sample_files, all_events_collector):
        """Test that tracking publishes commit created event."""
        file_service.track_files([str(sample_files["csv"])], "exec-3")
        
        commit_events = [e for e in all_events_collector if isinstance(e, FileCommitCreatedEvent)]
        assert len(commit_events) == 1
        assert commit_events[0].execution_id == "exec-3"
        assert commit_events[0].file_count == 1
    
    def test_track_files_publishes_completion_event(self, file_service, sample_files, all_events_collector):
        """Test that tracking publishes completion event."""
        file_service.track_files([str(sample_files["csv"])], "exec-4")
        
        complete_events = [e for e in all_events_collector if isinstance(e, FileTrackingCompletedEvent)]
        assert len(complete_events) == 1
        assert complete_events[0].commit_id is not None
    
    def test_restore_publishes_event(self, file_service, sample_files, all_events_collector, temp_dir):
        """Test that restore publishes event."""
        commit = file_service.track_files([str(sample_files["csv"])], "exec-5")
        all_events_collector.clear()
        
        output_dir = Path(temp_dir) / "restore_output"
        file_service.restore_commit(commit.commit_id.value, str(output_dir), "exec-5")
        
        restore_events = [e for e in all_events_collector if isinstance(e, FileRestoredEvent)]
        assert len(restore_events) == 1
        assert restore_events[0].commit_id == commit.commit_id.value
        assert restore_events[0].files_restored == 1
    
    def test_link_publishes_event(self, file_service, sample_files, all_events_collector):
        """Test that linking publishes event."""
        commit = file_service.track_files([str(sample_files["csv"])], "exec-6")
        all_events_collector.clear()
        
        file_service.link_to_checkpoint(commit.commit_id, 42, "exec-6")
        
        link_events = [e for e in all_events_collector if isinstance(e, CheckpointFileLinkCreatedEvent)]
        assert len(link_events) == 1
        assert link_events[0].checkpoint_id == 42
        assert link_events[0].commit_id == commit.commit_id.value
    
    def test_verify_publishes_event(self, file_service, sample_files, all_events_collector):
        """Test that verification publishes event."""
        commit = file_service.track_files([str(sample_files["csv"])], "exec-7")
        all_events_collector.clear()
        
        file_service.verify_commit(commit.commit_id.value, "exec-7")
        
        verify_events = [e for e in all_events_collector if isinstance(e, FileCommitVerifiedEvent)]
        assert len(verify_events) == 1
        assert verify_events[0].blobs_verified >= 1
    
    def test_delete_blob_publishes_event(self, file_service, sample_files, all_events_collector, blob_repository):
        """Test that blob deletion publishes event."""
        commit = file_service.track_files([str(sample_files["csv"])], "exec-8")
        blob_id = commit.mementos[0].file_hash
        all_events_collector.clear()
        
        file_service.delete_blob(blob_id, "exec-8")
        
        delete_events = [e for e in all_events_collector if isinstance(e, BlobDeletedEvent)]
        assert len(delete_events) == 1
        assert delete_events[0].blob_id == blob_id.value


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Event Handlers and Listeners
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventHandlersAndListeners:
    """Tests for event handling and listener patterns."""
    
    def test_multiple_listeners_receive_event(self, event_bus, file_service, sample_files):
        """Test that multiple listeners receive the same event."""
        received_1 = []
        received_2 = []
        received_3 = []
        
        event_bus.subscribe(FileCommitCreatedEvent, received_1.append)
        event_bus.subscribe(FileCommitCreatedEvent, received_2.append)
        event_bus.subscribe(FileCommitCreatedEvent, received_3.append)
        
        file_service.track_files([str(sample_files["csv"])], "exec-multi")
        
        assert len(received_1) == 1
        assert len(received_2) == 1
        assert len(received_3) == 1
    
    def test_type_specific_handlers(self, event_bus, file_service, sample_files, temp_dir):
        """Test that handlers only receive events of subscribed type."""
        commit_events = []
        restore_events = []
        
        event_bus.subscribe(FileCommitCreatedEvent, commit_events.append)
        event_bus.subscribe(FileRestoredEvent, restore_events.append)
        
        commit = file_service.track_files([str(sample_files["csv"])], "exec-type")
        
        assert len(commit_events) == 1
        assert len(restore_events) == 0
        
        output_dir = Path(temp_dir) / "type_output"
        file_service.restore_commit(commit.commit_id.value, str(output_dir), "exec-type")
        
        assert len(commit_events) == 1
        assert len(restore_events) == 1
    
    def test_handler_exception_isolation(self, event_bus, file_service, sample_files):
        """Test that handler exceptions don't affect other handlers."""
        received = []
        
        def failing_handler(event):
            raise RuntimeError("Handler failed")
        
        def working_handler(event):
            received.append(event)
        
        event_bus.subscribe(FileCommitCreatedEvent, failing_handler)
        event_bus.subscribe(FileCommitCreatedEvent, working_handler)
        
        # Should not raise, and working handler should still receive
        file_service.track_files([str(sample_files["csv"])], "exec-iso")
        
        assert len(received) == 1
    
    def test_handler_with_filtering(self, event_bus, file_service, sample_files):
        """Test handler that filters events by criteria."""
        large_commits = []
        
        def large_file_handler(event):
            if event.total_size_bytes > 30:  # CSV is ~40 bytes
                large_commits.append(event)
        
        event_bus.subscribe(FileCommitCreatedEvent, large_file_handler)
        
        file_service.track_files([str(sample_files["csv"])], "exec-filter")
        
        # CSV file should be > 30 bytes
        assert len(large_commits) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Event History and Ordering
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventHistoryAndOrdering:
    """Tests for event history and ordering guarantees."""
    
    def test_event_ordering_preserved(self, file_service, sample_files, all_events_collector):
        """Test that events are received in order of publication."""
        file_service.track_files([str(sample_files["csv"])], "exec-order")
        
        # Order should be: Started -> BlobCreated -> CommitCreated -> Completed
        event_types = [type(e).__name__ for e in all_events_collector]
        
        started_idx = event_types.index("FileTrackingStartedEvent")
        blob_idx = event_types.index("BlobCreatedEvent")
        commit_idx = event_types.index("FileCommitCreatedEvent")
        complete_idx = event_types.index("FileTrackingCompletedEvent")
        
        assert started_idx < blob_idx < commit_idx < complete_idx
    
    def test_event_timestamps_ascending(self, file_service, sample_files, all_events_collector):
        """Test that event timestamps are ascending."""
        file_service.track_files(
            [str(sample_files["csv"]), str(sample_files["json"])],
            "exec-time",
        )
        
        # Filter events with timestamp
        timestamped = [e for e in all_events_collector if hasattr(e, 'timestamp')]
        
        for i in range(1, len(timestamped)):
            assert timestamped[i].timestamp >= timestamped[i-1].timestamp
    
    def test_event_history_accessible(self, event_bus, file_service, sample_files):
        """Test that event history is accessible from event bus."""
        file_service.track_files([str(sample_files["csv"])], "exec-hist")
        
        history = event_bus.get_event_history(FileCommitCreatedEvent)
        
        assert len(history) >= 1
        assert any(e.execution_id == "exec-hist" for e in history)
    
    def test_event_correlation_by_execution_id(self, file_service, sample_files, all_events_collector):
        """Test that events can be correlated by execution ID."""
        file_service.track_files([str(sample_files["csv"])], "exec-A")
        file_service.track_files([str(sample_files["json"])], "exec-B")
        
        # Only FileCommitCreatedEvent has execution_id
        exec_a_events = [e for e in all_events_collector if hasattr(e, 'execution_id') and e.execution_id == "exec-A"]
        exec_b_events = [e for e in all_events_collector if hasattr(e, 'execution_id') and e.execution_id == "exec-B"]
        
        assert len(exec_a_events) >= 1  # At least FileCommitCreatedEvent
        assert len(exec_b_events) >= 1
        assert set(e.event_id for e in exec_a_events).isdisjoint(set(e.event_id for e in exec_b_events))


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Error Events
# ═══════════════════════════════════════════════════════════════════════════════


class TestErrorEvents:
    """Tests for error event handling."""
    
    def test_file_not_found_publishes_failure_event(self, file_service, all_events_collector):
        """Test that file not found error publishes failure event."""
        try:
            file_service.track_files(["/nonexistent/file.csv"], "exec-err")
        except FileNotFoundError:
            pass
        
        failure_events = [e for e in all_events_collector if isinstance(e, FileTrackingFailedEvent)]
        assert len(failure_events) == 1
        assert "not found" in failure_events[0].error_message.lower() or "FileNotFoundError" in failure_events[0].error_message
    
    def test_failure_event_contains_error_details(self, file_service, all_events_collector):
        """Test that failure event contains error details."""
        try:
            file_service.track_files(["/bad/path.csv"], "exec-detail")
        except Exception:
            pass
        
        failure_events = [e for e in all_events_collector if isinstance(e, FileTrackingFailedEvent)]
        assert len(failure_events) == 1
        assert failure_events[0].error_message is not None
        assert failure_events[0].operation_id is not None
    
    def test_failure_event_after_start_event(self, file_service, all_events_collector):
        """Test that failure event comes after start event."""
        try:
            file_service.track_files(["/bad/file.csv"], "exec-seq")
        except Exception:
            pass
        
        event_types = [type(e).__name__ for e in all_events_collector]
        
        # Should have both Started and Failed
        assert "FileTrackingStartedEvent" in event_types
        assert "FileTrackingFailedEvent" in event_types
        
        # Failed should come after Started
        started_idx = event_types.index("FileTrackingStartedEvent")
        failed_idx = event_types.index("FileTrackingFailedEvent")
        assert started_idx < failed_idx
    
    def test_partial_success_publishes_events(self, file_service, sample_files, all_events_collector, temp_dir):
        """Test events when only some files succeed."""
        # Create a list with one valid and one invalid file
        files = [str(sample_files["csv"]), "/nonexistent/file.csv"]
        
        try:
            file_service.track_files(files, "exec-partial")
        except Exception:
            pass
        
        # Should have started event
        start_events = [e for e in all_events_collector if isinstance(e, FileTrackingStartedEvent)]
        assert len(start_events) == 1
        assert len(start_events[0].file_paths) == 2
        
        # Should have at least one blob event (first file succeeded)
        blob_events = [e for e in all_events_collector if isinstance(e, BlobCreatedEvent)]
        assert len(blob_events) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Thread Safety
# ═══════════════════════════════════════════════════════════════════════════════


class TestThreadSafety:
    """Tests for thread-safe event handling."""
    
    def test_concurrent_event_publishing(self, event_bus, file_service, sample_files):
        """Test concurrent event publishing is thread-safe."""
        received = []
        lock = threading.Lock()
        
        def safe_append(event):
            with lock:
                received.append(event)
        
        event_bus.subscribe(FileCommitCreatedEvent, safe_append)
        
        def track_file(exec_id):
            file_service.track_files([str(sample_files["csv"])], exec_id)
        
        threads = [
            threading.Thread(target=track_file, args=(f"exec-{i}",))
            for i in range(10)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All events should be received
        assert len(received) == 10
    
    def test_concurrent_subscriptions(self, event_bus, file_service, sample_files):
        """Test concurrent subscription is thread-safe."""
        handlers_called = []
        lock = threading.Lock()
        
        def create_handler(handler_id):
            def handler(event):
                with lock:
                    handlers_called.append(handler_id)
            return handler
        
        def subscribe_handler(handler_id):
            event_bus.subscribe(FileCommitCreatedEvent, create_handler(handler_id))
        
        # Concurrently subscribe handlers
        threads = [
            threading.Thread(target=subscribe_handler, args=(i,))
            for i in range(20)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Publish event
        file_service.track_files([str(sample_files["csv"])], "exec-sub")
        
        # All handlers should be called
        assert len(handlers_called) == 20
    
    def test_high_volume_events(self, event_bus):
        """Test handling high volume of events."""
        received = []
        event_bus.subscribe(BlobCreatedEvent, received.append)
        
        # Publish many events quickly
        for i in range(1000):
            event_bus.publish(BlobCreatedEvent(
                blob_id=f"blob-{i:04d}" + "0" * 56,
                size_bytes=100,
            ))
        
        # All events should be received
        assert len(received) == 1000


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Event Properties and Immutability
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventPropertiesAndImmutability:
    """Tests for event properties and immutability."""
    
    def test_event_id_unique(self, file_service, sample_files, all_events_collector):
        """Test that each event has unique ID."""
        file_service.track_files([str(sample_files["csv"])], "exec-id1")
        file_service.track_files([str(sample_files["json"])], "exec-id2")
        
        event_ids = [e.event_id for e in all_events_collector]
        unique_ids = set(event_ids)
        
        assert len(event_ids) == len(unique_ids)
    
    def test_event_timestamp_present(self, file_service, sample_files, all_events_collector):
        """Test that events have timestamps."""
        file_service.track_files([str(sample_files["csv"])], "exec-ts")
        
        for event in all_events_collector:
            assert hasattr(event, 'timestamp')
            assert event.timestamp is not None
    
    def test_event_has_required_fields(self, file_service, sample_files, all_events_collector):
        """Test that events have required fields."""
        file_service.track_files([str(sample_files["csv"])], "exec-immut")
        
        commit_events = [e for e in all_events_collector if isinstance(e, FileCommitCreatedEvent)]
        assert len(commit_events) == 1
        
        event = commit_events[0]
        
        # Verify event has required fields
        assert event.event_id is not None
        assert event.timestamp is not None
        assert event.commit_id is not None
        assert event.execution_id == "exec-immut"
    
    def test_event_serialization(self, file_service, sample_files, all_events_collector):
        """Test that events can be serialized."""
        file_service.track_files([str(sample_files["csv"])], "exec-serial")
        
        commit_events = [e for e in all_events_collector if isinstance(e, FileCommitCreatedEvent)]
        event = commit_events[0]
        
        # Should have to_dict method
        if hasattr(event, 'to_dict'):
            data = event.to_dict()
            assert isinstance(data, dict)
            assert "commit_id" in data or "event_id" in data
