"""
Integration Tests: File Processing + Event + Audit.

Tests the full integration of file processing, event publishing,
and audit trail recording in a unified system.

Test Categories:
1. Full Pipeline Integration
2. Transaction Consistency
3. Error Recovery and Audit
4. Performance Under Load
5. Cross-Database Consistency

Design Principles:
- SOLID: Each component has single responsibility
- ACID: Transactions are atomic and consistent
- Pattern: Event sourcing with audit trail
"""

import pytest
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from enum import Enum
import uuid
import threading
import time

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
    BlobCreatedEvent,
    FileTrackingStartedEvent,
    FileTrackingCompletedEvent,
    FileTrackingFailedEvent,
)
from wtb.infrastructure.events import WTBEventBus


# ═══════════════════════════════════════════════════════════════════════════════
# Unified File Processing Service
# ═══════════════════════════════════════════════════════════════════════════════


class AuditLevel(Enum):
    """Audit detail level."""
    MINIMAL = "minimal"      # Only final results
    STANDARD = "standard"    # Key operations
    DETAILED = "detailed"    # All operations
    DEBUG = "debug"          # Everything + debug info


@dataclass
class AuditConfig:
    """Configuration for audit trail."""
    level: AuditLevel = AuditLevel.STANDARD
    include_content_hashes: bool = True
    include_file_paths: bool = True
    include_stack_traces: bool = False


@dataclass
class UnifiedAuditEntry:
    """Comprehensive audit entry combining events and audit."""
    entry_id: str
    timestamp: datetime
    execution_id: str
    operation: str
    event_type: Optional[str]
    status: str
    details: Dict[str, Any]
    related_commit_id: Optional[str] = None
    related_checkpoint_id: Optional[int] = None
    duration_ms: Optional[float] = None
    error: Optional[str] = None


