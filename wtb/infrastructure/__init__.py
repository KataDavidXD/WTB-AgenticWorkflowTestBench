"""
Infrastructure Layer - Data access, external system adapters, and persistence.

This layer implements the interfaces defined in the domain layer,
handling all technical concerns like database access and external integrations.
"""

try:
    from .adapters.inmemory_state_adapter import InMemoryStateAdapter
    from .database.unit_of_work import SQLAlchemyUnitOfWork

    # Environment Providers
    from .environment.providers import (
        InProcessEnvironmentProvider,
        RayEnvironmentProvider,
    )
    from .events.wtb_audit_trail import (
        AuditEventListener,
        WTBAuditEntry,
        WTBAuditEventType,
        WTBAuditSeverity,
        WTBAuditTrail,
    )

    # Event Bus and Audit Trail
    from .events.wtb_event_bus import (
        WTBEventBus,
        get_wtb_event_bus,
        reset_wtb_event_bus,
        set_wtb_event_bus,
    )

    # Checkpoint Stores (DDD - 2026-01-15)
    from .stores.inmemory_checkpoint_store import (
        InMemoryCheckpointStore,
        InMemoryCheckpointStoreFactory,
    )
    from .stores.langgraph_checkpoint_store import (
        LangGraphCheckpointConfig,
        LangGraphCheckpointStore,
        LangGraphCheckpointStoreFactory,
    )
except ImportError:
    SQLAlchemyUnitOfWork = None
    InMemoryStateAdapter = None
    InMemoryCheckpointStore = None
    InMemoryCheckpointStoreFactory = None
    LangGraphCheckpointStore = None
    LangGraphCheckpointConfig = None
    LangGraphCheckpointStoreFactory = None
    WTBEventBus = None
    get_wtb_event_bus = None
    set_wtb_event_bus = None
    reset_wtb_event_bus = None
    WTBAuditEventType = None
    WTBAuditSeverity = None
    WTBAuditEntry = None
    WTBAuditTrail = None
    AuditEventListener = None
    InProcessEnvironmentProvider = None
    RayEnvironmentProvider = None
from .llm import (
    LangChainOpenAIConfig,
    LangChainOpenAIService,
    TextGenerationResult,
)
from .llm import (
    get_service as get_langchain_openai_service,
)
from .llm import (
    reset_service_cache as reset_langchain_openai_service_cache,
)

__all__ = [
    # Database
    "SQLAlchemyUnitOfWork",
    # Adapters (Legacy)
    "InMemoryStateAdapter",
    # Checkpoint Stores (DDD - 2026-01-15)
    "InMemoryCheckpointStore",
    "InMemoryCheckpointStoreFactory",
    "LangGraphCheckpointStore",
    "LangGraphCheckpointConfig",
    "LangGraphCheckpointStoreFactory",
    # Event Bus
    "WTBEventBus",
    "get_wtb_event_bus",
    "set_wtb_event_bus",
    "reset_wtb_event_bus",
    # Audit Trail
    "WTBAuditEventType",
    "WTBAuditSeverity",
    "WTBAuditEntry",
    "WTBAuditTrail",
    "AuditEventListener",
    # Environment Providers
    "InProcessEnvironmentProvider",
    "RayEnvironmentProvider",
    # LLM Helpers
    "LangChainOpenAIConfig",
    "LangChainOpenAIService",
    "TextGenerationResult",
    "get_langchain_openai_service",
    "reset_langchain_openai_service_cache",
]
