"""Database infrastructure - ORM models, repositories, and unit of work."""

from .models import (
    Base,
    WorkflowORM,
    ExecutionORM,
    NodeVariantORM,
    BatchTestORM,
    EvaluationResultORM,
    NodeBoundaryORM,
    CheckpointFileORM,
)
from .unit_of_work import SQLAlchemyUnitOfWork
from .inmemory_unit_of_work import InMemoryUnitOfWork
from .factory import UnitOfWorkFactory
from .config import (
    DatabaseConfig,
    get_database_config,
    redirect_agentgit_database,
    create_wtb_engine,
    create_wtb_session_factory,
    print_database_locations,
)
from .setup import (
    setup_wtb_database,
    setup_agentgit_database,
    setup_all_databases,
    get_wtb_session,
)

__all__ = [
    # Models
    "Base",
    "WorkflowORM",
    "ExecutionORM",
    "NodeVariantORM",
    "BatchTestORM",
    "EvaluationResultORM",
    "NodeBoundaryORM",
    "CheckpointFileORM",
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