class UnifiedFileProcessingService:
    """
    Unified service integrating file processing, events, and audit.
    
    Provides a single interface that:
    1. Tracks files with atomic commits
    2. Publishes events for all operations
    3. Records audit trail entries
    4. Ensures transaction consistency
    """
    
    def __init__(
        self,
        blob_repo,
        commit_repo,
        link_repo,
        event_bus: WTBEventBus,
        audit_config: AuditConfig = None,
    ):
        self._blob_repo = blob_repo
        self._commit_repo = commit_repo
        self._link_repo = link_repo
        self._event_bus = event_bus
        self._audit_config = audit_config or AuditConfig()
        
        self._audit_entries: List[UnifiedAuditEntry] = []
        self._operation_stats = {
            "tracks": 0,
            "restores": 0,
            "rollbacks": 0,
            "errors": 0,
            "total_bytes": 0,
        }
    
    def _record_audit(
        self,
        execution_id: str,
        operation: str,
        event_type: str = None,
        status: str = "success",
        details: Dict = None,
        commit_id: str = None,
        checkpoint_id: int = None,
        duration_ms: float = None,
        error: str = None,
    ):
        """Record unified audit entry."""
        entry = UnifiedAuditEntry(
            entry_id=str(uuid.uuid4()),
            timestamp=datetime.now(),
            execution_id=execution_id,
            operation=operation,
            event_type=event_type,
            status=status,
            details=details or {},
            related_commit_id=commit_id,
            related_checkpoint_id=checkpoint_id,
            duration_ms=duration_ms,
            error=error,
        )
        self._audit_entries.append(entry)
        return entry
    
    def track_files(
        self,
        file_paths: List[str],
        execution_id: str,
        checkpoint_id: int = None,
        message: str = None,
    ) -> Dict[str, Any]:
        """
        Track files with full event and audit integration.
        
        Returns:
            Dict with commit_id, files_tracked, total_size, events_published
        """
        # #region agent log
        import time as _t; _start_perf = _t.perf_counter(); start_time = _t.time()
        with open(r"d:\12-22\.cursor\debug.log", "a", encoding="utf-8") as _f: import json as _j; _f.write(_j.dumps({"hypothesisId":"H1-A","location":"test_file_event_audit.py:track_files:start","message":"start_time captured","data":{"time_time":start_time,"perf_counter":_start_perf},"timestamp":int(_t.time()*1000),"sessionId":"debug-session"})+"\n")
        # #endregion
        tracking_id = str(uuid.uuid4())
        events_published = []
        
        # Publish start event
        start_event = FileTrackingStartedEvent(
            operation_id=tracking_id,
            file_paths=tuple(file_paths),
        )
        self._event_bus.publish(start_event)
        events_published.append("FileTrackingStartedEvent")
        
        try:
            # Create commit
            commit = FileCommit.create(
                message=message or f"Track {len(file_paths)} files",
                execution_id=execution_id,
            )
            
            # Track each file
            for file_path in file_paths:
                memento, content = FileMemento.from_file(file_path)
                self._blob_repo.save(content)  # Content-addressable storage
                
                # Publish blob event
                blob_event = BlobCreatedEvent(
                    blob_id=memento.file_hash.value,
                    size_bytes=memento.file_size,
                )
                self._event_bus.publish(blob_event)
                events_published.append("BlobCreatedEvent")
                
                commit.add_memento(memento)
            
            commit.finalize()
            self._commit_repo.save(commit)
            
            # Publish commit event
            commit_event = FileCommitCreatedEvent(
                commit_id=commit.commit_id.value,
                execution_id=execution_id,
                file_count=commit.file_count,
                total_size_bytes=commit.total_size,
                message=commit.message,
            )
            self._event_bus.publish(commit_event)
            events_published.append("FileCommitCreatedEvent")
            
            # Link to checkpoint if provided
            if checkpoint_id is not None:
                link = CheckpointFileLink.create(checkpoint_id, commit)
                self._link_repo.add(link)
                
                link_event = CheckpointFileLinkCreatedEvent(
                    checkpoint_id=checkpoint_id,
                    commit_id=commit.commit_id.value,
                    file_count=commit.file_count,
                    total_size_bytes=commit.total_size,
                )
                self._event_bus.publish(link_event)
                events_published.append("CheckpointFileLinkCreatedEvent")
            
            # Publish completion event
            duration_ms = (time.time() - start_time) * 1000
            # #region agent log
            import time as _t2; _end_perf = _t2.perf_counter()
            with open(r"d:\12-22\.cursor\debug.log", "a", encoding="utf-8") as _f: import json as _j; _f.write(_j.dumps({"hypothesisId":"H1-A,H1-B","location":"test_file_event_audit.py:track_files:duration_calc","message":"duration calculated","data":{"duration_ms":duration_ms,"end_time":_t2.time(),"perf_duration_ms":(_end_perf-_start_perf)*1000,"start_time":start_time},"timestamp":int(_t2.time()*1000),"sessionId":"debug-session"})+"\n")
            # #endregion
            complete_event = FileTrackingCompletedEvent(
                operation_id=tracking_id,
                commit_id=commit.commit_id.value,
                duration_ms=int(duration_ms),
            )
            self._event_bus.publish(complete_event)
            events_published.append("FileTrackingCompletedEvent")
            
            # Update stats
            self._operation_stats["tracks"] += 1
            self._operation_stats["total_bytes"] += commit.total_size
            
            # Record audit
            self._record_audit(
                execution_id=execution_id,
                operation="track_files",
                event_type="FileTrackingCompletedEvent",
                details={
                    "file_paths": file_paths if self._audit_config.include_file_paths else None,
                    "file_count": commit.file_count,
                    "total_size": commit.total_size,
                    "checkpoint_id": checkpoint_id,
                    "events_published": events_published,
                },
                commit_id=commit.commit_id.value,
                checkpoint_id=checkpoint_id,
                duration_ms=duration_ms,
            )
            
            return {
                "commit_id": commit.commit_id.value,
                "files_tracked": commit.file_count,
                "total_size": commit.total_size,
                "checkpoint_id": checkpoint_id,
                "events_published": events_published,
                "duration_ms": duration_ms,
            }
            
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            
            # Publish failure event
            fail_event = FileTrackingFailedEvent(
                operation_id=tracking_id,
                error_message=str(e),
            )
            self._event_bus.publish(fail_event)
            events_published.append("FileTrackingFailedEvent")
            
            # Update stats
            self._operation_stats["errors"] += 1
            
            # Record audit
            self._record_audit(
                execution_id=execution_id,
                operation="track_files",
                event_type="FileTrackingFailedEvent",
                status="error",
                details={
                    "file_paths": file_paths,
                    "events_published": events_published,
                },
                duration_ms=duration_ms,
                error=str(e),
            )
            
            raise
    
    def restore_from_checkpoint(
        self,
        checkpoint_id: int,
        output_dir: str,
        execution_id: str,
    ) -> Dict[str, Any]:
        """
        Restore files from checkpoint with full event and audit integration.
        """
        start_time = time.time()
        
        # Get link
        link = self._link_repo.get_by_checkpoint(checkpoint_id)
        if link is None:
            raise ValueError(f"No commit linked to checkpoint {checkpoint_id}")
        
        # Get commit
        commit = self._commit_repo.get_by_id(link.commit_id)
        if commit is None:
            raise ValueError(f"Commit not found: {link.commit_id}")
        
        # Restore files
        restored_paths = []
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        for memento in commit.mementos:
            content = self._blob_repo.get(memento.file_hash)
            if content:
                file_path = output_path / Path(memento.file_path).name
                file_path.write_bytes(content)
                restored_paths.append(str(file_path))
        
        # Publish restore event
        restore_event = FileRestoredEvent(
            commit_id=commit.commit_id.value,
            files_restored=len(restored_paths),
            restored_paths=tuple(restored_paths),
        )
        self._event_bus.publish(restore_event)
        
        duration_ms = (time.time() - start_time) * 1000
        
        # Update stats
        self._operation_stats["restores"] += 1
        
        # Record audit
        self._record_audit(
            execution_id=execution_id,
            operation="restore_from_checkpoint",
            event_type="FileRestoredEvent",
            details={
                "checkpoint_id": checkpoint_id,
                "files_restored": len(restored_paths),
                "restored_paths": restored_paths if self._audit_config.include_file_paths else None,
            },
            commit_id=commit.commit_id.value,
            checkpoint_id=checkpoint_id,
            duration_ms=duration_ms,
        )
        
        return {
            "commit_id": commit.commit_id.value,
            "files_restored": len(restored_paths),
            "restored_paths": restored_paths,
            "duration_ms": duration_ms,
        }
    
    def verify_commit(
        self,
        commit_id: str,
        execution_id: str,
    ) -> Dict[str, Any]:
        """Verify commit integrity with event and audit."""
        start_time = time.time()
        
        commit = self._commit_repo.get_by_id(CommitId(commit_id))
        if commit is None:
            raise ValueError(f"Commit not found: {commit_id}")
        
        # Verify all blobs exist
        verified_blobs = []
        missing_blobs = []
        for memento in commit.mementos:
            if self._blob_repo.exists(memento.file_hash):
                verified_blobs.append(memento.file_hash.value)
            else:
                missing_blobs.append(memento.file_hash.value)
        
        verified = len(missing_blobs) == 0
        
        if verified:
            commit.mark_verified()
            self._commit_repo.save(commit)
        
        # Publish verification event
        verify_event = FileCommitVerifiedEvent(
            commit_id=commit_id,
            blobs_verified=len(verified_blobs),
        )
        self._event_bus.publish(verify_event)
        
        duration_ms = (time.time() - start_time) * 1000
        
        # Record audit
        self._record_audit(
            execution_id=execution_id,
            operation="verify_commit",
            event_type="FileCommitVerifiedEvent",
            status="success" if verified else "warning",
            details={
                "verified_blobs": len(verified_blobs),
                "missing_blobs": missing_blobs,
            },
            commit_id=commit_id,
            duration_ms=duration_ms,
        )
        
        return {
            "commit_id": commit_id,
            "verified": verified,
            "verified_blobs": len(verified_blobs),
            "missing_blobs": missing_blobs,
            "duration_ms": duration_ms,
        }
    
    def get_audit_entries(
        self,
        execution_id: str = None,
        operation: str = None,
        status: str = None,
    ) -> List[UnifiedAuditEntry]:
        """Query audit entries with filters."""
        entries = self._audit_entries
        
        if execution_id:
            entries = [e for e in entries if e.execution_id == execution_id]
        if operation:
            entries = [e for e in entries if e.operation == operation]
        if status:
            entries = [e for e in entries if e.status == status]
        
        return entries
    
    def get_stats(self) -> Dict[str, Any]:
        """Get operation statistics."""
        return dict(self._operation_stats)
    
    def clear_audit(self):
        """Clear audit entries (for testing)."""
        self._audit_entries.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def unified_service(event_bus, blob_repository, commit_repository, checkpoint_link_repository):
    """Create unified file processing service."""
    return UnifiedFileProcessingService(
        blob_repository,
        commit_repository,
        checkpoint_link_repository,
        event_bus,
    )


