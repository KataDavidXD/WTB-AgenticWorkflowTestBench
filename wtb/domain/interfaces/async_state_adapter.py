
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from wtb.domain.models.workflow import ExecutionState

if TYPE_CHECKING:
    from wtb.domain.models.workflow import CheckpointTrigger


class IAsyncStateAdapter(ABC):
    """
    Async State Adapter Interface.
  
    Provides async operations for:
    - Session management
    - Checkpoint operations
    - Execution control
    - Streaming support
  
    SOLID:
    - SRP: Only state management
    - OCP: New adapters via implementation
    - LSP: All implementations interchangeable
    - ISP: Focused interface (~15 methods)
    - DIP: Application depends on this abstraction
    """
  
    # ═══════════════════════════════════════════════════════════════════════════
    # Session Management (Async)
    # ═══════════════════════════════════════════════════════════════════════════
  
    @abstractmethod
    async def ainitialize_session(
        self, 
        execution_id: str,
        initial_state: ExecutionState
    ) -> str | None:
        """Initialize session asynchronously."""
        pass
  
    @abstractmethod
    async def aset_current_session(
        self, 
        session_id: str,
        execution_id: str | None = None,
    ) -> bool:
        """Set current session asynchronously."""
        pass
  
    # ═══════════════════════════════════════════════════════════════════════════
    # Checkpoint Operations (Async)
    # ═══════════════════════════════════════════════════════════════════════════
  
    @abstractmethod
    async def asave_checkpoint(
        self,
        state: ExecutionState,
        node_id: str,
        trigger: "CheckpointTrigger",
        name: str | None = None,
        metadata: dict[str, Any] | None = None
    ) -> str:
        """Save checkpoint asynchronously. Returns checkpoint_id."""
        pass
  
    @abstractmethod
    async def aload_checkpoint(self, checkpoint_id: str) -> ExecutionState:
        """Load checkpoint asynchronously."""
        pass
  
    @abstractmethod
    async def arollback(self, to_checkpoint_id: str) -> ExecutionState:
        """Rollback to checkpoint asynchronously."""
        pass
  
    # ═══════════════════════════════════════════════════════════════════════════
    # Execution (Async)
    # ═══════════════════════════════════════════════════════════════════════════
  
    @abstractmethod
    async def aexecute(self, initial_state: dict[str, Any]) -> dict[str, Any]:
        """
        Execute workflow asynchronously.
      
        This is the PRIMARY async execution method. Uses LangGraph's ainvoke().
        """
        pass
  
    @abstractmethod
    async def astream(
        self, 
        initial_state: dict[str, Any],
        stream_mode: str = "updates"
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Stream execution events asynchronously.
      
        Yields state updates as they occur. Uses LangGraph's astream().
        """
        pass
  
    # ═══════════════════════════════════════════════════════════════════════════
    # State Operations (Async)
    # ═══════════════════════════════════════════════════════════════════════════
  
    @abstractmethod
    async def aupdate_state(
        self, 
        values: dict[str, Any],
        as_node: str | None = None
    ) -> bool:
        """Update state asynchronously (human-in-the-loop)."""
        pass
  
    @abstractmethod
    async def aget_current_state(self) -> dict[str, Any]:
        """Get current state asynchronously."""
        pass
