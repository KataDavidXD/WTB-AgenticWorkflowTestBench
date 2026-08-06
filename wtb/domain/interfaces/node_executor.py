"""
Node Executor Interface.

Defines the contract for executing workflow nodes.
Allows different execution strategies (local, remote, mock).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..models.workflow import WorkflowNode


@dataclass
class NodeExecutionResult:
    """Result of executing a workflow node."""
    success: bool
    output: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    tool_invocations: int = 0
    metadata: dict[str, Any] = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class INodeExecutor(ABC):
    """
    Interface for executing workflow nodes.
    
    Responsibilities:
    - Execute individual workflow nodes
    - Track tool invocations during execution
    - Report execution results and metrics
    
    Implementations:
    - DefaultNodeExecutor: Executes tools via ToolManager
    - MockNodeExecutor: For testing (returns predefined results)
    - RemoteNodeExecutor: Delegates to external service
    """
    
    @abstractmethod
    def execute(
        self,
        node: WorkflowNode,
        context: dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute a workflow node.
        
        Args:
            node: The node to execute
            context: Current workflow variables and state
            
        Returns:
            NodeExecutionResult with output or error
        """
        pass
    
    @abstractmethod
    def can_execute(self, node: WorkflowNode) -> bool:
        """
        Check if this executor can handle the given node.
        
        Args:
            node: The node to check
            
        Returns:
            True if this executor can execute the node
        """
        pass
    
    @abstractmethod
    def get_supported_node_types(self) -> list:
        """
        Get list of node types this executor supports.
        
        Returns:
            List of node type strings
        """
        pass


class INodeExecutorRegistry(ABC):
    """
    Registry for node executors.
    
    Maps node types to appropriate executors.
    """
    
    @abstractmethod
    def register(self, node_type: str, executor: INodeExecutor):
        """
        Register an executor for a node type.
        
        Args:
            node_type: Type of node (e.g., 'action', 'decision')
            executor: Executor instance
        """
        pass
    
    @abstractmethod
    def get_executor(self, node: WorkflowNode) -> INodeExecutor | None:
        """
        Get the appropriate executor for a node.
        
        Args:
            node: The node to find an executor for
            
        Returns:
            INodeExecutor if found, None otherwise
        """
        pass
    
    @abstractmethod
    def execute(
        self,
        node: WorkflowNode,
        context: dict[str, Any]
    ) -> NodeExecutionResult:
        """
        Execute a node using the appropriate executor.
        
        Convenience method that looks up and uses the correct executor.
        
        Args:
            node: The node to execute
            context: Current workflow variables and state
            
        Returns:
            NodeExecutionResult with output or error
            
        Raises:
            ValueError: If no executor found for node type
        """
        pass

