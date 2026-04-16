"""
Integration Tests: File Processing + Event + Audit + Ray Processing.

Tests the full integration of file processing with Ray distributed computing,
including rollback, same-commit scenarios, and transaction consistency debugging.

Test Categories:
1. Ray Distributed File Tracking
2. Rollback with Distributed Files
3. Same-Commit Scenarios
4. Transaction Consistency
5. Concurrent Actor File Operations
6. Error Recovery in Distributed Environment

Design Principles:
- SOLID: Actor isolation, single responsibility
- ACID: Distributed transaction consistency
- Pattern: Actor model with file state management

Note: Tests use mocks when Ray is not available.
"""

import pytest
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum
import uuid
import threading
import time
import copy
from unittest.mock import MagicMock, Mock, patch

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
    BlobCreatedEvent,
    FileTrackingStartedEvent,
    FileTrackingCompletedEvent,
)
from wtb.infrastructure.events import WTBEventBus


# ═══════════════════════════════════════════════════════════════════════════════
# Ray Simulation (for testing without Ray)
# ═══════════════════════════════════════════════════════════════════════════════


class ActorState(Enum):
    """State of a simulated Ray actor."""
    INITIALIZING = "initializing"
    READY = "ready"
    EXECUTING = "executing"
    FAILED = "failed"
    TERMINATED = "terminated"


@dataclass
class ActorFileState:
    """File state tracked by an actor."""
    actor_id: str
    execution_id: str
    commits: Dict[int, CommitId] = field(default_factory=dict)  # step -> commit_id
    current_step: int = 0
    file_hashes: Dict[str, BlobId] = field(default_factory=dict)  # path -> hash


@dataclass
class DistributedFileResult:
    """Result of distributed file operation."""
    actor_id: str
    execution_id: str
    success: bool
    commit_id: Optional[str] = None
    files_tracked: int = 0
    error: Optional[str] = None
    duration_ms: float = 0


