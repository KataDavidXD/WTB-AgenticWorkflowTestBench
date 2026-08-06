"""
Execution Controller Interface.

Defines the contract for controlling workflow execution lifecycle.
Follows Interface Segregation Principle (ISP) by providing focused methods.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass  # For future type hints like CompiledStateGraph

from ..models.workflow import Execution, ExecutionState, TestWorkflow


class IExecutionController(ABC):
    """
    Interface for controlling workflow execution lifecycle.
    
    Responsibilities:
    - Create executions from workflows
    - Manage execution lifecycle (run, pause, resume, stop)
    - Handle rollback to checkpoints
    - Query execution state
    
    Implementations should delegate state persistence to IStateAdapter.
    """
    
    @abstractmethod
    def create_execution(
        self, 
        workflow: TestWorkflow,
        initial_state: dict[str, Any] | None = None,
        breakpoints: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        execution_id: str | None = None,
    ) -> Execution:
        """
        Create a new execution for a workflow.
        
        Args:
            workflow: The workflow to execute
            initial_state: Optional initial workflow variables
            breakpoints: Optional list of node IDs to pause at
            metadata: Optional execution metadata kept outside workflow state
            execution_id: Optional caller-provided stable execution ID
            
        Returns:
            Created Execution in PENDING state
            
        Raises:
            ValueError: If workflow validation fails
        """
        pass
    
    @abstractmethod
    def run(self, execution_id: str, graph: Any = None) -> Execution:
        """
        Start or continue execution.
        
        Executes nodes until:
        - A breakpoint is reached (pauses)
        - The workflow completes
        - An error occurs
        
        Execution Strategy:
        - If graph is provided AND adapter supports LangGraph -> native LangGraph execution
        - Otherwise -> use DefaultNodeExecutor with WTB workflow nodes
        
        Args:
            execution_id: ID of the execution to run
            graph: Optional compiled LangGraph for native execution
            
        Returns:
            Updated Execution with current state
            
        Raises:
            ValueError: If execution not found
            RuntimeError: If execution cannot be run in current state
        """
        pass
    
    @abstractmethod
    def pause(self, execution_id: str) -> Execution:
        """
        Pause execution at current position.
        
        Creates a checkpoint before pausing.
        
        Args:
            execution_id: ID of the execution to pause
            
        Returns:
            Updated Execution in PAUSED state
            
        Raises:
            ValueError: If execution not found or cannot be paused
        """
        pass
    
    @abstractmethod
    def resume(
        self, 
        execution_id: str, 
        modified_state: dict[str, Any] | None = None
    ) -> Execution:
        """
        Resume paused execution, optionally with modified state.
        
        Args:
            execution_id: ID of the execution to resume
            modified_state: Optional state updates to apply before resuming
            
        Returns:
            Updated Execution (will be RUNNING or terminal state)
            
        Raises:
            ValueError: If execution not found or cannot be resumed
        """
        pass
    
    @abstractmethod
    def stop(self, execution_id: str) -> Execution:
        """
        Stop and cancel execution.
        
        Args:
            execution_id: ID of the execution to stop
            
        Returns:
            Updated Execution in CANCELLED state
            
        Raises:
            ValueError: If execution not found
        """
        pass
    
    @abstractmethod
    def rollback(self, execution_id: str, checkpoint_id: str) -> Execution:
        """
        Rollback execution to a previous checkpoint.
        
        Creates a new branch from the checkpoint.
        
        Args:
            execution_id: ID of the execution
            checkpoint_id: ID of the checkpoint to rollback to
            
        Returns:
            Updated Execution with restored state in PAUSED state
            
        Raises:
            ValueError: If execution or checkpoint not found
        """
        pass
    
    @abstractmethod
    def get_state(self, execution_id: str) -> ExecutionState:
        """
        Get current execution state.
        
        Args:
            execution_id: ID of the execution
            
        Returns:
            Current ExecutionState
            
        Raises:
            ValueError: If execution not found
        """
        pass
    
    @abstractmethod
    def get_status(self, execution_id: str) -> Execution:
        """
        Get execution with current status.
        
        Args:
            execution_id: ID of the execution
            
        Returns:
            Execution object with current state
            
        Raises:
            ValueError: If execution not found
        """
        pass


