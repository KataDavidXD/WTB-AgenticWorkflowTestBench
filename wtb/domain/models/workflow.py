"""
Domain Models for Workflow Test Bench.

This module contains the core domain entities and value objects:
- WorkflowNode: Immutable node definition (Value Object)
- WorkflowEdge: Immutable edge between nodes (Value Object)
- TestWorkflow: Workflow aggregate root
- ExecutionState: Snapshot of execution state (Value Object)
- Execution: Execution aggregate root
- NodeVariant: Node variant for A/B testing (Aggregate Root)
"""

from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
from datetime import datetime
from enum import Enum
import uuid


class ExecutionStatus(Enum):
    """Execution lifecycle states."""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class WorkflowNode:
    """
    Value Object - Immutable workflow node definition.
    
    Represents a single step in the workflow graph.
    Immutable to ensure consistency across branches.
    """
    id: str
    name: str
    type: str  # 'action', 'decision', 'start', 'end'
    tool_name: Optional[str] = None
    config: Dict[str, Any] = field(default_factory=dict)
    
    def __hash__(self):
        return hash(self.id)
    
    def with_config(self, **kwargs) -> "WorkflowNode":
        """Create a new node with updated config (immutable update)."""
        new_config = {**self.config, **kwargs}
        return WorkflowNode(
            id=self.id,
            name=self.name,
            type=self.type,
            tool_name=self.tool_name,
            config=new_config
        )


@dataclass(frozen=True)
class WorkflowEdge:
    """
    Value Object - Immutable edge between nodes.
    
    Defines transitions between workflow nodes with optional conditions.
    """
    source_id: str
    target_id: str
    condition: Optional[str] = None  # Condition expression or tool name
    priority: int = 0  # Higher priority edges evaluated first


@dataclass
class TestWorkflow:
    """
    Aggregate Root - Workflow definition.
    
    Encapsulates the complete workflow graph structure.
    Acts as the aggregate root for workflow-related operations.
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""
    nodes: Dict[str, WorkflowNode] = field(default_factory=dict)
    edges: List[WorkflowEdge] = field(default_factory=list)
    entry_point: Optional[str] = None
    version: str = "1.0.0"
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def add_node(self, node: WorkflowNode) -> "TestWorkflow":
        """Add a node to the workflow."""
        self.nodes[node.id] = node
        if self.entry_point is None and node.type == "start":
            self.entry_point = node.id
        self.updated_at = datetime.now()
        return self
    
    def add_edge(self, edge: WorkflowEdge) -> "TestWorkflow":
        """Add an edge to the workflow."""
        self.edges.append(edge)
        self.updated_at = datetime.now()
        return self
    
    def get_node(self, node_id: str) -> Optional[WorkflowNode]:
        """Get a node by ID."""
        return self.nodes.get(node_id)
    
    def get_outgoing_edges(self, node_id: str) -> List[WorkflowEdge]:
        """Get edges leaving a node, sorted by priority (descending)."""
        return sorted(
            [e for e in self.edges if e.source_id == node_id],
            key=lambda e: e.priority,
            reverse=True
        )
    
    def get_incoming_edges(self, node_id: str) -> List[WorkflowEdge]:
        """Get edges entering a node."""
        return [e for e in self.edges if e.target_id == node_id]
    
    def validate(self) -> List[str]:
        """
        Validate workflow structure.
        
        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []
        
        # Check entry point
        if not self.entry_point:
            errors.append("No entry point defined")
        elif self.entry_point not in self.nodes:
            errors.append(f"Entry point '{self.entry_point}' not found in nodes")
        
        # Check edge references
        for edge in self.edges:
            if edge.source_id not in self.nodes:
                errors.append(f"Edge source '{edge.source_id}' not found")
            if edge.target_id not in self.nodes:
                errors.append(f"Edge target '{edge.target_id}' not found")
        
        # Check for orphan nodes (no edges except entry/end)
        referenced_nodes = set()
        for edge in self.edges:
            referenced_nodes.add(edge.source_id)
            referenced_nodes.add(edge.target_id)
        
        for node_id, node in self.nodes.items():
            if node_id not in referenced_nodes and node_id != self.entry_point:
                if node.type not in ("start", "end"):
                    errors.append(f"Orphan node '{node_id}' has no connections")
        
        return errors
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize workflow to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "nodes": {
                nid: {
                    "id": n.id,
                    "name": n.name,
                    "type": n.type,
                    "tool_name": n.tool_name,
                    "config": n.config
                }
                for nid, n in self.nodes.items()
            },
            "edges": [
                {
                    "source_id": e.source_id,
                    "target_id": e.target_id,
                    "condition": e.condition,
                    "priority": e.priority
                }
                for e in self.edges
            ],
            "entry_point": self.entry_point,
            "version": self.version,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "metadata": self.metadata
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TestWorkflow":
        """Deserialize workflow from dictionary."""
        workflow = cls(
            id=data.get("id", str(uuid.uuid4())),
            name=data.get("name", ""),
            description=data.get("description", ""),
            entry_point=data.get("entry_point"),
            version=data.get("version", "1.0.0"),
            metadata=data.get("metadata", {})
        )
        
        # Parse created_at/updated_at
        if data.get("created_at"):
            workflow.created_at = datetime.fromisoformat(data["created_at"])
        if data.get("updated_at"):
            workflow.updated_at = datetime.fromisoformat(data["updated_at"])
        
        # Parse nodes
        for nid, node_data in data.get("nodes", {}).items():
            node = WorkflowNode(
                id=node_data["id"],
                name=node_data["name"],
                type=node_data["type"],
                tool_name=node_data.get("tool_name"),
                config=node_data.get("config", {})
            )
            workflow.nodes[nid] = node
        
        # Parse edges
        for edge_data in data.get("edges", []):
            edge = WorkflowEdge(
                source_id=edge_data["source_id"],
                target_id=edge_data["target_id"],
                condition=edge_data.get("condition"),
                priority=edge_data.get("priority", 0)
            )
            workflow.edges.append(edge)
        
        return workflow


