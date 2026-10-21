"""
State adapters - Anti-corruption layer implementations.

Default adapters:
- SqliteStateAdapter: WTB-native durable node state
- InMemoryStateAdapter: Lightweight testing state

LangGraph adapters remain explicit optional attributes for compatibility.

Optional LangGraph adapters are loaded only when their public symbol is
requested, so framework-neutral runtimes do not import LangGraph transitively.
"""

from importlib import import_module

from .inmemory_state_adapter import InMemoryStateAdapter
from .sqlite_state_adapter import SqliteStateAdapter


_SYNC_LANGGRAPH_EXPORTS = {
    "LANGGRAPH_AVAILABLE",
    "CheckpointerType",
    "LangGraphConfig",
    "LangGraphStateAdapter",
    "LangGraphStateAdapterFactory",
}
_ASYNC_LANGGRAPH_EXPORTS = {
    "ASYNC_LANGGRAPH_AVAILABLE",
    "AsyncLangGraphStateAdapter",
    "AsyncLangGraphStateAdapterFactory",
}


def __getattr__(name: str):
    """Load optional adapter modules only for explicit public access."""
    if name in _SYNC_LANGGRAPH_EXPORTS:
        module = import_module(".langgraph_state_adapter", __name__)
        value = getattr(module, name)
    elif name in _ASYNC_LANGGRAPH_EXPORTS:
        module = import_module(".async_langgraph_state_adapter", __name__)
        value = (
            module.LANGGRAPH_AVAILABLE
            if name == "ASYNC_LANGGRAPH_AVAILABLE"
            else getattr(module, name)
        )
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value

__all__ = [
    "InMemoryStateAdapter",
    "SqliteStateAdapter",
]