class SimulatedActor:
    """
    Simulated Ray actor for file operations.
    
    Mimics Ray actor behavior with:
    - Isolated state per actor
    - Sequential task execution
    - State persistence across tasks
    """
    
    def __init__(
        self,
        actor_id: str,
        blob_repo,
        commit_repo,
        link_repo,
        event_bus: WTBEventBus,
    ):
        self.actor_id = actor_id
        self._blob_repo = blob_repo
        self._commit_repo = commit_repo
        self._link_repo = link_repo
        self._event_bus = event_bus
        self._state = ActorState.INITIALIZING
        self._file_states: Dict[str, ActorFileState] = {}
        self._lock = threading.Lock()
        self._state = ActorState.READY
    
    def track_files(
        self,
        execution_id: str,
        file_paths: List[str],
        checkpoint_id: int,
        message: str = None,
    ) -> DistributedFileResult:
        """Track files in actor context."""
        start_time = time.time()
        
        with self._lock:
            self._state = ActorState.EXECUTING
            
            try:
                # Get or create file state
                if execution_id not in self._file_states:
                    self._file_states[execution_id] = ActorFileState(
                        actor_id=self.actor_id,
                        execution_id=execution_id,
                    )
                
                file_state = self._file_states[execution_id]
                
                # Create commit
                commit = FileCommit.create(
                    message=message or f"Actor {self.actor_id} checkpoint {checkpoint_id}",
                    execution_id=execution_id,
                )
                
                for file_path in file_paths:
                    memento, content = FileMemento.from_file(file_path)
                    self._blob_repo.save(content)  # Content-addressable storage
                    commit.add_memento(memento)
                    file_state.file_hashes[file_path] = memento.file_hash
                
                commit.finalize()
                self._commit_repo.save(commit)
                
                # Link to checkpoint
                link = CheckpointFileLink.create(checkpoint_id, commit)
                self._link_repo.add(link)
                
                # Update state
                file_state.commits[checkpoint_id] = commit.commit_id
                file_state.current_step = checkpoint_id
                
                # Publish event
                self._event_bus.publish(FileCommitCreatedEvent(
                    commit_id=commit.commit_id.value,
                    execution_id=execution_id,
                    file_count=commit.file_count,
                    total_size_bytes=commit.total_size,
                ))
                
                self._state = ActorState.READY
                duration_ms = (time.time() - start_time) * 1000
                
                return DistributedFileResult(
                    actor_id=self.actor_id,
                    execution_id=execution_id,
                    success=True,
                    commit_id=commit.commit_id.value,
                    files_tracked=commit.file_count,
                    duration_ms=duration_ms,
                )
                
            except Exception as e:
                self._state = ActorState.FAILED
                return DistributedFileResult(
                    actor_id=self.actor_id,
                    execution_id=execution_id,
                    success=False,
                    error=str(e),
                    duration_ms=(time.time() - start_time) * 1000,
                )
    
    def rollback_to_checkpoint(
        self,
        execution_id: str,
        checkpoint_id: int,
    ) -> DistributedFileResult:
        """Rollback actor state to checkpoint."""
        with self._lock:
            try:
                if execution_id not in self._file_states:
                    raise ValueError(f"No state for execution {execution_id}")
                
                file_state = self._file_states[execution_id]
                
                if checkpoint_id not in file_state.commits:
                    raise ValueError(f"No commit at checkpoint {checkpoint_id}")
                
                # Get commit
                commit_id = file_state.commits[checkpoint_id]
                commit = self._commit_repo.get_by_id(commit_id)
                
                # Update state to checkpoint
                file_state.current_step = checkpoint_id
                
                # Rebuild file hashes from commit
                file_state.file_hashes.clear()
                for memento in commit.mementos:
                    file_state.file_hashes[memento.file_path] = memento.file_hash
                
                return DistributedFileResult(
                    actor_id=self.actor_id,
                    execution_id=execution_id,
                    success=True,
                    commit_id=commit_id.value,
                )
                
            except Exception as e:
                return DistributedFileResult(
                    actor_id=self.actor_id,
                    execution_id=execution_id,
                    success=False,
                    error=str(e),
                )
    
    def get_state(self, execution_id: str) -> Optional[ActorFileState]:
        """Get actor's file state for execution."""
        return self._file_states.get(execution_id)
    
    def get_commit_at_checkpoint(
        self,
        execution_id: str,
        checkpoint_id: int,
    ) -> Optional[str]:
        """Get commit ID at specific checkpoint."""
        state = self._file_states.get(execution_id)
        if state and checkpoint_id in state.commits:
            return state.commits[checkpoint_id].value
        return None


class SimulatedActorPool:
    """
    Simulated Ray actor pool for file operations.
    
    Mimics Ray ActorPool behavior:
    - Pool of actors for parallel execution
    - Task distribution
    - Result collection
    """
    
    def __init__(
        self,
        num_actors: int,
        blob_repo,
        commit_repo,
        link_repo,
        event_bus: WTBEventBus,
    ):
        self._actors = [
            SimulatedActor(
                f"actor-{i}",
                blob_repo,
                commit_repo,
                link_repo,
                event_bus,
            )
            for i in range(num_actors)
        ]
        self._current_actor = 0
        self._lock = threading.Lock()
    
    def submit(
        self,
        task_fn: str,
        *args,
        **kwargs,
    ) -> "SimulatedObjectRef":
        """Submit task to pool (round-robin)."""
        with self._lock:
            actor = self._actors[self._current_actor]
            self._current_actor = (self._current_actor + 1) % len(self._actors)
        
        # Execute task
        method = getattr(actor, task_fn)
        result = method(*args, **kwargs)
        
        return SimulatedObjectRef(result)
    
    def get_actor(self, index: int) -> SimulatedActor:
        """Get specific actor."""
        return self._actors[index]
    
    @property
    def actors(self) -> List[SimulatedActor]:
        """Get all actors."""
        return self._actors


