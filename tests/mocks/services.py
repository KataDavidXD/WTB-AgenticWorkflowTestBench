"""
Mock Service Implementations for Testing.

Provides mock implementations of external services:
- MockActorPool: Ray actor pool simulation
- MockVenvManager: Virtual environment manager
- MockRayEventBridge: Ray-Outbox event bridge

These mocks enable testing of integrations without requiring
actual Ray clusters or virtual environments.

Updated: 2026-01-28
"""

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any

from wtb.domain.models.outbox import OutboxEvent, OutboxEventType
from wtb.domain.interfaces.repositories import IOutboxRepository


@dataclass
class MockActor:
    """
    Mock Ray actor.
    
    Simulates a Ray actor for testing distributed execution.
    """
    actor_id: str
    execution_id: str
    workspace_id: str
    state: str = "active"  # active, paused, killed
    created_at: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MockVenv:
    """
    Mock virtual environment.
    
    Simulates a Python virtual environment for testing.
    """
    venv_id: str
    venv_path: str
    packages: List[str] = field(default_factory=list)
    status: str = "created"  # created, ready, error
    python_version: str = "3.12"
    created_at: datetime = field(default_factory=datetime.now)


class MockActorPool:
    """
    Mock Ray actor pool.
    
    Simulates a pool of Ray actors for batch test execution.
    Thread-safe for concurrent test execution.
    
    Example:
        pool = MockActorPool(pool_size=4)
        actor = pool.create_actor(
            actor_id="actor-1",
            execution_id="exec-1",
            workspace_id="ws-1",
        )
        pool.pause_actor("actor-1")
        pool.resume_actor("actor-1")
    """
    
    def __init__(self, pool_size: int = 4):
        self._actors: Dict[str, MockActor] = {}
        self._pool_size = pool_size
        self._lock = threading.Lock()
    
    def create_actor(
        self, 
        actor_id: str, 
        execution_id: str, 
        workspace_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MockActor:
        """Create a new actor in the pool."""
        with self._lock:
            actor = MockActor(
                actor_id=actor_id,
                execution_id=execution_id,
                workspace_id=workspace_id,
                metadata=metadata or {},
            )
            self._actors[actor_id] = actor
            return actor
    
    def get_actor(self, actor_id: str) -> Optional[MockActor]:
        """Get an actor by ID."""
        return self._actors.get(actor_id)
    
    def pause_actor(self, actor_id: str) -> bool:
        """Pause an actor."""
        actor = self._actors.get(actor_id)
        if actor and actor.state == "active":
            actor.state = "paused"
            return True
        return False
    
    def resume_actor(self, actor_id: str) -> bool:
        """Resume a paused actor."""
        actor = self._actors.get(actor_id)
        if actor and actor.state == "paused":
            actor.state = "active"
            return True
        return False
    
    def kill_actor(self, actor_id: str) -> bool:
        """Kill an actor."""
        with self._lock:
            if actor_id in self._actors:
                self._actors[actor_id].state = "killed"
                return True
            return False
    
    def list_active(self) -> List[MockActor]:
        """List all active actors."""
        return [a for a in self._actors.values() if a.state == "active"]
    
    def list_all(self) -> List[MockActor]:
        """List all actors."""
        return list(self._actors.values())
    
    def clear(self) -> None:
        """Clear all actors."""
        with self._lock:
            self._actors.clear()
    
    @property
    def pool_size(self) -> int:
        """Get the pool size."""
        return self._pool_size
    
    @property
    def active_count(self) -> int:
        """Get count of active actors."""
        return len(self.list_active())


class MockVenvManager:
    """
    Mock virtual environment manager.
    
    Simulates creating and managing Python virtual environments.
    Thread-safe for concurrent test execution.
    
    Example:
        manager = MockVenvManager()
        venv = manager.create_venv(
            venv_id="venv-1",
            venv_path="/tmp/venv-1",
        )
        manager.install_packages("venv-1", ["numpy", "pandas"])
        assert manager.get_venv("venv-1").status == "ready"
    """
    
    def __init__(self):
        self._venvs: Dict[str, MockVenv] = {}
        self._lock = threading.Lock()
    
    def create_venv(
        self, 
        venv_id: str, 
        venv_path: str,
        python_version: str = "3.12",
    ) -> MockVenv:
        """Create a new virtual environment."""
        with self._lock:
            venv = MockVenv(
                venv_id=venv_id,
                venv_path=venv_path,
                python_version=python_version,
            )
            self._venvs[venv_id] = venv
            return venv
    
    def install_packages(self, venv_id: str, packages: List[str]) -> bool:
        """Install packages in a virtual environment."""
        venv = self._venvs.get(venv_id)
        if venv:
            venv.packages.extend(packages)
            venv.status = "ready"
            return True
        return False
    
    def get_venv(self, venv_id: str) -> Optional[MockVenv]:
        """Get a virtual environment by ID."""
        return self._venvs.get(venv_id)
    
    def delete_venv(self, venv_id: str) -> bool:
        """Delete a virtual environment."""
        with self._lock:
            if venv_id in self._venvs:
                del self._venvs[venv_id]
                return True
            return False
    
    def list_all(self) -> List[MockVenv]:
        """List all virtual environments."""
        return list(self._venvs.values())
    
    def clear(self) -> None:
        """Clear all virtual environments."""
        with self._lock:
            self._venvs.clear()
    
    # Compatibility aliases
    def create_environment(
        self, 
        venv_id: str, 
        venv_path: str,
        python_version: str = "3.12",
    ) -> MockVenv:
        """Alias for create_venv."""
        return self.create_venv(venv_id, venv_path, python_version)
    
    def get_status(self, venv_id: str) -> Optional[str]:
        """Get status of a virtual environment."""
        venv = self.get_venv(venv_id)
        return venv.status if venv else None
    
    def destroy(self, venv_id: str) -> bool:
        """Alias for delete_venv."""
        return self.delete_venv(venv_id)


class MockRayEventBridge:
    """
    Mock Ray-Outbox Event Bridge.
    
    Bridges Ray actor events to the Outbox pattern.
    Creates real OutboxEvent instances in the repository.
    
    This mock shows the expected interface for RayEventBridge
    that should be implemented in production.
    
    Example:
        from tests.mocks import MockOutboxRepository
        
        repo = MockOutboxRepository()
        bridge = MockRayEventBridge(repo)
        
        event = bridge.publish_actor_event(
            event_type=OutboxEventType.RAY_EVENT,
            actor_id="actor-1",
            payload={"action": "started"},
        )
        
        assert repo.get_by_id(event.event_id) is not None
    """
    
    def __init__(self, outbox_repository: IOutboxRepository):
        self._outbox = outbox_repository
        self._subscriptions: Dict[str, List[callable]] = {}
        self._lock = threading.Lock()
    
    def publish_actor_event(
        self, 
        event_type: OutboxEventType,
        actor_id: str,
        payload: Optional[Dict[str, Any]] = None,
        aggregate_type: str = "Actor",
    ) -> OutboxEvent:
        """
        Create outbox event for Ray actor operation.
        
        Args:
            event_type: Type of outbox event
            actor_id: ID of the actor
            payload: Event payload
            aggregate_type: Type of aggregate
            
        Returns:
            Created OutboxEvent
        """
        event = OutboxEvent(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=actor_id,
            payload=payload or {},
        )
        return self._outbox.add(event)
    
    def publish(
        self, 
        event_type: OutboxEventType,
        aggregate_id: str,
        payload: Optional[Dict[str, Any]] = None,
        aggregate_type: str = "Execution",
    ) -> OutboxEvent:
        """
        Generic publish method.
        
        Args:
            event_type: Type of outbox event
            aggregate_id: ID of the aggregate
            payload: Event payload
            aggregate_type: Type of aggregate
            
        Returns:
            Created OutboxEvent
        """
        event = OutboxEvent(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=payload or {},
        )
        return self._outbox.add(event)
    
    def subscribe(
        self, 
        event_type: OutboxEventType, 
        handler: callable,
    ) -> None:
        """
        Subscribe to events of a specific type.
        
        Args:
            event_type: Event type to subscribe to
            handler: Callback function
        """
        with self._lock:
            key = event_type.value
            if key not in self._subscriptions:
                self._subscriptions[key] = []
            self._subscriptions[key].append(handler)
    
    def get_events(
        self, 
        event_type: Optional[OutboxEventType] = None,
        limit: int = 100,
    ) -> List[OutboxEvent]:
        """
        Get events from the outbox.
        
        Args:
            event_type: Filter by event type (optional)
            limit: Maximum events to return
            
        Returns:
            List of OutboxEvent instances
        """
        all_events = self._outbox.list_all(limit=limit)
        if event_type:
            return [e for e in all_events if e.event_type == event_type]
        return all_events
    
    @property
    def outbox(self) -> IOutboxRepository:
        """Get the underlying outbox repository."""
        return self._outbox


# ═══════════════════════════════════════════════════════════════════════════════
# Verification Utilities
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class VerificationResult:
    """Result of a verification check."""
    success: bool
    message: str
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


def verify_outbox_consistency(
    outbox_repo: IOutboxRepository,
    expected_event_count: int,
    expected_processed: int = 0,
) -> VerificationResult:
    """
    Verify outbox consistency.
    
    Args:
        outbox_repo: Outbox repository to verify
        expected_event_count: Expected total event count
        expected_processed: Expected processed event count
        
    Returns:
        VerificationResult with success status
    """
    all_events = outbox_repo.list_all()
    processed = [e for e in all_events if e.status.value == "processed"]
    
    if len(all_events) != expected_event_count:
        return VerificationResult(
            success=False,
            message=f"Event count mismatch: expected {expected_event_count}, got {len(all_events)}",
            details={"actual_count": len(all_events)},
        )
    
    if len(processed) != expected_processed:
        return VerificationResult(
            success=False,
            message=f"Processed count mismatch: expected {expected_processed}, got {len(processed)}",
            details={"actual_processed": len(processed)},
        )
    
    return VerificationResult(
        success=True,
        message="Outbox consistency verified",
        details={"event_count": len(all_events), "processed_count": len(processed)},
    )


def verify_transaction_atomicity(
    operations: List[tuple],
) -> VerificationResult:
    """
    Verify all operations succeeded or all failed (atomicity).
    
    Args:
        operations: List of (operation_name, success_bool) tuples
        
    Returns:
        VerificationResult indicating atomicity compliance
    """
    successes = [op for op in operations if op[1]]
    failures = [op for op in operations if not op[1]]
    
    if len(successes) > 0 and len(failures) > 0:
        return VerificationResult(
            success=False,
            message="Atomicity violation: partial success",
            details={"successes": successes, "failures": failures},
        )
    
    return VerificationResult(
        success=True,
        message="Atomicity verified",
        details={"all_succeeded": len(failures) == 0},
    )
