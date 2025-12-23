"""
Cross-Database Integrity Checker.

Checks referential integrity between WTB, AgentGit, and FileTracker databases.
Detects dangling references, orphan data, and state inconsistencies.
"""

from typing import Optional, List, Any, Protocol
from datetime import datetime, timedelta
import time
import logging

from wtb.domain.models.integrity import (
    IntegrityReport,
    IntegrityIssue,
    IntegrityIssueType,
    IntegritySeverity,
    RepairAction,
)
from wtb.domain.models.outbox import OutboxStatus
from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork

logger = logging.getLogger(__name__)


class ICheckpointRepository(Protocol):
    """Protocol for AgentGit checkpoint repository access."""
    
    def get_by_id(self, checkpoint_id: int) -> Optional[Any]:
        """Get checkpoint by ID."""
        ...
    
    def list_all(self, limit: int = 1000) -> List[Any]:
        """List all checkpoints."""
        ...


class IntegrityChecker:
    """
    Cross-database integrity checker for WTB, AgentGit, and FileTracker.
    
    Checks:
    - Dangling references (WTB → AgentGit, WTB → FileTracker)
    - Orphan data (checkpoints not referenced by WTB)
    - State mismatches (status doesn't match data)
    - Stuck outbox events
    
    Usage:
        # Basic check (WTB + AgentGit)
        checker = IntegrityChecker(
            wtb_db_url="sqlite:///data/wtb.db",
            checkpoint_repo=checkpoint_repository
        )
        report = checker.check()
        print(report.summary())
        
        # Check and repair
        report = checker.check_and_repair()
        print(f"Repaired {report.repaired_count} issues")
    """
    
    def __init__(
        self,
        wtb_db_url: str,
        checkpoint_repo: Optional[ICheckpointRepository] = None,
        outbox_stuck_threshold_hours: float = 1.0,
    ):
        """
        Initialize the checker.
        
        Args:
            wtb_db_url: WTB database connection URL
            checkpoint_repo: Optional AgentGit checkpoint repository
            outbox_stuck_threshold_hours: Hours before outbox event is considered stuck
        """
        self._wtb_db_url = wtb_db_url
        self._checkpoint_repo = checkpoint_repo
        self._outbox_stuck_threshold = timedelta(hours=outbox_stuck_threshold_hours)
    
    def check(self) -> IntegrityReport:
        """
        Execute full integrity check.
        
        Returns:
            IntegrityReport with all found issues
        """
        start_time = time.time()
        report = IntegrityReport()
        
        logger.info("Starting integrity check...")
        
        # Run all checks
        self._check_node_boundary_references(report)
        self._check_checkpoint_file_references(report)
        self._check_orphan_checkpoints(report)
        self._check_outbox_status(report)
        
        report.duration_ms = (time.time() - start_time) * 1000
        
        logger.info(f"Integrity check completed: {report.summary()}")
        return report
    
    def check_and_repair(self) -> IntegrityReport:
        """
        Execute integrity check and auto-repair repairable issues.
        
        Returns:
            IntegrityReport with repair results
        """
        report = self.check()
        
        for issue in report.issues:
            if issue.auto_repairable:
                try:
                    self._repair_issue(issue)
                    report.repaired_count += 1
                    logger.info(f"Repaired issue: {issue.message}")
                except Exception as e:
                    logger.error(f"Failed to repair issue: {e}")
                    report.repair_failed_count += 1
        
        return report
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Check Methods
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _check_node_boundary_references(self, report: IntegrityReport) -> None:
        """Check node_boundaries references to AgentGit checkpoints."""
        if not self._checkpoint_repo:
            logger.debug("Skipping node boundary check - no checkpoint repo")
            return
        
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            # Get all node boundaries
            boundaries = uow.node_boundaries.list(limit=10000)
            report.total_checked += len(boundaries)
            
            for boundary in boundaries:
                # Check entry_checkpoint_id
                if boundary.entry_checkpoint_id:
                    cp = self._checkpoint_repo.get_by_id(boundary.entry_checkpoint_id)
                    if not cp:
                        report.add_issue(IntegrityIssue.dangling_reference(
                            source_table="wtb_node_boundaries",
                            source_id=str(boundary.id),
                            target_table="agentgit.checkpoints",
                            target_id=str(boundary.entry_checkpoint_id),
                            message=f"Node boundary {boundary.id} references non-existent entry checkpoint {boundary.entry_checkpoint_id}",
                            node_id=boundary.node_id,
                            execution_id=boundary.execution_id,
                        ))
                
                # Check exit_checkpoint_id
                if boundary.exit_checkpoint_id:
                    cp = self._checkpoint_repo.get_by_id(boundary.exit_checkpoint_id)
                    if not cp:
                        report.add_issue(IntegrityIssue.dangling_reference(
                            source_table="wtb_node_boundaries",
                            source_id=str(boundary.id),
                            target_table="agentgit.checkpoints",
                            target_id=str(boundary.exit_checkpoint_id),
                            message=f"Node boundary {boundary.id} references non-existent exit checkpoint {boundary.exit_checkpoint_id}",
                            node_id=boundary.node_id,
                        ))
    
    def _check_checkpoint_file_references(self, report: IntegrityReport) -> None:
        """Check checkpoint_files references to AgentGit checkpoints."""
        if not self._checkpoint_repo:
            logger.debug("Skipping checkpoint file check - no checkpoint repo")
            return
        
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            # Get all checkpoint file links
            links = uow.checkpoint_files.list(limit=10000)
            report.total_checked += len(links)
            
            for link in links:
                # Check checkpoint_id exists in AgentGit
                cp = self._checkpoint_repo.get_by_id(link.checkpoint_id)
                if not cp:
                    report.add_issue(IntegrityIssue.dangling_reference(
                        source_table="wtb_checkpoint_files",
                        source_id=str(link.id),
                        target_table="agentgit.checkpoints",
                        target_id=str(link.checkpoint_id),
                        message=f"Checkpoint file link {link.id} references non-existent checkpoint {link.checkpoint_id}",
                        file_commit_id=link.file_commit_id,
                    ))
                
                # Note: FileTracker verification would require FileTracker integration
    
    def _check_orphan_checkpoints(self, report: IntegrityReport) -> None:
        """Check for AgentGit checkpoints not referenced by WTB."""
        if not self._checkpoint_repo:
            logger.debug("Skipping orphan checkpoint check - no checkpoint repo")
            return
        
        # Build set of referenced checkpoint IDs
        referenced_ids = set()
        
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            # From node_boundaries
            boundaries = uow.node_boundaries.list(limit=10000)
            for boundary in boundaries:
                if boundary.entry_checkpoint_id:
                    referenced_ids.add(boundary.entry_checkpoint_id)
                if boundary.exit_checkpoint_id:
                    referenced_ids.add(boundary.exit_checkpoint_id)
            
            # From checkpoint_files
            links = uow.checkpoint_files.list(limit=10000)
            for link in links:
                referenced_ids.add(link.checkpoint_id)
        
        # Check all checkpoints (if repo supports listing)
        try:
            all_checkpoints = self._checkpoint_repo.list_all(limit=10000)
            report.total_checked += len(all_checkpoints)
            
            for cp in all_checkpoints:
                if cp.id not in referenced_ids:
                    # Check if it's a WTB-created checkpoint (has WTB metadata)
                    metadata = getattr(cp, 'metadata', {}) or {}
                    if metadata.get("wtb_execution_id"):
                        report.add_issue(IntegrityIssue.orphan_checkpoint(
                            checkpoint_id=cp.id,
                            message=f"Checkpoint {cp.id} not referenced by any WTB record",
                            execution_id=metadata.get("wtb_execution_id"),
                            created_at=getattr(cp, 'created_at', None),
                        ))
        except (AttributeError, NotImplementedError):
            logger.debug("Checkpoint repo does not support list_all")
    
    def _check_outbox_status(self, report: IntegrityReport) -> None:
        """Check for stuck or failed outbox events."""
        stuck_threshold = datetime.now() - self._outbox_stuck_threshold
        
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            # Check pending events that are too old
            pending_events = uow.outbox.get_pending(limit=1000)
            report.total_checked += len(pending_events)
            
            for event in pending_events:
                if event.created_at < stuck_threshold:
                    report.add_issue(IntegrityIssue.outbox_stuck(
                        event_id=event.event_id,
                        event_type=event.event_type.value,
                        retry_count=event.retry_count,
                        can_retry=True,
                        message=f"Outbox event {event.event_id} pending for over {self._outbox_stuck_threshold}",
                        created_at=event.created_at.isoformat(),
                    ))
            
            # Check failed events that have exhausted retries
            failed_events = uow.outbox.get_failed_for_retry(limit=1000)
            
            # Also check events that can no longer retry
            all_failed = uow.outbox.list_all(limit=1000)
            for event in all_failed:
                if event.status == OutboxStatus.FAILED and not event.can_retry():
                    report.add_issue(IntegrityIssue.outbox_stuck(
                        event_id=event.event_id,
                        event_type=event.event_type.value,
                        retry_count=event.retry_count,
                        can_retry=False,
                        message=f"Outbox event {event.event_id} failed and cannot retry (max retries exceeded)",
                        last_error=event.last_error,
                    ))
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Repair Methods
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _repair_issue(self, issue: IntegrityIssue) -> None:
        """Repair a single issue."""
        if issue.suggested_action == RepairAction.DELETE_ORPHAN:
            self._delete_orphan(issue)
        elif issue.suggested_action == RepairAction.UPDATE_STATUS:
            self._update_status(issue)
        elif issue.suggested_action == RepairAction.RETRY_OUTBOX:
            self._retry_outbox(issue)
        else:
            raise ValueError(f"Cannot auto-repair action: {issue.suggested_action}")
    
    def _delete_orphan(self, issue: IntegrityIssue) -> None:
        """Delete orphan record."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            if issue.source_table == "wtb_node_boundaries":
                uow.node_boundaries.delete(issue.source_id)
            elif issue.source_table == "wtb_checkpoint_files":
                uow.checkpoint_files.delete(issue.source_id)
            uow.commit()
        
        logger.info(f"Deleted orphan: {issue.source_table}/{issue.source_id}")
    
    def _update_status(self, issue: IntegrityIssue) -> None:
        """Update status to reflect reality."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            if issue.source_table == "wtb_node_boundaries":
                boundary = uow.node_boundaries.get(issue.source_id)
                if boundary:
                    # Clear invalid checkpoint references
                    if "exit" in issue.message.lower():
                        boundary.exit_checkpoint_id = None
                        boundary.node_status = "incomplete"
                    if "entry" in issue.message.lower():
                        boundary.entry_checkpoint_id = None
                    uow.node_boundaries.update(boundary)
            uow.commit()
        
        logger.info(f"Updated status: {issue.source_table}/{issue.source_id}")
    
    def _retry_outbox(self, issue: IntegrityIssue) -> None:
        """Reset outbox event for retry."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            event = uow.outbox.get_by_id(issue.source_id)
            if event:
                event.status = OutboxStatus.PENDING
                event.retry_count = 0  # Reset count for stuck events
                uow.outbox.update(event)
            uow.commit()
        
        logger.info(f"Reset outbox event for retry: {issue.source_id}")