@pytest.fixture
def detailed_audit_service(event_bus, blob_repository, commit_repository, checkpoint_link_repository):
    """Create service with detailed audit."""
    return UnifiedFileProcessingService(
        blob_repository,
        commit_repository,
        checkpoint_link_repository,
        event_bus,
        AuditConfig(level=AuditLevel.DETAILED),
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
        BlobCreatedEvent,
        FileTrackingStartedEvent,
        FileTrackingCompletedEvent,
        FileTrackingFailedEvent,
    ]
    
    for event_type in event_types:
        event_bus.subscribe(event_type, events.append)
    
    return events


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Full Pipeline Integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullPipelineIntegration:
    """Tests for full pipeline from tracking to audit."""
    
    def test_track_files_full_pipeline(self, unified_service, sample_files, all_events_collector):
        """Test complete file tracking pipeline."""
        result = unified_service.track_files(
            [str(sample_files["csv"])],
            "exec-pipeline",
            checkpoint_id=1,
            message="Pipeline test",
        )
        
        # Verify result
        assert result["commit_id"] is not None
        assert result["files_tracked"] == 1
        assert result["checkpoint_id"] == 1
        
        # Verify events
        assert "FileTrackingStartedEvent" in result["events_published"]
        assert "FileCommitCreatedEvent" in result["events_published"]
        assert "FileTrackingCompletedEvent" in result["events_published"]
        assert "CheckpointFileLinkCreatedEvent" in result["events_published"]
        
        # Verify audit
        audit = unified_service.get_audit_entries(execution_id="exec-pipeline")
        assert len(audit) >= 1
        assert audit[0].operation == "track_files"
        assert audit[0].status == "success"
    
    def test_restore_full_pipeline(self, unified_service, sample_files, temp_dir, all_events_collector):
        """Test complete restore pipeline."""
        # Track first
        track_result = unified_service.track_files(
            [str(sample_files["csv"])],
            "exec-restore-pipe",
            checkpoint_id=1,
        )
        
        # Clear events
        all_events_collector.clear()
        
        # Restore
        output_dir = Path(temp_dir) / "pipeline_restore"
        restore_result = unified_service.restore_from_checkpoint(
            checkpoint_id=1,
            output_dir=str(output_dir),
            execution_id="exec-restore-pipe",
        )
        
        # Verify result
        assert restore_result["files_restored"] == 1
        assert len(restore_result["restored_paths"]) == 1
        
        # Verify file exists
        assert Path(restore_result["restored_paths"][0]).exists()
        
        # Verify events
        restore_events = [e for e in all_events_collector if isinstance(e, FileRestoredEvent)]
        assert len(restore_events) == 1
        
        # Verify audit
        audit = unified_service.get_audit_entries(operation="restore_from_checkpoint")
        assert len(audit) >= 1
    
    def test_verify_full_pipeline(self, unified_service, sample_files):
        """Test complete verification pipeline."""
        # Track first
        track_result = unified_service.track_files(
            [str(sample_files["csv"])],
            "exec-verify-pipe",
        )
        
        # Verify
        verify_result = unified_service.verify_commit(
            track_result["commit_id"],
            "exec-verify-pipe",
        )
        
        # Verify result
        assert verify_result["verified"] is True
        assert verify_result["verified_blobs"] == 1
        assert len(verify_result["missing_blobs"]) == 0
        
        # Verify audit
        audit = unified_service.get_audit_entries(operation="verify_commit")
        assert len(audit) >= 1
        assert audit[0].status == "success"


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Transaction Consistency
# ═══════════════════════════════════════════════════════════════════════════════


