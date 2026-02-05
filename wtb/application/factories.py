"""
Application Factories.

Refactored (v1.6 → v1.7):
- WTBTestBench no longer receives state_adapter (layer separation)
- All IDs are strings (UUIDs)
- Removed AgentGit-specific code paths
- NEW: ExecutionControllerFactory supports isolated controller creation for ACID compliance
- NEW: Proper UoW lifecycle management with context managers

Factory pattern for creating application services with proper dependency injection.

ACID Compliance (v1.7):
- Each batch execution variant gets isolated ExecutionController + UoW
- ExecutionControllerFactory provides factory callables for batch runners
- UoW lifecycle properly managed (enter/exit)
"""

from typing import Optional, Callable, TYPE_CHECKING, Tuple
from dataclasses import dataclass
from contextlib import contextmanager

if TYPE_CHECKING:
    from wtb.sdk.test_bench import WTBTestBench
    from wtb.application.services.batch_execution_coordinator import BatchExecutionCoordinator

from wtb.domain.interfaces.state_adapter import IStateAdapter
from wtb.domain.interfaces.unit_of_work import IUnitOfWork
from wtb.domain.interfaces.node_executor import INodeExecutor
from wtb.domain.interfaces.batch_runner import IBatchTestRunner
from wtb.config import WTBConfig, get_config
from wtb.infrastructure.database import (
    UnitOfWorkFactory,
    SQLAlchemyUnitOfWork,
    InMemoryUnitOfWork,
)
from wtb.infrastructure.adapters import InMemoryStateAdapter

from .services.execution_controller import ExecutionController, DefaultNodeExecutor
from .services.node_replacer import NodeReplacer


