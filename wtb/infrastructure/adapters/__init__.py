"""
State adapters - Anti-corruption layer implementations.

Provides different implementations of IStateAdapter for various backends:
- InMemoryStateAdapter: For testing and development (no persistence)
- AgentGitStateAdapter: For production (integrates with AgentGit checkpoints)
"""

from .inmemory_state_adapter import InMemoryStateAdapter

# Conditionally import AgentGitStateAdapter (requires agentgit package)
try:
    from .agentgit_state_adapter import AgentGitStateAdapter
    _HAS_AGENTGIT = True
except ImportError:
    AgentGitStateAdapter = None  # type: ignore
    _HAS_AGENTGIT = False

__all__ = [
    "InMemoryStateAdapter",
    "AgentGitStateAdapter",
]

