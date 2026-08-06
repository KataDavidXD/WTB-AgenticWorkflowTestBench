"""Database infrastructure - ORM models, repositories, and unit of work."""

from .config import (
    DatabaseConfig,
    create_wtb_engine,
    create_wtb_session_factory,
    get_database_config,
    print_database_locations,
    redirect_agentgit_database,
)
from .factory import UnitOfWorkFactory
from .file_processing_orm import (
    CheckpointFileLinkORM,  # PRIMARY: Use this for checkpoint-file links
    FileBlobORM,
    FileCommitORM,
    FileMementoORM,
)
from .inmemory_unit_of_work import InMemoryUnitOfWork
from .models import (
    Base,
    BatchTestORM,
    EvaluationResultORM,
    ExecutionORM,
    NodeBoundaryORM,
    # CheckpointFileORM DEPRECATED (2026-01-27) - Use CheckpointFileLinkORM from file_processing_orm
    NodeVariantORM,
    WorkflowORM,
)
from .setup import (
    get_wtb_session,
    setup_agentgit_database,
    setup_all_databases,
    setup_wtb_database,
)
from .unit_of_work import SQLAlchemyUnitOfWork

__all__ = [
    # Core Models
    "Base",
    "WorkflowORM",
    "ExecutionORM",
    "NodeVariantORM",
    "BatchTestORM",
    "EvaluationResultORM",
    "NodeBoundaryORM",
    # File Processing Models (PRIMARY - 2026-01-27)
    "CheckpointFileLinkORM",
    "FileBlobORM",
    "FileCommitORM",
    "FileMementoORM",
    # UoW
    "SQLAlchemyUnitOfWork",
    "InMemoryUnitOfWork",
    "UnitOfWorkFactory",
    # Config
    "DatabaseConfig",
    "get_database_config",
    "redirect_agentgit_database",
    "create_wtb_engine",
    "create_wtb_session_factory",
    "print_database_locations",
    # Setup
    "setup_wtb_database",
    "setup_agentgit_database",
    "setup_all_databases",
    "get_wtb_session",
]

