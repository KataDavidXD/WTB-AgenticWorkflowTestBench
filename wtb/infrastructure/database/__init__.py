"""Database infrastructure - ORM models, repositories, and unit of work."""

from .models import (
    Base,
    WorkflowORM,
    ExecutionORM,
    NodeVariantORM,
    BatchTestORM,
    EvaluationResultORM,
    NodeBoundaryORM,
    # CheckpointFileORM DEPRECATED (2026-01-27) - Use CheckpointFileLinkORM from file_processing_orm
)
from .file_processing_orm import (
    CheckpointFileLinkORM,  # PRIMARY: Use this for checkpoint-file links
    FileBlobORM,
    FileCommitORM,
    FileMementoORM,
)
from .unit_of_work import SQLAlchemyUnitOfWork
from .inmemory_unit_of_work import InMemoryUnitOfWork
from .factory import UnitOfWorkFactory
from .config import (
    # DatabaseConfig,
    get_database_config,
    # create_wtb_engine,
    # create_wtb_session_factory,
    # print_database_locations,
)
from .setup import (
    setup_wtb_database,
    setup_agentgit_database,
    setup_all_databases,
    get_wtb_session,
)

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
    # "create_wtb_engine",
    # "create_wtb_session_factory",
    # "print_database_locations",
    # Setup
    "setup_wtb_database",
    "setup_agentgit_database",
    "setup_all_databases",
    "get_wtb_session",
]

