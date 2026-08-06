"""
WorkspaceManager - Central service for isolated workspace lifecycle.

Manages the complete lifecycle of isolated workspaces for parallel variant
execution, rollback, and fork operations. Implements orphan cleanup and
cross-platform file linking.

Design Principles:
- SOLID: Single responsibility (workspace management)
- ACID: Atomic creation/cleanup, consistent state, isolated workspaces
- DIP: Depends on abstractions (IFileTrackingService via config)

Architecture:
- WorkspaceManager: Singleton-like service, manages all workspaces
- Lock files: Process-level coordination for orphan detection
- Hard links: Efficient file sharing (with copy fallback)

Thread Safety:
- Thread-safe via RLock for all public methods
- Lock files for multi-process coordination

Related Documents:
- docs/issues/WORKSPACE_ISOLATION_DESIGN.md (Sections 4, 12)
"""

import hashlib
import logging
import os
import re
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from wtb.domain.events.workspace_events import (
    OrphanCleanupCompletedEvent,
    OrphanWorkspaceDetectedEvent,
    WorkspaceCleanedUpEvent,
    WorkspaceCreatedEvent,
)
from wtb.domain.models.workspace import (
    CleanupReport,
    LinkMethod,
    LinkResult,
    OrphanWorkspace,
    Workspace,
    WorkspaceConfig,
    WorkspaceStrategy,
)

if TYPE_CHECKING:
    from wtb.infrastructure.events.wtb_event_bus import WTBEventBus

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Exceptions
# ═══════════════════════════════════════════════════════════════════════════════


class WorkspaceManagerError(Exception):
    """Base exception for WorkspaceManager errors."""
    pass


class WorkspaceNotFoundError(WorkspaceManagerError):
    """Raised when workspace is not found."""
    pass


class WorkspaceCreationError(WorkspaceManagerError):
    """Raised when workspace creation fails."""
    pass


class WorkspaceCleanupError(WorkspaceManagerError):
    """Raised when workspace cleanup fails."""
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# File Link Utility
# ═══════════════════════════════════════════════════════════════════════════════


def create_file_link(
    source: Path,
    target: Path,
    use_hard_links: bool = True,
) -> LinkResult:
    """
    Create file link with cross-partition fallback.
    
    On Windows NTFS, hard links cannot cross drive partitions.
    This function detects cross-partition scenarios and falls back to copy.
    
    Args:
        source: Source file path
        target: Target file path
        use_hard_links: Whether to attempt hard links (vs always copy)
        
    Returns:
        LinkResult with method used and size information
        
    Raises:
        FileNotFoundError: If source does not exist
        OSError: If link/copy fails
    """
    if not source.exists():
        raise FileNotFoundError(f"Source file not found: {source}")
    
    # Ensure target directory exists
    target.parent.mkdir(parents=True, exist_ok=True)
    
    # Check for cross-partition on Windows
    if sys.platform == "win32" and use_hard_links:
        source_drive = source.drive.upper() if source.drive else ""
        target_drive = target.drive.upper() if target.drive else ""
        
        if source_drive != target_drive:
            logger.debug(
                f"Cross-partition link {source_drive} -> {target_drive}, "
                f"falling back to copy: {source}"
            )
            use_hard_links = False
    
    if use_hard_links:
        try:
            # Python 3.10+ has Path.hardlink_to()
            target.hardlink_to(source)
            return LinkResult(
                source_path=str(source),
                target_path=str(target),
                method=LinkMethod.HARDLINK,
                size_bytes=0,  # No new space used
            )
        except OSError as e:
            # Fallback reasons: permissions, max links reached, filesystem
            logger.debug(f"Hard link failed for {source}: {e}, falling back to copy")
    
    # Copy with metadata preservation
    shutil.copy2(source, target)
    size_bytes = target.stat().st_size
    
    return LinkResult(
        source_path=str(source),
        target_path=str(target),
        method=LinkMethod.COPY,
        size_bytes=size_bytes,
    )


def _is_process_alive(pid: int) -> bool:
    """
    Check if process with given PID is still running.
    
    Args:
        pid: Process ID to check
        
    Returns:
        True if process is running, False otherwise
    """
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    
    # Fallback without psutil
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    else:
        # POSIX: signal 0 checks existence without sending signal
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


# ═══════════════════════════════════════════════════════════════════════════════
# WorkspaceManager
# ═══════════════════════════════════════════════════════════════════════════════


