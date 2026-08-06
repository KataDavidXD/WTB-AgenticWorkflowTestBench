
from typing import ClassVar

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from wtb.domain.interfaces.async_unit_of_work import IAsyncUnitOfWork
from wtb.infrastructure.database.async_repositories import (
    AsyncAuditLogRepository,
    AsyncBatchTestRepository,
    AsyncEvaluationResultRepository,
    AsyncExecutionRepository,
    AsyncNodeBoundaryRepository,
    AsyncNodeVariantRepository,
    AsyncOutboxRepository,
    AsyncSQLAlchemyBlobRepository,
    AsyncSQLAlchemyCheckpointFileLinkRepository,
    AsyncSQLAlchemyFileCommitRepository,
    AsyncWorkflowRepository,
)
from wtb.infrastructure.database.engine_cache import configure_sqlite_foreign_keys


class AsyncSQLAlchemyUnitOfWork(IAsyncUnitOfWork):
    """
    Async SQLAlchemy Unit of Work implementation.
    
    Uses SQLAlchemy 2.0 async support with:
    - aiosqlite for SQLite
    - asyncpg for PostgreSQL
    
    ACID Compliance:
    - Atomicity: Session transaction
    - Consistency: ORM validation
    - Isolation: Session-level isolation
    - Durability: Async commit to disk
    """
    
    # Class-level engine cache - shared across all instances
    _engine_pool: ClassVar[dict[str, AsyncEngine]] = {}
    
    def __init__(self, db_url: str, blob_storage_path: str = "./data/blobs"):
        # Convert sync URL to async URL if needed
        if db_url.startswith("sqlite:///"):
            async_url = db_url.replace("sqlite:///", "sqlite+aiosqlite:///")
        elif db_url.startswith("postgresql://"):
            async_url = db_url.replace("postgresql://", "postgresql+asyncpg://")
        else:
            async_url = db_url
            
        self._db_url = async_url
        self._blob_storage_path = blob_storage_path
        self._session_factory = None
        self._session: AsyncSession | None = None
    
    @classmethod
    def get_engine(cls, db_url: str) -> AsyncEngine:
        """Get or create async engine with connection pooling."""
        if db_url not in cls._engine_pool:
            engine = create_async_engine(
                db_url,
                pool_size=5,
                max_overflow=10,
                pool_recycle=3600,
                echo=False,
            )
            configure_sqlite_foreign_keys(engine.sync_engine)
            cls._engine_pool[db_url] = engine
        return cls._engine_pool[db_url]

    async def __aenter__(self) -> "AsyncSQLAlchemyUnitOfWork":
        engine = self.get_engine(self._db_url)
        self._session_factory = async_sessionmaker(
            engine, 
            class_=AsyncSession,
            expire_on_commit=False,
        )
        self._session = self._session_factory()
        self._init_repositories()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        End transaction with guaranteed session cleanup.
        """
        try:
            if exc_type is not None:
                await self.arollback()
            # If no exception, caller should have called acommit() explicitly.
            # But standard Context Manager pattern usually commits on exit if no error?
            # IAsyncUnitOfWork definition says "commit()" is explicit.
            # So here we just cleanup.
        finally:
            if self._session:
                await self._session.close()
    
    async def acommit(self) -> None:
        if self._session:
            await self._session.commit()
    
    async def arollback(self) -> None:
        if self._session:
            await self._session.rollback()
    
    def _init_repositories(self):
        """Initialize async repositories with session."""
        if not self._session:
            raise RuntimeError("Session not initialized")
            
        # Core
        self.workflows = AsyncWorkflowRepository(self._session, None) # Mocks need orm_class passed? BaseAsyncRepository takes orm_class
        # BaseAsyncRepository requires orm_class. My stubs in async_core_repositories.py inherited from BaseAsyncRepository but didn't provide init?
        # They inherit __init__ from BaseAsyncRepository which expects (session, orm_class).
        # So I need to pass the ORM classes.
        
        from wtb.infrastructure.database.models import (
            AuditLogORM,
            BatchTestORM,
            EvaluationResultORM,
            ExecutionORM,
            NodeBoundaryORM,
            NodeVariantORM,
            WorkflowORM,
        )
        
        self.workflows = AsyncWorkflowRepository(self._session, WorkflowORM)
        self.executions = AsyncExecutionRepository(self._session, ExecutionORM)
        self.variants = AsyncNodeVariantRepository(self._session, NodeVariantORM)
        self.batch_tests = AsyncBatchTestRepository(self._session, BatchTestORM)
        self.evaluation_results = AsyncEvaluationResultRepository(self._session, EvaluationResultORM)
        self.audit_logs = AsyncAuditLogRepository(self._session, AuditLogORM)
        self.node_boundaries = AsyncNodeBoundaryRepository(self._session, NodeBoundaryORM)
        
        # Outbox (Specific impl, not BaseAsyncRepository based or different init?)
        # AsyncOutboxRepository only took session in my impl.
        self.outbox = AsyncOutboxRepository(self._session)
        
        # File Processing
        self.blobs = AsyncSQLAlchemyBlobRepository(self._session, self._blob_storage_path)
        self.file_commits = AsyncSQLAlchemyFileCommitRepository(self._session)
        self.checkpoint_file_links = AsyncSQLAlchemyCheckpointFileLinkRepository(self._session)
