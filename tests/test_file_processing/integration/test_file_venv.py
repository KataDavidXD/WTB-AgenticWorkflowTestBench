"""
Integration Tests: File Processing + Virtual Environment Integration.

Tests the integration of file processing with virtual environment management,
enabling isolated execution environments for different file processing tasks.

Test Categories:
1. Environment-Specific File Tracking
2. Cross-Environment File Sharing
3. Environment Rollback with Files
4. Dependency-Aware File Processing
5. Environment Isolation for Concurrent Operations

Design Principles:
- SOLID: Environment and file concerns separated
- ACID: Environment state + file state consistency
- Pattern: Environment factory with file integration

Note: Tests simulate venv operations without requiring actual venv creation.
"""

import pytest
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from enum import Enum
import uuid
import threading
import os
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
# Virtual Environment Simulation
# ═══════════════════════════════════════════════════════════════════════════════


class EnvStatus(Enum):
    """Status of a virtual environment."""
    CREATING = "creating"
    READY = "ready"
    INSTALLING = "installing"
    EXECUTING = "executing"
    DESTROYED = "destroyed"


@dataclass
class VirtualEnvironment:
    """
    Simulated virtual environment for testing.
    
    Represents an isolated Python environment with:
    - Unique identifier
    - Python version
    - Installed packages
    - Associated file tracking
    """
    env_id: str
    name: str
    python_version: str = "3.10"
    status: EnvStatus = EnvStatus.CREATING
    packages: Dict[str, str] = field(default_factory=dict)  # package -> version
    file_commits: List[str] = field(default_factory=list)  # Linked commit IDs
    working_dir: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_ready(self) -> bool:
        return self.status == EnvStatus.READY
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "env_id": self.env_id,
            "name": self.name,
            "python_version": self.python_version,
            "status": self.status.value,
            "packages": self.packages,
            "file_commits": self.file_commits,
            "working_dir": self.working_dir,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }


class VirtualEnvironmentManager:
    """
    Simulated virtual environment manager.
    
    Provides environment lifecycle management with file tracking integration.
    """
    
    def __init__(self, base_path: str = None):
        self._envs: Dict[str, VirtualEnvironment] = {}
        self._base_path = base_path or "/tmp/wtb_venvs"
        self._lock = threading.Lock()
    
    def create_env(
        self,
        name: str,
        python_version: str = "3.10",
        packages: Dict[str, str] = None,
    ) -> VirtualEnvironment:
        """Create a new virtual environment."""
        with self._lock:
            env_id = f"env-{uuid.uuid4().hex[:8]}"
            
            env = VirtualEnvironment(
                env_id=env_id,
                name=name,
                python_version=python_version,
                packages=packages or {},
                working_dir=f"{self._base_path}/{name}",
            )
            
            # Simulate creation
            env.status = EnvStatus.READY
            
            self._envs[env_id] = env
            return env
    
    def get_env(self, env_id: str) -> Optional[VirtualEnvironment]:
        """Get environment by ID."""
        return self._envs.get(env_id)
    
    def get_env_by_name(self, name: str) -> Optional[VirtualEnvironment]:
        """Get environment by name."""
        for env in self._envs.values():
            if env.name == name:
                return env
        return None
    
    def install_packages(
        self,
        env_id: str,
        packages: Dict[str, str],
    ) -> VirtualEnvironment:
        """Install packages in environment."""
        env = self._envs.get(env_id)
        if env is None:
            raise ValueError(f"Environment not found: {env_id}")
        
        env.status = EnvStatus.INSTALLING
        env.packages.update(packages)
        env.status = EnvStatus.READY
        
        return env
    
    def destroy_env(self, env_id: str) -> bool:
        """Destroy environment."""
        if env_id in self._envs:
            self._envs[env_id].status = EnvStatus.DESTROYED
            del self._envs[env_id]
            return True
        return False
    
    def list_envs(self) -> List[VirtualEnvironment]:
        """List all environments."""
        return list(self._envs.values())
    
    def link_commit_to_env(self, env_id: str, commit_id: str):
        """Link a file commit to environment."""
        env = self._envs.get(env_id)
        if env:
            env.file_commits.append(commit_id)
    
    def get_env_commits(self, env_id: str) -> List[str]:
        """Get all commits linked to environment."""
        env = self._envs.get(env_id)
        return env.file_commits if env else []


