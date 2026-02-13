"""
Outbox Pattern Processor.

Background worker that processes outbox events for cross-database
consistency verification between WTB, AgentGit, and FileTracker.

Design Principles:
- SOLID: Single responsibility per handler
- Dependency Inversion: Depends on protocols (interfaces)
- Fail-fast: Raises exceptions on verification failures
- ACID: Verification operations ensure cross-DB consistency

FileTracker Integration (2026-01-15):
- FILE_COMMIT_VERIFY: Verify commit exists with expected files
- FILE_BLOB_VERIFY: Verify individual blob integrity
- FILE_BATCH_VERIFY: Batch verification for batch test commits
- FILE_INTEGRITY_CHECK: Deep integrity verification with hash checking
- FILE_RESTORE_VERIFY: Verify files were correctly restored
- ROLLBACK_FILE_RESTORE: Coordinate file restoration during rollback
- ROLLBACK_VERIFY: Full rollback verification (state + files)

Usage:
    from wtb.domain.interfaces.file_tracking import IFileTrackingService
    
    processor = OutboxProcessor(
        wtb_db_url="sqlite:///data/wtb.db",
        checkpoint_repo=agentgit_checkpoint_repo,
        file_tracking_service=file_tracking_service,  # NEW!
    )
    processor.start()
"""

from typing import Callable, Dict, Optional, Any, Protocol, List
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import threading
import time
import logging
import hashlib

from wtb.domain.models.outbox import OutboxEvent, OutboxEventType, OutboxStatus
from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Protocol Definitions
# ═══════════════════════════════════════════════════════════════════════════════


class ICheckpointRepository(Protocol):
    """Protocol for AgentGit checkpoint repository access."""
    
    def get_by_id(self, checkpoint_id: int) -> Optional[Any]:
        """Get checkpoint by ID."""
        ...


class ICommitRepository(Protocol):
    """Protocol for FileTracker commit repository access."""
    
    def find_by_id(self, commit_id: str) -> Optional[Any]:
        """Find commit by ID. Returns commit with mementos if found."""
        ...
    
    def get_by_checkpoint_id(self, checkpoint_id: int) -> Optional[Any]:
        """Find commit linked to a checkpoint."""
        ...


class IBlobRepository(Protocol):
    """Protocol for FileTracker blob repository access."""
    
    def exists(self, content_hash: str) -> bool:
        """Check if blob with given hash exists."""
        ...
    
    def get(self, content_hash: str) -> Optional[bytes]:
        """Get blob content by hash."""
        ...


class IFileTrackingService(Protocol):
    """
    Protocol for FileTracker service integration.
    
    Follows DIP - OutboxProcessor depends on this abstraction,
    not on concrete FileTracker implementation.
    """
    
    def is_available(self) -> bool:
        """Check if service is available."""
        ...
    
    def get_commit_for_checkpoint(self, checkpoint_id: int) -> Optional[str]:
        """Get commit ID linked to a checkpoint."""
        ...
    
    def get_tracked_files(self, commit_id: str) -> List[Any]:
        """Get tracked files for a commit."""
        ...
    
    def restore_commit(self, commit_id: str) -> Any:
        """Restore files from a commit."""
        ...
    
    def restore_from_checkpoint(self, checkpoint_id: int) -> Any:
        """Restore files from checkpoint's linked commit."""
        ...
    
    def get_files_at_checkpoint(self, checkpoint_id: int) -> List[str]:
        """Get file paths at checkpoint (v1.9)."""
        ...


class IFileCleanupService(Protocol):
    """
    Protocol for file cleanup service integration (v1.9).
    
    Follows ISP - cleanup operations are separate from tracking.
    """
    
    def identify_orphaned_files(
        self,
        target_checkpoint_id: int,
        execution_id: str,
        current_workspace_path: Path,
        track_patterns: List[str],
        exclude_patterns: List[str],
        file_tracking_service: IFileTrackingService,
    ) -> List[str]:
        """Identify files created after checkpoint."""
        ...
    
    def cleanup_orphaned_files(
        self,
        checkpoint_id: int,
        execution_id: str,
        orphaned_paths: List[str],
        backup_dir: Optional[Path] = None,
        dry_run: bool = False,
        max_files: int = 100,
    ) -> Any:
        """Delete orphaned files with safety checks."""
        ...


# ═══════════════════════════════════════════════════════════════════════════════
# Verification Result Types
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class VerificationResult:
    """Result of a verification operation."""
    success: bool
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    verified_at: datetime = field(default_factory=datetime.now)
    
    @classmethod
    def ok(cls, message: str, **details) -> "VerificationResult":
        """Create successful verification result."""
        return cls(success=True, message=message, details=details)
    
    @classmethod
    def failed(cls, message: str, errors: List[str] = None, **details) -> "VerificationResult":
        """Create failed verification result."""
        return cls(success=False, message=message, errors=errors or [], details=details)


@dataclass
class FileVerificationResult(VerificationResult):
    """Extended result for file verification operations."""
    files_verified: int = 0
    blobs_verified: int = 0
    total_size_bytes: int = 0
    missing_files: List[str] = field(default_factory=list)
    corrupted_files: List[str] = field(default_factory=list)


