"""
WTB Event Bus - Thread-safe event bus for WTB domain events.

v1.8 (2026-02-06): Enhanced for batch rollback/fork coordination.

Provides a thread-safe publish-subscribe mechanism for domain events,
with optional bridging of AgentGit events to WTB events.

Design Decisions:
- Thread-safe via Lock for parallel batch testing
- Bounded history to prevent memory leaks
- Standalone implementation (no AgentGit inheritance to avoid import issues)
- Optional AgentGit event bridging when AgentGit is available

v1.8 Enhancements:
- Supports RollbackPerformed and ExecutionForked events
- Integrates with BatchExecutionCoordinator outbox pattern
- Enables graph factory-based checkpoint event tracking

Usage:
    bus = WTBEventBus()
    
    # Subscribe to events
    bus.subscribe(ExecutionStartedEvent, handle_execution_started)
    
    # Publish events
    bus.publish(ExecutionStartedEvent(execution_id="exec-1"))
    
    # Get history
    recent = bus.get_history(limit=10)
"""

from typing import Type, Callable, List, Dict, Optional, Any, Union
from threading import Lock, RLock
from datetime import datetime
from collections import deque
import logging

from wtb.domain.events import WTBEvent

logger = logging.getLogger(__name__)


class WTBEventBus:
    """
    Thread-safe WTB Event Bus with bounded history.
    
    Key Features:
    - Thread-safe via RLock (reentrant for nested publishes)
    - Bounded event history (default 1000 events)
    - Type-safe subscriptions
    - Optional AgentGit event bridging
    
    Thread Safety:
    - All public methods are thread-safe
    - Uses RLock to allow handlers to publish additional events
    """
    
    def __init__(self, max_history: int = 1000):
        """
        Initialize WTB Event Bus.
        
        Args:
            max_history: Maximum events to keep in history (bounded to prevent memory leaks)
        """
        self._lock = RLock()  # Reentrant for nested publishes
        self._subscribers: Dict[Type, List[Callable[[Any], None]]] = {}
        self._event_history: deque = deque(maxlen=max_history)
        self._max_history = max_history
        
        # AgentGit bridge state
        self._bridge_enabled = False
        self._bridge_handlers: List[tuple] = []
    
    # ═══════════════════════════════════════════════════════════════
    # Core Pub/Sub Operations
    # ═══════════════════════════════════════════════════════════════
    
    def subscribe(
        self, 
        event_type: Type[WTBEvent], 
        handler: Callable[[WTBEvent], None]
    ) -> None:
        """
        Subscribe to an event type.
        
        Args:
            event_type: The event class to subscribe to
            handler: Callback function that receives the event
        """
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            if handler not in self._subscribers[event_type]:
                self._subscribers[event_type].append(handler)
    
    def unsubscribe(
        self, 
        event_type: Type[WTBEvent], 
        handler: Callable[[WTBEvent], None]
    ) -> None:
        """
        Unsubscribe from an event type.
        
        Args:
            event_type: The event class to unsubscribe from
            handler: The callback to remove
        """
        with self._lock:
            if event_type in self._subscribers:
                try:
                    self._subscribers[event_type].remove(handler)
                except ValueError:
                    pass  # Handler not in list
    
    def publish(self, event: WTBEvent) -> None:
        """
        Publish an event to all subscribers.
        
        Events are stored in history and delivered synchronously to subscribers.
        Exceptions in handlers are logged but don't prevent other handlers.
        
        Args:
            event: The event instance to publish
        """
        with self._lock:
            # Store in history
            self._event_history.append(event)
            
            # Get handlers for this event type
            event_type = type(event)
            handlers = self._subscribers.get(event_type, [])[:]  # Copy to avoid modification during iteration
        
        # Call handlers outside lock to prevent deadlock
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                logger.error(f"Error in event handler for {event_type.__name__}: {e}")
    
    def publish_all(self, events: List[WTBEvent]) -> None:
        """
        Publish multiple events atomically.
        
        Args:
            events: List of events to publish
        """
        for event in events:
            self.publish(event)
    
    # ═══════════════════════════════════════════════════════════════
    # History and Query Operations
    # ═══════════════════════════════════════════════════════════════
    
    def get_event_history(
        self, 
        event_type: Optional[Type[WTBEvent]] = None
    ) -> List[WTBEvent]:
        """
        Get event history, optionally filtered by type.
        
        Args:
            event_type: Optional filter for specific event type
            
        Returns:
            List of events (oldest first)
        """
        with self._lock:
            events = list(self._event_history)
            
        if event_type is not None:
            events = [e for e in events if isinstance(e, event_type)]
            
        return events
    
    def get_history(
        self, 
        event_type: Optional[Type[WTBEvent]] = None,
        since: Optional[datetime] = None,
        limit: int = 100
    ) -> List[WTBEvent]:
        """
        Get event history with optional filtering.
        
        Args:
            event_type: Filter by event type
            since: Filter events after this timestamp
            limit: Maximum number of events to return
            
        Returns:
            List of events (most recent first)
        """
        events = self.get_event_history(event_type)
        
        if since is not None:
            events = [e for e in events if e.timestamp >= since]
        
        # Return most recent first, limited
        return list(reversed(events[-limit:]))
    
    def clear_history(self) -> None:
        """Clear all event history."""
        with self._lock:
            self._event_history.clear()
    
    def get_subscriber_count(
        self, 
        event_type: Optional[Type[WTBEvent]] = None
    ) -> int:
        """
        Get count of subscribers.
        
        Args:
            event_type: Count for specific type, or total if None
            
        Returns:
            Number of subscribers
        """
        with self._lock:
            if event_type is not None:
                return len(self._subscribers.get(event_type, []))
            return sum(len(h) for h in self._subscribers.values())
    
    # ═══════════════════════════════════════════════════════════════
    # AgentGit Bridge (Optional)
    # ═══════════════════════════════════════════════════════════════
    
    def enable_agentgit_bridge(self) -> bool:
        """
        Enable bridging of AgentGit events to WTB events.
        
        This subscribes to AgentGit's global event bus and translates
        relevant events (CheckpointCreated, Rollback, etc.) to WTB events.
        
        Returns:
            True if bridge was enabled, False if AgentGit not available
        """
        if self._bridge_enabled:
            return True
        
        try:
            from agentgit.events import (
                get_global_event_bus,
                CheckpointCreatedEvent as AGCheckpointCreated,
                RollbackPerformedEvent as AGRollback,
                SessionCreatedEvent as AGSessionCreated,
            )
            
            ag_bus = get_global_event_bus()
            
            # Create bridge handlers
            handlers = [
                (AGCheckpointCreated, self._bridge_checkpoint_created),
                (AGRollback, self._bridge_rollback),
                (AGSessionCreated, self._bridge_session_created),
            ]
            
            for ag_event_type, bridge_fn in handlers:
                ag_bus.subscribe(ag_event_type, bridge_fn)
                self._bridge_handlers.append((ag_event_type, bridge_fn))
            
            self._bridge_enabled = True
            logger.info("AgentGit event bridge enabled")
            return True
            
        except ImportError:
            logger.warning("AgentGit not available, bridge not enabled")
            return False
    
    def disable_agentgit_bridge(self) -> None:
        """Disable AgentGit event bridging."""
        if not self._bridge_enabled:
            return
        
        try:
            from agentgit.events import get_global_event_bus
            ag_bus = get_global_event_bus()
            
            for event_type, handler in self._bridge_handlers:
                ag_bus.unsubscribe(event_type, handler)
            
            self._bridge_handlers.clear()
            self._bridge_enabled = False
            logger.info("AgentGit event bridge disabled")
            
        except ImportError:
            pass
    
    def _bridge_checkpoint_created(self, ag_event) -> None:
        """Bridge AgentGit CheckpointCreated to WTB."""
        try:
            from wtb.domain.events import CheckpointCreatedEvent
            
            wtb_event = CheckpointCreatedEvent(
                checkpoint_id=ag_event.checkpoint_id,
                session_id=ag_event.session_id,
                checkpoint_name=ag_event.checkpoint_name,
                is_auto=ag_event.is_auto,
                tool_track_position=getattr(ag_event, 'tool_count', 0),
            )
            self.publish(wtb_event)
        except Exception as e:
            logger.error(f"Bridge error in _bridge_checkpoint_created: {e}")
    
    def _bridge_rollback(self, ag_event) -> None:
        """Bridge AgentGit Rollback to WTB."""
        try:
            from wtb.domain.events import RollbackPerformedEvent
            
            wtb_event = RollbackPerformedEvent(
                to_checkpoint_id=ag_event.checkpoint_id,
                session_id=getattr(ag_event, 'original_session_id', 0),
                new_session_id=getattr(ag_event, 'new_session_id', None),
                tools_reversed=getattr(ag_event, 'tools_reversed', 0),
            )
            self.publish(wtb_event)
        except Exception as e:
            logger.error(f"Bridge error in _bridge_rollback: {e}")
    
    def _bridge_session_created(self, ag_event) -> None:
        """Bridge AgentGit SessionCreated (when is_branch) to WTB BranchCreated."""
        try:
            if getattr(ag_event, 'is_branch', False):
                from wtb.domain.events import BranchCreatedEvent
                
                wtb_event = BranchCreatedEvent(
                    new_session_id=ag_event.session_id,
                    parent_session_id=getattr(ag_event, 'parent_session_id', 0),
                    branch_reason="rollback" if getattr(ag_event, 'parent_session_id', None) else "variant_test",
                )
                self.publish(wtb_event)
        except Exception as e:
            logger.error(f"Bridge error in _bridge_session_created: {e}")


# ═══════════════════════════════════════════════════════════════
# Global Instance Management
# ═══════════════════════════════════════════════════════════════

_global_wtb_event_bus: Optional[WTBEventBus] = None


def get_wtb_event_bus() -> WTBEventBus:
    """
    Get global WTB event bus instance.
    
    Creates a new instance on first call.
    
    Returns:
        WTBEventBus singleton instance
    """
    global _global_wtb_event_bus
    if _global_wtb_event_bus is None:
        _global_wtb_event_bus = WTBEventBus()
    return _global_wtb_event_bus


def set_wtb_event_bus(bus: WTBEventBus) -> None:
    """
    Set global WTB event bus instance.
    
    Useful for testing with custom bus configurations.
    
    Args:
        bus: WTBEventBus instance to use globally
    """
    global _global_wtb_event_bus
    _global_wtb_event_bus = bus


def reset_wtb_event_bus() -> None:
    """
    Reset global event bus to None.
    
    Next call to get_wtb_event_bus() will create a new instance.
    """
    global _global_wtb_event_bus
    _global_wtb_event_bus = None

