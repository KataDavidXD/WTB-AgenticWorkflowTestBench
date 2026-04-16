"""
Integration Tests: File Processing + LangGraph + Audit.

Tests the integration between file processing, LangGraph execution,
and audit trail recording.

Test Categories:
1. Audit Trail for File Operations
2. LangGraph Checkpoint Integration
3. Audit Queries and Retrieval
4. Cross-Component Audit Consistency
5. Audit Persistence and Recovery

Design Principles:
- SOLID: Single responsibility for audit components
- ACID: Audit entries are durable and consistent
- Pattern: Audit trail for compliance and debugging
"""

import pytest
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from enum import Enum
import uuid
import json

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
)
from wtb.infrastructure.events import WTBEventBus


# ═══════════════════════════════════════════════════════════════════════════════
# Audit Trail Simulation
# ═══════════════════════════════════════════════════════════════════════════════


class AuditEventType(Enum):
    """Types of audit events."""
    FILE_TRACKED = "file_tracked"
    FILE_RESTORED = "file_restored"
    COMMIT_CREATED = "commit_created"
    COMMIT_VERIFIED = "commit_verified"
    CHECKPOINT_LINKED = "checkpoint_linked"
    ROLLBACK_PERFORMED = "rollback_performed"
    BRANCH_CREATED = "branch_created"
    NODE_EXECUTED = "node_executed"


@dataclass
class AuditEntry:
    """Single audit trail entry."""
    audit_id: str
    event_type: AuditEventType
    execution_id: str
    timestamp: datetime
    details: Dict[str, Any]
    user_id: Optional[str] = None
    checkpoint_id: Optional[int] = None
    commit_id: Optional[str] = None
    node_id: Optional[str] = None
    
    @classmethod
    def create(
        cls,
        event_type: AuditEventType,
        execution_id: str,
        details: Dict[str, Any],
        **kwargs,
    ) -> "AuditEntry":
        return cls(
            audit_id=str(uuid.uuid4()),
            event_type=event_type,
            execution_id=execution_id,
            timestamp=datetime.now(),
            details=details,
            **kwargs,
        )
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "event_type": self.event_type.value,
            "execution_id": self.execution_id,
            "timestamp": self.timestamp.isoformat(),
            "details": self.details,
            "user_id": self.user_id,
            "checkpoint_id": self.checkpoint_id,
            "commit_id": self.commit_id,
            "node_id": self.node_id,
        }


