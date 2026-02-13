"""
WTB Configuration.

Centralized configuration for Workflow Test Bench with storage mode options.

Supports multiple deployment modes:
- Testing: All in-memory (no I/O)
- Development: SQLite persistence with AgentGit integration
- Production: PostgreSQL with full persistence

Usage:
    from wtb.config import WTBConfig, RayConfig
    
    # For testing
    config = WTBConfig.for_testing()
    
    # For development
    config = WTBConfig.for_development()
    
    # For production
    config = WTBConfig.for_production("postgresql://user:pass@host/db")
    
    # For production with Ray
    config = WTBConfig.for_ray_production(
        db_url="postgresql://...",
        ray_address="ray://cluster:10001",
    )
    
    # From environment
    config = WTBConfig.from_env()
"""

import os
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from pathlib import Path


# ═══════════════════════════════════════════════════════════════
# Ray Configuration
# ═══════════════════════════════════════════════════════════════


@dataclass
class LangGraphEventConfig:
    """
    LangGraph event streaming and audit configuration.
    
    Attributes:
        enabled: Enable LangGraph event bridging
        stream_modes: List of stream modes to enable
        include_inputs: Include input state in events (may contain PII)
        include_outputs: Include output state in events
        emit_audit_events: Emit LangGraphAuditEvent instances
        prometheus_enabled: Enable Prometheus metrics export
        filter_internal: Filter internal LangGraph nodes
    """
    enabled: bool = True
    stream_modes: List[str] = field(default_factory=lambda: ["updates"])
    include_inputs: bool = False
    include_outputs: bool = True
    emit_audit_events: bool = True
    prometheus_enabled: bool = False
    filter_internal: bool = True
    
    @classmethod
    def for_testing(cls) -> "LangGraphEventConfig":
        """Config for unit tests."""
        return cls(
            enabled=True,
            stream_modes=["updates"],
            include_inputs=False,
            include_outputs=True,
            emit_audit_events=False,
            prometheus_enabled=False,
        )
    
    @classmethod
    def for_development(cls) -> "LangGraphEventConfig":
        """Config for development with full debugging."""
        return cls(
            enabled=True,
            stream_modes=["values", "updates", "debug", "custom"],
            include_inputs=True,
            include_outputs=True,
            emit_audit_events=True,
            prometheus_enabled=False,
            filter_internal=False,
        )
    
    @classmethod
    def for_production(cls) -> "LangGraphEventConfig":
        """Config for production with metrics."""
        return cls(
            enabled=True,
            stream_modes=["updates", "custom"],
            include_inputs=False,
            include_outputs=True,
            emit_audit_events=True,
            prometheus_enabled=True,
            filter_internal=True,
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "enabled": self.enabled,
            "stream_modes": self.stream_modes,
            "include_inputs": self.include_inputs,
            "include_outputs": self.include_outputs,
            "emit_audit_events": self.emit_audit_events,
            "prometheus_enabled": self.prometheus_enabled,
            "filter_internal": self.filter_internal,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LangGraphEventConfig":
        """Deserialize from dictionary."""
        return cls(
            enabled=data.get("enabled", True),
            stream_modes=data.get("stream_modes", ["updates"]),
            include_inputs=data.get("include_inputs", False),
            include_outputs=data.get("include_outputs", True),
            emit_audit_events=data.get("emit_audit_events", True),
            prometheus_enabled=data.get("prometheus_enabled", False),
            filter_internal=data.get("filter_internal", True),
        )


@dataclass
class RayConfig:
    """
    Ray cluster configuration for batch testing.
    
    Attributes:
        ray_address: Ray cluster address ("auto" for local, or "ray://host:port")
        num_cpus_per_task: CPU allocation per actor
        memory_per_task_gb: Memory allocation per actor in GB
        max_pending_tasks: Maximum concurrent tasks (backpressure)
        max_retries: Max retries for failed tasks
        runtime_env: Optional runtime environment specification
        object_store_memory_gb: Object store memory allocation
        task_timeout_seconds: Timeout for individual task execution
    """
    ray_address: str = "auto"
    num_cpus_per_task: float = 1.0
    memory_per_task_gb: float = 2.0
    max_pending_tasks: int = 100
    max_retries: int = 3
    runtime_env: Optional[Dict[str, Any]] = None
    object_store_memory_gb: Optional[float] = None
    task_timeout_seconds: float = 3600.0  # 1 hour default
    
    @classmethod
    def for_local_development(cls) -> "RayConfig":
        """Config for local development (single node)."""
        return cls(
            ray_address="auto",
            num_cpus_per_task=1.0,
            memory_per_task_gb=1.0,
            max_pending_tasks=4,
            max_retries=1,
            task_timeout_seconds=300.0,
        )
    
    @classmethod
    def for_production(
        cls,
        ray_address: str,
        num_workers: int = 10,
        memory_gb: float = 4.0,
    ) -> "RayConfig":
        """Config for production cluster."""
        return cls(
            ray_address=ray_address,
            num_cpus_per_task=1.0,
            memory_per_task_gb=memory_gb,
            max_pending_tasks=num_workers * 2,
            max_retries=3,
            task_timeout_seconds=7200.0,
        )
    
    @classmethod
    def for_testing(cls) -> "RayConfig":
        """Config for testing (minimal resources)."""
        return cls(
            ray_address="auto",
            num_cpus_per_task=0.5,
            memory_per_task_gb=0.5,
            max_pending_tasks=2,
            max_retries=1,
            task_timeout_seconds=60.0,
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "ray_address": self.ray_address,
            "num_cpus_per_task": self.num_cpus_per_task,
            "memory_per_task_gb": self.memory_per_task_gb,
            "max_pending_tasks": self.max_pending_tasks,
            "max_retries": self.max_retries,
            "runtime_env": self.runtime_env,
            "object_store_memory_gb": self.object_store_memory_gb,
            "task_timeout_seconds": self.task_timeout_seconds,
        }


# ═══════════════════════════════════════════════════════════════
# FileTracking Configuration
# ═══════════════════════════════════════════════════════════════


@dataclass
class FileTrackingConfig:
    """
    FileTracker integration configuration.
    
    Configures the connection to FileTracker system for file version control
    integration with WTB checkpoints. Supports different deployment modes.
    
    Attributes:
        enabled: Enable file tracking integration
        postgres_url: PostgreSQL connection string for FileTracker DB
        storage_path: Path for blob storage (content-addressed files)
        wtb_db_url: WTB database URL for checkpoint_files table
        auto_track_patterns: Glob patterns for auto-tracking (e.g., ["*.csv", "*.pkl"])
        excluded_patterns: Patterns to exclude from tracking (e.g., ["*.tmp", "*.log"])
        max_file_size_mb: Maximum file size to track (in MB)
        
    Design Decisions:
    - Separate from WTBConfig for Ray serialization (actors receive this config)
    - Supports SQLite (dev) and PostgreSQL (prod) backends
    - Patterns support for automatic file discovery
    """
    enabled: bool = False
    postgres_url: Optional[str] = None
    storage_path: str = "./file_storage"
    wtb_db_url: Optional[str] = None
    auto_track_patterns: List[str] = field(default_factory=lambda: ["*.csv", "*.pkl", "*.json", "*.parquet"])
    excluded_patterns: List[str] = field(default_factory=lambda: ["*.tmp", "*.log", "*.pyc", "__pycache__/*"])
    max_file_size_mb: float = 100.0
    
    @classmethod
    def for_testing(cls) -> "FileTrackingConfig":
        """
        In-memory mock configuration for testing.
        
        Returns:
            FileTrackingConfig with tracking disabled
        """
        return cls(enabled=False)
    
    @classmethod
    def for_development(
        cls,
        storage_path: str = "./data/file_storage",
        wtb_db_url: str = "sqlite:///data/wtb.db",
    ) -> "FileTrackingConfig":
        """
        SQLite-based configuration for development.
        
        Args:
            storage_path: Local path for file blobs
            wtb_db_url: WTB SQLite database URL
            
        Returns:
            FileTrackingConfig for development
        """
        return cls(
            enabled=True,
            postgres_url=None,  # Use SQLite via WTB DB
            storage_path=storage_path,
            wtb_db_url=wtb_db_url,
        )
    
    @classmethod
    def for_production(
        cls,
        postgres_url: str,
        storage_path: str,
        wtb_db_url: str,
    ) -> "FileTrackingConfig":
        """
        PostgreSQL configuration for production.
        
        Args:
            postgres_url: PostgreSQL connection string
            storage_path: Network/shared storage path for file blobs
            wtb_db_url: WTB PostgreSQL database URL
            
        Returns:
            FileTrackingConfig for production
        """
        return cls(
            enabled=True,
            postgres_url=postgres_url,
            storage_path=storage_path,
            wtb_db_url=wtb_db_url,
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize to dictionary.
        
        Used for Ray serialization when passing config to actors.
        
        Returns:
            Dictionary representation
        """
        return {
            "enabled": self.enabled,
            "postgres_url": self.postgres_url,
            "storage_path": self.storage_path,
            "wtb_db_url": self.wtb_db_url,
            "auto_track_patterns": self.auto_track_patterns,
            "excluded_patterns": self.excluded_patterns,
            "max_file_size_mb": self.max_file_size_mb,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FileTrackingConfig":
        """
        Deserialize from dictionary.
        
        Used by Ray actors to reconstruct config.
        
        Args:
            data: Dictionary with config values
            
        Returns:
            FileTrackingConfig instance
        """
        return cls(
            enabled=data.get("enabled", False),
            postgres_url=data.get("postgres_url"),
            storage_path=data.get("storage_path", "./file_storage"),
            wtb_db_url=data.get("wtb_db_url"),
            auto_track_patterns=data.get("auto_track_patterns", ["*.csv", "*.pkl", "*.json"]),
            excluded_patterns=data.get("excluded_patterns", ["*.tmp", "*.log"]),
            max_file_size_mb=data.get("max_file_size_mb", 100.0),
        )
    
    def ensure_storage_path(self) -> Path:
        """
        Ensure storage path exists.
        
        Returns:
            Path to storage directory
        """
        path = Path(self.storage_path)
        path.mkdir(parents=True, exist_ok=True)
        return path


# ═══════════════════════════════════════════════════════════════
# WTB Configuration
# ═══════════════════════════════════════════════════════════════


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
        ray_enabled: Enable Ray for batch testing
        ray_config: Ray cluster configuration
        environment_provider: Environment provider type ("ray", "grpc", "inprocess")
        
    Rollback Cleanup (v1.9):
        rollback_cleanup_enabled: Enable auto-cleanup of files created after checkpoint
        rollback_cleanup_dry_run: Log cleanup actions without deleting
        rollback_cleanup_backup: Backup files before deletion
        rollback_cleanup_max_files: Safety limit on files to delete per rollback
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
    
    # Ray batch testing
    ray_enabled: bool = False
    ray_config: Optional[RayConfig] = None
    
    # File tracking integration
    file_tracking_config: Optional[FileTrackingConfig] = None
    
    # Environment provider
    environment_provider: str = "inprocess"  # "ray", "grpc", "inprocess"
    grpc_env_manager_url: Optional[str] = None
    
    # LangGraph event configuration
    langgraph_event_config: Optional[LangGraphEventConfig] = None
    
    # Logging
    log_sql: bool = False
    log_level: str = "INFO"
    
    # SQLite WAL mode (for concurrent access)
    sqlite_wal_mode: bool = False
    
    # v1.9: Rollback cleanup options (opt-in feature)
    # When enabled, files created after a checkpoint will be cleaned up on rollback
    rollback_cleanup_enabled: bool = False     # Default OFF - opt-in feature
    rollback_cleanup_dry_run: bool = False     # Log actions only, no actual deletion
    rollback_cleanup_backup: bool = True       # Backup files before deletion
    rollback_cleanup_max_files: int = 100      # Safety limit to prevent runaway deletion
    
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
            WTB_ROLLBACK_CLEANUP_ENABLED: Enable file cleanup on rollback (default: "false")
            WTB_ROLLBACK_CLEANUP_DRY_RUN: Dry run mode (default: "false")
            WTB_ROLLBACK_CLEANUP_BACKUP: Backup before delete (default: "true")
            WTB_ROLLBACK_CLEANUP_MAX_FILES: Max files to delete (default: "100")
        
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
            # v1.9: Rollback cleanup options
            rollback_cleanup_enabled=os.getenv("WTB_ROLLBACK_CLEANUP_ENABLED", "false").lower() == "true",
            rollback_cleanup_dry_run=os.getenv("WTB_ROLLBACK_CLEANUP_DRY_RUN", "false").lower() == "true",
            rollback_cleanup_backup=os.getenv("WTB_ROLLBACK_CLEANUP_BACKUP", "true").lower() == "true",
            rollback_cleanup_max_files=int(os.getenv("WTB_ROLLBACK_CLEANUP_MAX_FILES", "100")),
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
            langgraph_event_config=LangGraphEventConfig.for_testing(),
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
            langgraph_event_config=LangGraphEventConfig.for_development(),
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
            langgraph_event_config=LangGraphEventConfig.for_production(),
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
    
    @classmethod
    def for_ray_production(
        cls,
        db_url: str,
        ray_address: str,
        agentgit_db_path: str = "data/agentgit.db",
        data_dir: str = "data",
        num_ray_workers: int = 10,
        filetracker_postgres_url: Optional[str] = None,
        file_storage_path: str = "/data/file_storage",
    ) -> "WTBConfig":
        """
        Create config for production with Ray batch testing and FileTracker.
        
        Args:
            db_url: PostgreSQL database URL for WTB
            ray_address: Ray cluster address
            agentgit_db_path: Path to AgentGit database
            data_dir: Directory for local files
            num_ray_workers: Number of Ray workers
            filetracker_postgres_url: PostgreSQL URL for FileTracker (optional)
            file_storage_path: Path for file blob storage
            
        Returns:
            WTBConfig with PostgreSQL, Ray, and FileTracker enabled
        """
        file_tracking = None
        if filetracker_postgres_url:
            file_tracking = FileTrackingConfig.for_production(
                postgres_url=filetracker_postgres_url,
                storage_path=file_storage_path,
                wtb_db_url=db_url,
            )
        
        return cls(
            wtb_storage_mode="sqlalchemy",
            wtb_db_url=db_url,
            agentgit_db_path=agentgit_db_path,
            state_adapter_mode="agentgit",
            data_dir=data_dir,
            filetracker_enabled=filetracker_postgres_url is not None,
            ide_sync_enabled=True,
            ray_enabled=True,
            ray_config=RayConfig.for_production(ray_address, num_ray_workers),
            file_tracking_config=file_tracking,
            environment_provider="ray",
            log_sql=False,
            log_level="WARNING",
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
            "ray_enabled": self.ray_enabled,
            "ray_config": self.ray_config.to_dict() if self.ray_config else None,
            "file_tracking_config": self.file_tracking_config.to_dict() if self.file_tracking_config else None,
            "langgraph_event_config": self.langgraph_event_config.to_dict() if self.langgraph_event_config else None,
            "log_sql": self.log_sql,
            "log_level": self.log_level,
            # v1.9: Rollback cleanup options
            "rollback_cleanup_enabled": self.rollback_cleanup_enabled,
            "rollback_cleanup_dry_run": self.rollback_cleanup_dry_run,
            "rollback_cleanup_backup": self.rollback_cleanup_backup,
            "rollback_cleanup_max_files": self.rollback_cleanup_max_files,
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

