
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .async_repositories import (
        IAsyncBlobRepository,
        IAsyncCheckpointFileLinkRepository,
        IAsyncFileCommitRepository,
        IAsyncOutboxRepository,
        IAsyncRepository,
    )
    # Forward references to other repositories
    # These would be defined in async_repositories.py as well ideally, 
    # but for now we assume they follow the IAsyncRepository pattern.
    IAsyncWorkflowRepository = IAsyncRepository
    IAsyncExecutionRepository = IAsyncRepository
    IAsyncNodeVariantRepository = IAsyncRepository
    IAsyncBatchTestRepository = IAsyncRepository
    IAsyncEvaluationResultRepository = IAsyncRepository
    IAsyncAuditLogRepository = IAsyncRepository


class IAsyncUnitOfWork(ABC):
    """
    Async Unit of Work pattern interface.
    
    ACID Compliance:
    - Atomicity: All changes committed or rolled back together
    - Consistency: Validation before commit
    - Isolation: Session-level isolation
    - Durability: Persistent storage on commit
    
    Usage:
        async with uow:
            workflow = await uow.workflows.aget(workflow_id)
            execution = Execution(workflow_id=workflow.id)
            await uow.executions.aadd(execution)
            await uow.acommit()
    
    File Tracking Usage:
        async with uow:
            blob_id = await uow.blobs.asave(content)
            commit = FileCommit.create(mementos=[...])
            await uow.file_commits.asave(commit)
            await uow.checkpoint_file_links.aadd(link)
            await uow.acommit()  # ATOMIC: all or nothing
    """
    
    # ═══════════════════════════════════════════════════════════════════════════
    # WTB Core Repositories
    # ═══════════════════════════════════════════════════════════════════════════
    workflows: "IAsyncWorkflowRepository"
    executions: "IAsyncExecutionRepository"
    variants: "IAsyncNodeVariantRepository"
    batch_tests: "IAsyncBatchTestRepository"
    evaluation_results: "IAsyncEvaluationResultRepository"
    audit_logs: "IAsyncAuditLogRepository"
    
    # ═══════════════════════════════════════════════════════════════════════════
    # File Processing Repositories (from FILE_TRACKING_ARCHITECTURE_DECISION.md)
    # ═══════════════════════════════════════════════════════════════════════════
    blobs: "IAsyncBlobRepository"
    file_commits: "IAsyncFileCommitRepository"
    checkpoint_file_links: "IAsyncCheckpointFileLinkRepository"
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Infrastructure Repositories
    # ═══════════════════════════════════════════════════════════════════════════
    outbox: "IAsyncOutboxRepository"
  
    @abstractmethod
    async def __aenter__(self) -> "IAsyncUnitOfWork":
        """Begin async transaction."""
        pass
  
    @abstractmethod
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """End async transaction with auto-rollback on exception."""
        pass
  
    @abstractmethod
    async def acommit(self) -> None:
        """Commit transaction asynchronously."""
        pass
  
    @abstractmethod
    async def arollback(self) -> None:
        """Rollback transaction asynchronously."""
        pass