@dataclass
class ExecutionState:
    """
    Value Object - Snapshot of execution state.
    
    Captures the complete state of an execution at a point in time.
    Used for checkpointing and state restoration.
    """
    current_node_id: Optional[str] = None
    workflow_variables: Dict[str, Any] = field(default_factory=dict)
    execution_path: List[str] = field(default_factory=list)
    node_results: Dict[str, Any] = field(default_factory=dict)
    
    def clone(self) -> "ExecutionState":
        """Create a deep copy of the state."""
        return ExecutionState(
            current_node_id=self.current_node_id,
            workflow_variables=dict(self.workflow_variables),
            execution_path=list(self.execution_path),
            node_results=dict(self.node_results)
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize state to dictionary."""
        return {
            "current_node_id": self.current_node_id,
            "workflow_variables": self.workflow_variables,
            "execution_path": self.execution_path,
            "node_results": self.node_results
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionState":
        """Deserialize state from dictionary."""
        return cls(
            current_node_id=data.get("current_node_id"),
            workflow_variables=data.get("workflow_variables", {}),
            execution_path=data.get("execution_path", []),
            node_results=data.get("node_results", {})
        )


class InvalidStateTransition(Exception):
    """Exception raised when an invalid state transition is attempted."""
    
    def __init__(self, message: str, current_status: ExecutionStatus, attempted_action: str):
        super().__init__(message)
        self.current_status = current_status
        self.attempted_action = attempted_action


@dataclass
class Execution:
    """
    Aggregate Root - Workflow execution instance (Rich Domain Model).
    
    Encapsulates the complete lifecycle of a workflow execution including:
    - State machine transitions with validation
    - Node execution recording
    - Breakpoint detection
    - State restoration from checkpoints
    
    Design Philosophy (Rich Domain Model):
    - Business logic is encapsulated within the entity
    - Services only coordinate and persist
    - Entity enforces its own invariants
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str = ""
    status: ExecutionStatus = ExecutionStatus.PENDING
    state: ExecutionState = field(default_factory=ExecutionState)
    
    # AgentGit integration
    agentgit_session_id: Optional[int] = None
    agentgit_checkpoint_id: Optional[int] = None
    
    # Timing
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=datetime.now)
    
    # Error handling
    error_message: Optional[str] = None
    error_node_id: Optional[str] = None
    
    # Breakpoints
    breakpoints: List[str] = field(default_factory=list)
    
    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # State Machine Methods
    # ═══════════════════════════════════════════════════════════════════════════
    
    def can_run(self) -> bool:
        """Check if execution can be started/resumed."""
        return self.status in [ExecutionStatus.PENDING, ExecutionStatus.PAUSED]
    
    def can_pause(self) -> bool:
        """Check if execution can be paused."""
        return self.status == ExecutionStatus.RUNNING
    
    def can_resume(self) -> bool:
        """Check if execution can be resumed."""
        return self.status == ExecutionStatus.PAUSED
    
    def can_rollback(self) -> bool:
        """Check if execution supports rollback."""
        return self.status in [
            ExecutionStatus.PAUSED,
            ExecutionStatus.FAILED,
            ExecutionStatus.COMPLETED
        ]
    
    def start(self) -> None:
        """
        Transition to RUNNING state.
        
        Raises:
            InvalidStateTransition: If not in a runnable state
        """
        if not self.can_run():
            raise InvalidStateTransition(
                f"Cannot start execution in status {self.status.value}",
                current_status=self.status,
                attempted_action="start"
            )
        self.status = ExecutionStatus.RUNNING
        if self.started_at is None:
            self.started_at = datetime.now()
    
    def pause(self) -> None:
        """
        Transition to PAUSED state.
        
        Raises:
            InvalidStateTransition: If not running
        """
        if not self.can_pause():
            raise InvalidStateTransition(
                f"Cannot pause execution in status {self.status.value}",
                current_status=self.status,
                attempted_action="pause"
            )
        self.status = ExecutionStatus.PAUSED
    
    def resume(self) -> None:
        """
        Transition from PAUSED to RUNNING.
        
        Raises:
            InvalidStateTransition: If not paused
        """
        if not self.can_resume():
            raise InvalidStateTransition(
                f"Cannot resume execution in status {self.status.value}",
                current_status=self.status,
                attempted_action="resume"
            )
        self.status = ExecutionStatus.RUNNING
    
    def complete(self) -> None:
        """Transition to COMPLETED state."""
        self.status = ExecutionStatus.COMPLETED
        self.completed_at = datetime.now()
    
    def fail(self, error_message: str, node_id: Optional[str] = None) -> None:
        """Transition to FAILED state with error details."""
        self.status = ExecutionStatus.FAILED
        self.error_message = error_message
        self.error_node_id = node_id
        self.completed_at = datetime.now()
    
    def cancel(self) -> None:
        """Transition to CANCELLED state."""
        self.status = ExecutionStatus.CANCELLED
        self.completed_at = datetime.now()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Business Logic Methods (Rich Domain Model)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def is_at_breakpoint(self) -> bool:
        """
        Check if current node is a breakpoint.
        
        Returns:
            True if execution should pause at current node
        """
        return self.state.current_node_id in self.breakpoints
    
    def has_more_nodes(self) -> bool:
        """
        Check if there are more nodes to execute.
        
        Returns:
            True if current_node_id is set (not None)
        """
        return self.state.current_node_id is not None
    
    def record_node_result(
        self,
        node_id: str,
        result: Any,
        success: bool = True
    ) -> None:
        """
        Record the result of a node execution.
        
        Business Rules:
        - Adds node to execution path
        - Stores node result
        - Merges dict results into workflow_variables
        
        Args:
            node_id: ID of the executed node
            result: Node execution result
            success: Whether node succeeded (use fail() for failures)
            
        Raises:
            ValueError: If success=False (use fail() instead)
        """
        if not success:
            raise ValueError("Use fail() method for failed node execution")
        
        # Add to execution path
        self.state.execution_path.append(node_id)
        
        # Store result
        self.state.node_results[node_id] = result
        
        # Merge dict results into workflow variables
        if isinstance(result, dict):
            self.state.workflow_variables.update(result)
    
    def advance_to_node(self, next_node_id: Optional[str]) -> None:
        """
        Advance execution to the next node.
        
        Business Rules:
        - Updates current_node_id
        - If next_node_id is None, workflow is complete
        
        Args:
            next_node_id: ID of next node, or None if workflow complete
        """
        self.state.current_node_id = next_node_id
        
        # Auto-complete if no more nodes and still running
        if next_node_id is None and self.status == ExecutionStatus.RUNNING:
            self.complete()
    
    def apply_state_modification(self, modifications: Dict[str, Any]) -> None:
        """
        Apply modifications to workflow variables.
        
        Business Rules:
        - Only allowed when paused (for user intervention)
        - Merges modifications into workflow_variables
        
        Args:
            modifications: Key-value pairs to merge
            
        Raises:
            InvalidStateTransition: If not in PAUSED state
        """
        if self.status != ExecutionStatus.PAUSED:
            raise InvalidStateTransition(
                "Can only modify state when paused",
                current_status=self.status,
                attempted_action="modify_state"
            )
        
        self.state.workflow_variables.update(modifications)
    
    def restore_from_checkpoint(self, checkpoint_state: ExecutionState) -> None:
        """
        Restore execution state from a checkpoint.
        
        Business Rules:
        - Only allowed from rollback-capable states
        - Restores to PAUSED status for user review
        - Clears error information
        
        Args:
            checkpoint_state: State to restore from
            
        Raises:
            InvalidStateTransition: If rollback not allowed
        """
        if not self.can_rollback():
            raise InvalidStateTransition(
                f"Cannot rollback execution in status {self.status.value}",
                current_status=self.status,
                attempted_action="rollback"
            )
        
        # Restore state
        self.state = checkpoint_state.clone()
        
        # Enter paused state for review
        self.status = ExecutionStatus.PAUSED
        
        # Clear any error state
        self.error_message = None
        self.error_node_id = None
    
    def add_breakpoint(self, node_id: str) -> None:
        """Add a breakpoint at the specified node."""
        if node_id not in self.breakpoints:
            self.breakpoints.append(node_id)
    
    def remove_breakpoint(self, node_id: str) -> None:
        """Remove a breakpoint from the specified node."""
        if node_id in self.breakpoints:
            self.breakpoints.remove(node_id)
    
    def clear_breakpoints(self) -> None:
        """Clear all breakpoints."""
        self.breakpoints.clear()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Query Methods
    # ═══════════════════════════════════════════════════════════════════════════
    
    def is_terminal(self) -> bool:
        """Check if execution is in a terminal state."""
        return self.status in [
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED
        ]
    
    def get_duration_seconds(self) -> Optional[float]:
        """Get execution duration in seconds."""
        if not self.started_at:
            return None
        end_time = self.completed_at or datetime.now()
        return (end_time - self.started_at).total_seconds()
    
    def get_progress(self) -> Dict[str, Any]:
        """
        Get execution progress summary.
        
        Returns:
            Dict with progress metrics
        """
        return {
            "status": self.status.value,
            "current_node": self.state.current_node_id,
            "nodes_completed": len(self.state.execution_path),
            "at_breakpoint": self.is_at_breakpoint(),
            "duration_seconds": self.get_duration_seconds(),
            "has_error": self.error_message is not None,
        }
    
    def get_node_result(self, node_id: str) -> Optional[Any]:
        """Get the result of a specific node execution."""
        return self.state.node_results.get(node_id)
    
    def get_variable(self, key: str, default: Any = None) -> Any:
        """Get a workflow variable value."""
        return self.state.workflow_variables.get(key, default)
    
    def set_variable(self, key: str, value: Any) -> None:
        """Set a workflow variable value."""
        self.state.workflow_variables[key] = value
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize execution to dictionary."""
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "status": self.status.value,
            "state": self.state.to_dict(),
            "agentgit_session_id": self.agentgit_session_id,
            "agentgit_checkpoint_id": self.agentgit_checkpoint_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "error_message": self.error_message,
            "error_node_id": self.error_node_id,
            "breakpoints": self.breakpoints,
            "metadata": self.metadata
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Execution":
        """Deserialize execution from dictionary."""
        execution = cls(
            id=data.get("id", str(uuid.uuid4())),
            workflow_id=data.get("workflow_id", ""),
            status=ExecutionStatus(data.get("status", "pending")),
            state=ExecutionState.from_dict(data.get("state", {})),
            agentgit_session_id=data.get("agentgit_session_id"),
            agentgit_checkpoint_id=data.get("agentgit_checkpoint_id"),
            error_message=data.get("error_message"),
            error_node_id=data.get("error_node_id"),
            breakpoints=data.get("breakpoints", []),
            metadata=data.get("metadata", {}),
        )
        
        if data.get("started_at"):
            execution.started_at = datetime.fromisoformat(data["started_at"])
        if data.get("completed_at"):
            execution.completed_at = datetime.fromisoformat(data["completed_at"])
        if data.get("created_at"):
            execution.created_at = datetime.fromisoformat(data["created_at"])
        
        return execution


@dataclass
class NodeVariant:
    """
    Aggregate Root - Node variant for A/B testing.
    
    Represents an alternative implementation of a workflow node
    that can be swapped in for testing purposes.
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str = ""
    original_node_id: str = ""
    variant_name: str = ""
    variant_node: WorkflowNode = field(
        default_factory=lambda: WorkflowNode(id="", name="", type="action")
    )
    description: str = ""
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize variant to dictionary."""
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "original_node_id": self.original_node_id,
            "variant_name": self.variant_name,
            "variant_node": {
                "id": self.variant_node.id,
                "name": self.variant_node.name,
                "type": self.variant_node.type,
                "tool_name": self.variant_node.tool_name,
                "config": self.variant_node.config
            },
            "description": self.description,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "metadata": self.metadata
        }


