"""
Application Factories.

Factory pattern for creating application services with proper dependency injection.
Simplifies service creation while maintaining flexibility for testing.
"""

from typing import Optional

from wtb.domain.interfaces.state_adapter import IStateAdapter
from wtb.domain.interfaces.unit_of_work import IUnitOfWork
from wtb.domain.interfaces.node_executor import INodeExecutor
from wtb.config import WTBConfig, get_config
from wtb.infrastructure.database import (
    UnitOfWorkFactory,
    SQLAlchemyUnitOfWork,
    InMemoryUnitOfWork,
)
from wtb.infrastructure.adapters import InMemoryStateAdapter

from .services.execution_controller import ExecutionController, DefaultNodeExecutor
from .services.node_replacer import NodeReplacer


class ExecutionControllerFactory:
    """
    Factory for creating ExecutionController with proper dependencies.
    
    Handles dependency injection based on configuration:
    - Testing: In-memory UoW and InMemoryStateAdapter
    - Development: SQLAlchemy UoW and AgentGitStateAdapter
    - Production: Same as development with production URLs
    
    Usage:
        # Using config
        config = WTBConfig.for_development()
        controller = ExecutionControllerFactory.create(config)
        
        # With custom dependencies
        controller = ExecutionControllerFactory.create_with_dependencies(
            uow=my_uow,
            state_adapter=my_adapter,
        )
        
        # Quick test setup
        controller = ExecutionControllerFactory.create_for_testing()
    """
    
    @staticmethod
    def create(config: Optional[WTBConfig] = None) -> ExecutionController:
        """
        Create ExecutionController based on configuration.
        
        Args:
            config: WTB configuration (uses global config if None)
            
        Returns:
            ExecutionController with configured dependencies
        """
        if config is None:
            config = get_config()
        
        # Create UoW
        uow = UnitOfWorkFactory.create(
            mode=config.wtb_storage_mode,
            db_url=config.wtb_db_url,
            echo=config.log_sql,
        )
        
        # Create state adapter
        state_adapter = ExecutionControllerFactory._create_state_adapter(config, uow)
        
        # Get repositories from UoW
        # Note: For a real UoW, we need to enter the context first
        # For now, we pass the UoW and let the controller manage the context
        return ExecutionControllerFactory._create_controller(uow, state_adapter)
    
    @staticmethod
    def _create_state_adapter(config: WTBConfig, uow: IUnitOfWork) -> IStateAdapter:
        """Create state adapter based on config."""
        if config.state_adapter_mode == "inmemory":
            return InMemoryStateAdapter()
        
        elif config.state_adapter_mode == "agentgit":
            # Import conditionally to avoid hard dependency
            try:
                from wtb.infrastructure.adapters import AgentGitStateAdapter
                if AgentGitStateAdapter is None:
                    raise ImportError("AgentGit not installed")
                
                return AgentGitStateAdapter(
                    agentgit_db_path=config.agentgit_db_path,
                    wtb_db_url=config.wtb_db_url,
                )
            except ImportError:
                # Fall back to in-memory if AgentGit not available
                import warnings
                warnings.warn(
                    "AgentGit not available, falling back to InMemoryStateAdapter",
                    RuntimeWarning,
                )
                return InMemoryStateAdapter()
        
        else:
            raise ValueError(f"Unknown state adapter mode: {config.state_adapter_mode}")
    
    @staticmethod
    def _create_controller(
        uow: IUnitOfWork,
        state_adapter: IStateAdapter,
        node_executor: Optional[INodeExecutor] = None,
    ) -> ExecutionController:
        """Create controller with given dependencies."""
        # Enter UoW context to get repositories
        with uow:
            return ExecutionController(
                execution_repository=uow.executions,
                workflow_repository=uow.workflows,
                state_adapter=state_adapter,
                node_executor=node_executor or DefaultNodeExecutor(),
            )
    
    @staticmethod
    def create_with_dependencies(
        uow: IUnitOfWork,
        state_adapter: IStateAdapter,
        node_executor: Optional[INodeExecutor] = None,
    ) -> ExecutionController:
        """
        Create controller with explicit dependencies.
        
        Useful for testing with custom mock/fake implementations.
        
        Args:
            uow: Unit of Work providing repositories
            state_adapter: State adapter for checkpointing
            node_executor: Optional custom node executor
            
        Returns:
            ExecutionController with provided dependencies
        """
        return ExecutionControllerFactory._create_controller(
            uow, state_adapter, node_executor
        )
    
    @staticmethod
    def create_for_testing(
        node_executor: Optional[INodeExecutor] = None,
    ) -> ExecutionController:
        """
        Create controller for unit tests.
        
        Uses in-memory storage for speed and isolation.
        
        Args:
            node_executor: Optional custom node executor
            
        Returns:
            ExecutionController with in-memory dependencies
        """
        config = WTBConfig.for_testing()
        uow = InMemoryUnitOfWork()
        state_adapter = InMemoryStateAdapter()
        
        return ExecutionControllerFactory._create_controller(
            uow, state_adapter, node_executor
        )
    
    @staticmethod
    def create_for_development(
        data_dir: str = "data",
        node_executor: Optional[INodeExecutor] = None,
    ) -> ExecutionController:
        """
        Create controller for development.
        
        Uses SQLite persistence with optional AgentGit integration.
        
        Args:
            data_dir: Directory for database files
            node_executor: Optional custom node executor
            
        Returns:
            ExecutionController with SQLite persistence
        """
        config = WTBConfig.for_development(data_dir)
        return ExecutionControllerFactory.create(config)


class NodeReplacerFactory:
    """
    Factory for creating NodeReplacer with proper dependencies.
    
    Usage:
        # Using config
        replacer = NodeReplacerFactory.create()
        
        # For testing
        replacer = NodeReplacerFactory.create_for_testing()
    """
    
    @staticmethod
    def create(config: Optional[WTBConfig] = None) -> NodeReplacer:
        """
        Create NodeReplacer based on configuration.
        
        Args:
            config: WTB configuration (uses global config if None)
            
        Returns:
            NodeReplacer with configured dependencies
        """
        if config is None:
            config = get_config()
        
        uow = UnitOfWorkFactory.create(
            mode=config.wtb_storage_mode,
            db_url=config.wtb_db_url,
        )
        
        with uow:
            return NodeReplacer(variant_repository=uow.variants)
    
    @staticmethod
    def create_with_dependencies(uow: IUnitOfWork) -> NodeReplacer:
        """
        Create NodeReplacer with explicit UoW.
        
        Args:
            uow: Unit of Work providing repositories
            
        Returns:
            NodeReplacer with provided dependencies
        """
        with uow:
            return NodeReplacer(variant_repository=uow.variants)
    
    @staticmethod
    def create_for_testing() -> NodeReplacer:
        """
        Create NodeReplacer for unit tests.
        
        Returns:
            NodeReplacer with in-memory storage
        """
        uow = InMemoryUnitOfWork()
        return NodeReplacerFactory.create_with_dependencies(uow)

