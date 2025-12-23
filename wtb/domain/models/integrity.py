"""
Data Integrity Domain Models.

Models for cross-database integrity checking between WTB, AgentGit, and FileTracker.

Design Philosophy:
- IntegrityIssue represents a single data consistency problem
- IntegrityReport aggregates all issues from a check run
- Supports both detection and automated repair
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from datetime import datetime
from enum import Enum


class IntegrityIssueType(Enum):
    """Types of integrity issues detected."""
    DANGLING_REFERENCE = "dangling_reference"     # Reference to non-existent record
    ORPHAN_CHECKPOINT = "orphan_checkpoint"       # Checkpoint not referenced by WTB
    ORPHAN_FILE_COMMIT = "orphan_file_commit"     # File commit not linked to checkpoint
    STATE_MISMATCH = "state_mismatch"             # Status doesn't match data
    OUTBOX_STUCK = "outbox_stuck"                 # Outbox event stuck processing
    MISSING_BLOB = "missing_blob"                 # Blob file missing from storage


class IntegritySeverity(Enum):
    """Severity level of integrity issues."""
    CRITICAL = "critical"   # Must fix, affects functionality
    WARNING = "warning"     # Should fix, may cause problems
    INFO = "info"           # For reference only


class RepairAction(Enum):
    """Available repair actions for issues."""
    DELETE_ORPHAN = "delete_orphan"
    CREATE_MISSING = "create_missing"
    UPDATE_STATUS = "update_status"
    RETRY_OUTBOX = "retry_outbox"
    MANUAL_REQUIRED = "manual_required"


@dataclass
class IntegrityIssue:
    """
    Single integrity issue detected during checking.
    
    Attributes:
        issue_type: Type of the issue
        severity: How serious the issue is
        source_table: Table where the issue was found
        source_id: ID of the problematic record
        target_table: Referenced table (if applicable)
        target_id: Referenced ID that's missing (if applicable)
        message: Human-readable description
        details: Additional context
        suggested_action: Recommended repair action
        auto_repairable: Whether it can be fixed automatically
    """
    
    issue_type: IntegrityIssueType
    severity: IntegritySeverity
    
    # Location info
    source_table: str
    source_id: str
    target_table: Optional[str] = None
    target_id: Optional[str] = None
    
    # Details
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    
    # Repair info
    suggested_action: RepairAction = RepairAction.MANUAL_REQUIRED
    auto_repairable: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "issue_type": self.issue_type.value,
            "severity": self.severity.value,
            "source_table": self.source_table,
            "source_id": self.source_id,
            "target_table": self.target_table,
            "target_id": self.target_id,
            "message": self.message,
            "details": self.details,
            "suggested_action": self.suggested_action.value,
            "auto_repairable": self.auto_repairable,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IntegrityIssue":
        """Create from dictionary."""
        return cls(
            issue_type=IntegrityIssueType(data["issue_type"]),
            severity=IntegritySeverity(data["severity"]),
            source_table=data["source_table"],
            source_id=data["source_id"],
            target_table=data.get("target_table"),
            target_id=data.get("target_id"),
            message=data.get("message", ""),
            details=data.get("details", {}),
            suggested_action=RepairAction(data.get("suggested_action", "manual_required")),
            auto_repairable=data.get("auto_repairable", False),
        )
    
    @classmethod
    def dangling_reference(
        cls,
        source_table: str,
        source_id: str,
        target_table: str,
        target_id: str,
        message: str,
        auto_repair: bool = True,
        **details
    ) -> "IntegrityIssue":
        """Factory for dangling reference issues."""
        return cls(
            issue_type=IntegrityIssueType.DANGLING_REFERENCE,
            severity=IntegritySeverity.CRITICAL,
            source_table=source_table,
            source_id=source_id,
            target_table=target_table,
            target_id=target_id,
            message=message,
            details=details,
            suggested_action=RepairAction.DELETE_ORPHAN,
            auto_repairable=auto_repair,
        )
    
    @classmethod
    def orphan_checkpoint(
        cls,
        checkpoint_id: int,
        message: str,
        **details
    ) -> "IntegrityIssue":
        """Factory for orphan checkpoint issues."""
        return cls(
            issue_type=IntegrityIssueType.ORPHAN_CHECKPOINT,
            severity=IntegritySeverity.INFO,
            source_table="checkpoints",
            source_id=str(checkpoint_id),
            message=message,
            details=details,
            suggested_action=RepairAction.MANUAL_REQUIRED,
            auto_repairable=False,  # Need human confirmation
        )
    
    @classmethod
    def outbox_stuck(
        cls,
        event_id: str,
        event_type: str,
        retry_count: int,
        can_retry: bool,
        message: str,
        **details
    ) -> "IntegrityIssue":
        """Factory for stuck outbox event issues."""
        return cls(
            issue_type=IntegrityIssueType.OUTBOX_STUCK,
            severity=IntegritySeverity.CRITICAL if not can_retry else IntegritySeverity.WARNING,
            source_table="wtb_outbox",
            source_id=event_id,
            message=message,
            details={"event_type": event_type, "retry_count": retry_count, **details},
            suggested_action=RepairAction.RETRY_OUTBOX if can_retry else RepairAction.MANUAL_REQUIRED,
            auto_repairable=can_retry,
        )


@dataclass
class IntegrityReport:
    """
    Aggregated integrity check report.
    
    Contains all issues found during a check run, with statistics
    and repair results if repair was performed.
    """
    
    checked_at: datetime = field(default_factory=datetime.now)
    duration_ms: float = 0
    
    # Statistics
    total_checked: int = 0
    issues_found: int = 0
    critical_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    
    # Issue list
    issues: List[IntegrityIssue] = field(default_factory=list)
    
    # Repair results
    repaired_count: int = 0
    repair_failed_count: int = 0
    
    def add_issue(self, issue: IntegrityIssue) -> None:
        """Add an issue to the report."""
        self.issues.append(issue)
        self.issues_found += 1
        
        if issue.severity == IntegritySeverity.CRITICAL:
            self.critical_count += 1
        elif issue.severity == IntegritySeverity.WARNING:
            self.warning_count += 1
        else:
            self.info_count += 1
    
    @property
    def is_healthy(self) -> bool:
        """Check if no critical issues were found."""
        return self.critical_count == 0
    
    @property
    def auto_repairable_count(self) -> int:
        """Count of issues that can be auto-repaired."""
        return sum(1 for i in self.issues if i.auto_repairable)
    
    def get_issues_by_type(self, issue_type: IntegrityIssueType) -> List[IntegrityIssue]:
        """Get all issues of a specific type."""
        return [i for i in self.issues if i.issue_type == issue_type]
    
    def get_issues_by_severity(self, severity: IntegritySeverity) -> List[IntegrityIssue]:
        """Get all issues of a specific severity."""
        return [i for i in self.issues if i.severity == severity]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "checked_at": self.checked_at.isoformat(),
            "duration_ms": self.duration_ms,
            "total_checked": self.total_checked,
            "issues_found": self.issues_found,
            "critical_count": self.critical_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
            "is_healthy": self.is_healthy,
            "issues": [i.to_dict() for i in self.issues],
            "repaired_count": self.repaired_count,
            "repair_failed_count": self.repair_failed_count,
        }
    
    def summary(self) -> str:
        """Generate human-readable summary."""
        status = "✅ HEALTHY" if self.is_healthy else "❌ UNHEALTHY"
        lines = [
            status,
            f"Checked: {self.total_checked} items in {self.duration_ms:.2f}ms",
            f"Issues: {self.issues_found} (Critical: {self.critical_count}, Warning: {self.warning_count}, Info: {self.info_count})",
        ]
        
        if self.repaired_count > 0 or self.repair_failed_count > 0:
            lines.append(f"Repaired: {self.repaired_count}, Failed: {self.repair_failed_count}")
        
        return "\n".join(lines)
    
    def detailed_summary(self) -> str:
        """Generate detailed summary with issue breakdown."""
        lines = [self.summary(), "", "Details:"]
        
        for issue in self.issues:
            severity_icon = {
                IntegritySeverity.CRITICAL: "🔴",
                IntegritySeverity.WARNING: "🟡",
                IntegritySeverity.INFO: "🔵",
            }.get(issue.severity, "⚪")
            
            lines.append(f"  {severity_icon} [{issue.issue_type.value}] {issue.message}")
            if issue.auto_repairable:
                lines.append(f"      → Auto-fix: {issue.suggested_action.value}")
        
        return "\n".join(lines)