class OutboxProcessor:
    """
    Outbox event processor for cross-database consistency.
    
    Responsibilities:
    1. Poll wtb_outbox table for pending events
    2. Execute appropriate handler for each event type
    3. Update event status (processed/failed)
    4. Support graceful shutdown
    5. FileTracker verification for file consistency
    
    SOLID Compliance:
    - SRP: Each handler has single responsibility
    - OCP: New handlers can be added without modifying existing
    - DIP: Depends on protocols, not concrete implementations
    
    ACID Compliance:
    - Atomic: Each event processed in its own transaction
    - Consistent: Verification ensures cross-DB consistency
    - Isolated: Events processed independently
    - Durable: Status persisted before verification
    
    Usage:
        processor = OutboxProcessor(
            wtb_db_url="sqlite:///data/wtb.db",
            checkpoint_repo=checkpoint_repository,
            file_tracking_service=file_tracking_service,  # Primary FileTracker integration
        )
        processor.start()  # Background thread
        ...
        processor.stop()   # Graceful shutdown
    
    For testing:
        processed = processor.process_once()  # Single batch
    """
    
    def __init__(
        self,
        wtb_db_url: str,
        checkpoint_repo: Optional[ICheckpointRepository] = None,
        commit_repo: Optional[ICommitRepository] = None,
        blob_repo: Optional[IBlobRepository] = None,
        file_tracking_service: Optional[IFileTrackingService] = None,
        file_cleanup_service: Optional["IFileCleanupService"] = None,
        poll_interval_seconds: float = 1.0,
        batch_size: int = 50,
        strict_verification: bool = False,
        verify_blob_content: bool = False,
    ):
        """
        Initialize the processor.
        
        Args:
            wtb_db_url: WTB database connection URL
            checkpoint_repo: Optional AgentGit checkpoint repository for verification
            commit_repo: Optional FileTracker commit repository for verification
            blob_repo: Optional FileTracker blob repository for verification
            file_tracking_service: Optional IFileTrackingService for comprehensive FileTracker ops
            file_cleanup_service: Optional IFileCleanupService for rollback cleanup (v1.9)
            poll_interval_seconds: How often to poll for new events
            batch_size: Maximum events to process per batch
            strict_verification: If True, raise errors when repos are missing
            verify_blob_content: If True, verify blob content hashes during integrity checks
        """
        self._wtb_db_url = wtb_db_url
        self._checkpoint_repo = checkpoint_repo
        self._commit_repo = commit_repo
        self._blob_repo = blob_repo
        self._file_tracking_service = file_tracking_service
        self._file_cleanup_service = file_cleanup_service
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._strict = strict_verification
        self._verify_blob_content = verify_blob_content
        
        self._running = False
        self._thread: Optional[threading.Thread] = None
        
        # Track verification statistics
        self._stats = {
            "events_processed": 0,
            "events_failed": 0,
            "checkpoints_verified": 0,
            "commits_verified": 0,
            "blobs_verified": 0,
            "files_verified": 0,
            "integrity_checks": 0,
            "restores_verified": 0,
            "rollbacks_verified": 0,
        }
        
        # Verification results history (bounded)
        self._verification_history: List[VerificationResult] = []
        self._max_history = 100
        
        # Event handlers mapping
        self._handlers: Dict[OutboxEventType, Callable[[OutboxEvent], None]] = {
            # AgentGit handlers
            OutboxEventType.CHECKPOINT_CREATE: self._handle_checkpoint_create,
            OutboxEventType.CHECKPOINT_VERIFY: self._handle_checkpoint_verify,
            OutboxEventType.NODE_BOUNDARY_SYNC: self._handle_node_boundary_sync,
            # FileTracker basic handlers
            OutboxEventType.FILE_COMMIT_LINK: self._handle_file_commit_link,
            OutboxEventType.FILE_COMMIT_VERIFY: self._handle_file_commit_verify,
            OutboxEventType.FILE_BLOB_VERIFY: self._handle_file_blob_verify,
            # FileTracker batch handlers (2026-01-15)
            OutboxEventType.FILE_BATCH_VERIFY: self._handle_file_batch_verify,
            OutboxEventType.FILE_INTEGRITY_CHECK: self._handle_file_integrity_check,
            OutboxEventType.FILE_RESTORE_VERIFY: self._handle_file_restore_verify,
            # Cross-DB handlers
            OutboxEventType.CHECKPOINT_FILE_LINK_VERIFY: self._handle_checkpoint_file_link_verify,
            # Rollback handlers (2026-01-15)
            OutboxEventType.ROLLBACK_FILE_RESTORE: self._handle_rollback_file_restore,
            OutboxEventType.ROLLBACK_VERIFY: self._handle_rollback_verify,
        }
    
    def start(self) -> None:
        """Start the processor in a background thread."""
        if self._running:
            logger.warning("OutboxProcessor already running")
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="OutboxProcessor")
        self._thread.start()
        logger.info("OutboxProcessor started")
    
    def stop(self, timeout: float = 5.0) -> None:
        """
        Stop the processor gracefully.
        
        Args:
            timeout: Maximum seconds to wait for thread to finish
        """
        if not self._running:
            return
        
        self._running = False
        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("OutboxProcessor did not stop within timeout")
        logger.info("OutboxProcessor stopped")
    
    def is_running(self) -> bool:
        """Check if processor is running."""
        return self._running
    
    def process_once(self) -> int:
        """
        Process a single batch of events.
        
        Useful for testing or manual triggering.
        
        Returns:
            Number of successfully processed events
        """
        return self._process_batch()
    
    def _run_loop(self) -> None:
        """Main processing loop."""
        logger.info("OutboxProcessor loop started")
        while self._running:
            try:
                processed = self._process_batch()
                if processed == 0:
                    # No events to process, sleep
                    time.sleep(self._poll_interval)
            except Exception as e:
                logger.error(f"OutboxProcessor error in loop: {e}", exc_info=True)
                # Longer sleep on error to avoid tight error loops
                time.sleep(self._poll_interval * 2)
        logger.info("OutboxProcessor loop ended")
    
    def _process_batch(self) -> int:
        """
        Process a batch of pending events.
        
        Returns:
            Number of successfully processed events
        """
        processed = 0
        
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            # Get pending events
            events = uow.outbox.get_pending(limit=self._batch_size)
            
            if not events:
                return 0
            
            for event in events:
                try:
                    # Mark as processing
                    event.mark_processing()
                    uow.outbox.update(event)
                    uow.commit()
                    
                    # Execute handler
                    handler = self._handlers.get(event.event_type)
                    if handler:
                        handler(event)
                    else:
                        raise ValueError(f"No handler for event type: {event.event_type}")
                    
                    # Mark as processed
                    event.mark_processed()
                    uow.outbox.update(event)
                    uow.commit()
                    processed += 1
                    self._stats["events_processed"] += 1
                    
                    logger.debug(f"Processed event {event.event_id} ({event.event_type.value})")
                    
                except Exception as e:
                    logger.error(f"Failed to process event {event.event_id}: {e}")
                    event.mark_failed(str(e))
                    uow.outbox.update(event)
                    uow.commit()
                    self._stats["events_failed"] += 1
        
        if processed > 0:
            logger.info(f"Processed {processed}/{len(events)} outbox events")
        
        return processed
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Event Handlers
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _handle_checkpoint_create(self, event: OutboxEvent) -> None:
        """
        Handle checkpoint creation verification.
        
        Verifies that the AgentGit checkpoint was created successfully.
        
        Args:
            event: OutboxEvent with payload containing:
                - checkpoint_id: The checkpoint ID to verify
                - node_id: Optional node ID for context
                
        Raises:
            ValueError: If checkpoint not found
        """
        payload = event.payload
        checkpoint_id = payload.get("checkpoint_id")
        
        if not checkpoint_id:
            logger.warning(f"No checkpoint_id in event {event.event_id}")
            return
        
        if self._checkpoint_repo:
            cp = self._checkpoint_repo.get_by_id(checkpoint_id)
            if not cp:
                raise ValueError(f"Checkpoint {checkpoint_id} not found in AgentGit")
            self._stats["checkpoints_verified"] += 1
        elif self._strict:
            raise ValueError("checkpoint_repo not configured for verification")
        else:
            logger.warning(
                f"checkpoint_repo not configured, skipping verification for {checkpoint_id}"
            )
            return
        
        logger.info(f"Verified checkpoint {checkpoint_id} creation")
    
    def _handle_checkpoint_verify(self, event: OutboxEvent) -> None:
        """
        Handle checkpoint existence verification.
        
        Confirms that a checkpoint referenced by WTB exists in AgentGit.
        
        Args:
            event: OutboxEvent with payload containing:
                - checkpoint_id: The checkpoint ID to verify
                - node_id: Optional node ID for context
                
        Raises:
            ValueError: If checkpoint not found
        """
        payload = event.payload
        checkpoint_id = payload.get("checkpoint_id")
        node_id = payload.get("node_id")
        
        if not checkpoint_id:
            raise ValueError("Missing checkpoint_id in event payload")
        
        if self._checkpoint_repo:
            cp = self._checkpoint_repo.get_by_id(checkpoint_id)
            if not cp:
                raise ValueError(f"Checkpoint {checkpoint_id} not found in AgentGit")
            self._stats["checkpoints_verified"] += 1
        elif self._strict:
            raise ValueError("checkpoint_repo not configured for verification")
        else:
            logger.warning(
                f"checkpoint_repo not configured, skipping verification for {checkpoint_id}"
            )
            return
        
        logger.info(f"Verified checkpoint {checkpoint_id} for node {node_id}")
    
    def _handle_node_boundary_sync(self, event: OutboxEvent) -> None:
        """
        Handle node boundary sync to AgentGit metadata.
        
        Updates AgentGit checkpoint metadata with WTB node boundary info.
        """
        payload = event.payload
        checkpoint_id = payload.get("exit_checkpoint_id")
        node_id = payload.get("node_id")
        
        if checkpoint_id and self._checkpoint_repo:
            cp = self._checkpoint_repo.get_by_id(checkpoint_id)
            if cp:
                # Note: Actual metadata update would require write access
                # For now, just verify checkpoint exists
                logger.debug(f"Node boundary synced for node {node_id}")
    
    def _handle_file_commit_link(self, event: OutboxEvent) -> None:
        """
        Handle file commit link verification (legacy compatibility).
        
        Delegates to file_commit_verify handler.
        """
        self._handle_file_commit_verify(event)
    
    def _handle_file_commit_verify(self, event: OutboxEvent) -> None:
        """
        Handle FileTracker commit verification.
        
        Verifies that a FileTracker commit exists and contains expected files.
        
        Args:
            event: OutboxEvent with payload containing:
                - file_commit_id: The commit ID to verify
                - expected_file_count: Optional expected number of files
                
        Raises:
            ValueError: If commit not found or verification fails
        """
        payload = event.payload
        file_commit_id = payload.get("file_commit_id")
        expected_file_count = payload.get("expected_file_count")
        
        if not file_commit_id:
            logger.warning(f"No file_commit_id in event {event.event_id}")
            return
        
        # Check if FileTracker repository is available
        if self._commit_repo is None:
            if self._strict:
                raise ValueError(
                    f"Cannot verify commit {file_commit_id}: commit_repo not configured"
                )
            logger.warning(
                f"FileTracker commit_repo not configured, "
                f"skipping verification for commit {file_commit_id}"
            )
            return
        
        # Verify commit exists
        try:
            commit = self._commit_repo.find_by_id(file_commit_id)
            
            if commit is None:
                raise ValueError(
                    f"FileTracker commit {file_commit_id} not found"
                )
            
            # Optionally verify file count
            if expected_file_count is not None:
                actual_count = len(getattr(commit, '_mementos', []) or [])
                if actual_count != expected_file_count:
                    raise ValueError(
                        f"FileTracker commit {file_commit_id} has {actual_count} files, "
                        f"expected {expected_file_count}"
                    )
            
            self._stats["commits_verified"] += 1
            logger.info(f"Verified FileTracker commit {file_commit_id}")
            
        except Exception as e:
            logger.error(f"FileTracker commit verification failed: {e}")
            raise
    
    def _handle_file_blob_verify(self, event: OutboxEvent) -> None:
        """
        Handle individual blob file verification.
        
        Verifies that a blob file exists in FileTracker storage.
        
        Args:
            event: OutboxEvent with payload containing:
                - file_hash: The SHA-256 hash of the blob to verify
                - file_path: Optional original file path for logging
                
        Raises:
            ValueError: If blob not found or verification fails
        """
        payload = event.payload
        file_hash = payload.get("file_hash")
        file_path = payload.get("file_path", "unknown")
        
        if not file_hash:
            logger.warning(f"No file_hash in event {event.event_id}")
            return
        
        # Check if FileTracker blob repository is available
        if self._blob_repo is None:
            if self._strict:
                raise ValueError(
                    f"Cannot verify blob {file_hash}: blob_repo not configured"
                )
            logger.warning(
                f"FileTracker blob_repo not configured, "
                f"skipping verification for blob {file_hash}"
            )
            return
        
        # Verify blob exists
        try:
            exists = self._blob_repo.exists(file_hash)
            
            if not exists:
                raise ValueError(
                    f"FileTracker blob {file_hash} not found (file: {file_path})"
                )
            
            self._stats["blobs_verified"] += 1
            logger.info(f"Verified FileTracker blob {file_hash[:16]}... ({file_path})")
            
        except Exception as e:
            logger.error(f"FileTracker blob verification failed: {e}")
            raise
    
    def _handle_checkpoint_file_link_verify(self, event: OutboxEvent) -> None:
        """
        Handle full checkpoint-file link verification.
        
        Verifies consistency across all three databases:
        1. AgentGit checkpoint exists
        2. WTB checkpoint_files link exists
        3. FileTracker commit exists with all blobs
        
        This is the most comprehensive verification, ensuring full cross-DB consistency.
        
        Args:
            event: OutboxEvent with payload containing:
                - checkpoint_id: AgentGit checkpoint ID
                - file_commit_id: FileTracker commit ID
                - verify_blobs: Optional bool to verify individual blobs
                
        Raises:
            ValueError: If any verification step fails
        """
        payload = event.payload
        checkpoint_id = payload.get("checkpoint_id")
        file_commit_id = payload.get("file_commit_id")
        verify_blobs = payload.get("verify_blobs", False)
        
        errors = []
        
        # Step 1: Verify AgentGit checkpoint
        if checkpoint_id:
            if self._checkpoint_repo:
                cp = self._checkpoint_repo.get_by_id(checkpoint_id)
                if not cp:
                    errors.append(f"AgentGit checkpoint {checkpoint_id} not found")
                else:
                    self._stats["checkpoints_verified"] += 1
            elif self._strict:
                errors.append("checkpoint_repo not configured")
        
        # Step 2: Verify WTB link
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            link = uow.checkpoint_files.find_by_checkpoint(checkpoint_id)
            if not link:
                errors.append(
                    f"WTB checkpoint_files link not found for checkpoint {checkpoint_id}"
                )
            elif link.file_commit_id != file_commit_id:
                errors.append(
                    f"file_commit_id mismatch: expected {file_commit_id}, got {link.file_commit_id}"
                )
        
        # Step 3: Verify FileTracker commit
        if file_commit_id and self._commit_repo:
            commit = self._commit_repo.find_by_id(file_commit_id)
            if not commit:
                errors.append(f"FileTracker commit {file_commit_id} not found")
            else:
                self._stats["commits_verified"] += 1
                
                # Step 4: Optionally verify all blobs in commit
                if verify_blobs and self._blob_repo:
                    mementos = getattr(commit, '_mementos', []) or []
                    for memento in mementos:
                        file_hash = getattr(memento, '_file_hash', None)
                        if file_hash and not self._blob_repo.exists(file_hash):
                            errors.append(f"Blob {file_hash} not found")
                        elif file_hash:
                            self._stats["blobs_verified"] += 1
        elif file_commit_id and self._strict:
            errors.append("commit_repo not configured")
        
        # Report results
        if errors:
            error_msg = "; ".join(errors)
            logger.error(f"Checkpoint-file link verification failed: {error_msg}")
            raise ValueError(f"Cross-DB consistency check failed: {error_msg}")
        
        logger.info(
            f"Verified checkpoint-file link: cp={checkpoint_id}, commit={file_commit_id}"
        )
    
    def _handle_file_batch_verify(self, event: OutboxEvent) -> None:
        """
        Handle batch file verification for multiple commits.
        
        Verifies that all commits in a batch test exist and have expected files.
        
        Args:
            event: OutboxEvent with payload containing:
                - commit_ids: List of commit IDs to verify
                - expected_total_files: Optional total expected file count
                - verify_blobs: Whether to verify individual blobs
                
        Raises:
            ValueError: If any commit not found or verification fails
        """
        payload = event.payload
        commit_ids = payload.get("commit_ids", [])
        expected_total_files = payload.get("expected_total_files")
        verify_blobs = payload.get("verify_blobs", False)
        
        if not commit_ids:
            logger.warning(f"No commit_ids in batch verify event {event.event_id}")
            return
        
        # Check if FileTracker is available
        if not self._commit_repo and not self._file_tracking_service:
            if self._strict:
                raise ValueError("FileTracker not configured for batch verification")
            logger.warning("FileTracker not configured, skipping batch verification")
            return
        
        errors = []
        total_files = 0
        blobs_verified = 0
        
        for commit_id in commit_ids:
            try:
                # Use file tracking service if available
                if self._file_tracking_service and self._file_tracking_service.is_available():
                    files = self._file_tracking_service.get_tracked_files(commit_id)
                    if not files and files != []:
                        errors.append(f"Commit {commit_id} not found")
                        continue
                    total_files += len(files)
                    
                    # Verify blobs if requested
                    if verify_blobs:
                        for file in files:
                            file_hash = getattr(file, 'file_hash', None)
                            if file_hash and self._blob_repo:
                                if self._blob_repo.exists(file_hash):
                                    blobs_verified += 1
                                else:
                                    errors.append(f"Blob missing: {file_hash[:16]}...")
                
                # Fall back to commit_repo
                elif self._commit_repo:
                    commit = self._commit_repo.find_by_id(commit_id)
                    if not commit:
                        errors.append(f"Commit {commit_id} not found")
                        continue
                    
                    mementos = getattr(commit, '_mementos', []) or []
                    total_files += len(mementos)
                    
                    if verify_blobs and self._blob_repo:
                        for memento in mementos:
                            file_hash = getattr(memento, '_file_hash', None)
                            if file_hash and self._blob_repo.exists(file_hash):
                                blobs_verified += 1
                            elif file_hash:
                                errors.append(f"Blob missing: {file_hash[:16]}...")
                
                self._stats["commits_verified"] += 1
                
            except Exception as e:
                errors.append(f"Error verifying commit {commit_id}: {e}")
        
        # Check total file count
        if expected_total_files is not None and total_files != expected_total_files:
            errors.append(
                f"File count mismatch: expected {expected_total_files}, got {total_files}"
            )
        
        # Update stats
        self._stats["files_verified"] += total_files
        self._stats["blobs_verified"] += blobs_verified
        
        # Record result
        result = FileVerificationResult(
            success=len(errors) == 0,
            message=f"Batch verification: {len(commit_ids)} commits, {total_files} files",
            files_verified=total_files,
            blobs_verified=blobs_verified,
            errors=errors,
        )
        self._add_verification_result(result)
        
        if errors:
            error_msg = "; ".join(errors[:5])  # Limit error message length
            if len(errors) > 5:
                error_msg += f" (and {len(errors) - 5} more errors)"
            raise ValueError(f"Batch verification failed: {error_msg}")
        
        logger.info(
            f"Batch verification complete: {len(commit_ids)} commits, "
            f"{total_files} files, {blobs_verified} blobs verified"
        )
    
    def _handle_file_integrity_check(self, event: OutboxEvent) -> None:
        """
        Handle deep file integrity verification.
        
        Verifies file hashes match stored content. This is more thorough
        than existence checks and can detect corruption.
        
        Args:
            event: OutboxEvent with payload containing:
                - file_hashes: Dict mapping file_path to expected hash
                - verify_content: Whether to verify actual content (expensive)
                
        Raises:
            ValueError: If integrity check fails
        """
        payload = event.payload
        file_hashes = payload.get("file_hashes", {})
        verify_content = payload.get("verify_content", False)
        
        if not file_hashes:
            logger.warning(f"No file_hashes in integrity check event {event.event_id}")
            return
        
        if not self._blob_repo:
            if self._strict:
                raise ValueError("blob_repo not configured for integrity check")
            logger.warning("blob_repo not configured, skipping integrity check")
            return
        
        errors = []
        corrupted = []
        verified = 0
        
        for file_path, expected_hash in file_hashes.items():
            try:
                # Check blob exists
                from wtb.domain.models.file_processing import BlobId
                blob_id = BlobId(expected_hash)
                
                if not self._blob_repo.exists(blob_id):
                    errors.append(f"Blob not found: {file_path} ({expected_hash[:16]}...)")
                    continue
                
                # Optionally verify content hash
                if verify_content and self._verify_blob_content:
                    content = self._blob_repo.get(blob_id)
                    if content:
                        actual_hash = hashlib.sha256(content).hexdigest()
                        if actual_hash != expected_hash:
                            corrupted.append(file_path)
                            errors.append(
                                f"Hash mismatch: {file_path} expected {expected_hash[:16]}..., "
                                f"got {actual_hash[:16]}..."
                            )
                            continue
                
                verified += 1
                
            except Exception as e:
                errors.append(f"Error checking {file_path}: {e}")
        
        self._stats["integrity_checks"] += 1
        self._stats["blobs_verified"] += verified
        
        # Record result
        result = FileVerificationResult(
            success=len(errors) == 0,
            message=f"Integrity check: {len(file_hashes)} files",
            files_verified=verified,
            corrupted_files=corrupted,
            errors=errors,
        )
        self._add_verification_result(result)
        
        if errors:
            raise ValueError(f"Integrity check failed: {'; '.join(errors[:3])}")
        
        logger.info(f"Integrity check passed: {verified}/{len(file_hashes)} files verified")
    
    def _handle_file_restore_verify(self, event: OutboxEvent) -> None:
        """
        Verify files were correctly restored from a commit.
        
        Checks that restored files exist at expected paths and have correct content.
        
        Args:
            event: OutboxEvent with payload containing:
                - checkpoint_id: Checkpoint the files were restored from
                - commit_id: Commit the files were restored from
                - restored_paths: List of paths that should have been restored
        """
        payload = event.payload
        checkpoint_id = payload.get("checkpoint_id")
        commit_id = payload.get("commit_id")
        restored_paths = payload.get("restored_paths", [])
        
        if not restored_paths:
            logger.warning(f"No restored_paths in restore verify event {event.event_id}")
            return
        
        errors = []
        verified = 0
        missing = []
        
        for file_path in restored_paths:
            try:
                path = Path(file_path)
                if not path.exists():
                    missing.append(file_path)
                    errors.append(f"Restored file not found: {file_path}")
                else:
                    verified += 1
            except Exception as e:
                errors.append(f"Error checking {file_path}: {e}")
        
        self._stats["restores_verified"] += 1
        self._stats["files_verified"] += verified
        
        # Record result
        result = FileVerificationResult(
            success=len(errors) == 0,
            message=f"Restore verification: checkpoint {checkpoint_id}",
            files_verified=verified,
            missing_files=missing,
            errors=errors,
            details={
                "checkpoint_id": checkpoint_id,
                "commit_id": commit_id,
            }
        )
        self._add_verification_result(result)
        
        if errors:
            raise ValueError(f"Restore verification failed: {'; '.join(errors[:3])}")
        
        logger.info(
            f"Restore verified: {verified}/{len(restored_paths)} files for "
            f"checkpoint {checkpoint_id}, commit {commit_id}"
        )
    
    def _handle_rollback_file_restore(self, event: OutboxEvent) -> None:
        """
        Handle file restoration as part of a rollback operation.
        
        Coordinates with FileTracker to restore files from a checkpoint's
        linked commit during workflow rollback.
        
        v1.9: Also handles cleanup of orphaned files if cleanup_orphaned_files=True
        
        Args:
            event: OutboxEvent with payload containing:
                - source_checkpoint_id: Original checkpoint
                - target_checkpoint_id: Checkpoint to rollback to
                - source_commit_id: FileTracker commit to restore from
                - cleanup_orphaned_files: (v1.9) If True, cleanup files after checkpoint
                - cleanup_dry_run: (v1.9) If True, log only, don't delete
                - cleanup_backup: (v1.9) If True, backup before delete
                - cleanup_max_files: (v1.9) Safety limit for deletion
                - workspace_path: (v1.9) Workspace root for file discovery
                - track_patterns: (v1.9) Glob patterns for file discovery
                - exclude_patterns: (v1.9) Glob patterns to exclude
        """
        payload = event.payload
        source_checkpoint_id = payload.get("source_checkpoint_id")
        target_checkpoint_id = payload.get("target_checkpoint_id")
        source_commit_id = payload.get("source_commit_id")
        execution_id = payload.get("execution_id", "unknown")
        
        if not source_commit_id:
            logger.warning(f"No source_commit_id in rollback event {event.event_id}")
            return
        
        restore_success = False
        files_restored = 0
        
        # Use file tracking service if available
        if self._file_tracking_service and self._file_tracking_service.is_available():
            try:
                result = self._file_tracking_service.restore_commit(source_commit_id)
                
                if not result or not getattr(result, 'success', True):
                    raise ValueError(
                        f"File restore failed for commit {source_commit_id}: "
                        f"{getattr(result, 'error_message', 'Unknown error')}"
                    )
                
                files_restored = getattr(result, 'files_restored', 0)
                logger.info(
                    f"Rollback file restore complete: {files_restored} files from "
                    f"commit {source_commit_id} (checkpoint {source_checkpoint_id} -> {target_checkpoint_id})"
                )
                
                self._stats["restores_verified"] += 1
                self._stats["files_verified"] += files_restored
                restore_success = True
                
            except Exception as e:
                logger.error(f"FileTrackingService restore failed: {e}")
                if self._strict:
                    raise
        
        # Fall back to commit_repo + blob_repo
        elif self._commit_repo and self._blob_repo:
            commit = self._commit_repo.find_by_id(source_commit_id)
            if not commit:
                raise ValueError(f"Commit {source_commit_id} not found for rollback")
            
            mementos = getattr(commit, '_mementos', []) or []
            restored = 0
            
            for memento in mementos:
                file_path = getattr(memento, '_file_path', None)
                file_hash = getattr(memento, '_file_hash', None)
                
                if file_path and file_hash:
                    try:
                        from wtb.domain.models.file_processing import BlobId
                        self._blob_repo.restore_to_file(BlobId(file_hash), file_path)
                        restored += 1
                    except Exception as e:
                        logger.error(f"Failed to restore {file_path}: {e}")
                        if self._strict:
                            raise
            
            logger.info(
                f"Rollback file restore complete: {restored}/{len(mementos)} files"
            )
            self._stats["restores_verified"] += 1
            self._stats["files_verified"] += restored
            files_restored = restored
            restore_success = True
        
        else:
            # No FileTracker available
            if self._strict:
                raise ValueError("FileTracker not configured for rollback file restore")
            
            logger.warning(
                f"FileTracker not configured, skipping file restore for rollback "
                f"(checkpoint {source_checkpoint_id} -> {target_checkpoint_id})"
            )
        
        # v1.9: Cleanup orphaned files after restore if enabled
        if restore_success and payload.get("cleanup_orphaned_files", False):
            self._cleanup_orphaned_files_post_restore(
                payload=payload,
                target_checkpoint_id=target_checkpoint_id,
                execution_id=execution_id,
            )
    
    def _cleanup_orphaned_files_post_restore(
        self,
        payload: Dict[str, Any],
        target_checkpoint_id: Any,
        execution_id: str,
    ) -> None:
        """
        Cleanup files created after checkpoint (v1.9).
        
        Called after successful file restore to remove orphaned files.
        
        Args:
            payload: Event payload with cleanup configuration
            target_checkpoint_id: Checkpoint being rolled back to
            execution_id: Execution being rolled back
        """
        if not self._file_cleanup_service:
            logger.debug("FileCleanupService not configured, skipping cleanup")
            return
        
        if not self._file_tracking_service:
            logger.debug("FileTrackingService not configured, skipping cleanup")
            return
        
        try:
            # Extract cleanup config from payload
            workspace_path = Path(payload.get("workspace_path", "."))
            track_patterns = payload.get("track_patterns", [])
            exclude_patterns = payload.get("exclude_patterns", [])
            dry_run = payload.get("cleanup_dry_run", False)
            backup = payload.get("cleanup_backup", True)
            max_files = payload.get("cleanup_max_files", 100)
            
            # Convert checkpoint_id to int if it's a string (UUID)
            checkpoint_id = target_checkpoint_id
            if isinstance(checkpoint_id, str):
                # Try to parse as int, or use hash for UUID
                try:
                    checkpoint_id = int(checkpoint_id)
                except ValueError:
                    # For UUID checkpoint IDs, we need the numeric ID
                    # This should be passed in payload or looked up
                    checkpoint_id = payload.get("target_checkpoint_numeric_id", 0)
                    if not checkpoint_id:
                        logger.warning(
                            f"Cannot cleanup: checkpoint_id {target_checkpoint_id} "
                            f"is not numeric and no target_checkpoint_numeric_id in payload"
                        )
                        return
            
            # Identify orphaned files
            orphaned = self._file_cleanup_service.identify_orphaned_files(
                target_checkpoint_id=checkpoint_id,
                execution_id=execution_id,
                current_workspace_path=workspace_path,
                track_patterns=track_patterns,
                exclude_patterns=exclude_patterns,
                file_tracking_service=self._file_tracking_service,
            )
            
            if not orphaned:
                logger.info(f"No orphaned files to cleanup for checkpoint {checkpoint_id}")
                return
            
            # Cleanup with config options
            backup_dir = workspace_path / ".rollback_backup" if backup else None
            result = self._file_cleanup_service.cleanup_orphaned_files(
                checkpoint_id=checkpoint_id,
                execution_id=execution_id,
                orphaned_paths=orphaned,
                backup_dir=backup_dir,
                dry_run=dry_run,
                max_files=max_files,
            )
            
            mode = "DRY RUN" if dry_run else "LIVE"
            logger.info(
                f"[{mode}] File cleanup: {result.files_deleted} deleted, "
                f"{result.files_backed_up} backed up, {result.files_skipped} skipped"
            )
            
            # Track in stats
            if not dry_run:
                self._stats["files_verified"] += result.files_deleted
            
            # v1.9: Emit FILE_CLEANUP_COMPLETED event for audit trail
            self._emit_cleanup_completed_event(
                execution_id=execution_id,
                checkpoint_id=checkpoint_id,
                result=result,
                backup_dir=str(backup_dir) if backup_dir else None,
            )
            
        except Exception as e:
            logger.error(f"File cleanup failed: {e}")
            if self._strict:
                raise
    
    def _emit_cleanup_completed_event(
        self,
        execution_id: str,
        checkpoint_id: int,
        result: Any,  # FileCleanupResult
        backup_dir: Optional[str] = None,
    ) -> None:
        """
        Emit FILE_CLEANUP_COMPLETED outbox event for audit trail (v1.9).
        
        Args:
            execution_id: Execution that was rolled back
            checkpoint_id: Target checkpoint
            result: FileCleanupResult from cleanup service
            backup_dir: Directory where backups were stored
        """
        try:
            from wtb.domain.models.outbox import OutboxEvent, OutboxEventType
            
            event = OutboxEvent(
                event_type=OutboxEventType.FILE_CLEANUP_COMPLETED,
                aggregate_type="Execution",
                aggregate_id=execution_id,
                payload={
                    "execution_id": execution_id,
                    "checkpoint_id": checkpoint_id,
                    "files_deleted": result.files_deleted,
                    "files_backed_up": result.files_backed_up,
                    "files_skipped": result.files_skipped,
                    "deleted_paths": list(result.deleted_paths),
                    "backed_up_paths": list(result.backed_up_paths),
                    "backup_dir": backup_dir,
                    "dry_run": result.dry_run,
                    "errors": list(result.errors),
                },
            )
            
            # Save to outbox for audit/verification
            with self._get_session() as session:
                from wtb.infrastructure.database.repositories.outbox_repository import (
                    SQLAlchemyOutboxRepository,
                )
                repo = SQLAlchemyOutboxRepository(session)
                repo.add(event)
                session.commit()
            
            logger.debug(f"Emitted FILE_CLEANUP_COMPLETED event for {execution_id}")
            
        except Exception as e:
            logger.warning(f"Failed to emit cleanup event: {e}")
    
    def _handle_rollback_verify(self, event: OutboxEvent) -> None:
        """
        Handle full rollback verification.
        
        Verifies that both state and files were correctly restored during rollback.
        
        Args:
            event: OutboxEvent with payload containing:
                - checkpoint_id: Checkpoint that was rolled back to
                - restored_files_count: Expected number of restored files
                - state_verified: Whether workflow state was verified
        """
        payload = event.payload
        checkpoint_id = payload.get("checkpoint_id")
        restored_files_count = payload.get("restored_files_count", 0)
        state_verified = payload.get("state_verified", False)
        
        errors = []
        
        # Verify checkpoint exists in AgentGit
        if checkpoint_id and self._checkpoint_repo:
            cp = self._checkpoint_repo.get_by_id(checkpoint_id)
            if not cp:
                errors.append(f"Checkpoint {checkpoint_id} not found in AgentGit")
            else:
                self._stats["checkpoints_verified"] += 1
        
        # Verify file commit link exists
        if checkpoint_id:
            with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
                link = uow.checkpoint_files.find_by_checkpoint(checkpoint_id)
                if not link and restored_files_count > 0:
                    errors.append(
                        f"No file commit linked to checkpoint {checkpoint_id} "
                        f"but expected {restored_files_count} files"
                    )
                elif link:
                    # Verify commit exists in FileTracker
                    if self._commit_repo:
                        commit = self._commit_repo.find_by_id(link.file_commit_id)
                        if not commit:
                            errors.append(
                                f"FileTracker commit {link.file_commit_id} not found"
                            )
                        else:
                            self._stats["commits_verified"] += 1
        
        self._stats["rollbacks_verified"] += 1
        
        # Record result
        result = VerificationResult(
            success=len(errors) == 0,
            message=f"Rollback verification: checkpoint {checkpoint_id}",
            errors=errors,
            details={
                "checkpoint_id": checkpoint_id,
                "restored_files_count": restored_files_count,
                "state_verified": state_verified,
            }
        )
        self._add_verification_result(result)
        
        if errors:
            raise ValueError(f"Rollback verification failed: {'; '.join(errors)}")
        
        logger.info(
            f"Rollback verified: checkpoint {checkpoint_id}, "
            f"{restored_files_count} files, state_verified={state_verified}"
        )
    
    def _add_verification_result(self, result: VerificationResult) -> None:
        """Add a verification result to history (bounded)."""
        self._verification_history.append(result)
        if len(self._verification_history) > self._max_history:
            self._verification_history.pop(0)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Statistics and Monitoring
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_stats(self) -> Dict[str, int]:
        """
        Get verification statistics.
        
        Returns:
            Dictionary with counters for events processed, verifications, etc.
        """
        return dict(self._stats)
    
    def get_extended_stats(self) -> Dict[str, Any]:
        """
        Get extended statistics including verification history summary.
        
        Returns:
            Dictionary with stats and verification summary
        """
        recent_success = sum(
            1 for r in self._verification_history[-20:] if r.success
        )
        recent_total = min(20, len(self._verification_history))
        
        return {
            **self._stats,
            "verification_history_size": len(self._verification_history),
            "recent_success_rate": recent_success / recent_total if recent_total > 0 else 1.0,
            "recent_success_count": recent_success,
            "recent_total_count": recent_total,
        }
    
    def get_verification_history(
        self,
        limit: int = 20,
        success_only: bool = False,
        failed_only: bool = False,
    ) -> List[VerificationResult]:
        """
        Get verification history.
        
        Args:
            limit: Maximum results to return
            success_only: Only return successful verifications
            failed_only: Only return failed verifications
            
        Returns:
            List of VerificationResult objects (most recent first)
        """
        results = self._verification_history[-limit:][::-1]  # Most recent first
        
        if success_only:
            results = [r for r in results if r.success]
        elif failed_only:
            results = [r for r in results if not r.success]
        
        return results
    
    def get_failed_verifications(self, limit: int = 10) -> List[VerificationResult]:
        """
        Get recent failed verifications for debugging.
        
        Args:
            limit: Maximum results to return
            
        Returns:
            List of failed VerificationResult objects
        """
        return self.get_verification_history(limit=limit, failed_only=True)
    
    def reset_stats(self) -> None:
        """Reset all statistics counters."""
        for key in self._stats:
            self._stats[key] = 0
    
    def clear_verification_history(self) -> int:
        """
        Clear verification history.
        
        Returns:
            Number of entries cleared
        """
        count = len(self._verification_history)
        self._verification_history.clear()
        return count
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Maintenance Methods
    # ═══════════════════════════════════════════════════════════════════════════
    
    def cleanup_old_events(self, days_old: int = 7, limit: int = 1000) -> int:
        """
        Clean up old processed events.
        
        Args:
            days_old: Delete events processed more than this many days ago
            limit: Maximum events to delete per call
            
        Returns:
            Number of deleted events
        """
        from datetime import timedelta
        
        before = datetime.now() - timedelta(days=days_old)
        
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            deleted = uow.outbox.delete_processed(before=before, limit=limit)
            uow.commit()
        
        if deleted > 0:
            logger.info(f"Cleaned up {deleted} old outbox events")
        
        return deleted
    
    def retry_failed_events(self) -> int:
        """
        Reset failed events for retry.
        
        Returns:
            Number of events reset for retry
        """
        count = 0
        
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            failed_events = uow.outbox.get_failed_for_retry(limit=100)
            
            for event in failed_events:
                if event.can_retry():
                    event.status = OutboxStatus.PENDING
                    uow.outbox.update(event)
                    count += 1
            
            uow.commit()
        
        if count > 0:
            logger.info(f"Reset {count} failed events for retry")
        
        return count

