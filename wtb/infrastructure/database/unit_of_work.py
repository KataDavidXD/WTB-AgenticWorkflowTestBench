"""
SQLAlchemy Unit of Work Implementation.

Manages transaction boundaries across multiple repositories.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from typing import Optional

from wtb.domain.interfaces.unit_of_work import IUnitOfWork
from .models import Base
from .repositories import (
    WorkflowRepository,
    ExecutionRepository,
    NodeVariantRepository,
    BatchTestRepository,
    EvaluationResultRepository,
    NodeBoundaryRepository,
    SQLAlchemyCheckpointFileLinkRepository,
    SQLAlchemyBlobRepository,
    SQLAlchemyFileCommitRepository,
)
from .repositories.outbox_repository import SQLAlchemyOutboxRepository
from .repositories.audit_repository import SQLAlchemyAuditLogRepository


class SQLAlchemyUnitOfWork(IUnitOfWork):
    """
    SQLAlchemy-based Unit of Work implementation.
    
    Usage:
        with SQLAlchemyUnitOfWork("sqlite:///wtb.db") as uow:
            workflow = uow.workflows.get(workflow_id)
            execution = Execution(workflow_id=workflow.id)
            uow.executions.add(execution)
            uow.commit()
    """
    
    def __init__(self, db_url: str = "sqlite:///wtb.db", echo: bool = False, blob_storage_path: str = "./data/blobs"):
        """
        Initialize the Unit of Work.
        
        Args:
            db_url: Database connection URL
            echo: If True, log SQL statements
            blob_storage_path: Path to blob storage
        """
        self._db_url = db_url
        self._blob_storage_path = blob_storage_path
        self._engine = create_engine(db_url, echo=echo)
        self._session_factory = sessionmaker(bind=self._engine)
        self._session: Optional[Session] = None
        
        # Create tables if they don't exist
        Base.metadata.create_all(self._engine)
    
    def __enter__(self) -> "SQLAlchemyUnitOfWork":
        """Begin transaction and initialize repositories."""
        self._session = self._session_factory()
        
        # Initialize WTB Core repositories
        self.workflows = WorkflowRepository(self._session)
        self.executions = ExecutionRepository(self._session)
        self.variants = NodeVariantRepository(self._session)
        self.batch_tests = BatchTestRepository(self._session)
        self.evaluation_results = EvaluationResultRepository(self._session)
        self.audit_logs = SQLAlchemyAuditLogRepository(self._session)
        
        # Initialize WTB Anti-Corruption Layer repositories
        self.node_boundaries = NodeBoundaryRepository(self._session)
        self.checkpoint_file_links = SQLAlchemyCheckpointFileLinkRepository(self._session)
        
        # Initialize File Processing repositories (2026-01-27)
        self.blobs = SQLAlchemyBlobRepository(self._session, self._blob_storage_path)
        self.file_commits = SQLAlchemyFileCommitRepository(self._session)
        
        # Initialize Outbox Pattern repository
        self.outbox = SQLAlchemyOutboxRepository(self._session)
        
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """End transaction, rolling back on exception."""
        if exc_type:
            self.rollback()
        if self._session:
            self._session.close()
    
    def commit(self):
        """Commit the transaction."""
        if self._session:
            try:
                self._session.commit()
            except Exception:
                self.rollback()
                raise
    
    def rollback(self):
        """Rollback the transaction."""
        if self._session:
            self._session.rollback()
    
    @property
    def session(self) -> Optional[Session]:
        """Get the current session (for advanced usage)."""
        return self._session
    
    def dispose(self):
        """
        Dispose the engine and release all resources.
        
        Call this when the UoW is no longer needed to properly release
        database file locks (especially important on Windows with SQLite).
        """
        if self._session:
            self._session.close()
            self._session = None
        if self._engine:
            self._engine.dispose()
            self._engine = None

