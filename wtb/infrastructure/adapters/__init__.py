"""
State adapters - Anti-corruption layer implementations.

Refactored (v1.6):
- LangGraphStateAdapter: PRIMARY for all workflows (string IDs native)
- InMemoryStateAdapter: For testing (string IDs)
- AgentGitStateAdapter: REMOVED (use LangGraph instead)

Added (v2.0 - Async Architecture):
- AsyncLangGraphStateAdapter: Async-first implementation of IAsyncStateAdapter
- AsyncLangGraphStateAdapterFactory: Factory for async adapters

All adapters now use string IDs throughout (UUIDs).
"""

from .inmemory_state_adapter import InMemoryStateAdapter

# Conditionally import LangGraphStateAdapter (requires langgraph package)
try:
    from .langgraph_state_adapter import (
        LANGGRAPH_AVAILABLE,
        CheckpointerType,
        LangGraphConfig,
        LangGraphStateAdapter,
        LangGraphStateAdapterFactory,
    )
    _HAS_LANGGRAPH = LANGGRAPH_AVAILABLE
except ImportError:
    LangGraphStateAdapter = None  # type: ignore
    LangGraphConfig = None  # type: ignore
    LangGraphStateAdapterFactory = None  # type: ignore
    CheckpointerType = None  # type: ignore
    _HAS_LANGGRAPH = False

# Async LangGraph adapter (v2.0)
try:
    from .async_langgraph_state_adapter import (
        AsyncLangGraphStateAdapter,
        AsyncLangGraphStateAdapterFactory,
    )
    ASYNC_LANGGRAPH_AVAILABLE = _HAS_LANGGRAPH
except ImportError:
    AsyncLangGraphStateAdapter = None  # type: ignore
    AsyncLangGraphStateAdapterFactory = None  # type: ignore
    ASYNC_LANGGRAPH_AVAILABLE = False

__all__ = [
    # Sync adapters
    "InMemoryStateAdapter",
    "LangGraphStateAdapter",
    "LangGraphConfig",
    "LangGraphStateAdapterFactory",
    "CheckpointerType",
    "LANGGRAPH_AVAILABLE",
    # Async adapters (v2.0)
    "AsyncLangGraphStateAdapter",
    "AsyncLangGraphStateAdapterFactory",
    "ASYNC_LANGGRAPH_AVAILABLE",
]
