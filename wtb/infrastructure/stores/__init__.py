"""
Infrastructure Stores - Checkpoint persistence implementations.

Contains implementations of ICheckpointStore for different backends:
- InMemoryCheckpointStore: For unit testing
- LangGraphCheckpointStore: Production-ready using LangGraph checkpointers
"""

from importlib import import_module

from .inmemory_checkpoint_store import InMemoryCheckpointStore


_LANGGRAPH_EXPORTS = {
    "LangGraphCheckpointConfig",
    "LangGraphCheckpointStore",
}


def __getattr__(name: str):
    """Load the optional LangGraph store only when explicitly requested."""
    if name not in _LANGGRAPH_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        module = import_module(".langgraph_checkpoint_store", __name__)
    except ModuleNotFoundError as error:
        if error.name != "langgraph":
            raise
        for export in _LANGGRAPH_EXPORTS:
            globals()[export] = None
        return None
    for export in _LANGGRAPH_EXPORTS:
        globals()[export] = getattr(module, export)
    return globals()[name]

__all__ = [
    "InMemoryCheckpointStore",
    "LangGraphCheckpointStore",
    "LangGraphCheckpointConfig",
]
