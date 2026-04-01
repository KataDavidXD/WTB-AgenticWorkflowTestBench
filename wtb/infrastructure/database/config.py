"""
Database Configuration for WTB and AgentGit.

This module provides centralized database path configuration.
Following the existing architecture pattern:
- Config: Only paths and URLs (this file)
- Models: SQLAlchemy ORM definitions (models.py)
- Repositories: Data access abstraction (repositories/)
- UoW: Transaction management (unit_of_work.py)

Usage:
    from wtb.infrastructure.database.config import get_database_config
    
    config = get_database_config()
    print(config.wtb_db_url)
"""

import os
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from sqlalchemy.orm import sessionmaker

from .engine_cache import get_engine


@dataclass
class DatabaseConfig:
    """Database configuration settings (paths and URLs only)."""
    
    # Base data directory
    data_dir: Path
    
    # AgentGit database
    agentgit_db_path: Path
    
    # WTB database
    wtb_db_path: Path
    wtb_db_url: str
    
    # FileTracker (PostgreSQL)
    filetracker_host: str = "localhost"
    filetracker_port: int = 5432
    filetracker_database: str = "filetracker"
    filetracker_user: str = "postgres"
    filetracker_password: str = ""
    
    @property
    def agentgit_db_url(self) -> str:
        """Get AgentGit SQLite URL."""
        return f"sqlite:///{self.agentgit_db_path}"
    
    @property
    def filetracker_url(self) -> str:
        """Get FileTracker PostgreSQL URL."""
        if self.filetracker_password:
            return f"postgresql://{self.filetracker_user}:{self.filetracker_password}@{self.filetracker_host}:{self.filetracker_port}/{self.filetracker_database}"
        return f"postgresql://{self.filetracker_user}@{self.filetracker_host}:{self.filetracker_port}/{self.filetracker_database}"


# Global configuration cache
_config: Optional[DatabaseConfig] = None


def get_project_root() -> Path:
    """Get the project root directory."""
    current = Path(__file__).resolve()
    
    for parent in current.parents:
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            return parent
    
    return Path.cwd()


def get_database_config(
    data_dir: Optional[Path] = None,
    use_env: bool = True
) -> DatabaseConfig:
    """
    Get database configuration (paths and URLs only).
    
    Args:
        data_dir: Override data directory path
        use_env: Whether to read from environment variables
        
    Returns:
        DatabaseConfig with all paths configured
    """
    global _config
    
    if _config is not None and data_dir is None:
        return _config
    
    # Determine data directory
    if data_dir is None:
        if use_env and os.getenv("WTB_DATA_DIR"):
            data_dir = Path(os.getenv("WTB_DATA_DIR"))
        else:
            data_dir = get_project_root() / "data"
    
    # Ensure data directory exists
    data_dir.mkdir(parents=True, exist_ok=True)
    
    # AgentGit database path
    agentgit_db_path = data_dir / "agentgit.db"
    
    # WTB database path
    wtb_db_path = data_dir / "wtb.db"
    wtb_db_url = f"sqlite:///{wtb_db_path}"
    
    # Allow PostgreSQL override for WTB
    if use_env and os.getenv("WTB_DATABASE_URL"):
        wtb_db_url = os.getenv("WTB_DATABASE_URL")
    
    # FileTracker settings from environment
    filetracker_host = os.getenv("FILETRACKER_HOST", "localhost")
    filetracker_port = int(os.getenv("FILETRACKER_PORT", "5432"))
    filetracker_database = os.getenv("FILETRACKER_DATABASE", "filetracker")
    filetracker_user = os.getenv("FILETRACKER_USER", "postgres")
    filetracker_password = os.getenv("FILETRACKER_PASSWORD", "")
    
    _config = DatabaseConfig(
        data_dir=data_dir,
        agentgit_db_path=agentgit_db_path,
        wtb_db_path=wtb_db_path,
        wtb_db_url=wtb_db_url,
        filetracker_host=filetracker_host,
        filetracker_port=filetracker_port,
        filetracker_database=filetracker_database,
        filetracker_user=filetracker_user,
        filetracker_password=filetracker_password,
    )
    
    return _config


def redirect_agentgit_database() -> bool:
    """
    Redirect AgentGit to use our local database path.
    
    Patches agentgit.database.db_config.get_database_path to return
    our project-local database path.
    """
    config = get_database_config()
    
    try:
        import agentgit.database.db_config as agentgit_db_config
        
        def patched_get_database_path() -> str:
            return str(config.agentgit_db_path)
        
        agentgit_db_config.get_database_path = patched_get_database_path
        return True
        
    except ImportError:
        return False


def create_wtb_engine(echo: bool = False):
    """
    Create SQLAlchemy engine for WTB database.
    
    Args:
        echo: Whether to log SQL statements
        
    Returns:
        SQLAlchemy Engine
    """
    config = get_database_config()
    return get_engine(config.wtb_db_url, echo)


def create_wtb_session_factory(engine=None):
    """
    Create SQLAlchemy session factory for WTB.
    
    Args:
        engine: Optional engine (creates new if not provided)
        
    Returns:
        SQLAlchemy sessionmaker
    """
    if engine is None:
        engine = create_wtb_engine()
    return sessionmaker(bind=engine)


def print_database_locations():
    """Print all database locations."""
    config = get_database_config()
    
    print("\n" + "="*60)
    print("DATABASE LOCATIONS")
    print("="*60)
    print(f"\nData Directory: {config.data_dir}")
    print(f"\nAgentGit (SQLite):")
    print(f"  Path: {config.agentgit_db_path}")
    print(f"  URL:  {config.agentgit_db_url}")
    print(f"  Exists: {config.agentgit_db_path.exists()}")
    if config.agentgit_db_path.exists():
        size = config.agentgit_db_path.stat().st_size / 1024
        print(f"  Size: {size:.2f} KB")
    
    print(f"\nWTB (SQLite/PostgreSQL):")
    print(f"  Path: {config.wtb_db_path}")
    print(f"  URL:  {config.wtb_db_url}")
    print(f"  Exists: {config.wtb_db_path.exists()}")
    if config.wtb_db_path.exists():
        size = config.wtb_db_path.stat().st_size / 1024
        print(f"  Size: {size:.2f} KB")
    
    print(f"\nFileTracker (PostgreSQL):")
    print(f"  URL: {config.filetracker_url}")
    print("="*60 + "\n")


if __name__ == "__main__":
    print_database_locations()
