"""
Workspace Domain Model - Value Object for Isolated Execution Environments.

Represents an isolated workspace for parallel variant execution, rollback,
and fork operations. Each workspace provides complete file system isolation
to prevent race conditions during parallel execution.

Design Principles:
- SOLID: Single responsibility (workspace configuration and metadata)
- Immutable core: All computed paths derived from root_path
- Value Object: Equality based on workspace_id
- ACID support: Lock files for orphan cleanup

Architecture:
- WorkspaceConfig: Configuration for workspace creation
- Workspace: Value object representing an isolated workspace
- WorkspaceStrategy: Enum for isolation strategies
- LinkResult: Result of file link operation

Related Documents:
- docs/issues/WORKSPACE_ISOLATION_DESIGN.md
"""

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class WorkspaceStrategy(Enum):
    """
    Workspace isolation strategy.
    
    Determines how source files are copied/linked to the workspace.
    """
    WORKSPACE = "workspace"      # Full workspace with hard links (default)
    SNAPSHOT = "snapshot"        # File snapshot only (minimal)
    CLONE = "clone"              # Full copy (for cross-partition)
    NONE = "none"                # No isolation (for single execution)


class LinkMethod(Enum):
    """Method used to link source files to workspace."""
    HARDLINK = "hardlink"        # NTFS/POSIX hard link (efficient)
    COPY = "copy"                # Full copy (cross-partition fallback)
    SYMLINK = "symlink"          # Symbolic link (optional)


@dataclass(frozen=True)
class LinkResult:
    """
    Result of a file link operation.
    
    Value object capturing how a file was linked to workspace.
    """
    source_path: str
    target_path: str
    method: LinkMethod
    size_bytes: int              # 0 for hardlink (no new space), actual for copy


@dataclass(frozen=True)
class WorkspaceConfig:
    """
    Configuration for workspace creation.
    
    Value object specifying how to create and manage workspaces.
    Extends FileTrackingConfig with workspace-specific settings.
    """
    # Isolation settings
    enabled: bool = True
    strategy: WorkspaceStrategy = WorkspaceStrategy.WORKSPACE
    
    # Paths
    base_dir: Path | None = None       # Default: system temp
    
    # Lifecycle
    cleanup_on_complete: bool = True      # Auto-cleanup on success
    preserve_on_failure: bool = True      # Keep for debugging on failure
    
    # Efficiency
    use_hard_links: bool = True           # Use hard links when possible
    
    # Pre-execution
    pre_execution_snapshot: bool = True   # Snapshot source before execution
    
    # Orphan cleanup
    orphan_cleanup_grace_hours: float = 1.0  # Hours before cleaning orphans
    
    def get_base_dir(self) -> Path:
        """Get base directory, defaulting to system temp."""
        if self.base_dir:
            return self.base_dir
        
        # Default to system temp with wtb_workspaces subdirectory
        import tempfile
        return Path(tempfile.gettempdir()) / "wtb_workspaces"
    
    def validate_cross_partition(self, source_paths: list[Path]) -> list[Path]:
        """
        Check for cross-partition sources on Windows.
        
        Returns list of paths that are on different partitions from base_dir.
        These will require copy instead of hard link.
        """
        if sys.platform != "win32":
            return []
        
        base_drive = self.get_base_dir().drive.upper()
        cross_partition = [
            p for p in source_paths
            if Path(p).drive.upper() != base_drive
        ]
        return cross_partition


