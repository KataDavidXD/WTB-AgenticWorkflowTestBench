"""
Infrastructure Layer - Data access, external system adapters, and persistence.

This layer implements the interfaces defined in the domain layer,
handling all technical concerns like database access and external integrations.
"""

from importlib import import_module

from .adapters.inmemory_state_adapter import InMemoryStateAdapter
from .adapters.sqlite_state_adapter import SqliteStateAdapter
from .database.unit_of_work import SQLAlchemyUnitOfWork

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

from .events.wtb_event_bus import (
    WTBEventBus,
    get_wtb_event_bus,
    reset_wtb_event_bus,
    set_wtb_event_bus,
)

from .stores.inmemory_checkpoint_store import (
    InMemoryCheckpointStore,
    InMemoryCheckpointStoreFactory,
)

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


_LANGGRAPH_STORE_EXPORTS = {
    "LangGraphCheckpointConfig",
    "LangGraphCheckpointStore",
    "LangGraphCheckpointStoreFactory",
}


def __getattr__(name: str):
    """Load optional LangGraph stores only for explicit public access."""
    if name not in _LANGGRAPH_STORE_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        module = import_module(".stores.langgraph_checkpoint_store", __name__)
    except ModuleNotFoundError as error:
        if error.name != "langgraph":
            raise
        for export in _LANGGRAPH_STORE_EXPORTS:
            globals()[export] = None
        return None
    for export in _LANGGRAPH_STORE_EXPORTS:
        globals()[export] = getattr(module, export)
    return globals()[name]

__all__ = [
    # Database
    "SQLAlchemyUnitOfWork",
    # Adapters (Legacy)
    "InMemoryStateAdapter",
    "SqliteStateAdapter",
    # Checkpoint Stores (DDD - 2026-01-15)
    "InMemoryCheckpointStore",
    "InMemoryCheckpointStoreFactory",
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