class FileAuditTrail:
    """
    Audit trail for file processing operations.
    
    Records all file-related actions for compliance,
    debugging, and time-travel analysis.
    """
    
    def __init__(self, event_bus: Optional[WTBEventBus] = None):
        self._entries: List[AuditEntry] = []
        self._event_bus = event_bus
        
        if event_bus:
            self._subscribe_to_events()
    
    def _subscribe_to_events(self):
        """Subscribe to file processing events."""
        self._event_bus.subscribe(
            FileCommitCreatedEvent,
            self._on_commit_created,
        )
        self._event_bus.subscribe(
            FileRestoredEvent,
            self._on_file_restored,
        )
    
    def _on_commit_created(self, event: FileCommitCreatedEvent):
        """Handle commit created event."""
        self.record(
            event_type=AuditEventType.COMMIT_CREATED,
            execution_id=event.execution_id,
            details={
                "commit_id": event.commit_id,
                "file_count": event.file_count,
                "total_size_bytes": event.total_size_bytes,
                "message": event.message,
            },
            commit_id=event.commit_id,
        )
    
    def _on_file_restored(self, event: FileRestoredEvent):
        """Handle file restored event."""
        self.record(
            event_type=AuditEventType.FILE_RESTORED,
            execution_id=event.commit_id,  # Use commit_id as execution context
            details={
                "commit_id": event.commit_id,
                "files_restored": event.files_restored,
                "restored_paths": list(event.restored_paths),
            },
            commit_id=event.commit_id,
        )
    
    def record(
        self,
        event_type: AuditEventType,
        execution_id: str,
        details: Dict[str, Any],
        **kwargs,
    ) -> AuditEntry:
        """Record an audit entry."""
        entry = AuditEntry.create(
            event_type=event_type,
            execution_id=execution_id,
            details=details,
            **kwargs,
        )
        self._entries.append(entry)
        return entry
    
    def get_by_execution(self, execution_id: str) -> List[AuditEntry]:
        """Get all entries for an execution."""
        return [e for e in self._entries if e.execution_id == execution_id]
    
    def get_by_commit(self, commit_id: str) -> List[AuditEntry]:
        """Get all entries for a commit."""
        return [e for e in self._entries if e.commit_id == commit_id]
    
    def get_by_checkpoint(self, checkpoint_id: int) -> List[AuditEntry]:
        """Get all entries for a checkpoint."""
        return [e for e in self._entries if e.checkpoint_id == checkpoint_id]
    
    def get_by_type(self, event_type: AuditEventType) -> List[AuditEntry]:
        """Get all entries of a specific type."""
        return [e for e in self._entries if e.event_type == event_type]
    
    def get_by_time_range(
        self,
        start: datetime,
        end: datetime,
    ) -> List[AuditEntry]:
        """Get entries within a time range."""
        return [
            e for e in self._entries
            if start <= e.timestamp <= end
        ]
    
    def get_execution_timeline(self, execution_id: str) -> List[Dict[str, Any]]:
        """Get timeline of events for an execution."""
        entries = self.get_by_execution(execution_id)
        sorted_entries = sorted(entries, key=lambda e: e.timestamp)
        
        return [
            {
                "timestamp": e.timestamp.isoformat(),
                "event_type": e.event_type.value,
                "node_id": e.node_id,
                "commit_id": e.commit_id,
                "details": e.details,
            }
            for e in sorted_entries
        ]
    
    def count_by_type(self) -> Dict[str, int]:
        """Get count of entries by type."""
        counts = {}
        for entry in self._entries:
            key = entry.event_type.value
            counts[key] = counts.get(key, 0) + 1
        return counts
    
    def clear(self):
        """Clear all entries (for testing)."""
        self._entries.clear()
    
    @property
    def all_entries(self) -> List[AuditEntry]:
        """Get all entries."""
        return list(self._entries)


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph-File Integration Service
# ═══════════════════════════════════════════════════════════════════════════════