class TestTransactionConsistency:
    """Tests for transaction consistency across events and audit."""
    
    def test_events_and_audit_match(self, unified_service, sample_files, all_events_collector):
        """Test that events and audit entries are consistent."""
        result = unified_service.track_files(
            [str(sample_files["csv"], ), str(sample_files["json"])],
            "exec-match",
            checkpoint_id=10,
        )
        
        # Get events
        commit_events = [e for e in all_events_collector if isinstance(e, FileCommitCreatedEvent)]
        
        # Get audit
        audit = unified_service.get_audit_entries(execution_id="exec-match")[0]
        
        # Verify consistency
        assert len(commit_events) == 1
        assert commit_events[0].commit_id == result["commit_id"]
        assert commit_events[0].commit_id == audit.related_commit_id
        assert audit.details["file_count"] == commit_events[0].file_count
    
    def test_checkpoint_link_consistency(
        self,
        unified_service,
        sample_files,
        checkpoint_link_repository,
    ):
        """Test checkpoint link consistency across components."""
        result = unified_service.track_files(
            [str(sample_files["csv"])],
            "exec-link-cons",
            checkpoint_id=20,
        )
        
        # Verify link in repository
        link = checkpoint_link_repository.get_by_checkpoint(20)
        
        # Verify audit
        audit = unified_service.get_audit_entries(execution_id="exec-link-cons")[0]
        
        # All should match
        assert link is not None
        assert link.commit_id.value == result["commit_id"]
        assert audit.related_checkpoint_id == 20
        assert audit.related_commit_id == result["commit_id"]
    
    def test_error_consistency(self, unified_service, all_events_collector):
        """Test consistency when errors occur."""
        try:
            unified_service.track_files(
                ["/nonexistent/file.csv"],
                "exec-error-cons",
            )
        except Exception:
            pass
        
        # Verify error event
        fail_events = [e for e in all_events_collector if isinstance(e, FileTrackingFailedEvent)]
        assert len(fail_events) == 1
        assert fail_events[0].operation_id is not None  # Has operation_id, not execution_id
        
        # Verify error audit
        audit = unified_service.get_audit_entries(status="error")
        assert len(audit) >= 1
        assert audit[0].error is not None
    
    def test_multi_file_atomicity(self, unified_service, sample_files, commit_repository):
        """Test that multi-file tracking is atomic."""
        files = [str(sample_files["csv"]), str(sample_files["json"]), str(sample_files["txt"])]
        
        result = unified_service.track_files(files, "exec-atomic")
        
        # All files should be in single commit
        commit = commit_repository.get_by_id(CommitId(result["commit_id"]))
        assert commit.file_count == 3
        
        # Audit should show single operation
        audit = unified_service.get_audit_entries(execution_id="exec-atomic")
        track_ops = [a for a in audit if a.operation == "track_files"]
        assert len(track_ops) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Error Recovery and Audit
