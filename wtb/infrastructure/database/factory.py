"""
Unit of Work Factory.

Factory pattern for creating the appropriate UnitOfWork implementation
based on configuration or mode.

Supports dependency injection and configuration-based switching.
"""

from typing import Optional

from wtb.domain.interfaces.unit_of_work import IUnitOfWork
from .inmemory_unit_of_work import InMemoryUnitOfWork
from .unit_of_work import SQLAlchemyUnitOfWork
from .config import get_database_config


class UnitOfWorkFactory:
    """
    Factory for creating the appropriate UnitOfWork implementation.
    
    Supports dependency injection and configuration-based switching.
    
    Usage:
        # For testing (no database)
        uow = UnitOfWorkFactory.create_inmemory()
        
        # For development (SQLite file)
        uow = UnitOfWorkFactory.create_sqlalchemy("sqlite:///data/wtb.db")
        
        # For production (PostgreSQL)
        uow = UnitOfWorkFactory.create_sqlalchemy("postgresql://user:pass@host/db")
        
        # Config-based
        uow = UnitOfWorkFactory.create(
            mode=config.wtb_storage_mode,
            db_url=config.wtb_db_url
        )
    """
    
    @staticmethod
    def create_inmemory() -> InMemoryUnitOfWork:
        """
        Create in-memory UoW for testing.
        
        Returns:
            InMemoryUnitOfWork instance
        """
        return InMemoryUnitOfWork()
    
    @staticmethod
    def create_sqlalchemy(db_url: Optional[str] = None, echo: bool = False) -> SQLAlchemyUnitOfWork:
        """
        Create SQLAlchemy UoW for production.
        
        Args:
            db_url: Database URL (uses config default if None)
            echo: If True, log SQL statements
            
        Returns:
            SQLAlchemyUnitOfWork instance
        """
        if db_url is None:
            config = get_database_config()
            db_url = config.wtb_db_url
        return SQLAlchemyUnitOfWork(db_url, echo=echo)
    
    @staticmethod
    def create(
        mode: str = "inmemory",
        db_url: Optional[str] = None,
        echo: bool = False
    ) -> IUnitOfWork:
        """
        Create UoW based on mode configuration.
        
        Args:
            mode: "inmemory" or "sqlalchemy"
            db_url: Database URL (required for sqlalchemy mode if not using config)
            echo: If True, log SQL statements (sqlalchemy only)
            
        Returns:
            IUnitOfWork implementation
            
        Raises:
            ValueError: If mode is unknown
            
        Examples:
            # Testing mode
            uow = UnitOfWorkFactory.create(mode="inmemory")
            
            # Development mode with default SQLite
            uow = UnitOfWorkFactory.create(mode="sqlalchemy")
            
            # Production mode with PostgreSQL
            uow = UnitOfWorkFactory.create(
                mode="sqlalchemy",
                db_url="postgresql://user:pass@localhost/wtb"
            )
        """
        if mode == "inmemory":
            return UnitOfWorkFactory.create_inmemory()
        
        elif mode == "sqlalchemy":
            return UnitOfWorkFactory.create_sqlalchemy(db_url, echo=echo)
        
        else:
            raise ValueError(
                f"Unknown storage mode: {mode}. Use 'inmemory' or 'sqlalchemy'"
            )
    
    @staticmethod
    def create_for_testing() -> InMemoryUnitOfWork:
        """
        Convenience method for tests.
        
        Returns:
            Fresh InMemoryUnitOfWork instance for test isolation
        """
        return UnitOfWorkFactory.create_inmemory()
    
    @staticmethod
    def create_for_development() -> SQLAlchemyUnitOfWork:
        """
        Convenience method for development.
        
        Uses SQLite from config with SQL logging enabled.
        
        Returns:
            SQLAlchemyUnitOfWork instance
        """
        return UnitOfWorkFactory.create_sqlalchemy(echo=True)
    
    @staticmethod
    def create_for_production(db_url: Optional[str] = None) -> SQLAlchemyUnitOfWork:
        """
        Convenience method for production.
        
        Uses provided URL or config default, no SQL logging.
        
        Args:
            db_url: Database URL (uses config if None)
            
        Returns:
            SQLAlchemyUnitOfWork instance
        """
        return UnitOfWorkFactory.create_sqlalchemy(db_url, echo=False)

