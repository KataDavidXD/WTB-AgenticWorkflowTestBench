"""
Domain Object Factory Functions for Testing.

Provides factory functions that create REAL domain objects (not mocks)
for testing purposes. This ensures tests work with the actual types
used in production.

Key Principle:
- Factory functions create REAL domain types
- Tests use real types, ensuring production compatibility
- Convenience methods for common test scenarios

Usage:
    from tests.mocks import create_test_outbox_event, create_test_execution
    
    # Creates a REAL OutboxEvent
    event = create_test_outbox_event(
        event_type=OutboxEventType.CHECKPOINT_CREATE,
        aggregate_id="exec-1",
    )

Updated: 2026-01-28
"""

import uuid
import hashlib
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

# Import REAL domain types
from wtb.domain.models.outbox import OutboxEvent, OutboxEventType, OutboxStatus
from wtb.domain.models.workflow import (
    TestWorkflow, 
    Execution, 
    ExecutionStatus,
    WorkflowNode,
    WorkflowEdge,
)


def create_test_outbox_event(
    event_type: OutboxEventType = OutboxEventType.CHECKPOINT_CREATE,
    aggregate_id: Optional[str] = None,
    aggregate_type: str = "Execution",
    payload: Optional[Dict[str, Any]] = None,
    event_id: Optional[str] = None,
    status: OutboxStatus = OutboxStatus.PENDING,
) -> OutboxEvent:
    """
    Create a REAL OutboxEvent for testing.
    
    Args:
        event_type: OutboxEventType enum (not string!)
        aggregate_id: ID of the aggregate this event relates to
        aggregate_type: Type of aggregate (e.g., "Execution", "BatchTest")
        payload: Event payload data
        event_id: Optional specific event ID (UUID auto-generated if None)
        status: Initial status
        
    Returns:
        Real OutboxEvent instance
        
    Example:
        event = create_test_outbox_event(
            event_type=OutboxEventType.EXECUTION_PAUSED,
            aggregate_id="exec-123",
            payload={"reason": "user_requested"},
        )
    """
    return OutboxEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id or f"test-{uuid.uuid4().hex[:8]}",
        payload=payload or {},
        status=status,
    )