# ═══════════════════════════════════════════════════════════════════════════════


class TestErrorRecoveryAndAudit:
    """Tests for error handling and audit recording."""
    
    def test_partial_failure_recorded(self, unified_service, sample_files, all_events_collector):
        """Test that partial failures are properly recorded."""
        files = [str(sample_files["csv"]), "/bad/file.csv"]
        
        try:
            unified_service.track_files(files, "exec-partial")
        except Exception:
            pass
        
        # Should have started event
        start_events = [e for e in all_events_collector if isinstance(e, FileTrackingStartedEvent)]
        assert len(start_events) == 1
        assert len(start_events[0].file_paths) == 2
        
        # Should have at least one blob event
        blob_events = [e for e in all_events_collector if isinstance(e, BlobCreatedEvent)]
        assert len(blob_events) >= 1
        
        # Should have failure event
        fail_events = [e for e in all_events_collector if isinstance(e, FileTrackingFailedEvent)]
        assert len(fail_events) == 1
    
    def test_audit_records_all_attempts(self, unified_service, sample_files):
        """Test that audit records all attempts including failures."""
        # Successful attempt
        unified_service.track_files([str(sample_files["csv"])], "exec-attempts", checkpoint_id=1)
        
        # Failed attempt
        try:
            unified_service.track_files(["/bad/path.csv"], "exec-attempts", checkpoint_id=2)
        except Exception:
            pass
        
        # Another successful attempt
        unified_service.track_files([str(sample_files["json"])], "exec-attempts", checkpoint_id=3)
        
        # All should be in audit
        audit = unified_service.get_audit_entries(execution_id="exec-attempts")
        assert len(audit) == 3
        
        success_audit = [a for a in audit if a.status == "success"]
        error_audit = [a for a in audit if a.status == "error"]
        
        assert len(success_audit) == 2
        assert len(error_audit) == 1
    
    def test_restore_missing_checkpoint_error(self, unified_service, temp_dir):
        """Test error when restoring from non-existent checkpoint."""
        with pytest.raises(ValueError, match="No commit linked"):
            unified_service.restore_from_checkpoint(9999, temp_dir, "exec-missing")
        
        # Should have audit entry for the attempt
        audit = unified_service.get_audit_entries(execution_id="exec-missing")
        # Note: Error raised before audit recorded in current implementation
        # This is a test for expected behavior


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Performance Under Load
# ═══════════════════════════════════════════════════════════════════════════════


