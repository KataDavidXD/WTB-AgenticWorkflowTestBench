"""
Pytest fixtures for LangGraph test suite.

Provides shared fixtures for:
- Graph definitions (simple, branching, parallel)
- Checkpointers (memory, SQLite)
- Event bus and audit components
- File tracking infrastructure
- Mock repositories

SOLID Compliance:
- Fixtures follow Dependency Inversion (provide abstractions)
- Interface Segregation (separate fixtures for each concern)
"""

import pytest
import tempfile
from typing import List, Dict, Any, Optional
from datetime import datetime
from pathlib import Path

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from wtb.infrastructure.stores.langgraph_checkpoint_store import (
    LangGraphCheckpointStore,
    LangGraphCheckpointConfig,
)
from wtb.domain.models.checkpoint import Checkpoint, CheckpointId, ExecutionHistory
from wtb.infrastructure.events import (
    WTBEventBus,
    WTBAuditTrail,
    AuditEventListener,
    LangGraphEventBridge,
    StreamModeConfig,
    InMemoryMetricsCollector,
    create_event_bridge_for_testing,
)
from wtb.domain.events import (
    ExecutionStartedEvent,
    ExecutionCompletedEvent,
    ExecutionFailedEvent,
    NodeStartedEvent,
    NodeCompletedEvent,
    NodeFailedEvent,
)
from wtb.domain.events.checkpoint_events import CheckpointCreated