@dataclass
class SimulatedObjectRef:
    """Simulated Ray ObjectRef."""
    _result: Any
    
    def get(self) -> Any:
        """Get result (like ray.get)."""
        return self._result


# ═══════════════════════════════════════════════════════════════════════════════
# Distributed File Service
# ═══════════════════════════════════════════════════════════════════════════════


class DistributedFileService:
    """
    Distributed file tracking service using actor pool.
    
    Coordinates file operations across multiple actors with:
    - Event publishing
    - Audit recording
    - Transaction consistency
    """
    
    def __init__(
        self,
        actor_pool: SimulatedActorPool,
        blob_repo,
        commit_repo,
        link_repo,
        event_bus: WTBEventBus,
    ):
        self._pool = actor_pool
        self._blob_repo = blob_repo
        self._commit_repo = commit_repo
        self._link_repo = link_repo
        self._event_bus = event_bus
        self._audit_entries: List[Dict[str, Any]] = []
    
    def track_files_distributed(
        self,
        execution_id: str,
        file_paths: List[str],
        checkpoint_id: int,
    ) -> DistributedFileResult:
        """Track files using actor pool."""
        result = self._pool.submit(
            "track_files",
            execution_id,
            file_paths,
            checkpoint_id,
        ).get()
        
        # Record audit
        self._audit_entries.append({
            "timestamp": datetime.now().isoformat(),
            "operation": "track_files_distributed",
            "execution_id": execution_id,
            "actor_id": result.actor_id,
            "checkpoint_id": checkpoint_id,
            "success": result.success,
            "commit_id": result.commit_id,
            "error": result.error,
        })
        
        return result
    
    def rollback_distributed(
        self,
        execution_id: str,
        checkpoint_id: int,
        actor_id: str = None,
    ) -> List[DistributedFileResult]:
        """Rollback all actors to checkpoint."""
        results = []
        
        for actor in self._pool.actors:
            if actor_id and actor.actor_id != actor_id:
                continue
            
            result = actor.rollback_to_checkpoint(execution_id, checkpoint_id)
            results.append(result)
            
            # Record audit
            self._audit_entries.append({
                "timestamp": datetime.now().isoformat(),
                "operation": "rollback_distributed",
                "execution_id": execution_id,
                "actor_id": actor.actor_id,
                "checkpoint_id": checkpoint_id,
                "success": result.success,
                "error": result.error,
            })
        
        return results
    
    def verify_consistency_across_actors(
        self,
        execution_id: str,
        checkpoint_id: int,
    ) -> Dict[str, Any]:
        """
        Verify that all actors have consistent state at checkpoint.
        
        Critical for transaction consistency debugging.
        """
        commit_ids = []
        actor_states = []
        
        for actor in self._pool.actors:
            commit_id = actor.get_commit_at_checkpoint(execution_id, checkpoint_id)
            state = actor.get_state(execution_id)
            
            if commit_id:
                commit_ids.append(commit_id)
                actor_states.append({
                    "actor_id": actor.actor_id,
                    "commit_id": commit_id,
                    "current_step": state.current_step if state else None,
                })
        
        # Check if all actors have same commit
        unique_commits = set(commit_ids)
        is_consistent = len(unique_commits) <= 1
        
        return {
            "checkpoint_id": checkpoint_id,
            "is_consistent": is_consistent,
            "unique_commits": len(unique_commits),
            "commit_ids": list(unique_commits),
            "actor_states": actor_states,
        }
    
    def track_same_commit_scenario(
        self,
        execution_id: str,
        file_paths: List[str],
        num_actors: int,
    ) -> Dict[str, Any]:
        """
        Test same-commit scenario across actors.
        
        All actors track same files at same checkpoint.
        """
        results = []
        checkpoint_id = 1
        
        for i in range(num_actors):
            actor = self._pool.get_actor(i % len(self._pool.actors))
            result = actor.track_files(
                f"{execution_id}-actor-{i}",
                file_paths,
                checkpoint_id,
            )
            results.append(result)
        
        # Analyze results
        commit_ids = [r.commit_id for r in results if r.success]
        file_hashes = {}
        
        for r in results:
            if r.success:
                commit = self._commit_repo.get_by_id(CommitId(r.commit_id))
                for memento in commit.mementos:
                    if memento.file_path not in file_hashes:
                        file_hashes[memento.file_path] = set()
                    file_hashes[memento.file_path].add(memento.file_hash.value)
        
        return {
            "total_actors": num_actors,
            "successful_tracks": len(commit_ids),
            "unique_commits": len(set(commit_ids)),
            "file_hash_consistency": {
                path: len(hashes) == 1
                for path, hashes in file_hashes.items()
            },
        }
    
    def get_audit_entries(self) -> List[Dict[str, Any]]:
        """Get audit entries."""
        return list(self._audit_entries)


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def actor_pool(blob_repository, commit_repository, checkpoint_link_repository, event_bus):
    """Create simulated actor pool."""
    return SimulatedActorPool(
        num_actors=4,
        blob_repo=blob_repository,
        commit_repo=commit_repository,
        link_repo=checkpoint_link_repository,
        event_bus=event_bus,
    )