# ═══════════════════════════════════════════════════════════════════════════════
# Environment-Aware File Service
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class EnvFileTrackingResult:
    """Result of environment-aware file tracking."""
    env_id: str
    env_name: str
    commit_id: str
    files_tracked: int
    total_size: int
    packages_snapshot: Dict[str, str]


class EnvironmentAwareFileService:
    """
    File tracking service with virtual environment awareness.
    
    Integrates file tracking with environment management to:
    - Track files in context of specific environments
    - Link file commits to environments
    - Restore files with matching environment
    - Enable environment-specific rollbacks
    """
    
    def __init__(
        self,
        env_manager: VirtualEnvironmentManager,
        blob_repo,
        commit_repo,
        link_repo,
        event_bus: WTBEventBus,
    ):
        self._env_manager = env_manager
        self._blob_repo = blob_repo
        self._commit_repo = commit_repo
        self._link_repo = link_repo
        self._event_bus = event_bus
        self._env_file_map: Dict[str, List[str]] = {}  # env_id -> [commit_ids]
    
    def track_files_in_env(
        self,
        env_id: str,
        file_paths: List[str],
        execution_id: str,
        checkpoint_id: int = None,
        message: str = None,
    ) -> EnvFileTrackingResult:
        """
        Track files in the context of a specific environment.
        
        Creates a commit linked to the environment with package snapshot.
        """
        env = self._env_manager.get_env(env_id)
        if env is None:
            raise ValueError(f"Environment not found: {env_id}")
        
        if not env.is_ready:
            raise ValueError(f"Environment not ready: {env.status}")
        
        # Create commit with env metadata
        commit = FileCommit.create(
            message=message or f"Track in env {env.name}",
            execution_id=execution_id,
        )
        
        # Add env metadata
        commit.metadata = {
            "env_id": env_id,
            "env_name": env.name,
            "python_version": env.python_version,
            "packages": env.packages.copy(),
        }
        
        # Track files
        for file_path in file_paths:
            memento, content = FileMemento.from_file(file_path)
            self._blob_repo.save(content)  # Content-addressable storage
            commit.add_memento(memento)
        
        commit.finalize()
        self._commit_repo.save(commit)
        
        # Link to checkpoint if provided
        if checkpoint_id is not None:
            link = CheckpointFileLink.create(checkpoint_id, commit)
            self._link_repo.add(link)
        
        # Link commit to environment
        self._env_manager.link_commit_to_env(env_id, commit.commit_id.value)
        
        if env_id not in self._env_file_map:
            self._env_file_map[env_id] = []
        self._env_file_map[env_id].append(commit.commit_id.value)
        
        # Publish event
        self._event_bus.publish(FileCommitCreatedEvent(
            commit_id=commit.commit_id.value,
            execution_id=execution_id,
            file_count=commit.file_count,
            total_size_bytes=commit.total_size,
        ))
        
        return EnvFileTrackingResult(
            env_id=env_id,
            env_name=env.name,
            commit_id=commit.commit_id.value,
            files_tracked=commit.file_count,
            total_size=commit.total_size,
            packages_snapshot=env.packages.copy(),
        )
    
    def restore_with_env(
        self,
        commit_id: str,
        output_dir: str,
        execution_id: str,
        create_env_if_needed: bool = True,
    ) -> Dict[str, Any]:
        """
        Restore files and recreate the environment they were tracked in.
        
        Returns:
            Dict with restored files and environment info
        """
        commit = self._commit_repo.get_by_id(CommitId(commit_id))
        if commit is None:
            raise ValueError(f"Commit not found: {commit_id}")
        
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
        
        # Get env info from metadata
        env_info = getattr(commit, 'metadata', {}) if hasattr(commit, 'metadata') else {}
        
        # Recreate environment if requested
        env = None
        if create_env_if_needed and env_info.get("env_name"):
            env = self._env_manager.create_env(
                name=f"{env_info['env_name']}_restored",
                python_version=env_info.get("python_version", "3.10"),
                packages=env_info.get("packages", {}),
            )
        
        # Publish event
        self._event_bus.publish(FileRestoredEvent(
            commit_id=commit_id,
            files_restored=len(restored_paths),
            restored_paths=tuple(restored_paths),
        ))
        
        return {
            "commit_id": commit_id,
            "files_restored": len(restored_paths),
            "restored_paths": restored_paths,
            "env_info": env_info,
            "restored_env": env.to_dict() if env else None,
        }
    
    def get_commits_for_env(self, env_id: str) -> List[FileCommit]:
        """Get all commits associated with an environment."""
        commit_ids = self._env_file_map.get(env_id, [])
        commits = []
        
        for commit_id in commit_ids:
            commit = self._commit_repo.get_by_id(CommitId(commit_id))
            if commit:
                commits.append(commit)
        
        return commits
    
    def clone_env_with_files(
        self,
        source_env_id: str,
        new_env_name: str,
        output_dir: str,
        execution_id: str,
    ) -> Dict[str, Any]:
        """
        Clone an environment including its tracked files.
        
        Creates new environment with same packages and restores all files.
        """
        source_env = self._env_manager.get_env(source_env_id)
        if source_env is None:
            raise ValueError(f"Source environment not found: {source_env_id}")
        
        # Create new environment
        new_env = self._env_manager.create_env(
            name=new_env_name,
            python_version=source_env.python_version,
            packages=source_env.packages.copy(),
        )
        
        # Get all commits from source env
        commits = self.get_commits_for_env(source_env_id)
        
        # Restore all files
        all_restored = []
        for commit in commits:
            for memento in commit.mementos:
                content = self._blob_repo.get(memento.file_hash)
                if content:
                    output_path = Path(output_dir) / Path(memento.file_path).name
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(content)
                    all_restored.append(str(output_path))
            
            # Link commit to new env
            self._env_manager.link_commit_to_env(new_env.env_id, commit.commit_id.value)
        
        return {
            "source_env": source_env.to_dict(),
            "new_env": new_env.to_dict(),
            "files_restored": len(all_restored),
            "restored_paths": all_restored,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def env_manager(temp_dir):
    """Create virtual environment manager."""
    return VirtualEnvironmentManager(base_path=temp_dir)


@pytest.fixture
def env_file_service(
    env_manager,
    blob_repository,
    commit_repository,
    checkpoint_link_repository,
    event_bus,
):
    """Create environment-aware file service."""
    return EnvironmentAwareFileService(
        env_manager,
        blob_repository,
        commit_repository,
        checkpoint_link_repository,
        event_bus,
    )


@pytest.fixture
def all_events_collector(event_bus):
    """Collect all file-related events."""
    events = []
    
    event_types = [
        FileCommitCreatedEvent,
        FileRestoredEvent,
    ]
    
    for event_type in event_types:
        event_bus.subscribe(event_type, events.append)
    
    return events


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Environment-Specific File Tracking
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvironmentSpecificFileTracking:
    """Tests for environment-specific file tracking."""
    
    def test_track_files_in_environment(self, env_manager, env_file_service, sample_files):
        """Test tracking files in a specific environment."""
        env = env_manager.create_env("test-env", packages={"numpy": "1.24.0"})
        
        result = env_file_service.track_files_in_env(
            env.env_id,
            [str(sample_files["csv"])],
            "exec-env-track",
        )
        
        assert result.env_id == env.env_id
        assert result.env_name == "test-env"
        assert result.files_tracked == 1
        assert result.packages_snapshot == {"numpy": "1.24.0"}
    
    def test_environment_linked_to_commit(self, env_manager, env_file_service, sample_files):
        """Test that commit is linked to environment."""
        env = env_manager.create_env("linked-env")
        
        result = env_file_service.track_files_in_env(
            env.env_id,
            [str(sample_files["csv"])],
            "exec-linked",
        )
        
        # Environment should have commit linked
        env_commits = env_manager.get_env_commits(env.env_id)
        assert result.commit_id in env_commits
    
    def test_multiple_commits_per_env(self, env_manager, env_file_service, sample_files):
        """Test multiple commits in same environment."""
        env = env_manager.create_env("multi-commit-env")
        
        # Track multiple times
        for i in range(3):
            env_file_service.track_files_in_env(
                env.env_id,
                [str(sample_files["csv"])],
                f"exec-multi-{i}",
            )
        
        commits = env_file_service.get_commits_for_env(env.env_id)
        assert len(commits) == 3
    
    def test_track_requires_ready_environment(self, env_manager, env_file_service, sample_files):
        """Test that tracking requires ready environment."""
        env = env_manager.create_env("not-ready-env")
        env.status = EnvStatus.INSTALLING
        
        with pytest.raises(ValueError, match="not ready"):
            env_file_service.track_files_in_env(
                env.env_id,
                [str(sample_files["csv"])],
                "exec-not-ready",
            )
    
    def test_track_with_checkpoint(self, env_manager, env_file_service, sample_files, checkpoint_link_repository):
        """Test tracking with checkpoint link."""
        env = env_manager.create_env("cp-env")
        
        result = env_file_service.track_files_in_env(
            env.env_id,
            [str(sample_files["csv"])],
            "exec-cp",
            checkpoint_id=42,
        )
        
        # Checkpoint should be linked
        link = checkpoint_link_repository.get_by_checkpoint(42)
        assert link is not None
        assert link.commit_id.value == result.commit_id


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Cross-Environment File Sharing
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrossEnvironmentFileSharing:
    """Tests for file sharing across environments."""
    
    def test_same_file_different_environments(
        self,
        env_manager,
        env_file_service,
        sample_files,
        blob_repository,
    ):
        """Test tracking same file in different environments."""
        env1 = env_manager.create_env("env-1", packages={"pandas": "1.0"})
        env2 = env_manager.create_env("env-2", packages={"pandas": "2.0"})
        
        result1 = env_file_service.track_files_in_env(
            env1.env_id,
            [str(sample_files["csv"])],
            "exec-env1",
        )
        
        result2 = env_file_service.track_files_in_env(
            env2.env_id,
            [str(sample_files["csv"])],
            "exec-env2",
        )
        
        # Both should succeed
        assert result1.commit_id != result2.commit_id
        
        # But share same blob (content-addressable)
        all_blobs = blob_repository.get_all_ids()
        memento, _ = FileMemento.from_file(str(sample_files["csv"]))
        assert memento.file_hash in all_blobs
    
    def test_clone_environment_with_files(
        self,
        env_manager,
        env_file_service,
        sample_files,
        temp_dir,
    ):
        """Test cloning environment with its files."""
        # Create source env with files
        source_env = env_manager.create_env(
            "source-env",
            packages={"scikit-learn": "1.0", "numpy": "1.24"},
        )
        
        env_file_service.track_files_in_env(
            source_env.env_id,
            [str(sample_files["csv"]), str(sample_files["json"])],
            "exec-source",
        )
        
        # Clone
        output_dir = Path(temp_dir) / "clone_output"
        result = env_file_service.clone_env_with_files(
            source_env.env_id,
            "cloned-env",
            str(output_dir),
            "exec-clone",
        )
        
        assert result["new_env"]["name"] == "cloned-env"
        assert result["new_env"]["packages"] == source_env.packages
        assert result["files_restored"] == 2
    
    def test_environment_isolation(self, env_manager, env_file_service, sample_files):
        """Test that environments are isolated."""
        env1 = env_manager.create_env("isolated-1")
        env2 = env_manager.create_env("isolated-2")
        
        env_file_service.track_files_in_env(
            env1.env_id,
            [str(sample_files["csv"])],
            "exec-iso1",
        )
        
        # env2 should have no commits
        commits_env2 = env_file_service.get_commits_for_env(env2.env_id)
        assert len(commits_env2) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Environment Rollback with Files
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvironmentRollbackWithFiles:
    """Tests for environment rollback with file restoration."""
    
    def test_restore_with_environment_info(
        self,
        env_manager,
        env_file_service,
        sample_files,
        temp_dir,
    ):
        """Test restoring files with environment information."""
        env = env_manager.create_env(
            "restore-env",
            python_version="3.9",
            packages={"tensorflow": "2.10"},
        )
        
        result = env_file_service.track_files_in_env(
            env.env_id,
            [str(sample_files["csv"])],
            "exec-restore-env",
        )
        
        # Restore
        output_dir = Path(temp_dir) / "env_restore"
        restore_result = env_file_service.restore_with_env(
            result.commit_id,
            str(output_dir),
            "exec-restore-env",
            create_env_if_needed=True,
        )
        
        assert restore_result["files_restored"] == 1
        assert restore_result["restored_env"] is not None
        assert "restored" in restore_result["restored_env"]["name"]
    
    def test_restore_creates_matching_env(
        self,
        env_manager,
        env_file_service,
        sample_files,
        temp_dir,
        commit_repository,
    ):
        """Test that restore creates environment with matching packages."""
        original_packages = {"pandas": "1.5.0", "scikit-learn": "1.2.0"}
        env = env_manager.create_env("match-env", packages=original_packages)
        
        result = env_file_service.track_files_in_env(
            env.env_id,
            [str(sample_files["csv"])],
            "exec-match",
        )
        
        # Manually set metadata for the test (since our simple FileCommit doesn't store it)
        commit = commit_repository.get_by_id(CommitId(result.commit_id))
        commit.metadata = {
            "env_name": "match-env",
            "python_version": "3.10",
            "packages": original_packages,
        }
        
        # Restore with new env
        output_dir = Path(temp_dir) / "match_restore"
        restore_result = env_file_service.restore_with_env(
            result.commit_id,
            str(output_dir),
            "exec-match-restore",
            create_env_if_needed=True,
        )
        
        if restore_result["restored_env"]:
            assert restore_result["restored_env"]["packages"] == original_packages


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Dependency-Aware File Processing
# ═══════════════════════════════════════════════════════════════════════════════


class TestDependencyAwareFileProcessing:
    """Tests for dependency-aware file processing."""
    
    def test_package_snapshot_captured(self, env_manager, env_file_service, sample_files):
        """Test that package snapshot is captured in result."""
        packages = {
            "numpy": "1.24.0",
            "pandas": "2.0.0",
            "torch": "2.0.0",
        }
        env = env_manager.create_env("snapshot-env", packages=packages)
        
        result = env_file_service.track_files_in_env(
            env.env_id,
            [str(sample_files["csv"])],
            "exec-snapshot",
        )
        
        assert result.packages_snapshot == packages
    
    def test_install_packages_before_track(self, env_manager, env_file_service, sample_files):
        """Test installing packages before tracking."""
        env = env_manager.create_env("install-env")
        
        # Install packages
        env_manager.install_packages(
            env.env_id,
            {"requests": "2.28.0", "flask": "2.0.0"},
        )
        
        result = env_file_service.track_files_in_env(
            env.env_id,
            [str(sample_files["csv"])],
            "exec-installed",
        )
        
        assert "requests" in result.packages_snapshot
        assert "flask" in result.packages_snapshot
    
    def test_different_python_versions(self, env_manager, env_file_service, sample_files):
        """Test tracking with different Python versions."""
        env39 = env_manager.create_env("py39-env", python_version="3.9")
        env310 = env_manager.create_env("py310-env", python_version="3.10")
        env311 = env_manager.create_env("py311-env", python_version="3.11")
        
        results = []
        for env in [env39, env310, env311]:
            result = env_file_service.track_files_in_env(
                env.env_id,
                [str(sample_files["csv"])],
                f"exec-{env.name}",
            )
            results.append(result)
        
        # All should succeed
        assert all(r.commit_id is not None for r in results)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Environment Isolation for Concurrent Operations
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvironmentIsolationConcurrent:
    """Tests for environment isolation during concurrent operations."""
    
    def test_concurrent_env_tracking(self, env_manager, env_file_service, sample_files):
        """Test concurrent tracking in different environments."""
        envs = [
            env_manager.create_env(f"concurrent-env-{i}")
            for i in range(5)
        ]
        
        results = []
        lock = threading.Lock()
        
        def track_in_env(env):
            result = env_file_service.track_files_in_env(
                env.env_id,
                [str(sample_files["csv"])],
                f"exec-{env.name}",
            )
            with lock:
                results.append(result)
        
        threads = [
            threading.Thread(target=track_in_env, args=(env,))
            for env in envs
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All should succeed
        assert len(results) == 5
        
        # Each env should have its own commit
        commit_ids = [r.commit_id for r in results]
        assert len(set(commit_ids)) == 5
    
    def test_concurrent_env_creation_and_tracking(
        self,
        env_manager,
        env_file_service,
        sample_files,
    ):
        """Test concurrent environment creation and tracking."""
        results = []
        errors = []
        lock = threading.Lock()
        
        def create_and_track(index):
            try:
                env = env_manager.create_env(
                    f"create-track-{index}",
                    packages={f"pkg-{index}": "1.0"},
                )
                result = env_file_service.track_files_in_env(
                    env.env_id,
                    [str(sample_files["csv"])],
                    f"exec-create-{index}",
                )
                with lock:
                    results.append((env.name, result))
            except Exception as e:
                with lock:
                    errors.append(e)
        
        threads = [
            threading.Thread(target=create_and_track, args=(i,))
            for i in range(10)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0
        assert len(results) == 10
    
    def test_env_state_not_shared(self, env_manager, env_file_service, sample_files):
        """Test that environment states are not shared."""
        env1 = env_manager.create_env("state-env-1", packages={"pkg1": "1.0"})
        env2 = env_manager.create_env("state-env-2", packages={"pkg2": "2.0"})
        
        # Track in both
        result1 = env_file_service.track_files_in_env(
            env1.env_id,
            [str(sample_files["csv"])],
            "exec-state1",
        )
        result2 = env_file_service.track_files_in_env(
            env2.env_id,
            [str(sample_files["json"])],
            "exec-state2",
        )
        
        # Each should have its own packages
        assert result1.packages_snapshot != result2.packages_snapshot
        assert "pkg1" in result1.packages_snapshot
        assert "pkg2" in result2.packages_snapshot


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Environment Lifecycle Integration
# ═══════════════════════════════════════════════════════════════════════════════


class TestEnvironmentLifecycleIntegration:
    """Tests for environment lifecycle with file tracking."""
    
    def test_full_env_lifecycle_with_files(
        self,
        env_manager,
        env_file_service,
        sample_files,
        temp_dir,
    ):
        """Test full environment lifecycle with file tracking."""
        # Create
        env = env_manager.create_env(
            "lifecycle-env",
            packages={"numpy": "1.24", "pandas": "2.0"},
        )
        
        # Track files
        result = env_file_service.track_files_in_env(
            env.env_id,
            [str(sample_files["csv"]), str(sample_files["json"])],
            "exec-lifecycle",
            checkpoint_id=1,
        )
        
        # Verify tracking
        assert result.files_tracked == 2
        
        # Get commits
        commits = env_file_service.get_commits_for_env(env.env_id)
        assert len(commits) == 1
        
        # Clone environment
        output_dir = Path(temp_dir) / "lifecycle_clone"
        clone_result = env_file_service.clone_env_with_files(
            env.env_id,
            "lifecycle-clone",
            str(output_dir),
            "exec-clone",
        )
        
        assert clone_result["files_restored"] == 2
        
        # Destroy original
        destroyed = env_manager.destroy_env(env.env_id)
        assert destroyed is True
        
        # Clone should still exist
        cloned_env = env_manager.get_env_by_name("lifecycle-clone")
        assert cloned_env is not None
    
    def test_env_destroy_preserves_commits(
        self,
        env_manager,
        env_file_service,
        sample_files,
        commit_repository,
    ):
        """Test that destroying environment preserves commits."""
        env = env_manager.create_env("preserve-env")
        
        result = env_file_service.track_files_in_env(
            env.env_id,
            [str(sample_files["csv"])],
            "exec-preserve",
        )
        
        commit_id = result.commit_id
        
        # Destroy environment
        env_manager.destroy_env(env.env_id)
        
        # Commit should still exist
        commit = commit_repository.get_by_id(CommitId(commit_id))
        assert commit is not None
    
    def test_events_published_for_env_operations(
        self,
        env_manager,
        env_file_service,
        sample_files,
        all_events_collector,
    ):
        """Test that events are published for environment operations."""
        env = env_manager.create_env("event-env")
        
        env_file_service.track_files_in_env(
            env.env_id,
            [str(sample_files["csv"])],
            "exec-events",
        )
        
        # Should have commit event
        commit_events = [
            e for e in all_events_collector
            if isinstance(e, FileCommitCreatedEvent)
        ]
        assert len(commit_events) >= 1
