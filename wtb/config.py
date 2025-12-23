"""
WTB Configuration.

Centralized configuration for Workflow Test Bench with storage mode options.

Supports multiple deployment modes:
- Testing: All in-memory (no I/O)
- Development: SQLite persistence with AgentGit integration
- Production: PostgreSQL with full persistence

Usage:
    from wtb.config import WTBConfig
    
    # For testing
    config = WTBConfig.for_testing()
    
    # For development
    config = WTBConfig.for_development()
    
    # For production
    config = WTBConfig.for_production("postgresql://user:pass@host/db")
    
    # From environment
    config = WTBConfig.from_env()
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path


@dataclass
class WTBConfig:
    """
    WTB configuration with storage options.
    
    Attributes:
        wtb_storage_mode: Storage backend - "inmemory" or "sqlalchemy"
        wtb_db_url: Database URL for sqlalchemy mode
        agentgit_db_path: Path to AgentGit SQLite database
        state_adapter_mode: State adapter - "inmemory" or "agentgit"
        data_dir: Base directory for data files
    """
    
    # Storage mode: "inmemory" or "sqlalchemy"
    wtb_storage_mode: str = "inmemory"
    
    # Database URL (for sqlalchemy mode)
    wtb_db_url: Optional[str] = None
    
    # AgentGit database path
    agentgit_db_path: str = "data/agentgit.db"
    
    # State adapter mode: "inmemory" or "agentgit"
    state_adapter_mode: str = "inmemory"
    
    # Base data directory
    data_dir: str = "data"
    
    # FileTracker settings
    filetracker_enabled: bool = False
    filetracker_storage_path: Optional[str] = None
    
    # IDE sync settings
    ide_sync_enabled: bool = False
    ide_sync_url: Optional[str] = None
    
    # Logging
    log_sql: bool = False
    log_level: str = "INFO"
    
    def __post_init__(self):
        """Set default wtb_db_url if not provided."""
        if self.wtb_db_url is None and self.wtb_storage_mode == "sqlalchemy":
            self.wtb_db_url = f"sqlite:///{self.data_dir}/wtb.db"
    
    @classmethod
    def from_env(cls) -> "WTBConfig":
        """
        Create config from environment variables.
        
        Environment Variables:
            WTB_STORAGE_MODE: "inmemory" or "sqlalchemy" (default: "inmemory")
            WTB_DATABASE_URL: Database URL for sqlalchemy mode
            AGENTGIT_DB_PATH: Path to AgentGit database
            STATE_ADAPTER_MODE: "inmemory" or "agentgit" (default: "inmemory")
            WTB_DATA_DIR: Base data directory (default: "data")
            FILETRACKER_ENABLED: Enable FileTracker integration (default: "false")
            FILETRACKER_STORAGE: FileTracker storage path
            IDE_SYNC_ENABLED: Enable IDE sync (default: "false")
            IDE_SYNC_URL: IDE WebSocket URL
            WTB_LOG_SQL: Log SQL statements (default: "false")
            WTB_LOG_LEVEL: Logging level (default: "INFO")
        
        Returns:
            WTBConfig instance
        """
        data_dir = os.getenv("WTB_DATA_DIR", "data")
        
        return cls(
            wtb_storage_mode=os.getenv("WTB_STORAGE_MODE", "inmemory"),
            wtb_db_url=os.getenv("WTB_DATABASE_URL"),
            agentgit_db_path=os.getenv("AGENTGIT_DB_PATH", f"{data_dir}/agentgit.db"),
            state_adapter_mode=os.getenv("STATE_ADAPTER_MODE", "inmemory"),
            data_dir=data_dir,
            filetracker_enabled=os.getenv("FILETRACKER_ENABLED", "false").lower() == "true",
            filetracker_storage_path=os.getenv("FILETRACKER_STORAGE"),
            ide_sync_enabled=os.getenv("IDE_SYNC_ENABLED", "false").lower() == "true",
            ide_sync_url=os.getenv("IDE_SYNC_URL"),
            log_sql=os.getenv("WTB_LOG_SQL", "false").lower() == "true",
            log_level=os.getenv("WTB_LOG_LEVEL", "INFO"),
        )
    
    @classmethod
    def for_testing(cls) -> "WTBConfig":
        """
        Create config for unit tests (all in-memory).
        
        Returns:
            WTBConfig with in-memory storage
        """
        return cls(
            wtb_storage_mode="inmemory",
            state_adapter_mode="inmemory",
            data_dir="data",
            filetracker_enabled=False,
            ide_sync_enabled=False,
        )
    
    @classmethod
    def for_development(cls, data_dir: str = "data") -> "WTBConfig":
        """
        Create config for development (SQLite persistence).
        
        Args:
            data_dir: Directory for database files
            
        Returns:
            WTBConfig with SQLite persistence
        """
        return cls(
            wtb_storage_mode="sqlalchemy",
            wtb_db_url=f"sqlite:///{data_dir}/wtb.db",
            agentgit_db_path=f"{data_dir}/agentgit.db",
            state_adapter_mode="agentgit",
            data_dir=data_dir,
            filetracker_enabled=False,
            ide_sync_enabled=False,
            log_sql=True,
        )
    
    @classmethod
    def for_production(
        cls,
        db_url: str,
        agentgit_db_path: str = "data/agentgit.db",
        data_dir: str = "data",
    ) -> "WTBConfig":
        """
        Create config for production (PostgreSQL).
        
        Args:
            db_url: PostgreSQL database URL
            agentgit_db_path: Path to AgentGit database
            data_dir: Directory for local files
            
        Returns:
            WTBConfig with PostgreSQL persistence
        """
        return cls(
            wtb_storage_mode="sqlalchemy",
            wtb_db_url=db_url,
            agentgit_db_path=agentgit_db_path,
            state_adapter_mode="agentgit",
            data_dir=data_dir,
            filetracker_enabled=True,
            ide_sync_enabled=True,
            log_sql=False,
            log_level="WARNING",
        )
    
    @classmethod
    def for_standalone(cls, data_dir: str = "data") -> "WTBConfig":
        """
        Create config for standalone mode (complete persistence, no external deps).
        
        Similar to development but without SQL logging.
        
        Args:
            data_dir: Directory for database files
            
        Returns:
            WTBConfig with SQLite persistence
        """
        return cls(
            wtb_storage_mode="sqlalchemy",
            wtb_db_url=f"sqlite:///{data_dir}/wtb.db",
            agentgit_db_path=f"{data_dir}/agentgit.db",
            state_adapter_mode="agentgit",
            data_dir=data_dir,
            filetracker_enabled=False,
            ide_sync_enabled=False,
            log_sql=False,
        )
    
    def ensure_data_dir(self) -> Path:
        """
        Ensure data directory exists.
        
        Returns:
            Path to data directory
        """
        path = Path(self.data_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path
    
    def to_dict(self) -> dict:
        """Serialize config to dictionary."""
        return {
            "wtb_storage_mode": self.wtb_storage_mode,
            "wtb_db_url": self.wtb_db_url,
            "agentgit_db_path": self.agentgit_db_path,
            "state_adapter_mode": self.state_adapter_mode,
            "data_dir": self.data_dir,
            "filetracker_enabled": self.filetracker_enabled,
            "filetracker_storage_path": self.filetracker_storage_path,
            "ide_sync_enabled": self.ide_sync_enabled,
            "ide_sync_url": self.ide_sync_url,
            "log_sql": self.log_sql,
            "log_level": self.log_level,
        }


# Global config instance (lazily initialized)
_global_config: Optional[WTBConfig] = None


def get_config() -> WTBConfig:
    """
    Get global WTB configuration.
    
    Initializes from environment on first call.
    
    Returns:
        WTBConfig instance
    """
    global _global_config
    if _global_config is None:
        _global_config = WTBConfig.from_env()
    return _global_config


def set_config(config: WTBConfig) -> None:
    """
    Set global WTB configuration.
    
    Useful for tests to override configuration.
    
    Args:
        config: Configuration to use globally
    """
    global _global_config
    _global_config = config


def reset_config() -> None:
    """
    Reset global configuration to None.
    
    Next call to get_config() will reinitialize from environment.
    """
    global _global_config
    _global_config = None