@pytest.fixture
def distributed_service(
    actor_pool,
    blob_repository,
    commit_repository,
    checkpoint_link_repository,
    event_bus,
):
    """Create distributed file service."""
    return DistributedFileService(
        actor_pool,
        blob_repository,
        commit_repository,
        checkpoint_link_repository,
        event_bus,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Ray Distributed File Tracking
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayDistributedFileTracking:
    """Tests for distributed file tracking operations."""
    
    def test_single_actor_track(self, actor_pool, sample_files):
        """Test single actor file tracking."""
        actor = actor_pool.get_actor(0)
        
        result = actor.track_files(
            "exec-single",
            [str(sample_files["csv"])],
            checkpoint_id=1,
        )
        
        assert result.success is True
        assert result.commit_id is not None
        assert result.files_tracked == 1
        assert result.actor_id == "actor-0"
    
    def test_pool_distributes_tasks(self, distributed_service, sample_files):
        """Test that pool distributes tasks across actors."""
        results = []
        
        for i in range(8):
            result = distributed_service.track_files_distributed(
                f"exec-dist-{i}",
                [str(sample_files["csv"])],
                checkpoint_id=i + 1,
            )
            results.append(result)
        
        # Tasks should be distributed (round-robin)
        actor_ids = [r.actor_id for r in results]
        unique_actors = set(actor_ids)
        
        assert len(unique_actors) >= 2  # At least 2 actors used
        assert all(r.success for r in results)
    
    def test_actor_state_isolation(self, actor_pool, sample_files):
        """Test that actors have isolated state."""
        actor0 = actor_pool.get_actor(0)
        actor1 = actor_pool.get_actor(1)
        
        # Track on actor 0
        actor0.track_files("exec-iso-0", [str(sample_files["csv"])], 1)
        
        # Track on actor 1
        actor1.track_files("exec-iso-1", [str(sample_files["json"])], 1)
        
        # States should be independent
        state0 = actor0.get_state("exec-iso-0")
        state1 = actor1.get_state("exec-iso-1")
        
        assert state0.execution_id != state1.execution_id
        assert state0.commits[1] != state1.commits[1]


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Rollback with Distributed Files
# ═══════════════════════════════════════════════════════════════════════════════


class TestRollbackWithDistributedFiles:
    """Tests for rollback operations in distributed environment."""
    
    def test_single_actor_rollback(self, actor_pool, sample_files):
        """Test rollback on single actor."""
        actor = actor_pool.get_actor(0)
        
        # Create multiple checkpoints
        actor.track_files("exec-rb", [str(sample_files["csv"])], 1)
        actor.track_files("exec-rb", [str(sample_files["json"])], 2)
        actor.track_files("exec-rb", [str(sample_files["txt"])], 3)
        
        # Rollback to checkpoint 2
        result = actor.rollback_to_checkpoint("exec-rb", 2)
        
        assert result.success is True
        
        # State should be at checkpoint 2
        state = actor.get_state("exec-rb")
        assert state.current_step == 2
    
    def test_distributed_rollback(self, distributed_service, sample_files, actor_pool):
        """Test rollback across all actors."""
        # Track on multiple actors
        for i in range(4):
            actor = actor_pool.get_actor(i)
            actor.track_files("exec-dist-rb", [str(sample_files["csv"])], 1)
            actor.track_files("exec-dist-rb", [str(sample_files["json"])], 2)
        
        # Rollback all actors
        results = distributed_service.rollback_distributed("exec-dist-rb", 1)
        
        # All should succeed
        assert all(r.success for r in results)
        
        # All actors should be at step 1
        for actor in actor_pool.actors:
            state = actor.get_state("exec-dist-rb")
            if state:
                assert state.current_step == 1
    
    def test_rollback_preserves_earlier_checkpoints(self, actor_pool, sample_files):
        """Test that rollback preserves earlier checkpoints."""
        actor = actor_pool.get_actor(0)
        
        # Create checkpoints
        for i in range(5):
            actor.track_files("exec-preserve", [str(sample_files["csv"])], i + 1)
        
        # Rollback to checkpoint 3
        actor.rollback_to_checkpoint("exec-preserve", 3)
        
        # Earlier checkpoints should still exist
        state = actor.get_state("exec-preserve")
        assert 1 in state.commits
        assert 2 in state.commits
        assert 3 in state.commits
    
    def test_rollback_audit_recorded(self, distributed_service, sample_files, actor_pool):
        """Test that rollback is recorded in audit."""
        actor = actor_pool.get_actor(0)
        actor.track_files("exec-audit-rb", [str(sample_files["csv"])], 1)
        actor.track_files("exec-audit-rb", [str(sample_files["json"])], 2)
        
        distributed_service.rollback_distributed("exec-audit-rb", 1, actor_id="actor-0")
        
        audit = distributed_service.get_audit_entries()
        rollback_entries = [e for e in audit if e["operation"] == "rollback_distributed"]
        
        assert len(rollback_entries) >= 1
        assert rollback_entries[0]["checkpoint_id"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Same-Commit Scenarios
# ═══════════════════════════════════════════════════════════════════════════════


class TestSameCommitScenarios:
    """Tests for same-commit scenarios across actors."""
    
    def test_same_file_different_actors(self, distributed_service, sample_files):
        """Test tracking same file on different actors."""
        result = distributed_service.track_same_commit_scenario(
            "exec-same",
            [str(sample_files["csv"])],
            num_actors=4,
        )
        
        # All tracks should succeed
        assert result["successful_tracks"] == 4
        
        # File hashes should be consistent (same content = same hash)
        for path, is_consistent in result["file_hash_consistency"].items():
            assert is_consistent is True
    
    def test_same_files_produce_same_hashes(self, actor_pool, sample_files):
        """Test that same files produce same blob hashes."""
        hashes = []
        
        for i in range(3):
            actor = actor_pool.get_actor(i)
            actor.track_files(f"exec-hash-{i}", [str(sample_files["csv"])], 1)
            
            state = actor.get_state(f"exec-hash-{i}")
            hash_value = list(state.file_hashes.values())[0].value
            hashes.append(hash_value)
        
        # All hashes should be identical
        assert len(set(hashes)) == 1
    
    def test_unique_commits_per_execution(self, distributed_service, sample_files):
        """Test that each execution gets unique commit ID."""
        result = distributed_service.track_same_commit_scenario(
            "exec-unique",
            [str(sample_files["csv"])],
            num_actors=4,
        )
        
        # Each execution should have unique commit
        assert result["unique_commits"] == 4


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Transaction Consistency
# ═══════════════════════════════════════════════════════════════════════════════


class TestTransactionConsistency:
    """Tests for transaction consistency in distributed environment."""
    
    def test_consistency_after_track(self, distributed_service, sample_files, actor_pool):
        """Test consistency after tracking on single actor."""
        actor = actor_pool.get_actor(0)
        actor.track_files("exec-cons", [str(sample_files["csv"])], 1)
        
        result = distributed_service.verify_consistency_across_actors("exec-cons", 1)
        
        # Only one actor has this execution
        assert len(result["actor_states"]) == 1
        assert result["actor_states"][0]["commit_id"] is not None
    
    def test_consistency_after_parallel_tracks(self, actor_pool, sample_files, distributed_service):
        """Test consistency after parallel tracking."""
        threads = []
        execution_id = "exec-parallel"
        
        def track_on_actor(actor, checkpoint):
            actor.track_files(execution_id, [str(sample_files["csv"])], checkpoint)
        
        # Track in parallel on all actors at same checkpoint
        for i, actor in enumerate(actor_pool.actors):
            t = threading.Thread(target=track_on_actor, args=(actor, 1))
            threads.append(t)
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Verify consistency
        result = distributed_service.verify_consistency_across_actors(execution_id, 1)
        
        # All actors should have commits
        assert len(result["actor_states"]) == 4
        
        # All should have valid commits
        for state in result["actor_states"]:
            assert state["commit_id"] is not None
    
    def test_consistency_after_rollback(self, actor_pool, sample_files, distributed_service):
        """Test consistency after rollback."""
        execution_id = "exec-cons-rb"
        
        # Track on all actors
        for actor in actor_pool.actors:
            actor.track_files(execution_id, [str(sample_files["csv"])], 1)
            actor.track_files(execution_id, [str(sample_files["json"])], 2)
        
        # Rollback all to checkpoint 1
        distributed_service.rollback_distributed(execution_id, 1)
        
        # Verify consistency at checkpoint 1
        result = distributed_service.verify_consistency_across_actors(execution_id, 1)
        
        # All actors should be at step 1
        for state in result["actor_states"]:
            assert state["current_step"] == 1
    
    def test_blob_repository_consistency(
        self,
        actor_pool,
        sample_files,
        blob_repository,
    ):
        """Test blob repository consistency across actors."""
        # Track same file on multiple actors
        for i, actor in enumerate(actor_pool.actors):
            actor.track_files(f"exec-blob-{i}", [str(sample_files["csv"])], 1)
        
        # Content-addressable storage should have single blob
        all_ids = blob_repository.get_all_ids()
        
        # Get file hash for the CSV file
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        
        # Same content should produce same hash (deduplication)
        assert memento.file_hash in all_ids
    
    def test_commit_repository_isolation(
        self,
        actor_pool,
        sample_files,
        commit_repository,
    ):
        """Test commit repository has isolated commits per execution."""
        # Track on multiple actors
        for i, actor in enumerate(actor_pool.actors):
            actor.track_files(f"exec-commit-{i}", [str(sample_files["csv"])], 1)
        
        # Each execution should have its own commit
        for i in range(4):
            commits = commit_repository.get_by_execution_id(f"exec-commit-{i}")
            assert len(commits) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Concurrent Actor File Operations
# ═══════════════════════════════════════════════════════════════════════════════


class TestConcurrentActorOperations:
    """Tests for concurrent operations across actors."""
    
    def test_concurrent_tracks_no_conflicts(self, actor_pool, sample_files):
        """Test concurrent tracking without conflicts."""
        results = []
        lock = threading.Lock()
        
        def track_async(actor, exec_id, checkpoint):
            result = actor.track_files(exec_id, [str(sample_files["csv"])], checkpoint)
            with lock:
                results.append(result)
        
        threads = []
        for i in range(10):
            actor = actor_pool.get_actor(i % 4)
            t = threading.Thread(
                target=track_async,
                args=(actor, f"exec-conc-{i}", i + 1),
            )
            threads.append(t)
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All should succeed
        assert len(results) == 10
        assert all(r.success for r in results)
    
    def test_concurrent_rollbacks(self, actor_pool, sample_files):
        """Test concurrent rollback operations."""
        # Setup: create checkpoints
        for actor in actor_pool.actors:
            actor.track_files("exec-conc-rb", [str(sample_files["csv"])], 1)
            actor.track_files("exec-conc-rb", [str(sample_files["json"])], 2)
        
        results = []
        lock = threading.Lock()
        
        def rollback_async(actor):
            result = actor.rollback_to_checkpoint("exec-conc-rb", 1)
            with lock:
                results.append(result)
        
        threads = [
            threading.Thread(target=rollback_async, args=(actor,))
            for actor in actor_pool.actors
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All should succeed
        assert all(r.success for r in results)
    
    def test_high_volume_distributed_operations(self, distributed_service, sample_files):
        """Test high volume of distributed operations."""
        results = []
        
        for i in range(50):
            result = distributed_service.track_files_distributed(
                f"exec-volume-{i}",
                [str(sample_files["csv"])],
                checkpoint_id=1,
            )
            results.append(result)
        
        # All should succeed
        assert all(r.success for r in results)
        
        # Audit should have all entries
        audit = distributed_service.get_audit_entries()
        assert len(audit) >= 50


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Error Recovery in Distributed Environment
# ═══════════════════════════════════════════════════════════════════════════════


class TestErrorRecoveryDistributed:
    """Tests for error recovery in distributed operations."""
    
    def test_actor_recovers_from_failed_track(self, actor_pool, sample_files):
        """Test actor recovery after failed track."""
        actor = actor_pool.get_actor(0)
        
        # Failed track
        result1 = actor.track_files("exec-recover", ["/bad/path.csv"], 1)
        assert result1.success is False
        
        # Should be able to track successfully after
        result2 = actor.track_files("exec-recover", [str(sample_files["csv"])], 1)
        assert result2.success is True
    
    def test_rollback_to_nonexistent_checkpoint(self, actor_pool, sample_files):
        """Test error handling for rollback to non-existent checkpoint."""
        actor = actor_pool.get_actor(0)
        actor.track_files("exec-no-cp", [str(sample_files["csv"])], 1)
        
        result = actor.rollback_to_checkpoint("exec-no-cp", 999)
        
        assert result.success is False
        assert "No commit at checkpoint" in result.error
    
    def test_partial_actor_failure(self, actor_pool, sample_files, distributed_service):
        """Test handling when some actors fail."""
        execution_id = "exec-partial-fail"
        
        # Manually cause one actor to have different state
        actor0 = actor_pool.get_actor(0)
        actor0.track_files(execution_id, [str(sample_files["csv"])], 1)
        
        # Rollback on actor that doesn't have the execution
        results = distributed_service.rollback_distributed(execution_id, 1)
        
        # Some may succeed, some may fail
        success_count = sum(1 for r in results if r.success)
        
        # At least one should succeed (actor0)
        assert success_count >= 1
    
    def test_audit_records_failures(self, distributed_service, sample_files):
        """Test that audit records failed operations."""
        # Attempt to track non-existent file
        result = distributed_service.track_files_distributed(
            "exec-audit-fail",
            ["/bad/file.csv"],
            checkpoint_id=1,
        )
        
        assert result.success is False
        
        # Audit should record failure
        audit = distributed_service.get_audit_entries()
        fail_entries = [e for e in audit if not e["success"]]
        
        assert len(fail_entries) >= 1
        assert fail_entries[0]["error"] is not None