@dataclass
class ManagedController:
    """
    ExecutionController with managed UoW lifecycle.
    
    v1.7: Ensures proper UoW cleanup for ACID compliance.
    
    Usage:
        with factory.create_managed() as managed:
            result = managed.controller.run(execution_id, graph)
        # UoW automatically closed
    """
    controller: ExecutionController
    uow: IUnitOfWork
    
    def __enter__(self) -> "ManagedController":
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Properly close UoW on exit."""
        try:
            if exc_type is None:
                self.uow.commit()
            else:
                self.uow.rollback()
        finally:
            self.uow.__exit__(exc_type, exc_val, exc_tb)
        return False


class ExecutionControllerFactory:
    """
    Factory for creating ExecutionController with proper dependencies.
    
    Refactored (v1.6 → v1.7):
    - Uses string IDs throughout
    - NEW: Provides factory callables for ACID-compliant batch execution
    - NEW: Proper UoW lifecycle management
    
    SOLID Compliance:
    - SRP: Creates controllers only
    - OCP: New adapters via configuration
    - DIP: Depends on IStateAdapter, IUnitOfWork abstractions
    
    ACID Compliance (v1.7):
    - Each call to create_isolated() creates new UoW (Isolation)
    - UoW manages transaction boundaries (Atomicity)
    - get_factory_callable() returns factory for batch runners
    """
    
    def __init__(self, config: Optional[WTBConfig] = None):
        """
        Initialize factory with configuration.
        
        Args:
            config: WTB configuration (defaults to global config)
        """
        self._config = config or get_config()
    
    def create_isolated(self) -> ManagedController:
        """
        Create an isolated ExecutionController with its own UoW.
        
        CRITICAL: Each call creates NEW UoW for ACID Isolation.
        Use this for batch execution where each variant needs isolation.
        
        Returns:
            ManagedController with controller and managed UoW
            
        Usage:
            factory = ExecutionControllerFactory(config)
            with factory.create_isolated() as managed:
                result = managed.controller.run(exec_id, graph)
            # UoW automatically closed after context exit
        """
        uow = UnitOfWorkFactory.create(
            mode=self._config.wtb_storage_mode,
            db_url=self._config.wtb_db_url,
            echo=self._config.log_sql,
        )
        uow.__enter__()  # Start UoW context
        
        state_adapter = self._create_state_adapter_instance()
        
        controller = ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=state_adapter,
            node_executor=DefaultNodeExecutor(),
            unit_of_work=uow,
        )
        
        return ManagedController(controller=controller, uow=uow)
    
    def _create_state_adapter_instance(self) -> IStateAdapter:
        """Create a new state adapter instance."""
        return ExecutionControllerFactory._create_state_adapter(self._config, None)
    
    @classmethod
    def get_factory_callable(
        cls,
        config: Optional[WTBConfig] = None,
    ) -> Callable[[], ManagedController]:
        """
        Get a factory callable for batch runners.
        
        CRITICAL for ACID Isolation: Each call to the returned factory
        creates a NEW isolated controller with its own UoW.
        
        Args:
            config: WTB configuration
            
        Returns:
            Callable that creates new ManagedController instances
            
        Usage in Batch Runners:
            controller_factory = ExecutionControllerFactory.get_factory_callable(config)
            
            # In each thread/actor:
            with controller_factory() as managed:
                result = managed.controller.run(exec_id, graph)
        """
        factory_instance = cls(config)
        return factory_instance.create_isolated
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Static Factory Methods (backward compatibility)
    # ═══════════════════════════════════════════════════════════════════════════
    
    @staticmethod
    def create(config: Optional[WTBConfig] = None) -> ExecutionController:
        """
        Create ExecutionController based on configuration.
        
        WARNING: UoW lifecycle is NOT managed. Prefer create_isolated() for
        proper resource management.
        """
        if config is None:
            config = get_config()
        
        uow = UnitOfWorkFactory.create(
            mode=config.wtb_storage_mode,
            db_url=config.wtb_db_url,
            echo=config.log_sql,
        )
        
        state_adapter = ExecutionControllerFactory._create_state_adapter(config, uow)
        
        return ExecutionControllerFactory._create_controller(uow, state_adapter)
    
    @staticmethod
    def _create_state_adapter(config: WTBConfig, uow: Optional[IUnitOfWork]) -> IStateAdapter:
        """Create state adapter based on config."""
        if config.state_adapter_mode == "inmemory":
            return InMemoryStateAdapter()
        
        elif config.state_adapter_mode == "langgraph":
            try:
                from wtb.infrastructure.adapters.langgraph_state_adapter import (
                    LangGraphStateAdapter, LangGraphConfig, LANGGRAPH_AVAILABLE,
                )
                
                if not LANGGRAPH_AVAILABLE:
                    raise ImportError("LangGraph not available")
                
                if config.wtb_storage_mode == "sqlalchemy":
                    checkpoint_db = f"{config.data_dir}/wtb_checkpoints.db"
                    lg_config = LangGraphConfig.for_development(checkpoint_db)
                else:
                    lg_config = LangGraphConfig.for_testing()
                
                return LangGraphStateAdapter(lg_config)
            except ImportError:
                import warnings
                warnings.warn(
                    "LangGraph not available, falling back to InMemoryStateAdapter",
                    RuntimeWarning,
                )
                return InMemoryStateAdapter()
        
        else:
            return InMemoryStateAdapter()
    
    @staticmethod
    def _create_controller(
        uow: IUnitOfWork,
        state_adapter: IStateAdapter,
        node_executor: Optional[INodeExecutor] = None,
        file_tracking_service=None,
        output_dir: Optional[str] = None,
    ) -> ExecutionController:
        """
        Create controller with given dependencies.
        
        WARNING: Calls uow.__enter__() but does NOT manage exit.
        For proper lifecycle, use create_isolated() instead.
        """
        uow.__enter__()
        return ExecutionController(
            execution_repository=uow.executions,
            workflow_repository=uow.workflows,
            state_adapter=state_adapter,
            node_executor=node_executor or DefaultNodeExecutor(),
            unit_of_work=uow,
            file_tracking_service=file_tracking_service,
            output_dir=output_dir,
        )
    
    @staticmethod
    def create_with_dependencies(
        uow: IUnitOfWork,
        state_adapter: IStateAdapter,
        node_executor: Optional[INodeExecutor] = None,
    ) -> ExecutionController:
        """Create controller with explicit dependencies."""
        return ExecutionControllerFactory._create_controller(
            uow, state_adapter, node_executor
        )
    
    @staticmethod
    def create_for_testing(
        node_executor: Optional[INodeExecutor] = None,
    ) -> ExecutionController:
        """Create controller for unit tests."""
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
        """Create controller for development."""
        config = WTBConfig.for_development(data_dir)
        return ExecutionControllerFactory.create(config)


class NodeReplacerFactory:
    """Factory for creating NodeReplacer with proper dependencies."""
    
    @staticmethod
    def create(config: Optional[WTBConfig] = None) -> NodeReplacer:
        """Create NodeReplacer based on configuration."""
        if config is None:
            config = get_config()
        
        uow = UnitOfWorkFactory.create(
            mode=config.wtb_storage_mode,
            db_url=config.wtb_db_url,
        )
        
        uow.__enter__()
        return NodeReplacer(
            variant_repository=uow.variants,
            unit_of_work=uow,
        )
    
    @staticmethod
    def create_with_dependencies(uow: IUnitOfWork) -> NodeReplacer:
        """Create NodeReplacer with explicit UoW."""
        return NodeReplacer(
            variant_repository=uow.variants,
            unit_of_work=uow,
        )
    
    @staticmethod
    def create_for_testing() -> NodeReplacer:
        """Create NodeReplacer for unit tests."""
        uow = InMemoryUnitOfWork()
        uow.__enter__()
        return NodeReplacer(
            variant_repository=uow.variants,
            unit_of_work=uow,
        )


class BatchTestRunnerFactory:
    """
    Factory for creating batch test runners.
    
    Refactored (v1.7):
    - Uses ExecutionControllerFactory for ACID-compliant isolated execution
    - Each variant execution gets isolated controller + UoW
    
    ACID Compliance:
    - Isolation: Each execution gets its own controller + UoW
    - Atomicity: Each variant execution is atomic
    """
    
    @staticmethod
    def create(config: Optional[WTBConfig] = None) -> IBatchTestRunner:
        """Create batch test runner based on configuration."""
        if config is None:
            config = get_config()
        
        ray_enabled = getattr(config, 'ray_enabled', False)
        ray_config = getattr(config, 'ray_config', None)
        
        if ray_enabled and ray_config is not None:
            return BatchTestRunnerFactory.create_ray(config)
        else:
            return BatchTestRunnerFactory.create_threadpool(config)
    
    @staticmethod
    def create_threadpool(
        config: Optional[WTBConfig] = None,
        max_workers: int = 4,
        execution_timeout_seconds: float = 300.0,
    ) -> IBatchTestRunner:
        """
        Create ThreadPool-based batch test runner.
        
        Refactored (v1.7): Uses ExecutionControllerFactory for ACID compliance.
        """
        from .services.batch_test_runner import ThreadPoolBatchTestRunner
        
        if config is None:
            config = get_config()
        
        # v1.7: Use controller factory for ACID-compliant isolated execution
        controller_factory = ExecutionControllerFactory.get_factory_callable(config)
        
        return ThreadPoolBatchTestRunner(
            controller_factory=controller_factory,
            max_workers=max_workers,
            execution_timeout_seconds=execution_timeout_seconds,
        )
    
    @staticmethod
    def create_ray(config: Optional[WTBConfig] = None) -> IBatchTestRunner:
        """
        Create Ray-based batch test runner.
        
        Note: Ray runner has its own Actor-based isolation pattern.
        """
        from .services.ray_batch_runner import RayBatchTestRunner
        from wtb.config import RayConfig
        
        if config is None:
            config = get_config()
        
        ray_config = getattr(config, 'ray_config', None)
        if ray_config is None:
            ray_config = RayConfig.for_local_development()
        
        return RayBatchTestRunner(
            config=ray_config,
            agentgit_db_url=config.agentgit_db_path,
            wtb_db_url=config.wtb_db_url or f"sqlite:///{config.data_dir}/wtb.db",
        )
    
    @staticmethod
    def create_for_testing(max_workers: int = 2) -> IBatchTestRunner:
        """
        Create batch test runner for unit tests.
        
        Uses in-memory dependencies for isolation.
        """
        from .services.batch_test_runner import ThreadPoolBatchTestRunner
        from wtb.config import WTBConfig
        
        # Create test config with in-memory settings
        test_config = WTBConfig.for_testing()
        controller_factory = ExecutionControllerFactory.get_factory_callable(test_config)
        
        return ThreadPoolBatchTestRunner(
            controller_factory=controller_factory,
            max_workers=max_workers,
            execution_timeout_seconds=60.0,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# BatchCoordinator Factory (v1.8)
# ═══════════════════════════════════════════════════════════════════════════════


class BatchCoordinatorFactory:
    """
    Factory for creating BatchExecutionCoordinator.
    
    Design (v1.8):
    - Application layer factory handles all infrastructure wiring
    - SDK layer calls this factory (not infrastructure directly)
    - Ensures proper DIP compliance
    
    ACID Compliance:
    - UoW factory creates fresh UoW for each operation (Isolation)
    - Coordinator manages transaction boundaries (Atomicity)
    """
    
    @staticmethod
    def create_default(config: Optional[WTBConfig] = None) -> "BatchExecutionCoordinator":
        """
        Create BatchExecutionCoordinator with default configuration.
        
        Args:
            config: WTB configuration (defaults to global config)
            
        Returns:
            BatchExecutionCoordinator with properly wired dependencies
        """
        from wtb.application.services.batch_execution_coordinator import (
            BatchExecutionCoordinator,
            DefaultExecutionControllerFactory,
        )
        
        if config is None:
            config = get_config()
        
        # Create UoW factory using Application layer factory (not direct infrastructure)
        def uow_factory() -> IUnitOfWork:
            return UnitOfWorkFactory.create(
                mode=config.wtb_storage_mode,
                db_url=config.wtb_db_url,
                echo=config.log_sql,
            )
        
        # Create state adapter using existing factory method
        state_adapter = ExecutionControllerFactory._create_state_adapter(config, None)
        
        return BatchExecutionCoordinator(
            uow_factory=uow_factory,
            controller_factory=DefaultExecutionControllerFactory(),
            state_adapter=state_adapter,
        )
    
    @staticmethod
    def create_for_testing() -> "BatchExecutionCoordinator":
        """
        Create BatchExecutionCoordinator for unit tests.
        
        Uses in-memory dependencies.
        """
        from wtb.application.services.batch_execution_coordinator import (
            BatchExecutionCoordinator,
            DefaultExecutionControllerFactory,
        )
        
        def uow_factory() -> IUnitOfWork:
            return InMemoryUnitOfWork()
        
        state_adapter = InMemoryStateAdapter()
        
        return BatchExecutionCoordinator(
            uow_factory=uow_factory,
            controller_factory=DefaultExecutionControllerFactory(),
            state_adapter=state_adapter,
        )
    
    @staticmethod
    def create_for_development(data_dir: str = "data") -> "BatchExecutionCoordinator":
        """
        Create BatchExecutionCoordinator for development.
        
        Uses SQLite persistence with LangGraph checkpointer.
        """
        from wtb.application.services.batch_execution_coordinator import (
            BatchExecutionCoordinator,
            DefaultExecutionControllerFactory,
        )
        import os
        
        os.makedirs(data_dir, exist_ok=True)
        
        db_url = f"sqlite:///{data_dir}/wtb.db"
        
        def uow_factory() -> IUnitOfWork:
            return UnitOfWorkFactory.create(mode="sqlalchemy", db_url=db_url)
        
        # Use LangGraph state adapter if available
        try:
            from wtb.infrastructure.adapters.langgraph_state_adapter import (
                LangGraphStateAdapter, 
                LangGraphConfig,
                LANGGRAPH_AVAILABLE,
            )
            if LANGGRAPH_AVAILABLE:
                checkpoint_db_path = os.path.join(data_dir, "wtb_checkpoints.db")
                state_adapter = LangGraphStateAdapter(
                    LangGraphConfig.for_development(checkpoint_db_path)
                )
            else:
                state_adapter = InMemoryStateAdapter()
        except ImportError:
            state_adapter = InMemoryStateAdapter()
        
        return BatchExecutionCoordinator(
            uow_factory=uow_factory,
            controller_factory=DefaultExecutionControllerFactory(),
            state_adapter=state_adapter,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# WTBTestBench Factory (Composition Root)
# ═══════════════════════════════════════════════════════════════════════════════


class WTBTestBenchFactory:
    """
    Application-level factory for WTBTestBench.
    
    Refactored (v1.6):
    - WTBTestBench no longer receives state_adapter (layer separation)
    - All IDs are strings (UUIDs)
    - PRIMARY: LangGraphStateAdapter with ICheckpointStore
    
    This is the COMPOSITION ROOT where all infrastructure dependencies
    are wired together. The SDK layer should NOT create infrastructure.
    """
    
    @staticmethod
    def create(config: Optional[WTBConfig] = None) -> "WTBTestBench":
        """Create WTBTestBench based on configuration."""
        from wtb.sdk.test_bench import WTBTestBench
        from wtb.application.services import ProjectService, VariantService
        
        if config is None:
            config = get_config()
        
        uow = UnitOfWorkFactory.create(
            mode=config.wtb_storage_mode,
            db_url=config.wtb_db_url,
        )
        
        state_adapter = WTBTestBenchFactory._create_state_adapter(config)
        
        exec_ctrl = ExecutionControllerFactory.create_with_dependencies(
            uow=uow,
            state_adapter=state_adapter,
        )
        
        variant_registry = NodeReplacerFactory.create_with_dependencies(uow)
        
        project_service = ProjectService(uow)
        variant_service = VariantService(uow, variant_registry)
        
        return WTBTestBench(
            project_service=project_service,
            variant_service=variant_service,
            execution_controller=exec_ctrl,
            batch_runner=None,
        )
    
    @staticmethod
    def _create_state_adapter(config: WTBConfig) -> IStateAdapter:
        """
        Create state adapter based on config.
        
        DRY: Delegates to ExecutionControllerFactory._create_state_adapter()
        to avoid code duplication.
        """
        return ExecutionControllerFactory._create_state_adapter(config, None)
    
    @staticmethod
    def create_for_testing() -> "WTBTestBench":
        """Create WTBTestBench for unit tests."""
        from wtb.sdk.test_bench import WTBTestBench
        from wtb.application.services import ProjectService, VariantService
        
        uow = InMemoryUnitOfWork()
        state_adapter = InMemoryStateAdapter()
        
        exec_ctrl = ExecutionControllerFactory.create_with_dependencies(
            uow=uow,
            state_adapter=state_adapter,
        )
        
        variant_registry = NodeReplacerFactory.create_with_dependencies(uow)
        
        project_service = ProjectService(uow)
        variant_service = VariantService(uow, variant_registry)
        
        return WTBTestBench(
            project_service=project_service,
            variant_service=variant_service,
            execution_controller=exec_ctrl,
            batch_runner=None,
        )
    
    @staticmethod
    def create_for_development(
        data_dir: str = "data",
        enable_file_tracking: bool = False,
    ) -> "WTBTestBench":
        """
        Create WTBTestBench for development.
        
        Uses SQLite persistence for both UoW and LangGraph checkpoints.
        """
        from wtb.sdk.test_bench import WTBTestBench
        from wtb.application.services import ProjectService, VariantService
        import os
        from pathlib import Path
        
        os.makedirs(data_dir, exist_ok=True)
        
        db_url = f"sqlite:///{data_dir}/wtb.db"
        uow = UnitOfWorkFactory.create(mode="sqlalchemy", db_url=db_url)
        
        # Use LangGraph state adapter with SQLite checkpointer
        try:
            from wtb.infrastructure.adapters.langgraph_state_adapter import (
                LangGraphStateAdapter, 
                LangGraphConfig,
                LANGGRAPH_AVAILABLE,
            )
            if LANGGRAPH_AVAILABLE:
                checkpoint_db_path = os.path.join(data_dir, "wtb_checkpoints.db")
                state_adapter = LangGraphStateAdapter(
                    LangGraphConfig.for_development(checkpoint_db_path)
                )
            else:
                import warnings
                warnings.warn(
                    "LangGraph not available, falling back to InMemoryStateAdapter",
                    RuntimeWarning,
                )
                state_adapter = InMemoryStateAdapter()
        except ImportError:
            import warnings
            warnings.warn(
                "LangGraph checkpoint dependencies not available",
                RuntimeWarning,
            )
            state_adapter = InMemoryStateAdapter()
        
        # Create file tracking service if enabled
        file_tracking_service = None
        output_dir = None
        if enable_file_tracking:
            from wtb.infrastructure.file_tracking import SqliteFileTrackingService
            file_tracking_service = SqliteFileTrackingService(
                workspace_path=Path(data_dir),
                db_name="filetrack.db"
            )
            output_dir = os.path.join(data_dir, "outputs")
            os.makedirs(output_dir, exist_ok=True)
        
        exec_ctrl = ExecutionControllerFactory._create_controller(
            uow=uow,
            state_adapter=state_adapter,
            file_tracking_service=file_tracking_service,
            output_dir=output_dir,
        )
        
        variant_registry = NodeReplacerFactory.create_with_dependencies(uow)
        
        project_service = ProjectService(uow)
        variant_service = VariantService(uow, variant_registry)
        
        return WTBTestBench(
            project_service=project_service,
            variant_service=variant_service,
            execution_controller=exec_ctrl,
            batch_runner=None,
        )
    
    @staticmethod
    def create_with_langgraph(
        checkpointer_type: str = "sqlite",
        connection_string: Optional[str] = None,
        data_dir: str = "data",
        enable_file_tracking: bool = False,
    ) -> "WTBTestBench":
        """
        Create WTBTestBench with LangGraph checkpointers.
        
        Args:
            checkpointer_type: "memory", "sqlite", or "postgres"
            connection_string: Database path for sqlite, or connection string for postgres
            data_dir: Directory for database files (used with sqlite)
            enable_file_tracking: Enable file tracking for rollback
        """
        from wtb.sdk.test_bench import WTBTestBench
        from wtb.application.services import ProjectService, VariantService
        import os
        from pathlib import Path
        
        if checkpointer_type == "sqlite":
            os.makedirs(data_dir, exist_ok=True)
        
        if checkpointer_type == "memory":
            uow = InMemoryUnitOfWork()
        else:
            db_url = f"sqlite:///{data_dir}/wtb.db"
            uow = UnitOfWorkFactory.create(mode="sqlalchemy", db_url=db_url)
        
        try:
            from wtb.infrastructure.adapters.langgraph_state_adapter import (
                LangGraphStateAdapter, LangGraphConfig, CheckpointerType,
                LANGGRAPH_AVAILABLE,
            )
            
            if not LANGGRAPH_AVAILABLE:
                raise ImportError("LangGraph not available")
            
            type_map = {
                "memory": CheckpointerType.MEMORY,
                "sqlite": CheckpointerType.SQLITE,
                "postgres": CheckpointerType.POSTGRES,
            }
            
            if checkpointer_type == "sqlite":
                conn_str = connection_string or os.path.join(data_dir, "wtb_checkpoints.db")
            else:
                conn_str = connection_string
            
            lg_config = LangGraphConfig(
                checkpointer_type=type_map.get(checkpointer_type, CheckpointerType.SQLITE),
                connection_string=conn_str,
            )
            state_adapter = LangGraphStateAdapter(lg_config)
            
        except ImportError:
            import warnings
            warnings.warn(
                "LangGraph not available, using in-memory",
                RuntimeWarning,
            )
            state_adapter = InMemoryStateAdapter()
        
        # Create file tracking service if enabled
        file_tracking_service = None
        output_dir = None
        if enable_file_tracking and checkpointer_type != "memory":
            from wtb.infrastructure.file_tracking import SqliteFileTrackingService
            file_tracking_service = SqliteFileTrackingService(
                workspace_path=Path(data_dir),
                db_name="filetrack.db"
            )
            output_dir = os.path.join(data_dir, "outputs")
            os.makedirs(output_dir, exist_ok=True)
        
        exec_ctrl = ExecutionControllerFactory._create_controller(
            uow=uow,
            state_adapter=state_adapter,
            file_tracking_service=file_tracking_service,
            output_dir=output_dir,
        )
        
        variant_registry = NodeReplacerFactory.create_with_dependencies(uow)
        
        project_service = ProjectService(uow)
        variant_service = VariantService(uow, variant_registry)
        
        return WTBTestBench(
            project_service=project_service,
            variant_service=variant_service,
            execution_controller=exec_ctrl,
            batch_runner=None,
        )
