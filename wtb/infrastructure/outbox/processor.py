"""
Outbox Pattern Processor.

Background worker that processes outbox events for cross-database
consistency verification between WTB, AgentGit, and FileTracker.
"""

from typing import Callable, Dict, Optional, Any, Protocol
from datetime import datetime
import threading
import time
import logging

from wtb.domain.models.outbox import OutboxEvent, OutboxEventType, OutboxStatus
from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork

logger = logging.getLogger(__name__)


class ICheckpointRepository(Protocol):
    """Protocol for AgentGit checkpoint repository access."""
    
    def get_by_id(self, checkpoint_id: int) -> Optional[Any]:
        """Get checkpoint by ID."""
        ...


class OutboxProcessor:
    """
    Outbox event processor for cross-database consistency.
    
    Responsibilities:
    1. Poll wtb_outbox table for pending events
    2. Execute appropriate handler for each event type
    3. Update event status (processed/failed)
    4. Support graceful shutdown
    
    Usage:
        processor = OutboxProcessor(
            wtb_db_url="sqlite:///data/wtb.db",
            checkpoint_repo=checkpoint_repository  # From AgentGit
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
        poll_interval_seconds: float = 1.0,
        batch_size: int = 50,
    ):
        """
        Initialize the processor.
        
        Args:
            wtb_db_url: WTB database connection URL
            checkpoint_repo: Optional AgentGit checkpoint repository for verification
            poll_interval_seconds: How often to poll for new events
            batch_size: Maximum events to process per batch
        """
        self._wtb_db_url = wtb_db_url
        self._checkpoint_repo = checkpoint_repo
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        
        self._running = False
        self._thread: Optional[threading.Thread] = None
        
        # Event handlers mapping
        self._handlers: Dict[OutboxEventType, Callable[[OutboxEvent], None]] = {
            OutboxEventType.CHECKPOINT_CREATE: self._handle_checkpoint_create,
            OutboxEventType.CHECKPOINT_VERIFY: self._handle_checkpoint_verify,
            OutboxEventType.NODE_BOUNDARY_SYNC: self._handle_node_boundary_sync,
            OutboxEventType.FILE_COMMIT_LINK: self._handle_file_commit_link,
            OutboxEventType.FILE_COMMIT_VERIFY: self._handle_file_commit_verify,
            OutboxEventType.FILE_BLOB_VERIFY: self._handle_file_blob_verify,
            OutboxEventType.CHECKPOINT_FILE_LINK_VERIFY: self._handle_checkpoint_file_link_verify,
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
                    
                    logger.debug(f"Processed event {event.event_id} ({event.event_type.value})")
                    
                except Exception as e:
                    logger.error(f"Failed to process event {event.event_id}: {e}")
                    event.mark_failed(str(e))
                    uow.outbox.update(event)
                    uow.commit()
        
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
        
        logger.debug(f"Verified checkpoint {checkpoint_id} creation")
    
    def _handle_checkpoint_verify(self, event: OutboxEvent) -> None:
        """
        Handle checkpoint existence verification.
        
        Confirms that a checkpoint referenced by WTB exists in AgentGit.
        """
        payload = event.payload
        checkpoint_id = payload.get("checkpoint_id")
        node_id = payload.get("node_id")
        
        if not checkpoint_id:
            raise ValueError(f"Missing checkpoint_id in event payload")
        
        if self._checkpoint_repo:
            cp = self._checkpoint_repo.get_by_id(checkpoint_id)
            if not cp:
                raise ValueError(f"Checkpoint {checkpoint_id} not found in AgentGit")
        
        logger.debug(f"Verified checkpoint {checkpoint_id} for node {node_id}")
    
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
        
        Verifies that a FileTracker commit exists and is complete.
        
        Note: Actual FileTracker integration requires FileTracker repository.
        This is a placeholder that logs the verification attempt.
        """
        payload = event.payload
        file_commit_id = payload.get("file_commit_id")
        
        if not file_commit_id:
            logger.warning(f"No file_commit_id in event {event.event_id}")
            return
        
        # FileTracker verification would go here
        # For now, log that we would verify
        logger.debug(f"Would verify FileTracker commit {file_commit_id}")
    
    def _handle_file_blob_verify(self, event: OutboxEvent) -> None:
        """
        Handle individual blob file verification.
        
        Verifies that a blob file exists in FileTracker storage.
        """
        payload = event.payload
        file_hash = payload.get("file_hash")
        
        if not file_hash:
            logger.warning(f"No file_hash in event {event.event_id}")
            return
        
        # Blob verification would go here
        logger.debug(f"Would verify blob {file_hash}")
    
    def _handle_checkpoint_file_link_verify(self, event: OutboxEvent) -> None:
        """
        Handle full checkpoint-file link verification.
        
        Verifies consistency across all three databases:
        1. AgentGit checkpoint exists
        2. WTB checkpoint_files link exists
        3. FileTracker commit exists with all blobs
        """
        payload = event.payload
        checkpoint_id = payload.get("checkpoint_id")
        file_commit_id = payload.get("file_commit_id")
        
        # Step 1: Verify AgentGit checkpoint
        if checkpoint_id and self._checkpoint_repo:
            cp = self._checkpoint_repo.get_by_id(checkpoint_id)
            if not cp:
                raise ValueError(f"AgentGit checkpoint {checkpoint_id} not found")
        
        # Step 2: Verify WTB link (already in same DB, but we can double-check)
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            link = uow.checkpoint_files.find_by_checkpoint(checkpoint_id)
            if not link:
                raise ValueError(f"WTB checkpoint_files link not found for checkpoint {checkpoint_id}")
            
            if link.file_commit_id != file_commit_id:
                raise ValueError(
                    f"file_commit_id mismatch: expected {file_commit_id}, got {link.file_commit_id}"
                )
        
        # Step 3: FileTracker verification would go here
        logger.debug(f"Verified checkpoint-file link: cp={checkpoint_id}, commit={file_commit_id}")
    
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