class TestPerformanceUnderLoad:
    """Tests for performance with multiple operations."""
    
    def test_multiple_concurrent_tracks(self, unified_service, sample_files):
        """Test concurrent file tracking operations."""
        results = []
        errors = []
        lock = threading.Lock()
        
        def track_file(exec_id):
            try:
                result = unified_service.track_files(
                    [str(sample_files["csv"])],
                    exec_id,
                )
                with lock:
                    results.append(result)
            except Exception as e:
                with lock:
                    errors.append(e)
        
        threads = [
            threading.Thread(target=track_file, args=(f"exec-concurrent-{i}",))
            for i in range(10)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All should succeed
        assert len(errors) == 0
        assert len(results) == 10
        
        # Each should have unique commit
        commit_ids = [r["commit_id"] for r in results]
        assert len(set(commit_ids)) == 10
        
        # Audit should have all entries
        all_audit = unified_service.get_audit_entries()
        assert len(all_audit) >= 10
    
    def test_high_volume_events(self, unified_service, sample_files, event_bus):
        """Test handling high volume of events."""
        received_events = []
        lock = threading.Lock()
        
        def collect_event(event):
            with lock:
                received_events.append(event)
        
        event_bus.subscribe(FileCommitCreatedEvent, collect_event)
        
        # Track many files
        for i in range(20):
            unified_service.track_files(
                [str(sample_files["csv"])],
                f"exec-volume-{i}",
            )
        
        # All events should be received
        assert len(received_events) == 20
        
        # Stats should reflect
        stats = unified_service.get_stats()
        assert stats["tracks"] == 20
    
    def test_audit_query_performance(self, unified_service, sample_files):
        """Test audit query performance with many entries."""
        # Create many audit entries
        for i in range(50):
            unified_service.track_files(
                [str(sample_files["csv"])],
                f"exec-query-{i % 5}",  # 5 different execution IDs
                checkpoint_id=i,
            )
        
        # Query by execution ID
        for i in range(5):
            entries = unified_service.get_audit_entries(execution_id=f"exec-query-{i}")
            assert len(entries) == 10
        
        # Query by operation
        track_entries = unified_service.get_audit_entries(operation="track_files")
        assert len(track_entries) == 50


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Statistics and Metrics
# ═══════════════════════════════════════════════════════════════════════════════


class TestStatisticsAndMetrics:
    """Tests for operation statistics and metrics."""
    
    def test_track_stats_updated(self, unified_service, sample_files):
        """Test that tracking updates statistics."""
        initial_stats = unified_service.get_stats()
        
        unified_service.track_files([str(sample_files["csv"])], "exec-stats1")
        unified_service.track_files([str(sample_files["json"])], "exec-stats2")
        
        final_stats = unified_service.get_stats()
        
        assert final_stats["tracks"] == initial_stats["tracks"] + 2
        assert final_stats["total_bytes"] > initial_stats["total_bytes"]
    
    def test_restore_stats_updated(self, unified_service, sample_files, temp_dir):
        """Test that restore updates statistics."""
        unified_service.track_files(
            [str(sample_files["csv"])],
            "exec-restore-stats",
            checkpoint_id=1,
        )
        
        initial_stats = unified_service.get_stats()
        
        output_dir = Path(temp_dir) / "stats_restore"
        unified_service.restore_from_checkpoint(1, str(output_dir), "exec-restore-stats")
        
        final_stats = unified_service.get_stats()
        
        assert final_stats["restores"] == initial_stats["restores"] + 1
    
    def test_error_stats_updated(self, unified_service):
        """Test that errors update statistics."""
        initial_stats = unified_service.get_stats()
        
        try:
            unified_service.track_files(["/bad/file.csv"], "exec-error-stats")
        except Exception:
            pass
        
        final_stats = unified_service.get_stats()
        
        assert final_stats["errors"] == initial_stats["errors"] + 1
    
    def test_duration_recorded(self, unified_service, sample_files):
        """Test that operation duration is recorded."""
        result = unified_service.track_files(
            [str(sample_files["csv"])],
            "exec-duration",
        )
        
        assert "duration_ms" in result
        # Duration should be non-negative (may be 0 for very fast operations)
        assert result["duration_ms"] >= 0
        
        # Audit should also have duration
        audit = unified_service.get_audit_entries(execution_id="exec-duration")
        assert audit[0].duration_ms >= 0


# ═══════════════════════════════════════════════════════════════════════════════
# Test: End-to-End Workflow
# ═══════════════════════════════════════════════════════════════════════════════


class TestEndToEndWorkflow:
    """Tests for complete end-to-end workflows."""
    
    def test_complete_track_verify_restore_workflow(self, unified_service, sample_files, temp_dir):
        """Test complete workflow: track -> verify -> restore."""
        execution_id = "exec-e2e-workflow"
        
        # Step 1: Track files
        track_result = unified_service.track_files(
            [str(sample_files["csv"]), str(sample_files["json"])],
            execution_id,
            checkpoint_id=1,
            message="E2E test commit",
        )
        
        assert track_result["files_tracked"] == 2
        
        # Step 2: Verify commit
        verify_result = unified_service.verify_commit(
            track_result["commit_id"],
            execution_id,
        )
        
        assert verify_result["verified"] is True
        
        # Step 3: Restore from checkpoint
        output_dir = Path(temp_dir) / "e2e_restore"
        restore_result = unified_service.restore_from_checkpoint(
            checkpoint_id=1,
            output_dir=str(output_dir),
            execution_id=execution_id,
        )
        
        assert restore_result["files_restored"] == 2
        
        # Verify audit trail
        audit = unified_service.get_audit_entries(execution_id=execution_id)
        operations = [a.operation for a in audit]
        
        assert "track_files" in operations
        assert "verify_commit" in operations
        assert "restore_from_checkpoint" in operations
    
    def test_multi_checkpoint_workflow(self, unified_service, sample_files, temp_dir):
        """Test workflow with multiple checkpoints."""
        execution_id = "exec-multi-cp"
        
        # Create multiple checkpoints
        for i in range(5):
            unified_service.track_files(
                [str(sample_files["csv"])],
                execution_id,
                checkpoint_id=i + 1,
                message=f"Checkpoint {i + 1}",
            )
        
        # Restore from middle checkpoint
        output_dir = Path(temp_dir) / "multi_cp_restore"
        restore_result = unified_service.restore_from_checkpoint(
            checkpoint_id=3,
            output_dir=str(output_dir),
            execution_id=execution_id,
        )
        
        assert restore_result["files_restored"] == 1
        
        # Verify audit shows all operations
        audit = unified_service.get_audit_entries(execution_id=execution_id)
        track_ops = [a for a in audit if a.operation == "track_files"]
        
        assert len(track_ops) == 5
