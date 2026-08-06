"""
Deprecated Interfaces - Kept for backward compatibility only.

DO NOT USE THESE IN NEW CODE.

These interfaces are deprecated and will be removed in v2.0.
Use the recommended alternatives instead.

Migration Guide:
================
- IStateAdapter → ICheckpointStore (LangGraph-native)
- CheckpointTrigger → Still valid, moved to state_adapter.py
- CheckpointInfo → Still valid, moved to state_adapter.py  
- NodeBoundaryInfo → Still valid, moved to state_adapter.py

Deprecation Timeline:
- v1.6 (2026-01-27): Marked deprecated, runtime warnings added
- v2.0 (planned): Will be removed
"""

import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def _emit_deprecation_warning(name: str, alternative: str) -> None:
    """Emit a deprecation warning for deprecated interfaces."""
    warnings.warn(
        f"{name} is deprecated. Use {alternative} instead. "
        f"See docs/Project_Init/new_issues.md for migration details.",
        DeprecationWarning,
        stacklevel=3
    )


# Re-export deprecated interfaces with warnings
# These are imported from state_adapter.py but wrapped to emit warnings

def get_deprecated_state_adapter():
    """
    DEPRECATED: Use ICheckpointStore instead.
    
    Returns IStateAdapter class with deprecation warning.
    """
    _emit_deprecation_warning("IStateAdapter", "ICheckpointStore")
    from .state_adapter import IStateAdapter
    return IStateAdapter


__all__ = [
    "get_deprecated_state_adapter",
]
