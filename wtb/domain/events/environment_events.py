"""
Environment Domain Events - Events for UV Virtual Environment operations.

These events enable cross-cutting concerns like audit logging, monitoring,
and WTB integration for environment management operations.

SOLID Compliance:
- Single Responsibility: Each event represents one domain action
- Open/Closed: New events can be added without modifying existing code
- Liskov Substitution: All events inherit from WTBEvent
- Interface Segregation: Events are focused and specific
- Dependency Inversion: Events depend on abstract WTBEvent base
"""

from dataclasses import dataclass, field

from wtb.domain.events.execution_events import WTBEvent


@dataclass
class EnvironmentEvent(WTBEvent):
    """Base class for environment-related events."""
    workflow_id: str = ""
    node_id: str = ""
    version_id: str | None = None
    
    @property
    def env_id(self) -> str:
        """Get the environment identifier."""
        base = f"{self.workflow_id}_{self.node_id}"
        if self.version_id:
            return f"{base}_{self.version_id}"
        return base


# ═══════════════════════════════════════════════════════════════
# Environment Lifecycle Events
# ═══════════════════════════════════════════════════════════════

@dataclass
class EnvironmentCreationStartedEvent(EnvironmentEvent):
    """
    Published when environment creation begins.
    
    ACID: Marks the start of a transactional operation.
    Used for tracking long-running operations.
    """
    python_version: str = ""
    packages: list[str] = field(default_factory=list)
    source: str = "api"  # "api", "grpc", "internal"


@dataclass
class EnvironmentCreatedEvent(EnvironmentEvent):
    """
    Published when environment is successfully created.
    
    ACID: Marks successful commit of environment creation.
    """
    env_path: str = ""
    python_version: str = ""
    packages: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    source: str = "api"


@dataclass
class EnvironmentCreationFailedEvent(EnvironmentEvent):
    """
    Published when environment creation fails.
    
    ACID: Indicates rollback has been performed.
    """
    error: str = ""
    python_version: str = ""
    packages: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    rollback_performed: bool = False
    source: str = "api"


@dataclass
class EnvironmentDeletedEvent(EnvironmentEvent):
    """Published when environment is deleted."""
    env_path: str = ""
    duration_ms: float = 0.0


# ═══════════════════════════════════════════════════════════════
# Dependency Management Events
# ═══════════════════════════════════════════════════════════════

@dataclass
class DependenciesAddedEvent(EnvironmentEvent):
    """Published when packages are added to environment."""
    packages: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    exit_code: int = 0


@dataclass
class DependenciesUpdatedEvent(EnvironmentEvent):
    """Published when packages are upgraded in environment."""
    packages: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    exit_code: int = 0


@dataclass
class DependenciesRemovedEvent(EnvironmentEvent):
    """Published when packages are removed from environment."""
    packages: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    exit_code: int = 0


@dataclass
class DependencyOperationFailedEvent(EnvironmentEvent):
    """Published when a dependency operation fails."""
    operation: str = ""  # "add", "update", "remove"
    packages: list[str] = field(default_factory=list)
    error: str = ""
    duration_ms: float = 0.0


# ═══════════════════════════════════════════════════════════════
# Environment Sync Events
# ═══════════════════════════════════════════════════════════════

@dataclass
class EnvironmentSyncedEvent(EnvironmentEvent):
    """Published when environment is synchronized with lock file."""
    duration_ms: float = 0.0
    exit_code: int = 0


@dataclass
class EnvironmentSyncFailedEvent(EnvironmentEvent):
    """Published when environment sync fails."""
    error: str = ""
    duration_ms: float = 0.0


# ═══════════════════════════════════════════════════════════════
# Cleanup Events
# ═══════════════════════════════════════════════════════════════

@dataclass
class EnvironmentCleanupStartedEvent(WTBEvent):
    """Published when cleanup process starts."""
    idle_hours: int = 0


@dataclass
class EnvironmentCleanupCompletedEvent(WTBEvent):
    """Published when cleanup process completes."""
    deleted_envs: list[str] = field(default_factory=list)
    deleted_count: int = 0
    duration_ms: float = 0.0
    idle_hours: int = 0


# ═══════════════════════════════════════════════════════════════
# Lock Events
# ═══════════════════════════════════════════════════════════════

@dataclass
class EnvironmentLockAcquiredEvent(EnvironmentEvent):
    """Published when environment lock is acquired."""
    operation: str = ""


@dataclass
class EnvironmentLockReleasedEvent(EnvironmentEvent):
    """Published when environment lock is released."""
    operation: str = ""
    held_ms: float = 0.0


@dataclass
class EnvironmentLockTimeoutEvent(EnvironmentEvent):
    """Published when environment lock acquisition times out."""
    operation: str = ""
    timeout_ms: float = 0.0