# Import state types and helpers from helpers module
from tests.test_langgraph.helpers import (
    SimpleState,
    AdvancedState,
    ParallelState,
    BranchState,
    FileTrackingState,
    simple_node_a,
    simple_node_b,
    simple_node_c,
    advanced_node_a,
    advanced_node_b,
    advanced_node_c,
    advanced_node_d,
    failing_node,
    route_by_switch,
    route_by_count,
    file_processing_node,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Graph Definition Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def simple_graph_def() -> StateGraph:
    """Create simple linear graph: A -> B -> C -> END."""
    workflow = StateGraph(SimpleState)
    
    workflow.add_node("node_a", simple_node_a)
    workflow.add_node("node_b", simple_node_b)
    workflow.add_node("node_c", simple_node_c)
    
    workflow.set_entry_point("node_a")
    workflow.add_edge("node_a", "node_b")
    workflow.add_edge("node_b", "node_c")
    workflow.add_edge("node_c", END)
    
    return workflow


@pytest.fixture
def branching_graph_def() -> StateGraph:
    """Create branching graph: A -> B -> (C or D) -> END."""
    workflow = StateGraph(BranchState)
    
    workflow.add_node("node_a", lambda s: {"messages": ["A"], "count": s["count"] + 1, "path": ["node_a"]})
    workflow.add_node("node_b", lambda s: {"messages": ["B"], "count": s["count"] + 1, "path": ["node_b"]})
    workflow.add_node("node_c", lambda s: {"messages": ["C"], "count": s["count"] + 1, "path": ["node_c"]})
    workflow.add_node("node_d", lambda s: {"messages": ["D"], "count": s["count"] + 1, "path": ["node_d"]})
    
    workflow.set_entry_point("node_a")
    workflow.add_edge("node_a", "node_b")
    workflow.add_conditional_edges("node_b", route_by_switch)
    workflow.add_edge("node_c", END)
    workflow.add_edge("node_d", END)
    
    return workflow


@pytest.fixture
def advanced_graph_def() -> StateGraph:
    """Create advanced graph with conditional routing."""
    workflow = StateGraph(AdvancedState)
    
    workflow.add_node("node_a", advanced_node_a)
    workflow.add_node("node_b", advanced_node_b)
    workflow.add_node("node_c", advanced_node_c)
    workflow.add_node("node_d", advanced_node_d)
    
    workflow.set_entry_point("node_a")
    workflow.add_edge("node_a", "node_b")
    workflow.add_conditional_edges("node_b", route_by_count)
    workflow.add_edge("node_c", END)
    workflow.add_edge("node_d", END)
    
    return workflow


@pytest.fixture
def failing_graph_def() -> StateGraph:
    """Create graph with a failing node."""
    workflow = StateGraph(SimpleState)
    
    workflow.add_node("node_a", simple_node_a)
    workflow.add_node("failing_node", failing_node)
    workflow.add_node("node_b", simple_node_b)
    
    workflow.set_entry_point("node_a")
    workflow.add_edge("node_a", "failing_node")
    workflow.add_edge("failing_node", "node_b")
    workflow.add_edge("node_b", END)
    
    return workflow


@pytest.fixture
def file_tracking_graph_def() -> StateGraph:
    """Create graph for file tracking tests."""
    workflow = StateGraph(FileTrackingState)
    
    workflow.add_node("ingest", lambda s: {
        "messages": ["Ingested"], 
        "count": s["count"] + 1, 
        "files_processed": ["input.csv"]
    })
    workflow.add_node("process", file_processing_node)
    workflow.add_node("save", lambda s: {
        "messages": ["Saved"], 
        "count": s["count"] + 1,
        "commit_ids": [f"commit_{len(s.get('files_processed', []))}"]
    })
    
    workflow.set_entry_point("ingest")
    workflow.add_edge("ingest", "process")
    workflow.add_edge("process", "save")
    workflow.add_edge("save", END)
    
    return workflow


# ═══════════════════════════════════════════════════════════════════════════════
# Checkpointer Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def memory_checkpointer() -> MemorySaver:
    """Create in-memory checkpointer."""
    return MemorySaver()


@pytest.fixture
def checkpoint_store(memory_checkpointer) -> LangGraphCheckpointStore:
    """Create checkpoint store with memory backend."""
    return LangGraphCheckpointStore(checkpointer=memory_checkpointer)


@pytest.fixture
def sqlite_temp_path(tmp_path) -> Path:
    """Create temporary SQLite database path."""
    return tmp_path / "test_checkpoints.db"


# ═══════════════════════════════════════════════════════════════════════════════
# Compiled Graph Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def simple_compiled_graph(simple_graph_def, memory_checkpointer):
    """Create compiled simple graph with checkpointer."""
    return simple_graph_def.compile(checkpointer=memory_checkpointer)


@pytest.fixture
def branching_compiled_graph(branching_graph_def, memory_checkpointer):
    """Create compiled branching graph with checkpointer."""
    return branching_graph_def.compile(checkpointer=memory_checkpointer)


@pytest.fixture
def advanced_compiled_graph(advanced_graph_def, memory_checkpointer):
    """Create compiled advanced graph with checkpointer."""
    return advanced_graph_def.compile(checkpointer=memory_checkpointer)


@pytest.fixture
def file_tracking_compiled_graph(file_tracking_graph_def, memory_checkpointer):
    """Create compiled file tracking graph with checkpointer."""
    return file_tracking_graph_def.compile(checkpointer=memory_checkpointer)


# ═══════════════════════════════════════════════════════════════════════════════
# Event System Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def event_bus() -> WTBEventBus:
    """Create fresh event bus."""
    return WTBEventBus(max_history=1000)


@pytest.fixture
def audit_trail() -> WTBAuditTrail:
    """Create audit trail."""
    return WTBAuditTrail()


@pytest.fixture
def metrics_collector(event_bus) -> InMemoryMetricsCollector:
    """Create metrics collector."""
    return InMemoryMetricsCollector(event_bus)


@pytest.fixture
def stream_config() -> StreamModeConfig:
    """Create stream configuration for testing."""
    return StreamModeConfig.for_testing()


@pytest.fixture
def event_bridge(event_bus, stream_config) -> LangGraphEventBridge:
    """Create event bridge for testing."""
    return LangGraphEventBridge(
        event_bus=event_bus,
        stream_config=stream_config,
        emit_audit_events=False,
    )


@pytest.fixture
def audit_event_listener(event_bus, audit_trail) -> AuditEventListener:
    """Create audit event listener attached to event bus."""
    listener = AuditEventListener(audit_trail)
    listener.attach(event_bus)
    return listener


@pytest.fixture
def collected_events(event_bus) -> List:
    """Fixture to collect published events."""
    events = []
    
    def collect(event):
        events.append(event)
    
    event_bus.subscribe(ExecutionStartedEvent, collect)
    event_bus.subscribe(ExecutionCompletedEvent, collect)
    event_bus.subscribe(ExecutionFailedEvent, collect)
    event_bus.subscribe(NodeStartedEvent, collect)
    event_bus.subscribe(NodeCompletedEvent, collect)
    event_bus.subscribe(NodeFailedEvent, collect)
    event_bus.subscribe(CheckpointCreated, collect)
    
    return events


# ═══════════════════════════════════════════════════════════════════════════════
# Full Integration Setup Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def full_integration_setup(event_bus, audit_trail) -> Dict[str, Any]:
    """Set up full integration with all components."""
    metrics_collector = InMemoryMetricsCollector(event_bus)
    event_bridge = LangGraphEventBridge(
        event_bus=event_bus,
        stream_config=StreamModeConfig.for_development(),
        emit_audit_events=True,
    )
    audit_listener = AuditEventListener(audit_trail)
    audit_listener.attach(event_bus)
    
    return {
        "event_bus": event_bus,
        "audit_trail": audit_trail,
        "metrics_collector": metrics_collector,
        "event_bridge": event_bridge,
        "audit_listener": audit_listener,
    }


@pytest.fixture
def execution_context() -> Dict[str, Any]:
    """Create standard execution context."""
    import uuid
    execution_id = f"exec-{uuid.uuid4().hex[:8]}"
    thread_id = f"wtb-{execution_id}"
    
    return {
        "execution_id": execution_id,
        "thread_id": thread_id,
        "config": {"configurable": {"thread_id": thread_id}},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# File Processing Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def temp_files(tmp_path) -> Dict[str, Dict[str, Any]]:
    """Create temporary test files."""
    files = {}
    test_data = [
        ("data.csv", b"id,name,value\n1,Alice,100\n2,Bob,200"),
        ("model.pkl", b"PICKLE_BINARY_MODEL_DATA_PLACEHOLDER"),
        ("config.json", b'{"setting": "value", "enabled": true}'),
    ]
    
    for name, content in test_data:
        path = tmp_path / name
        path.write_bytes(content)
        files[name] = {
            "path": str(path),
            "content": content,
            "size": len(content),
        }
    
    return files


@pytest.fixture
def blob_repository():
    """Create in-memory blob repository."""
    from wtb.infrastructure.database.repositories.file_processing_repository import (
        InMemoryBlobRepository,
    )
    return InMemoryBlobRepository()


@pytest.fixture
def commit_repository():
    """Create in-memory commit repository."""
    from wtb.infrastructure.database.repositories.file_processing_repository import (
        InMemoryFileCommitRepository,
    )
    return InMemoryFileCommitRepository()


@pytest.fixture
def link_repository():
    """Create in-memory checkpoint link repository."""
    from wtb.infrastructure.database.repositories.file_processing_repository import (
        InMemoryCheckpointFileLinkRepository,
    )
    return InMemoryCheckpointFileLinkRepository()