def create_test_checkpoint(
    checkpoint_id: Optional[int] = None,
    thread_id: Optional[str] = None,
    step: int = 1,
    state: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Create test checkpoint data.
    
    Note: LangGraph checkpoints have their own format.
    This returns a dict compatible with LangGraph checkpoint structure.
    
    Args:
        checkpoint_id: Unique checkpoint ID
        thread_id: Thread/execution ID
        step: Step number
        state: State snapshot
        metadata: Additional metadata
        
    Returns:
        Dict with checkpoint data
    """
    return {
        "checkpoint_id": checkpoint_id or abs(hash(f"cp-{uuid.uuid4().hex[:8]}")) % 10000,
        "thread_id": thread_id or f"wtb-{uuid.uuid4().hex[:8]}",
        "step": step,
        "state": state or {"messages": [], "count": 0},
        "metadata": metadata or {},
        "created_at": datetime.now().isoformat(),
    }


def create_test_memento(
    file_path: Optional[str] = None,
    file_hash: Optional[str] = None,
    file_size: int = 1024,
) -> Dict[str, Any]:
    """
    Create test file memento data.
    
    Args:
        file_path: Path to the file
        file_hash: SHA256 hash of file content
        file_size: Size in bytes
        
    Returns:
        Dict with memento data
    """
    return {
        "file_path": file_path or f"test_file_{uuid.uuid4().hex[:8]}.txt",
        "file_hash": file_hash or hashlib.sha256(f"content-{uuid.uuid4()}".encode()).hexdigest(),
        "file_size": file_size,
    }


def create_test_commit(
    commit_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    file_count: int = 1,
    message: str = "test commit",
) -> Dict[str, Any]:
    """
    Create test file commit data.
    
    Args:
        commit_id: Unique commit ID
        execution_id: Associated execution ID
        file_count: Number of files in the commit
        message: Commit message
        
    Returns:
        Dict with commit data including mementos
    """
    mementos = [create_test_memento() for _ in range(file_count)]
    
    return {
        "commit_id": commit_id or f"commit-{uuid.uuid4().hex[:8]}",
        "execution_id": execution_id,
        "mementos": mementos,
        "message": message,
        "created_at": datetime.now().isoformat(),
        "file_count": file_count,
    }


def create_test_execution(
    execution_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
    status: ExecutionStatus = ExecutionStatus.PENDING,
    thread_id: Optional[str] = None,
    initial_state: Optional[Dict[str, Any]] = None,
) -> Execution:
    """
    Create a REAL Execution for testing.
    
    Args:
        execution_id: Unique execution ID
        workflow_id: Associated workflow ID
        status: Initial execution status
        thread_id: LangGraph thread ID
        initial_state: Initial execution state
        
    Returns:
        Real Execution instance
    """
    exec_id = execution_id or f"exec-{uuid.uuid4().hex[:8]}"
    return Execution(
        id=exec_id,
        workflow_id=workflow_id or f"wf-{uuid.uuid4().hex[:8]}",
        status=status,
        thread_id=thread_id or f"wtb-{exec_id}",
        initial_state=initial_state or {},
    )


def create_test_workflow(
    workflow_id: Optional[str] = None,
    name: str = "test_workflow",
    node_count: int = 3,
    description: str = "Test workflow for unit tests",
) -> TestWorkflow:
    """
    Create a REAL TestWorkflow for testing.
    
    Creates a simple linear workflow with the specified number of nodes.
    
    Args:
        workflow_id: Unique workflow ID
        name: Workflow name
        node_count: Number of nodes to create
        description: Workflow description
        
    Returns:
        Real TestWorkflow instance with nodes and edges
    """
    workflow = TestWorkflow(
        id=workflow_id or f"wf-{uuid.uuid4().hex[:8]}",
        name=name,
        description=description,
    )
    
    # Add start node
    start_node = WorkflowNode(
        id="start",
        name="Start",
        type="start",
    )
    workflow.add_node(start_node)
    
    # Add intermediate nodes
    prev_node_id = "start"
    for i in range(node_count - 2):  # -2 for start and end
        node = WorkflowNode(
            id=f"node_{i}",
            name=f"Node {i}",
            type="action",
        )
        workflow.add_node(node)
        workflow.add_edge(WorkflowEdge(source_id=prev_node_id, target_id=node.id))
        prev_node_id = node.id
    
    # Add end node
    end_node = WorkflowNode(
        id="end",
        name="End",
        type="end",
    )
    workflow.add_node(end_node)
    workflow.add_edge(WorkflowEdge(source_id=prev_node_id, target_id="end"))
    
    return workflow


# ═══════════════════════════════════════════════════════════════════════════════
# Test Data Generators
# ═══════════════════════════════════════════════════════════════════════════════


def generate_test_files(count: int = 3) -> List[Tuple[str, bytes]]:
    """
    Generate test file data.
    
    Args:
        count: Number of test files to generate
        
    Returns:
        List of (filename, content) tuples
    """
    files = []
    for i in range(count):
        name = f"test_file_{i}.txt"
        content = f"Test content for file {i}\nLine 2\nLine 3".encode()
        files.append((name, content))
    return files


def generate_test_commits(
    count: int = 3,
    files_per_commit: int = 2,
    execution_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Generate test commits with mementos.
    
    Args:
        count: Number of commits to generate
        files_per_commit: Number of files per commit
        execution_id: Associated execution ID
        
    Returns:
        List of commit dictionaries
    """
    commits = []
    for i in range(count):
        commit = create_test_commit(
            commit_id=f"commit-{uuid.uuid4().hex[:8]}",
            execution_id=execution_id,
            file_count=files_per_commit,
            message=f"Test commit {i}",
        )
        commits.append(commit)
    return commits


def generate_batch_test_variants(
    count: int = 5,
    base_config: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Generate batch test variant configurations.
    
    Args:
        count: Number of variants to generate
        base_config: Base configuration to extend
        
    Returns:
        List of variant configuration dictionaries
    """
    base = base_config or {"learning_rate": 0.01}
    variants = []
    
    for i in range(count):
        variant = {
            "variant_name": f"variant_{i}",
            "variant_config": {
                **base,
                "seed": i,
                "batch_size": 16 * (i + 1),
            },
        }
        variants.append(variant)
    
    return variants


def generate_outbox_events_for_execution(
    execution_id: str,
    event_count: int = 5,
) -> List[OutboxEvent]:
    """
    Generate a sequence of outbox events for an execution.
    
    Creates events representing a typical execution lifecycle.
    
    Args:
        execution_id: Execution ID
        event_count: Number of events to generate
        
    Returns:
        List of real OutboxEvent instances
    """
    event_types = [
        OutboxEventType.CHECKPOINT_CREATE,
        OutboxEventType.CHECKPOINT_VERIFY,
        OutboxEventType.FILE_COMMIT_LINK,
        OutboxEventType.FILE_COMMIT_VERIFY,
        OutboxEventType.CHECKPOINT_SAVED,
    ]
    
    events = []
    for i in range(min(event_count, len(event_types))):
        event = create_test_outbox_event(
            event_type=event_types[i],
            aggregate_id=execution_id,
            payload={"step": i + 1},
        )
        events.append(event)
    
    # Generate additional events if more requested
    for i in range(len(event_types), event_count):
        event = create_test_outbox_event(
            event_type=OutboxEventType.CHECKPOINT_VERIFY,
            aggregate_id=execution_id,
            payload={"step": i + 1},
        )
        events.append(event)
    
    return events
