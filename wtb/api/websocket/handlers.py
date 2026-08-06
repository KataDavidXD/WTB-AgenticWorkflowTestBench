"""
WebSocket Connection Manager and Handlers.

Manages WebSocket connections with:
- Topic-based subscriptions (execution:123, audit:*, batch:456)
- Broadcast to topic subscribers
- Heartbeat/keepalive
- Integration with WTBEventBus
"""

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import WebSocket, WebSocketDisconnect

from wtb.domain.events import (
    BranchCreatedEvent,
    CheckpointCreatedEvent,
    ExecutionCancelledEvent,
    ExecutionCompletedEvent,
    ExecutionFailedEvent,
    ExecutionPausedEvent,
    ExecutionResumedEvent,
    ExecutionStartedEvent,
    NodeCompletedEvent,
    NodeFailedEvent,
    NodeSkippedEvent,
    NodeStartedEvent,
    RollbackPerformedEvent,
    WTBEvent,
)
from wtb.infrastructure.events import WTBEventBus, get_wtb_event_bus


class ConnectionManager:
    """
    Manages WebSocket connections and subscriptions.
    
    Supports:
    - Topic-based subscriptions (execution:123, audit:*, batch:456)
    - Broadcast to topic subscribers
    - Heartbeat/keepalive
    
    Thread Safety:
    - All operations are async-safe
    - Uses asyncio locks for connection management
    """
    
    def __init__(self, event_bus: WTBEventBus | None = None):
        self._connections: dict[str, WebSocket] = {}
        self._subscriptions: dict[str, set[str]] = {}  # topic → {connection_ids}
        self._connection_topics: dict[str, set[str]] = {}  # connection_id → {topics}
        self._event_bus = event_bus
        self._lock = asyncio.Lock()
        self._event_bridge_active = False
    
    async def connect(self, websocket: WebSocket, client_id: str | None = None) -> str:
        """
        Accept WebSocket connection.
        
        Args:
            websocket: FastAPI WebSocket connection
            client_id: Optional client ID (generated if not provided)
            
        Returns:
            Client ID for this connection
        """
        await websocket.accept()
        
        client_id = client_id or str(uuid.uuid4())
        
        async with self._lock:
            self._connections[client_id] = websocket
            self._connection_topics[client_id] = set()
        
        # Send welcome message
        await self._send_to_client(client_id, {
            "type": "connected",
            "client_id": client_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        
        return client_id
    
    async def disconnect(self, client_id: str) -> None:
        """Handle WebSocket disconnection."""
        async with self._lock:
            if client_id in self._connections:
                del self._connections[client_id]
            
            # Remove from all subscriptions
            if client_id in self._connection_topics:
                for topic in self._connection_topics[client_id]:
                    if topic in self._subscriptions:
                        self._subscriptions[topic].discard(client_id)
                del self._connection_topics[client_id]
    
    async def subscribe(self, client_id: str, topics: list) -> None:
        """
        Subscribe client to topics.
        
        Topics support patterns:
        - execution:123 - Specific execution
        - execution:* - All executions
        - audit:* - All audit events
        - batch:456 - Specific batch test
        """
        async with self._lock:
            for topic in topics:
                if topic not in self._subscriptions:
                    self._subscriptions[topic] = set()
                self._subscriptions[topic].add(client_id)
                
                if client_id in self._connection_topics:
                    self._connection_topics[client_id].add(topic)
        
        await self._send_to_client(client_id, {
            "type": "subscribed",
            "topics": topics,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    
    async def unsubscribe(self, client_id: str, topics: list) -> None:
        """Unsubscribe client from topics."""
        async with self._lock:
            for topic in topics:
                if topic in self._subscriptions:
                    self._subscriptions[topic].discard(client_id)
                
                if client_id in self._connection_topics:
                    self._connection_topics[client_id].discard(topic)
        
        await self._send_to_client(client_id, {
            "type": "unsubscribed",
            "topics": topics,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    
    async def broadcast_to_topic(self, topic: str, message: dict) -> int:
        """
        Broadcast message to all subscribers of a topic.
        
        Also broadcasts to wildcard subscribers (e.g., execution:* matches execution:123).
        
        Args:
            topic: Topic to broadcast to
            message: Message to send
            
        Returns:
            Number of clients message was sent to
        """
        sent_count = 0
        clients_to_notify: set[str] = set()
        
        async with self._lock:
            # Direct subscribers
            if topic in self._subscriptions:
                clients_to_notify.update(self._subscriptions[topic])
            
            # Wildcard subscribers
            topic_prefix = topic.split(":")[0] if ":" in topic else topic
            wildcard_topic = f"{topic_prefix}:*"
            if wildcard_topic in self._subscriptions:
                clients_to_notify.update(self._subscriptions[wildcard_topic])
        
        # Send to all matched clients
        for client_id in clients_to_notify:
            if await self._send_to_client(client_id, message):
                sent_count += 1
        
        return sent_count
    
    async def _send_to_client(self, client_id: str, message: dict) -> bool:
        """Send message to a specific client."""
        if client_id not in self._connections:
            return False
        
        try:
            websocket = self._connections[client_id]
            await websocket.send_json(message)
            return True
        except Exception:
            # Connection broken, will be cleaned up
            await self.disconnect(client_id)
            return False
    
    def setup_event_bridge(self, event_bus: WTBEventBus | None = None) -> None:
        """
        Setup bridge from WTB events to WebSocket topics.
        
        Maps WTB domain events to WebSocket messages and broadcasts
        to appropriate topics.
        """
        if self._event_bridge_active:
            return
        
        bus = event_bus or self._event_bus or get_wtb_event_bus()
        
        # Event to topic mapping
        event_config = [
            (ExecutionStartedEvent, "execution"),
            (ExecutionCompletedEvent, "execution"),
            (ExecutionFailedEvent, "execution"),
            (ExecutionPausedEvent, "execution"),
            (ExecutionResumedEvent, "execution"),
            (ExecutionCancelledEvent, "execution"),
            (NodeStartedEvent, "execution"),
            (NodeCompletedEvent, "execution"),
            (NodeFailedEvent, "execution"),
            (NodeSkippedEvent, "execution"),
            (CheckpointCreatedEvent, "execution"),
            (RollbackPerformedEvent, "execution"),
            (BranchCreatedEvent, "execution"),
        ]
        
        for event_type, topic_prefix in event_config:
            def create_handler(prefix: str, evt_type):
                async def handler(event: WTBEvent):
                    execution_id = getattr(event, "execution_id", "unknown")
                    topic = f"{prefix}:{execution_id}"
                    
                    message = {
                        "type": evt_type.__name__,
                        "execution_id": execution_id,
                        "timestamp": event.timestamp.isoformat(),
                        "data": event.to_dict() if hasattr(event, "to_dict") else {},
                    }
                    
                    await self.broadcast_to_topic(topic, message)
                
                return lambda e: asyncio.create_task(handler(e))
            
            bus.subscribe(event_type, create_handler(topic_prefix, event_type))
        
        self._event_bridge_active = True
    
    @property
    def active_connections(self) -> int:
        """Get number of active connections."""
        return len(self._connections)
    
    @property
    def active_subscriptions(self) -> dict[str, int]:
        """Get subscription counts per topic."""
        return {topic: len(clients) for topic, clients in self._subscriptions.items()}


# Global connection manager
_connection_manager: ConnectionManager | None = None


def get_connection_manager() -> ConnectionManager:
    """Get the global connection manager instance."""
    global _connection_manager
    if _connection_manager is None:
        _connection_manager = ConnectionManager()
        _connection_manager.setup_event_bridge()
    return _connection_manager


def set_connection_manager(manager: ConnectionManager) -> None:
    """Set the global connection manager (for testing)."""
    global _connection_manager
    _connection_manager = manager


async def websocket_endpoint(
    websocket: WebSocket,
    connection_manager: ConnectionManager | None = None,
) -> None:
    """
    WebSocket endpoint for real-time updates.
    
    Protocol:
    - Client sends: {"action": "subscribe", "topics": ["execution:123", "audit:*"]}
    - Client sends: {"action": "unsubscribe", "topics": ["execution:123"]}
    - Client sends: {"action": "ping"}
    - Server sends: {"type": "NodeCompleted", "execution_id": "123", "data": {...}}
    - Server sends: {"type": "pong"}
    
    Topics:
    - execution:{id} - Events for specific execution
    - execution:* - All execution events
    - audit:* - All audit events
    - batch:{id} - Batch test events
    """
    manager = connection_manager or get_connection_manager()
    client_id = await manager.connect(websocket)
    
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            
            if action == "subscribe":
                await manager.subscribe(client_id, data.get("topics", []))
            elif action == "unsubscribe":
                await manager.unsubscribe(client_id, data.get("topics", []))
            elif action == "ping":
                await websocket.send_json({
                    "type": "pong",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            else:
                await websocket.send_json({
                    "type": "error",
                    "message": f"Unknown action: {action}",
                })
    
    except WebSocketDisconnect:
        await manager.disconnect(client_id)
    except Exception:
        await manager.disconnect(client_id)
        raise