class LangGraphFileIntegration:
    """
    Integration service connecting LangGraph execution with file tracking.
    
    Coordinates:
    - LangGraph checkpoint creation
    - File commit creation
    - Checkpoint-commit linking
    - Audit trail recording
    """
    
    def __init__(
        self,
        blob_repo,
        commit_repo,
        link_repo,
        audit_trail: FileAuditTrail,
        event_bus: WTBEventBus = None,
    ):
        self._blob_repo = blob_repo
        self._commit_repo = commit_repo
        self._link_repo = link_repo
        self._audit = audit_trail
        self._event_bus = event_bus
        
        # Simulated LangGraph state
        self._checkpoints: Dict[str, Dict[int, Dict]] = {}  # thread_id -> {step -> checkpoint}
        self._current_step: Dict[str, int] = {}
    
    def execute_node_with_files(
        self,
        thread_id: str,
        node_id: str,
        file_paths: List[str],
        state_update: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """
        Execute a node with file tracking and audit.
        
        Simulates LangGraph node execution with:
        1. State update
        2. File tracking
        3. Checkpoint creation
        4. Audit recording
        """
        # Initialize thread if needed
        if thread_id not in self._checkpoints:
            self._checkpoints[thread_id] = {}
            self._current_step[thread_id] = 0
        
        # Increment step
        self._current_step[thread_id] += 1
        step = self._current_step[thread_id]
        
        # Create file commit
        commit = FileCommit.create(
            message=f"Node {node_id} execution",
            execution_id=thread_id,
        )
        
        for file_path in file_paths:
            memento, content = FileMemento.from_file(file_path)
            self._blob_repo.save(content)  # Content-addressable storage
            commit.add_memento(memento)
        
        if commit.file_count > 0:
            commit.finalize()
            self._commit_repo.save(commit)
        
        # Create checkpoint
        checkpoint_data = {
            "step": step,
            "node_id": node_id,
            "state": state_update or {},
            "commit_id": commit.commit_id.value if commit.is_finalized else None,
            "timestamp": datetime.now().isoformat(),
        }
        self._checkpoints[thread_id][step] = checkpoint_data
        
        # Link checkpoint to commit
        if commit.is_finalized:
            link = CheckpointFileLink.create(step, commit)
            self._link_repo.add(link)
        
        # Record audit
        self._audit.record(
            event_type=AuditEventType.NODE_EXECUTED,
            execution_id=thread_id,
            details={
                "node_id": node_id,
                "step": step,
                "file_count": commit.file_count if commit.is_finalized else 0,
                "state_update": state_update,
            },
            node_id=node_id,
            checkpoint_id=step,
            commit_id=commit.commit_id.value if commit.is_finalized else None,
        )
        
        if commit.is_finalized:
            self._audit.record(
                event_type=AuditEventType.FILE_TRACKED,
                execution_id=thread_id,
                details={
                    "file_paths": file_paths,
                    "total_size": commit.total_size,
                },
                commit_id=commit.commit_id.value,
                checkpoint_id=step,
            )
            
            self._audit.record(
                event_type=AuditEventType.CHECKPOINT_LINKED,
                execution_id=thread_id,
                details={
                    "checkpoint_id": step,
                    "commit_id": commit.commit_id.value,
                },
                checkpoint_id=step,
                commit_id=commit.commit_id.value,
            )
        
        # Publish events
        if self._event_bus and commit.is_finalized:
            self._event_bus.publish(FileCommitCreatedEvent(
                commit_id=commit.commit_id.value,
                execution_id=thread_id,
                file_count=commit.file_count,
                total_size_bytes=commit.total_size,
            ))
        
        return {
            "step": step,
            "commit_id": commit.commit_id.value if commit.is_finalized else None,
            "files_tracked": commit.file_count,
        }
    
    def rollback_to_step(
        self,
        thread_id: str,
        target_step: int,
        restore_files: bool = False,
        output_dir: str = None,
    ) -> Dict[str, Any]:
        """
        Rollback to a previous step with optional file restoration.
        
        Records audit entry for rollback.
        """
        if thread_id not in self._checkpoints:
            raise ValueError(f"Unknown thread: {thread_id}")
        
        if target_step not in self._checkpoints[thread_id]:
            raise ValueError(f"Unknown step: {target_step}")
        
        checkpoint = self._checkpoints[thread_id][target_step]
        
        restored_files = []
        if restore_files and checkpoint.get("commit_id") and output_dir:
            commit = self._commit_repo.get_by_id(CommitId(checkpoint["commit_id"]))
            if commit:
                for memento in commit.mementos:
                    content = self._blob_repo.get(memento.file_hash)
                    if content:
                        output_path = Path(output_dir) / Path(memento.file_path).name
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        output_path.write_bytes(content)
                        restored_files.append(str(output_path))
        
        # Reset current step
        from_step = self._current_step[thread_id]
        self._current_step[thread_id] = target_step
        
        # Record audit
        self._audit.record(
            event_type=AuditEventType.ROLLBACK_PERFORMED,
            execution_id=thread_id,
            details={
                "from_step": from_step,
                "to_step": target_step,
                "files_restored": len(restored_files),
                "restored_paths": restored_files,
            },
            checkpoint_id=target_step,
            commit_id=checkpoint.get("commit_id"),
        )
        
        return {
            "from_step": from_step,
            "to_step": target_step,
            "restored_files": restored_files,
            "state": checkpoint.get("state", {}),
        }
    
    def branch_from_step(
        self,
        source_thread_id: str,
        target_step: int,
        new_thread_id: str,
    ) -> str:
        """
        Create a new execution branch from a checkpoint.
        
        Records audit entry for branch creation.
        """
        if source_thread_id not in self._checkpoints:
            raise ValueError(f"Unknown thread: {source_thread_id}")
        
        if target_step not in self._checkpoints[source_thread_id]:
            raise ValueError(f"Unknown step: {target_step}")
        
        # Copy checkpoints up to branch point
        self._checkpoints[new_thread_id] = {
            step: dict(checkpoint)
            for step, checkpoint in self._checkpoints[source_thread_id].items()
            if step <= target_step
        }
        self._current_step[new_thread_id] = target_step
        
        # Record audit
        self._audit.record(
            event_type=AuditEventType.BRANCH_CREATED,
            execution_id=new_thread_id,
            details={
                "source_thread": source_thread_id,
                "branch_point": target_step,
            },
            checkpoint_id=target_step,
        )
        
        return new_thread_id
    
    def get_checkpoint(self, thread_id: str, step: int) -> Optional[Dict]:
        """Get checkpoint data."""
        if thread_id in self._checkpoints:
            return self._checkpoints[thread_id].get(step)
        return None
    
    def get_all_checkpoints(self, thread_id: str) -> List[Dict]:
        """Get all checkpoints for a thread."""
        if thread_id in self._checkpoints:
            return list(self._checkpoints[thread_id].values())
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def audit_trail(event_bus):
    """Create audit trail with event bus integration."""
    return FileAuditTrail(event_bus)


@pytest.fixture
def langgraph_integration(
    blob_repository,
    commit_repository,
    checkpoint_link_repository,
    audit_trail,
    event_bus,
):
    """Create LangGraph file integration service."""
    return LangGraphFileIntegration(
        blob_repository,
        commit_repository,
        checkpoint_link_repository,
        audit_trail,
        event_bus,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Audit Trail for File Operations
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuditTrailForFileOperations:
    """Tests for audit trail recording of file operations."""
    
    def test_node_execution_recorded(self, langgraph_integration, sample_files, audit_trail):
        """Test that node execution is recorded in audit."""
        langgraph_integration.execute_node_with_files(
            "thread-1",
            "preprocess",
            [str(sample_files["csv"])],
        )
        
        entries = audit_trail.get_by_execution("thread-1")
        node_entries = [e for e in entries if e.event_type == AuditEventType.NODE_EXECUTED]
        
        assert len(node_entries) >= 1
        assert node_entries[0].node_id == "preprocess"
    
    def test_file_tracking_recorded(self, langgraph_integration, sample_files, audit_trail):
        """Test that file tracking is recorded in audit."""
        langgraph_integration.execute_node_with_files(
            "thread-2",
            "track_node",
            [str(sample_files["csv"])],
        )
        
        entries = audit_trail.get_by_type(AuditEventType.FILE_TRACKED)
        
        assert len(entries) >= 1
        file_paths = entries[0].details.get("file_paths", [])
        assert str(sample_files["csv"]) in file_paths
    
    def test_checkpoint_linking_recorded(self, langgraph_integration, sample_files, audit_trail):
        """Test that checkpoint linking is recorded in audit."""
        langgraph_integration.execute_node_with_files(
            "thread-3",
            "linked_node",
            [str(sample_files["csv"])],
        )
        
        entries = audit_trail.get_by_type(AuditEventType.CHECKPOINT_LINKED)
        
        assert len(entries) >= 1
        assert entries[0].checkpoint_id == 1
    
    def test_rollback_recorded(self, langgraph_integration, sample_files, audit_trail, temp_dir):
        """Test that rollback is recorded in audit."""
        langgraph_integration.execute_node_with_files(
            "thread-4",
            "step1",
            [str(sample_files["csv"])],
        )
        langgraph_integration.execute_node_with_files(
            "thread-4",
            "step2",
            [str(sample_files["json"])],
        )
        
        langgraph_integration.rollback_to_step("thread-4", 1)
        
        entries = audit_trail.get_by_type(AuditEventType.ROLLBACK_PERFORMED)
        
        assert len(entries) >= 1
        assert entries[0].details["from_step"] == 2
        assert entries[0].details["to_step"] == 1
    
    def test_branch_creation_recorded(self, langgraph_integration, sample_files, audit_trail):
        """Test that branch creation is recorded in audit."""
        langgraph_integration.execute_node_with_files(
            "thread-5",
            "main_node",
            [str(sample_files["csv"])],
        )
        
        langgraph_integration.branch_from_step("thread-5", 1, "thread-5-branch")
        
        entries = audit_trail.get_by_type(AuditEventType.BRANCH_CREATED)
        
        assert len(entries) >= 1
        assert entries[0].details["source_thread"] == "thread-5"


# ═══════════════════════════════════════════════════════════════════════════════
# Test: LangGraph Checkpoint Integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestLangGraphCheckpointIntegration:
    """Tests for LangGraph checkpoint and file integration."""
    
    def test_checkpoint_contains_commit_id(self, langgraph_integration, sample_files):
        """Test that checkpoints contain commit ID."""
        result = langgraph_integration.execute_node_with_files(
            "thread-cp1",
            "node_a",
            [str(sample_files["csv"])],
        )
        
        checkpoint = langgraph_integration.get_checkpoint("thread-cp1", 1)
        
        assert checkpoint is not None
        assert checkpoint["commit_id"] == result["commit_id"]
    
    def test_multiple_checkpoints_tracked(self, langgraph_integration, sample_files):
        """Test that multiple checkpoints are tracked."""
        for node in ["node_a", "node_b", "node_c"]:
            langgraph_integration.execute_node_with_files(
                "thread-cp2",
                node,
                [str(sample_files["csv"])],
            )
        
        checkpoints = langgraph_integration.get_all_checkpoints("thread-cp2")
        
        assert len(checkpoints) == 3
        assert checkpoints[0]["node_id"] == "node_a"
        assert checkpoints[1]["node_id"] == "node_b"
        assert checkpoints[2]["node_id"] == "node_c"
    
    def test_rollback_restores_files(self, langgraph_integration, sample_files, temp_dir):
        """Test that rollback can restore files."""
        # Execute with different files at each step
        langgraph_integration.execute_node_with_files(
            "thread-restore",
            "step1",
            [str(sample_files["csv"])],
        )
        langgraph_integration.execute_node_with_files(
            "thread-restore",
            "step2",
            [str(sample_files["json"])],
        )
        
        output_dir = Path(temp_dir) / "rollback_restore"
        result = langgraph_integration.rollback_to_step(
            "thread-restore",
            1,
            restore_files=True,
            output_dir=str(output_dir),
        )
        
        assert len(result["restored_files"]) == 1
        assert "data.csv" in result["restored_files"][0]
    
    def test_branch_preserves_checkpoints(self, langgraph_integration, sample_files):
        """Test that branching preserves checkpoints up to branch point."""
        langgraph_integration.execute_node_with_files(
            "thread-branch",
            "step1",
            [str(sample_files["csv"])],
        )
        langgraph_integration.execute_node_with_files(
            "thread-branch",
            "step2",
            [str(sample_files["json"])],
        )
        langgraph_integration.execute_node_with_files(
            "thread-branch",
            "step3",
            [str(sample_files["txt"])],
        )
        
        # Branch from step 2
        langgraph_integration.branch_from_step("thread-branch", 2, "thread-branch-alt")
        
        branch_checkpoints = langgraph_integration.get_all_checkpoints("thread-branch-alt")
        
        assert len(branch_checkpoints) == 2  # Only steps 1 and 2
        assert branch_checkpoints[0]["step"] == 1
        assert branch_checkpoints[1]["step"] == 2


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Audit Queries and Retrieval
# ═══════════════════════════════════════════════════════════════════════════════


class TestAuditQueriesAndRetrieval:
    """Tests for audit query capabilities."""
    
    def test_query_by_execution_id(self, langgraph_integration, sample_files, audit_trail):
        """Test querying audit by execution ID."""
        langgraph_integration.execute_node_with_files(
            "exec-query1",
            "node",
            [str(sample_files["csv"])],
        )
        langgraph_integration.execute_node_with_files(
            "exec-query2",
            "node",
            [str(sample_files["json"])],
        )
        
        entries_1 = audit_trail.get_by_execution("exec-query1")
        entries_2 = audit_trail.get_by_execution("exec-query2")
        
        assert all(e.execution_id == "exec-query1" for e in entries_1)
        assert all(e.execution_id == "exec-query2" for e in entries_2)
    
    def test_query_by_commit_id(self, langgraph_integration, sample_files, audit_trail):
        """Test querying audit by commit ID."""
        result = langgraph_integration.execute_node_with_files(
            "exec-commit",
            "node",
            [str(sample_files["csv"])],
        )
        
        entries = audit_trail.get_by_commit(result["commit_id"])
        
        assert len(entries) >= 1
        assert all(e.commit_id == result["commit_id"] for e in entries)
    
    def test_query_by_checkpoint_id(self, langgraph_integration, sample_files, audit_trail):
        """Test querying audit by checkpoint ID."""
        langgraph_integration.execute_node_with_files(
            "exec-cp",
            "node1",
            [str(sample_files["csv"])],
        )
        langgraph_integration.execute_node_with_files(
            "exec-cp",
            "node2",
            [str(sample_files["json"])],
        )
        
        entries = audit_trail.get_by_checkpoint(2)
        
        assert len(entries) >= 1
        assert all(e.checkpoint_id == 2 for e in entries)
    
    def test_query_by_type(self, langgraph_integration, sample_files, audit_trail):
        """Test querying audit by event type."""
        langgraph_integration.execute_node_with_files(
            "exec-type",
            "node",
            [str(sample_files["csv"])],
        )
        
        node_entries = audit_trail.get_by_type(AuditEventType.NODE_EXECUTED)
        file_entries = audit_trail.get_by_type(AuditEventType.FILE_TRACKED)
        
        assert all(e.event_type == AuditEventType.NODE_EXECUTED for e in node_entries)
        assert all(e.event_type == AuditEventType.FILE_TRACKED for e in file_entries)
    
    def test_execution_timeline(self, langgraph_integration, sample_files, audit_trail):
        """Test getting execution timeline."""
        langgraph_integration.execute_node_with_files(
            "exec-timeline",
            "node1",
            [str(sample_files["csv"])],
        )
        langgraph_integration.execute_node_with_files(
            "exec-timeline",
            "node2",
            [str(sample_files["json"])],
        )
        
        timeline = audit_trail.get_execution_timeline("exec-timeline")
        
        assert len(timeline) >= 4  # Node + File + Link for each step
        
        # Verify ordering
        timestamps = [t["timestamp"] for t in timeline]
        assert timestamps == sorted(timestamps)
    
    def test_count_by_type(self, langgraph_integration, sample_files, audit_trail):
        """Test counting entries by type."""
        langgraph_integration.execute_node_with_files(
            "exec-count",
            "node1",
            [str(sample_files["csv"])],
        )
        langgraph_integration.execute_node_with_files(
            "exec-count",
            "node2",
            [str(sample_files["json"])],
        )
        
        counts = audit_trail.count_by_type()
        
        assert counts.get(AuditEventType.NODE_EXECUTED.value, 0) >= 2


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Cross-Component Audit Consistency
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrossComponentAuditConsistency:
    """Tests for audit consistency across components."""
    
    def test_audit_matches_checkpoints(self, langgraph_integration, sample_files, audit_trail):
        """Test that audit entries match actual checkpoints."""
        for i, node in enumerate(["a", "b", "c"], 1):
            langgraph_integration.execute_node_with_files(
                "exec-match",
                f"node_{node}",
                [str(sample_files["csv"])],
            )
        
        checkpoints = langgraph_integration.get_all_checkpoints("exec-match")
        audit_entries = audit_trail.get_by_execution("exec-match")
        
        # Each checkpoint should have corresponding audit entry
        checkpoint_steps = {cp["step"] for cp in checkpoints}
        audit_checkpoint_ids = {e.checkpoint_id for e in audit_entries if e.checkpoint_id}
        
        assert checkpoint_steps == audit_checkpoint_ids
    
    def test_audit_matches_commits(self, langgraph_integration, sample_files, audit_trail, commit_repository):
        """Test that audit entries match actual commits."""
        result = langgraph_integration.execute_node_with_files(
            "exec-commit-match",
            "node",
            [str(sample_files["csv"])],
        )
        
        # Get commit from repository
        commit = commit_repository.get_by_id(CommitId(result["commit_id"]))
        
        # Get audit entry
        audit_entries = audit_trail.get_by_commit(result["commit_id"])
        
        assert len(audit_entries) >= 1
        
        # Verify file count matches
        file_tracked_entry = next(
            (e for e in audit_entries if e.event_type == AuditEventType.FILE_TRACKED),
            None,
        )
        if file_tracked_entry:
            assert len(file_tracked_entry.details.get("file_paths", [])) == commit.file_count
    
    def test_audit_rollback_consistency(self, langgraph_integration, sample_files, audit_trail):
        """Test audit consistency after rollback."""
        # Execute
        langgraph_integration.execute_node_with_files(
            "exec-rb-cons",
            "step1",
            [str(sample_files["csv"])],
        )
        langgraph_integration.execute_node_with_files(
            "exec-rb-cons",
            "step2",
            [str(sample_files["json"])],
        )
        
        # Rollback
        langgraph_integration.rollback_to_step("exec-rb-cons", 1)
        
        # Execute new path
        langgraph_integration.execute_node_with_files(
            "exec-rb-cons",
            "step2_alt",
            [str(sample_files["txt"])],
        )
        
        # Audit should show full history
        entries = audit_trail.get_by_execution("exec-rb-cons")
        
        # Should have: step1, step2, rollback, step2_alt (with file tracking for each)
        rollback_entries = [e for e in entries if e.event_type == AuditEventType.ROLLBACK_PERFORMED]
        assert len(rollback_entries) >= 1
        
        node_entries = [e for e in entries if e.event_type == AuditEventType.NODE_EXECUTED]
        assert len(node_entries) >= 3


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Event-Driven Audit
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventDrivenAudit:
    """Tests for event-driven audit recording."""
    
    def test_events_create_audit_entries(self, event_bus, audit_trail):
        """Test that events automatically create audit entries."""
        # Audit trail subscribes to events in constructor
        
        # Publish event directly
        event_bus.publish(FileCommitCreatedEvent(
            commit_id="test-commit-123",
            execution_id="exec-event",
            file_count=2,
            total_size_bytes=1024,
        ))
        
        entries = audit_trail.get_by_type(AuditEventType.COMMIT_CREATED)
        
        assert len(entries) >= 1
        assert any(e.details.get("commit_id") == "test-commit-123" for e in entries)
    
    def test_restore_event_creates_audit(self, event_bus, audit_trail):
        """Test that restore events create audit entries."""
        event_bus.publish(FileRestoredEvent(
            commit_id="restore-commit-456",
            files_restored=3,
            restored_paths=("/a.csv", "/b.json", "/c.txt"),
        ))
        
        entries = audit_trail.get_by_type(AuditEventType.FILE_RESTORED)
        
        assert len(entries) >= 1
        assert any(e.details.get("files_restored") == 3 for e in entries)