@dataclass
class Workspace:
    """
    Value Object - Isolated workspace for variant execution.
    
    Represents a complete isolated environment with:
    - Separate file system namespace
    - Input/output directory separation
    - Optional venv isolation
    - Lock file for orphan detection
    
    All paths are derived from root_path for consistency.
    
    Thread Safety:
    - Immutable after creation (fields computed from root_path)
    - Lock file provides process-level coordination
    
    ACID Properties:
    - Atomic: Workspace created with all directories or rolled back
    - Consistent: All paths validated on creation
    - Isolated: Each workspace has unique root_path
    - Durable: Lock file persists workspace metadata
    """
    # Identity
    workspace_id: str
    batch_test_id: str
    variant_name: str
    execution_id: str
    
    # Root path (all other paths derived from this)
    root_path: Path
    
    # Metadata
    created_at: datetime = field(default_factory=datetime.now)
    strategy: WorkspaceStrategy = WorkspaceStrategy.WORKSPACE
    
    # Source tracking
    source_snapshot_commit_id: str | None = None
    source_paths: list[str] = field(default_factory=list)
    
    # Venv tracking (Phase V1)
    venv_spec_hash: str | None = None
    venv_commit_id: str | None = None
    
    # Statistics
    file_count: int = 0
    total_size_bytes: int = 0
    
    # Internal state
    _original_cwd: Path | None = field(default=None, repr=False)
    _is_active: bool = field(default=False, repr=False)
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # Derived Paths (computed from root_path)
    # ═══════════════════════════════════════════════════════════════════════════════
    
    @property
    def input_dir(self) -> Path:
        """Directory for source/input files."""
        return self.root_path / "input"
    
    @property
    def output_dir(self) -> Path:
        """Directory for output files (isolated per workspace)."""
        return self.root_path / "output"
    
    @property
    def venv_dir(self) -> Path:
        """Directory for isolated virtual environment."""
        return self.root_path / ".venv"
    
    @property
    def python_path(self) -> Path:
        """Path to Python executable in workspace venv."""
        if sys.platform == "win32":
            return self.venv_dir / "Scripts" / "python.exe"
        return self.venv_dir / "bin" / "python"
    
    @property
    def lock_file(self) -> Path:
        """Lock file path for orphan detection."""
        return self.root_path / ".workspace_lock"
    
    @property
    def meta_file(self) -> Path:
        """Metadata file path."""
        return self.root_path / ".workspace_meta.json"
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # Workspace Activation (CWD Management)
    # ═══════════════════════════════════════════════════════════════════════════════
    
    def activate(self) -> None:
        """
        Activate workspace by changing CWD.
        
        Call this before executing variant code to ensure file writes
        go to the isolated workspace.
        
        NOT thread-safe: Should be called from the Ray actor's main thread.
        """
        if self._is_active:
            return
        
        self._original_cwd = Path.cwd()
        os.chdir(self.root_path)
        self._is_active = True
    
    def deactivate(self) -> None:
        """
        Deactivate workspace and restore original CWD.
        
        Call this after variant execution completes.
        """
        if not self._is_active:
            return
        
        if self._original_cwd:
            os.chdir(self._original_cwd)
        
        self._original_cwd = None
        self._is_active = False
    
    @property
    def is_active(self) -> bool:
        """Check if workspace is currently active (CWD set)."""
        return self._is_active
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # Path Operations
    # ═══════════════════════════════════════════════════════════════════════════════
    
    def get_relative_path(self, absolute_path: Path) -> Path | None:
        """
        Get path relative to workspace root.
        
        Args:
            absolute_path: Absolute path to convert
            
        Returns:
            Relative path if within workspace, None otherwise
        """
        try:
            return absolute_path.relative_to(self.root_path)
        except ValueError:
            return None
    
    def get_output_path(self, relative_path: str) -> Path:
        """
        Get absolute output path for a relative path.
        
        Args:
            relative_path: Path relative to output directory
            
        Returns:
            Absolute path in output directory
        """
        return self.output_dir / relative_path
    
    def get_input_path(self, relative_path: str) -> Path:
        """
        Get absolute input path for a relative path.
        
        Args:
            relative_path: Path relative to input directory
            
        Returns:
            Absolute path in input directory
        """
        return self.input_dir / relative_path
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # Output File Writing
    # ═══════════════════════════════════════════════════════════════════════════════
    
    def write_output_files(
        self, 
        output_files: dict[str, str],
        subdirectory: str | None = None,
    ) -> list[Path]:
        """
        Write output files from state to the workspace output directory.
        
        This bridges the gap between _output_files in workflow state (filename→content)
        and actual files on disk that FileTracker can track.
        
        Args:
            output_files: Dict mapping filename to content (e.g., {"result.json": "..."})
            subdirectory: Optional subdirectory within output_dir
            
        Returns:
            List of absolute paths to written files
            
        Example:
            # In node code:
            state["_output_files"] = {"result.json": json.dumps(data)}
            
            # After execution:
            workspace.write_output_files(state.get("_output_files", {}))
            # Creates: {workspace}/output/result.json
        """
        if not output_files:
            return []
        
        # Ensure output directory exists
        target_dir = self.output_dir
        if subdirectory:
            target_dir = target_dir / subdirectory
        target_dir.mkdir(parents=True, exist_ok=True)
        
        written_paths: list[Path] = []
        
        for filename, content in output_files.items():
            # Sanitize filename (remove path separators for security)
            safe_filename = Path(filename).name
            if not safe_filename:
                continue
                
            file_path = target_dir / safe_filename
            
            try:
                # Write content to file
                if isinstance(content, bytes):
                    file_path.write_bytes(content)
                else:
                    file_path.write_text(str(content), encoding="utf-8")
                
                written_paths.append(file_path)
            except Exception as e:
                # Log but don't fail - output files are best-effort
                import logging
                logging.getLogger(__name__).warning(
                    f"Failed to write output file {filename}: {e}"
                )
        
        return written_paths
    
    def collect_output_file_paths(self) -> list[Path]:
        """
        Collect all files in the output directory.
        
        Returns:
            List of absolute paths to all files in output_dir
        """
        if not self.output_dir.exists():
            return []
        
        return [f for f in self.output_dir.rglob("*") if f.is_file()]
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # Serialization
    # ═══════════════════════════════════════════════════════════════════════════════
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for storage/transmission."""
        return {
            "workspace_id": self.workspace_id,
            "batch_test_id": self.batch_test_id,
            "variant_name": self.variant_name,
            "execution_id": self.execution_id,
            "root_path": str(self.root_path),
            "created_at": self.created_at.isoformat(),
            "strategy": self.strategy.value,
            "source_snapshot_commit_id": self.source_snapshot_commit_id,
            "source_paths": self.source_paths,
            "venv_spec_hash": self.venv_spec_hash,
            "venv_commit_id": self.venv_commit_id,
            "file_count": self.file_count,
            "total_size_bytes": self.total_size_bytes,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Workspace":
        """Create Workspace from dictionary."""
        return cls(
            workspace_id=data["workspace_id"],
            batch_test_id=data["batch_test_id"],
            variant_name=data["variant_name"],
            execution_id=data["execution_id"],
            root_path=Path(data["root_path"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            strategy=WorkspaceStrategy(data.get("strategy", "workspace")),
            source_snapshot_commit_id=data.get("source_snapshot_commit_id"),
            source_paths=data.get("source_paths", []),
            venv_spec_hash=data.get("venv_spec_hash"),
            venv_commit_id=data.get("venv_commit_id"),
            file_count=data.get("file_count", 0),
            total_size_bytes=data.get("total_size_bytes", 0),
        )
    
    def save_metadata(self) -> None:
        """Save workspace metadata to meta file."""
        self.meta_file.write_text(
            json.dumps(self.to_dict(), indent=2),
            encoding="utf-8"
        )
    
    @classmethod
    def load_from_path(cls, root_path: Path) -> Optional["Workspace"]:
        """
        Load workspace from path if meta file exists.
        
        Args:
            root_path: Workspace root path
            
        Returns:
            Workspace if meta file exists and is valid, None otherwise
        """
        meta_file = root_path / ".workspace_meta.json"
        if not meta_file.exists():
            return None
        
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            return cls.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            return None
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # Lock File Operations (Orphan Detection)
    # ═══════════════════════════════════════════════════════════════════════════════
    
    def create_lock_file(self, pid: int, session_id: str) -> None:
        """
        Create lock file for orphan detection.
        
        Args:
            pid: Process ID owning this workspace
            session_id: WTB session identifier
        """
        import socket
        
        lock_data = {
            "pid": pid,
            "hostname": socket.gethostname(),
            "created_at": datetime.now().isoformat(),
            "session_id": session_id,
            "batch_test_id": self.batch_test_id,
            "workspace_id": self.workspace_id,
        }
        
        self.lock_file.write_text(
            json.dumps(lock_data, indent=2),
            encoding="utf-8"
        )
    
    def read_lock_file(self) -> dict[str, Any] | None:
        """
        Read lock file data.
        
        Returns:
            Lock file data if exists, None otherwise
        """
        if not self.lock_file.exists():
            return None
        
        try:
            return json.loads(self.lock_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    
    def remove_lock_file(self) -> bool:
        """
        Remove lock file.
        
        Returns:
            True if removed, False if not found
        """
        if self.lock_file.exists():
            self.lock_file.unlink()
            return True
        return False
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # Equality and Hashing
    # ═══════════════════════════════════════════════════════════════════════════════
    
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Workspace):
            return False
        return self.workspace_id == other.workspace_id
    
    def __hash__(self) -> int:
        return hash(self.workspace_id)


@dataclass(frozen=True)
class OrphanWorkspace:
    """
    Value object representing a detected orphan workspace.
    
    Used by WorkspaceManager for cleanup operations.
    """
    path: Path
    pid: int
    created_at: datetime
    age: float                   # Age in hours
    session_id: str | None = None
    batch_test_id: str | None = None


@dataclass
class CleanupReport:
    """
    Report from workspace cleanup operation.
    """
    orphans_found: int = 0
    orphans_cleaned: int = 0
    space_freed_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    
    @property
    def space_freed_mb(self) -> float:
        """Space freed in megabytes."""
        return self.space_freed_bytes / (1024 * 1024)


def compute_venv_spec_hash(
    python_version: str,
    dependencies: list[str],
    requirements_file_content: str | None = None,
    lock_file_content: str | None = None,
) -> str:
    """
    Compute hash for venv specification.
    
    Delegates to the canonical hash schema used by ``VenvSpec.compute_hash()``
    so that cache lookups, storage, and domain code all produce identical
    hashes for the same logical spec.
    
    Args:
        python_version: Python version string (e.g., "3.12")
        dependencies: List of package specifiers
        requirements_file_content: Optional requirements.txt content
        lock_file_content: Optional uv.lock content
        
    Returns:
        16-character hex SHA-256 hash of the venv specification
    """
    spec_data = {
        "python_version": python_version,
        "packages": sorted(dependencies),
        "requirements": requirements_file_content,
        "lock": lock_file_content,
    }
    
    spec_json = json.dumps(spec_data, sort_keys=True)
    return hashlib.sha256(spec_json.encode()).hexdigest()[:16]