def _safe_path_component(value: str) -> str:
    """Encode an external identifier as one deterministic path component."""
    text = str(value)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    slug = slug.strip("._-")[:40] or "id"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def _path_identity_key(path: Path) -> str:
    """Normalize aliases that name the same Windows path spelling."""
    normalized = os.path.normcase(os.path.normpath(os.fspath(path)))
    if os.name != "nt":
        return normalized

    extended_prefix = "\\\\?\\"
    extended_unc_prefix = extended_prefix + "unc\\"
    if normalized.startswith(extended_unc_prefix):
        return "\\\\" + normalized[len(extended_unc_prefix):]
    if normalized.startswith(extended_prefix):
        return normalized[len(extended_prefix):]
    return normalized


class WorkspaceManager:
    """
    Central service for isolated workspace lifecycle management.
    
    Responsibilities:
    - Create isolated workspaces with file linking
    - Manage workspace activation/deactivation
    - Track active workspaces
    - Cleanup workspaces (individual and batch)
    - Detect and cleanup orphan workspaces
    
    Thread Safety:
    - All public methods are thread-safe via RLock
    - Lock files provide process-level coordination
    
    ACID Properties:
    - Atomic: Workspace created with all directories or rolled back
    - Consistent: Workspace state always valid (via lock files)
    - Isolated: Each workspace has unique root_path
    - Durable: Metadata and lock files persist state
    
    Usage:
        manager = WorkspaceManager(config, event_bus)
        
        # Create workspace
        workspace = manager.create_workspace(
            batch_id="batch-001",
            variant_name="bm25",
            execution_id="exec-001",
            source_paths=[Path("./data/input.csv")],
        )
        
        # Activate during execution
        workspace.activate()
        try:
            # Execute variant code (writes go to workspace)
            pass
        finally:
            workspace.deactivate()
        
        # Cleanup
        manager.cleanup_workspace(workspace.workspace_id)
    """
    
    def __init__(
        self,
        config: WorkspaceConfig,
        event_bus: Optional["WTBEventBus"] = None,
        session_id: str | None = None,
    ):
        """
        Initialize WorkspaceManager.
        
        Args:
            config: Workspace configuration
            event_bus: Optional event bus for publishing events
            session_id: Optional session ID for lock files
        """
        self._config = config
        self._event_bus = event_bus
        self._session_id = session_id or f"session-{os.getpid()}"
        self._lock = threading.RLock()
        
        # Active workspaces: workspace_id -> Workspace
        self._workspaces: dict[str, Workspace] = {}

        # Immutable physical identity recorded when an isolated workspace is
        # created. Cleanup must match this map before touching the filesystem.
        self._workspace_roots: dict[str, Path] = {}
        
        # Batch tracking: batch_id -> set of workspace_ids
        self._batch_workspaces: dict[str, set] = {}
        
        # Anchor every later containment check to one canonical directory.
        configured_base_dir = Path(config.get_base_dir()).absolute()
        configured_base_dir.mkdir(parents=True, exist_ok=True)
        self._base_dir = configured_base_dir.resolve()
        
        # Run orphan cleanup on startup
        if config.enabled:
            try:
                self._cleanup_orphans()
            except Exception as e:
                logger.warning(f"Orphan cleanup on startup failed: {e}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Workspace Creation
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _managed_path(self, path: Path) -> Path:
        """Prove a path is canonical, unredirected, and below the base."""
        base_dir = self._base_dir
        candidate = Path(os.path.abspath(os.fspath(path)))
        try:
            candidate.relative_to(base_dir)
        except ValueError as exc:
            raise WorkspaceCleanupError(
                f"Refusing workspace path outside configured base: {candidate}"
            ) from exc
        if candidate == base_dir:
            raise WorkspaceCleanupError(
                "Refusing to treat the workspace base directory as a workspace"
            )
        resolved = candidate.resolve()
        if _path_identity_key(resolved) != _path_identity_key(candidate):
            raise WorkspaceCleanupError(
                "Refusing redirected workspace path: "
                f"{candidate} resolves to {resolved}"
            )
        return candidate

    def create_workspace(
        self,
        batch_id: str,
        variant_name: str,
        execution_id: str,
        source_paths: list[Path] | None = None,
        env_spec: Any | None = None,  # EnvSpec for venv
    ) -> Workspace:
        """
        Create isolated workspace for variant execution.
        
        Creates directory structure, copies/links source files, and
        creates lock file for orphan detection.
        
        Args:
            batch_id: Batch test ID for grouping
            variant_name: Variant name for identification
            execution_id: Execution ID for tracking
            source_paths: Optional list of source files to copy/link
            env_spec: Optional EnvSpec for venv creation (Phase V1)
            
        Returns:
            Workspace instance
            
        Raises:
            WorkspaceCreationError: If creation fails
        """
        if not self._config.enabled:
            # Return a "virtual" workspace with no isolation
            return self._create_no_isolation_workspace(
                batch_id, variant_name, execution_id
            )
        
        import uuid
        workspace_id = f"ws-{uuid.uuid4().hex[:12]}"
        workspace_dir: Path | None = None
        workspace_dir_created = False
        
        with self._lock:
            try:
                # Create workspace directory structure
                batch_component = _safe_path_component(batch_id)
                variant_component = _safe_path_component(variant_name)
                execution_component = _safe_path_component(execution_id)
                workspace_dir = self._managed_path(
                    self._base_dir
                    / f"batch_{batch_component}"
                    / f"{variant_component}_{execution_component}_{workspace_id}"
                )
                workspace_dir.mkdir(parents=True, exist_ok=False)
                workspace_dir_created = True
                workspace_dir = self._managed_path(workspace_dir)
                
                input_dir = workspace_dir / "input"
                output_dir = workspace_dir / "output"
                input_dir.mkdir(exist_ok=True)
                output_dir.mkdir(exist_ok=True)
                
                # Track file copying stats
                file_count = 0
                total_size = 0
                link_results: list[LinkResult] = []
                
                # Copy/link source files
                if source_paths:
                    for source_path in source_paths:
                        source = Path(source_path)
                        if source.is_file():
                            target = input_dir / source.name
                            result = create_file_link(
                                source,
                                target,
                                use_hard_links=self._config.use_hard_links,
                            )
                            link_results.append(result)
                            file_count += 1
                            total_size += result.size_bytes
                        elif source.is_dir():
                            # Copy directory tree
                            for src_file in source.rglob("*"):
                                if src_file.is_file():
                                    rel_path = src_file.relative_to(source)
                                    target = input_dir / source.name / rel_path
                                    target.parent.mkdir(parents=True, exist_ok=True)
                                    result = create_file_link(
                                        src_file,
                                        target,
                                        use_hard_links=self._config.use_hard_links,
                                    )
                                    link_results.append(result)
                                    file_count += 1
                                    total_size += result.size_bytes
                
                # Create workspace object
                workspace = Workspace(
                    workspace_id=workspace_id,
                    batch_test_id=batch_id,
                    variant_name=variant_name,
                    execution_id=execution_id,
                    root_path=workspace_dir,
                    created_at=datetime.now(),
                    strategy=self._config.strategy,
                    source_paths=[str(p) for p in (source_paths or [])],
                    file_count=file_count,
                    total_size_bytes=total_size,
                )
                
                # Create lock file
                workspace.create_lock_file(os.getpid(), self._session_id)
                
                # Save metadata
                workspace.save_metadata()
                
                # Track workspace
                self._workspaces[workspace_id] = workspace
                self._workspace_roots[workspace_id] = workspace_dir
                if batch_id not in self._batch_workspaces:
                    self._batch_workspaces[batch_id] = set()
                self._batch_workspaces[batch_id].add(workspace_id)
                
                logger.info(
                    f"Created workspace {workspace_id}: "
                    f"{workspace_dir}, {file_count} files, {total_size} bytes"
                )
                
                # Publish event
                self._publish_event(WorkspaceCreatedEvent(
                    workspace_id=workspace_id,
                    batch_test_id=batch_id,
                    execution_id=execution_id,
                    variant_name=variant_name,
                    workspace_path=str(workspace_dir),
                    file_count=file_count,
                    total_size_bytes=total_size,
                    strategy=self._config.strategy.value,
                ))
                
                return workspace
                
            except Exception as e:
                # Only remove a directory this call created, and revalidate it
                # immediately before deletion to avoid following redirects.
                cleanup_dir: Path | None = None
                if workspace_dir_created and workspace_dir is not None:
                    try:
                        cleanup_dir = self._managed_path(workspace_dir)
                    except WorkspaceCleanupError:
                        pass
                if cleanup_dir is not None and cleanup_dir.exists():
                    try:
                        shutil.rmtree(cleanup_dir)
                    except Exception:
                        pass
                logger.error(f"Workspace creation failed: {e}")
                raise WorkspaceCreationError(f"Failed to create workspace: {e}") from e
    
    def _create_no_isolation_workspace(
        self,
        batch_id: str,
        variant_name: str,
        execution_id: str,
    ) -> Workspace:
        """Create a virtual workspace with no actual isolation."""
        import uuid
        
        workspace = Workspace(
            workspace_id=f"ws-virtual-{uuid.uuid4().hex[:8]}",
            batch_test_id=batch_id,
            variant_name=variant_name,
            execution_id=execution_id,
            root_path=Path.cwd(),
            strategy=WorkspaceStrategy.NONE,
        )
        
        with self._lock:
            self._workspaces[workspace.workspace_id] = workspace
            if batch_id not in self._batch_workspaces:
                self._batch_workspaces[batch_id] = set()
            self._batch_workspaces[batch_id].add(workspace.workspace_id)
        
        return workspace
    
    def create_branch_workspace(
        self,
        source_workspace_id: str,
        fork_checkpoint_id: str,
        new_execution_id: str,
        file_commit_id: str | None = None,
    ) -> Workspace:
        """
        Create workspace for a fork/branch operation.
        
        Clones the source workspace state at the given checkpoint
        for independent execution.
        
        Args:
            source_workspace_id: Parent workspace ID
            fork_checkpoint_id: Checkpoint to fork from
            new_execution_id: New execution ID for branch
            file_commit_id: FileTracker commit ID to restore files from
            
        Returns:
            New Workspace for branch execution
            
        Raises:
            WorkspaceNotFoundError: If source workspace not found
            WorkspaceCreationError: If creation fails
        """
        with self._lock:
            source = self._workspaces.get(source_workspace_id)
            if not source:
                raise WorkspaceNotFoundError(
                    f"Source workspace not found: {source_workspace_id}"
                )
            
            # Create new workspace with source's batch_id
            new_workspace = self.create_workspace(
                batch_id=source.batch_test_id,
                variant_name=f"branch_{source.variant_name}",
                execution_id=new_execution_id,
                source_paths=[Path(p) for p in source.source_paths],
            )
            
            # Mark as fork from source
            new_workspace.source_snapshot_commit_id = file_commit_id
            new_workspace.save_metadata()
            
            return new_workspace
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Workspace Retrieval
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_workspace(self, workspace_id: str) -> Workspace | None:
        """
        Get workspace by ID.
        
        Args:
            workspace_id: Workspace ID
            
        Returns:
            Workspace if found, None otherwise
        """
        with self._lock:
            return self._workspaces.get(workspace_id)
    
    def get_workspace_by_execution(self, execution_id: str) -> Workspace | None:
        """
        Get workspace by execution ID.
        
        Args:
            execution_id: Execution ID
            
        Returns:
            Workspace if found, None otherwise
        """
        with self._lock:
            for workspace in self._workspaces.values():
                if workspace.execution_id == execution_id:
                    return workspace
            return None
    
    def list_workspaces(
        self,
        batch_id: str | None = None,
    ) -> list[Workspace]:
        """
        List all workspaces, optionally filtered by batch.
        
        Args:
            batch_id: Optional batch ID filter
            
        Returns:
            List of Workspace objects
        """
        with self._lock:
            if batch_id:
                workspace_ids = self._batch_workspaces.get(batch_id, set())
                return [
                    self._workspaces[wid]
                    for wid in workspace_ids
                    if wid in self._workspaces
                ]
            return list(self._workspaces.values())
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Workspace Cleanup
    # ═══════════════════════════════════════════════════════════════════════════
    
    def cleanup_workspace(
        self,
        workspace_id: str,
        reason: str = "completed",
        preserve: bool = False,
    ) -> bool:
        """
        Cleanup a single workspace.
        
        Removes workspace directory and tracking data.
        
        Args:
            workspace_id: Workspace ID to cleanup
            reason: Cleanup reason for audit trail
            preserve: If True, keep directory but remove tracking
            
        Returns:
            True if cleaned up, False if not found

        Raises:
            OSError: If the workspace directory cannot be removed. Tracking
                remains intact so callers can retry cleanup.
        """
        with self._lock:
            # Keep tracking until physical deletion succeeds. Callers such as
            # RayBatchTestRunner can then retry a failed cleanup safely.
            workspace = self._workspaces.get(workspace_id)
            if not workspace:
                return False
            
            files_removed = 0
            space_freed = 0
            workspace_root: Path | None = None
            if workspace.strategy is not WorkspaceStrategy.NONE:
                expected_root = self._workspace_roots.get(workspace_id)
                if expected_root is None:
                    raise WorkspaceCleanupError(
                        f"Missing registered root identity for {workspace_id}"
                    )
                workspace_root = self._managed_path(workspace.root_path)
                if workspace_root != expected_root:
                    raise WorkspaceCleanupError(
                        f"Workspace root identity mismatch for {workspace_id}: "
                        f"expected {expected_root}, got {workspace_root}"
                    )
            
            if (
                not preserve
                and workspace_root is not None
                and workspace_root.exists()
            ):
                # Calculate stats before removal
                for f in workspace_root.rglob("*"):
                    if f.is_file():
                        try:
                            files_removed += 1
                            space_freed += f.stat().st_size
                        except OSError:
                            pass
                
                try:
                    shutil.rmtree(workspace_root)
                except Exception:
                    logger.exception(
                        "Failed to remove workspace directory %s",
                        workspace_root,
                    )
                    raise

            self._workspaces.pop(workspace_id, None)
            self._workspace_roots.pop(workspace_id, None)
            if workspace.batch_test_id in self._batch_workspaces:
                batch_workspaces = self._batch_workspaces[workspace.batch_test_id]
                batch_workspaces.discard(workspace_id)
                if not batch_workspaces:
                    self._batch_workspaces.pop(workspace.batch_test_id, None)
            
            logger.info(
                f"Cleaned up workspace {workspace_id}: "
                f"reason={reason}, preserved={preserve}"
            )
            
            # Publish event only after cleanup reaches a committed state.
            self._publish_event(WorkspaceCleanedUpEvent(
                workspace_id=workspace_id,
                reason=reason,
                files_removed=files_removed,
                space_freed_bytes=space_freed,
                preserved=preserve,
            ))
            
            return True
    
    def cleanup_batch(self, batch_id: str, reason: str = "batch_cleanup") -> int:
        """
        Cleanup all workspaces for a batch.
        
        Args:
            batch_id: Batch ID to cleanup
            reason: Cleanup reason for audit trail
            
        Returns:
            Number of workspaces cleaned up
        """
        with self._lock:
            workspace_ids = list(self._batch_workspaces.get(batch_id, set()))
            cleanup_physical_batch_dir = (
                self._config.enabled
                if not workspace_ids
                else any(
                    workspace_id in self._workspace_roots
                    for workspace_id in workspace_ids
                )
            )
        
        count = 0
        for workspace_id in workspace_ids:
            if self.cleanup_workspace(workspace_id, reason=reason):
                count += 1

        if not cleanup_physical_batch_dir:
            return count
        
        # Remove batch directory if empty
        batch_dir = self._managed_path(
            self._base_dir
            / f"batch_{_safe_path_component(batch_id)}"
        )
        if batch_dir.exists() and not any(batch_dir.iterdir()):
            batch_dir.rmdir()
        
        return count
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Orphan Cleanup
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _cleanup_orphans(self) -> CleanupReport:
        """
        Clean up zombie workspaces from dead sessions.
        
        Scans base directory for workspaces with lock files pointing to
        dead processes. Removes workspaces older than grace period.
        
        Returns:
            CleanupReport with statistics
        """
        orphans: list[OrphanWorkspace] = []
        grace_hours = self._config.orphan_cleanup_grace_hours
        
        # Scan for orphan workspaces
        for discovered_batch_dir in self._base_dir.iterdir():
            if not discovered_batch_dir.name.startswith("batch_"):
                continue
            try:
                batch_dir = self._managed_path(discovered_batch_dir)
            except WorkspaceCleanupError as exc:
                logger.warning(
                    "Skipping redirected orphan batch path %s: %s",
                    discovered_batch_dir,
                    exc,
                )
                continue
            if not batch_dir.is_dir():
                continue

            for discovered_ws_dir in batch_dir.iterdir():
                try:
                    ws_dir = self._managed_path(discovered_ws_dir)
                except WorkspaceCleanupError as exc:
                    logger.warning(
                        "Skipping redirected orphan workspace path %s: %s",
                        discovered_ws_dir,
                        exc,
                    )
                    continue
                if not ws_dir.is_dir():
                    continue
                
                lock_file = ws_dir / ".workspace_lock"
                if not lock_file.exists():
                    continue
                
                try:
                    import json
                    lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
                    pid = lock_data.get("pid", 0)
                    created_at_str = lock_data.get("created_at", "")
                    
                    if created_at_str:
                        created_at = datetime.fromisoformat(created_at_str)
                    else:
                        created_at = datetime.fromtimestamp(lock_file.stat().st_mtime)
                    
                    age_hours = (datetime.now() - created_at).total_seconds() / 3600
                    
                    if not _is_process_alive(pid):
                        orphans.append(OrphanWorkspace(
                            path=ws_dir,
                            pid=pid,
                            created_at=created_at,
                            age=age_hours,
                            session_id=lock_data.get("session_id"),
                            batch_test_id=lock_data.get("batch_test_id"),
                        ))
                        
                except Exception as e:
                    logger.debug(f"Failed to read lock file {lock_file}: {e}")
        
        # Clean up orphans past grace period
        cleaned = 0
        space_freed = 0
        errors = []
        
        for orphan in orphans:
            action = "preserved"
            
            if orphan.age > grace_hours:
                try:
                    orphan_path = self._managed_path(orphan.path)
                    # Calculate space before removal
                    for f in orphan_path.rglob("*"):
                        if f.is_file():
                            try:
                                space_freed += f.stat().st_size
                            except OSError:
                                pass
                    
                    shutil.rmtree(orphan_path)
                    cleaned += 1
                    action = "cleaned"
                    logger.info(f"Cleaned orphan workspace: {orphan_path} (age={orphan.age:.1f}h)")
                    
                except Exception as e:
                    errors.append(f"Failed to clean {orphan.path}: {e}")
                    logger.warning(f"Failed to clean orphan workspace: {orphan.path}: {e}")
            else:
                logger.debug(f"Recent orphan, keeping: {orphan.path} (age={orphan.age:.1f}h)")
            
            # Publish event for each orphan
            self._publish_event(OrphanWorkspaceDetectedEvent(
                workspace_path=str(orphan.path),
                original_pid=orphan.pid,
                age_hours=orphan.age,
                action=action,
            ))
        
        report = CleanupReport(
            orphans_found=len(orphans),
            orphans_cleaned=cleaned,
            space_freed_bytes=space_freed,
            errors=errors,
        )
        
        if orphans:
            self._publish_event(OrphanCleanupCompletedEvent(
                orphans_found=len(orphans),
                orphans_cleaned=cleaned,
                space_freed_mb=report.space_freed_mb,
                errors=errors,
            ))
            logger.info(
                f"Orphan cleanup complete: {cleaned}/{len(orphans)} cleaned, "
                f"{report.space_freed_mb:.1f}MB freed"
            )
        
        return report
    
    def force_cleanup_orphans(self) -> CleanupReport:
        """
        Force cleanup of all orphan workspaces (ignores grace period).
        
        Returns:
            CleanupReport with statistics
        """
        # Temporarily set grace period to 0
        original_grace = self._config.orphan_cleanup_grace_hours
        self._config = WorkspaceConfig(
            **{
                **self._config.__dict__,
                "orphan_cleanup_grace_hours": 0,
            }
        )
        
        try:
            return self._cleanup_orphans()
        finally:
            self._config = WorkspaceConfig(
                **{
                    **self._config.__dict__,
                    "orphan_cleanup_grace_hours": original_grace,
                }
            )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Event Publishing
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _publish_event(self, event: Any) -> None:
        """Publish event to event bus if available."""
        if self._event_bus:
            try:
                self._event_bus.publish(event)
            except Exception as e:
                logger.error(f"Failed to publish event {event.event_type}: {e}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Context Manager
    # ═══════════════════════════════════════════════════════════════════════════
    
    def __enter__(self) -> "WorkspaceManager":
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Context manager exit - cleanup all workspaces."""
        for workspace_id in list(self._workspaces.keys()):
            try:
                self.cleanup_workspace(workspace_id, reason="context_exit")
            except Exception as e:
                logger.warning(f"Failed to cleanup workspace on exit: {e}")
        return False
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Statistics
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_stats(self) -> dict[str, Any]:
        """
        Get workspace manager statistics.
        
        Returns:
            Dict with counts and resource usage
        """
        with self._lock:
            total_size = sum(
                ws.total_size_bytes for ws in self._workspaces.values()
            )
            
            return {
                "active_workspaces": len(self._workspaces),
                "active_batches": len(self._batch_workspaces),
                "total_size_bytes": total_size,
                "total_size_mb": total_size / (1024 * 1024),
                "base_dir": str(self._base_dir),
                "strategy": self._config.strategy.value,
                "session_id": self._session_id,
            }
