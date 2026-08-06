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

import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wtb.application.services.batch_execution_coordinator import (
        BatchExecutionCoordinator,
    )
    from wtb.sdk.test_bench import WTBTestBench

from wtb.config import WTBConfig, get_config
from wtb.domain.interfaces.batch_runner import IBatchTestRunner
from wtb.domain.interfaces.node_executor import INodeExecutor
from wtb.domain.interfaces.state_adapter import IStateAdapter
from wtb.domain.interfaces.unit_of_work import IUnitOfWork
from wtb.infrastructure.adapters import InMemoryStateAdapter
from wtb.infrastructure.database import (
    InMemoryUnitOfWork,
    UnitOfWorkFactory,
)

from .services.execution_controller import DefaultNodeExecutor, ExecutionController
from .services.node_replacer import NodeReplacer
from .services.outbox_controller_decorator import OutboxExecutionControllerDecorator


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
    owns_state_adapter: bool = False
    owns_file_tracking: bool = False
    
    def __enter__(self) -> "ManagedController":
        self.controller.set_deferred_commit(True)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Commit (or rollback) and close the UoW."""
        try:
            if exc_type is None:
                self.uow.commit()
            else:
                self.uow.rollback()
        finally:
            seen_resources = set()
            for attribute, is_owned in (
                ("_state_adapter", self.owns_state_adapter),
                ("_file_tracking", self.owns_file_tracking),
            ):
                if not is_owned:
                    continue
                resource = getattr(self.controller, attribute, None)
                if resource is None or id(resource) in seen_resources:
                    continue
                seen_resources.add(id(resource))
                close_resource = getattr(resource, "close", None)
                if callable(close_resource):
                    try:
                        close_resource()
                    except Exception:
                        # UoW closure must still run even if an adapter's
                        # best-effort cleanup reports an error.
                        pass
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
    
    def __init__(self, config: WTBConfig | None = None):
        """
        Initialize factory with configuration.
        
        Args:
            config: WTB configuration (defaults to global config)
        """
        self._config = config or get_config()
    
    def create_isolated(
        self,
        file_tracking_service=None,
        output_dir: str | None = None,
    ) -> ManagedController:
        """
        Create an isolated ExecutionController with its own UoW.
        
        CRITICAL: Each call creates NEW UoW for ACID Isolation.
        Use this for batch execution where each variant needs isolation.
        
        Args:
            file_tracking_service: Optional CAS file tracking service
            output_dir: Optional output directory for tracked files
        
        Returns:
            ManagedController with controller and managed UoW
        """
        owns_file_tracking = False
        if file_tracking_service is None and self._config.file_tracking_config:
            ft_config = self._config.file_tracking_config
            if ft_config.enabled and not ft_config.postgres_url:
                from pathlib import Path

                from wtb.infrastructure.file_tracking import SqliteFileTrackingService

                file_tracking_service = SqliteFileTrackingService(
                    workspace_path=Path(ft_config.storage_path),
                    db_name="filetrack.db",
                )
                owns_file_tracking = True
                output_dir = output_dir or str(Path(ft_config.storage_path) / "outputs")

        uow = None
        state_adapter = None
        uow_entered = False
        try:
            uow = UnitOfWorkFactory.create(
                mode=self._config.wtb_storage_mode,
                db_url=self._config.wtb_db_url,
                echo=False, #=self._config.log_sql for sql details
            )
            uow.__enter__()
            uow_entered = True

            state_adapter = self._create_state_adapter_instance()

            controller = ExecutionController(
                execution_repository=uow.executions,
                workflow_repository=uow.workflows,
                state_adapter=state_adapter,
                node_executor=DefaultNodeExecutor(),
                unit_of_work=uow,
                file_tracking_service=file_tracking_service,
                output_dir=output_dir,
            )
        except BaseException as error:
            seen_resources = set()
            for resource, is_owned in (
                (state_adapter, True),
                (file_tracking_service, owns_file_tracking),
            ):
                if (
                    not is_owned
                    or resource is None
                    or id(resource) in seen_resources
                ):
                    continue
                seen_resources.add(id(resource))
                close_resource = getattr(resource, "close", None)
                if callable(close_resource):
                    try:
                        close_resource()
                    except Exception as cleanup_error:
                        error.add_note(f"Resource cleanup failed: {cleanup_error}")
            if uow_entered:
                try:
                    uow.__exit__(type(error), error, error.__traceback__)
                except Exception as cleanup_error:
                    error.add_note(f"UoW cleanup failed: {cleanup_error}")
            raise

        return ManagedController(
            controller=controller,
            uow=uow,
            owns_state_adapter=True,
            owns_file_tracking=owns_file_tracking,
        )
    
    def _create_state_adapter_instance(self) -> IStateAdapter:
        """Create a new state adapter instance."""
        return ExecutionControllerFactory._create_state_adapter(self._config, None)
    
    @classmethod
    def get_factory_callable(
        cls,
        config: WTBConfig | None = None,
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
    def create(config: WTBConfig | None = None) -> ExecutionController:
        """
        Create ExecutionController based on configuration.
        
        WARNING: UoW lifecycle is NOT managed. Prefer create_isolated() for
        proper resource management.
        """
        warnings.warn(
            "ExecutionControllerFactory.create() leaks UoW. Use create_isolated() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
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
    def _create_state_adapter(config: WTBConfig, uow: IUnitOfWork | None) -> IStateAdapter:
        """Create state adapter based on config."""
        mode = config.state_adapter_mode
        if mode == "inmemory":
            return InMemoryStateAdapter()
        if mode != "langgraph":
            raise ValueError(f"Unsupported state adapter mode: {mode!r}")

        try:
            from wtb.infrastructure.adapters.langgraph_state_adapter import (
                LANGGRAPH_AVAILABLE,
                LangGraphConfig,
                LangGraphStateAdapter,
            )
        except ImportError as error:
            raise ImportError(
                "LangGraph state adapter dependencies are unavailable"
            ) from error

        if not LANGGRAPH_AVAILABLE:
            raise ImportError("LangGraph state adapter is unavailable")

        storage_mode = config.wtb_storage_mode
        if storage_mode == "inmemory":
            lg_config = LangGraphConfig.for_testing()
        elif storage_mode == "sqlalchemy":
            database_url = str(config.wtb_db_url or "").strip()
            normalized_url = database_url.lower()
            if normalized_url.startswith(
                ("postgresql://", "postgresql+", "postgres://")
            ):
                lg_config = LangGraphConfig.for_production(database_url)
            elif normalized_url.startswith("sqlite:"):
                import os

                checkpoint_db = (
                    getattr(config, "langgraph_checkpoint_path", None)
                    or os.path.join(
                        str(config.data_dir),
                        "wtb_checkpoints.db",
                    )
                )
                lg_config = LangGraphConfig.for_development(checkpoint_db)
            elif not database_url:
                raise ValueError(
                    "Durable langgraph state requires wtb_db_url"
                )
            else:
                raise ValueError(
                    "Unsupported database URL for langgraph state adapter: "
                    f"{database_url!r}"
                )
        else:
            raise ValueError(f"Unsupported storage mode: {storage_mode!r}")

        return LangGraphStateAdapter(lg_config)
    
    @staticmethod
    def _create_controller(
        uow: IUnitOfWork,
        state_adapter: IStateAdapter,
        node_executor: INodeExecutor | None = None,
        file_tracking_service=None,
        output_dir: str | None = None,
    ) -> ExecutionController:
        """
        Create controller with given dependencies.
        
        Durable UoWs are entered for their repository/session lifecycle.
        In-memory repositories are ready immediately and must not hold a
        construction-thread transaction lock for the controller lifetime.
        """
        if not isinstance(uow, InMemoryUnitOfWork):
            warnings.warn(
                "ExecutionControllerFactory.create() leaks UoW. Use create_isolated() instead.",
                DeprecationWarning,
                stacklevel=2,
            )
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
        node_executor: INodeExecutor | None = None,
    ) -> ExecutionController:
        """Create controller with explicit dependencies."""
        return ExecutionControllerFactory._create_controller(
            uow, state_adapter, node_executor
        )
    
    @staticmethod
    def create_for_testing(
        node_executor: INodeExecutor | None = None,
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
        node_executor: INodeExecutor | None = None,
    ) -> ExecutionController:
        """Create controller for development."""
        config = WTBConfig.for_development(data_dir)
        return ExecutionControllerFactory.create(config)


class NodeReplacerFactory:
    """Factory for creating NodeReplacer with proper dependencies."""
    
    @staticmethod
    def create(config: WTBConfig | None = None) -> NodeReplacer:
        """Create NodeReplacer based on configuration."""
        warnings.warn(
            "NodeReplacerFactory.create() leaks UoW. Use create_with_dependencies() with a managed UnitOfWork instead.",
            DeprecationWarning,
            stacklevel=2,
        )
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
    def create(config: WTBConfig | None = None) -> IBatchTestRunner:
        """Create batch test runner based on configuration."""
        if config is None:
            config = get_config()
        
        ray_enabled = getattr(config, 'ray_enabled', False)
        
        if ray_enabled:
            return BatchTestRunnerFactory.create_ray(config)
        else:
            return BatchTestRunnerFactory.create_threadpool(config)
    
    @staticmethod
    def create_threadpool(
        config: WTBConfig | None = None,
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
            config=config,
        )
    
    @staticmethod
    def create_ray(config: WTBConfig | None = None) -> IBatchTestRunner:
        """
        Create Ray-based batch test runner.
        
        When ``config.environment_provider == "grpc"`` and
        ``config.grpc_env_manager_url`` is set, a
        ``GrpcEnvironmentProvider`` is created and injected so that
        Ray actors provision isolated UV venvs via the Docker service.
        """
        from wtb.config import RayConfig

        from .services.ray_batch_runner import RayBatchTestRunner
        
        if config is None:
            config = get_config()
        
        ray_config = getattr(config, 'ray_config', None)
        if ray_config is None:
            ray_config = RayConfig.for_local_development()
        
        env_provider = None
        if (
            getattr(config, "environment_provider", "inprocess") == "grpc"
            and getattr(config, "grpc_env_manager_url", None)
        ):
            from wtb.infrastructure.environment import GrpcEnvironmentProvider
            env_provider = GrpcEnvironmentProvider(config.grpc_env_manager_url)
        
        return RayBatchTestRunner(
            config=ray_config,
            agentgit_db_url=config.agentgit_db_path,
            wtb_db_url=config.wtb_db_url or f"sqlite:///{config.data_dir}/wtb.db",
            filetracker_config=(
                config.file_tracking_config.to_dict()
                if config.file_tracking_config and config.file_tracking_config.enabled
                else None
            ),
            environment_provider=env_provider,
            owns_environment_provider=env_provider is not None,
        )
    
    @staticmethod
    def create_for_testing(max_workers: int = 2) -> IBatchTestRunner:
        """
        Create batch test runner for unit tests.
        
        Uses in-memory dependencies for isolation.
        """
        from wtb.config import WTBConfig

        from .services.batch_test_runner import ThreadPoolBatchTestRunner
        
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
    def create_default(config: WTBConfig | None = None) -> "BatchExecutionCoordinator":
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

        file_tracking = None
        if config.file_tracking_config and config.file_tracking_config.enabled:
            ft_config = config.file_tracking_config
            if not ft_config.postgres_url:
                from pathlib import Path

                from wtb.infrastructure.file_tracking import SqliteFileTrackingService

                file_tracking = SqliteFileTrackingService(
                    workspace_path=Path(ft_config.storage_path),
                    db_name="filetrack.db",
                )
        
        return BatchExecutionCoordinator(
            uow_factory=uow_factory,
            controller_factory=DefaultExecutionControllerFactory(),
            state_adapter=state_adapter,
            file_tracking=file_tracking,
            config=config,
            owns_state_adapter=True,
            owns_file_tracking=file_tracking is not None,
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
            owns_state_adapter=True,
        )
    
    @staticmethod
    def create_for_development(data_dir: str = "data") -> "BatchExecutionCoordinator":
        """
        Create BatchExecutionCoordinator for development.
        
        Uses SQLite persistence with LangGraph checkpointer.
        """
        import os

        from wtb.application.services.batch_execution_coordinator import (
            BatchExecutionCoordinator,
            DefaultExecutionControllerFactory,
        )
        
        os.makedirs(data_dir, exist_ok=True)
        
        db_url = f"sqlite:///{data_dir}/wtb.db"
        
        def uow_factory() -> IUnitOfWork:
            return UnitOfWorkFactory.create(mode="sqlalchemy", db_url=db_url)
        
        development_config = WTBConfig.for_development(data_dir)
        state_adapter = ExecutionControllerFactory._create_state_adapter(
            development_config,
            None,
        )
        
        return BatchExecutionCoordinator(
            uow_factory=uow_factory,
            controller_factory=DefaultExecutionControllerFactory(),
            state_adapter=state_adapter,
            config=development_config,
            owns_state_adapter=True,
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
    def create(config: WTBConfig | None = None) -> "WTBTestBench":
        """Create WTBTestBench based on configuration."""
        from wtb.application.services import ProjectService, VariantService
        from wtb.sdk.test_bench import WTBTestBench
        
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
        
        outbox_repo = getattr(uow, 'outbox', None)
        wrapped_ctrl = OutboxExecutionControllerDecorator(
            exec_ctrl,
            outbox_repo,
            commit_fn=uow.commit,
            rollback_fn=uow.rollback,
        )
        
        batch_runner = BatchTestRunnerFactory.create(config)
        
        variant_registry = NodeReplacerFactory.create_with_dependencies(uow)
        
        project_service = ProjectService(uow)
        variant_service = VariantService(uow, variant_registry)
        
        return WTBTestBench(
            project_service=project_service,
            variant_service=variant_service,
            execution_controller=wrapped_ctrl,
            batch_runner=batch_runner,
            owns_batch_runner=True,
            owns_execution_resources=True,
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
        from wtb.application.services import ProjectService, VariantService
        from wtb.sdk.test_bench import WTBTestBench
        
        uow = InMemoryUnitOfWork()
        state_adapter = InMemoryStateAdapter()
        
        exec_ctrl = ExecutionControllerFactory.create_with_dependencies(
            uow=uow,
            state_adapter=state_adapter,
        )
        
        outbox_repo = getattr(uow, 'outbox', None)
        wrapped_ctrl = OutboxExecutionControllerDecorator(
            exec_ctrl,
            outbox_repo,
            commit_fn=uow.commit,
            rollback_fn=uow.rollback,
        )
        
        batch_runner = BatchTestRunnerFactory.create_for_testing()
        
        variant_registry = NodeReplacerFactory.create_with_dependencies(uow)
        
        project_service = ProjectService(uow)
        variant_service = VariantService(uow, variant_registry)
        
        return WTBTestBench(
            project_service=project_service,
            variant_service=variant_service,
            execution_controller=wrapped_ctrl,
            batch_runner=batch_runner,
            owns_batch_runner=True,
            owns_execution_resources=True,
        )
    
    @staticmethod
    def create_for_development(
        data_dir: str = "data",
        enable_file_tracking: bool = False,
        enable_ray: bool = False,
        grpc_env_url: str | None = None,
    ) -> "WTBTestBench":
        """
        Create WTBTestBench for development.
        
        Uses SQLite persistence for both UoW and LangGraph checkpoints.
        
        Args:
            data_dir: Directory for database files
            enable_file_tracking: Enable file tracking for rollback
            enable_ray: Use Ray-based batch runner instead of thread pool.
                        Requires ``ray`` to be installed and ``ray.init()``
                        to have been called before invoking batch operations.
            grpc_env_url: Optional gRPC URL for UV venv manager Docker
                          service (e.g. ``localhost:50051``). When set
                          alongside ``enable_ray=True``, each Ray actor
                          gets an isolated virtual environment.
        """
        import os
        from pathlib import Path

        from wtb.application.services import ProjectService, VariantService
        from wtb.sdk.test_bench import WTBTestBench
        
        os.makedirs(data_dir, exist_ok=True)
        
        config = WTBConfig.for_development(data_dir)
        db_url = config.wtb_db_url
        state_adapter = ExecutionControllerFactory._create_state_adapter(
            config,
            None,
        )
        uow = UnitOfWorkFactory.create(mode="sqlalchemy", db_url=db_url)
        
        # Create file tracking service if enabled
        file_tracking_service = None
        output_dir = None
        if enable_file_tracking:
            from wtb.config import FileTrackingConfig
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
        
        if enable_file_tracking:
            config.filetracker_enabled = True
            config.file_tracking_config = FileTrackingConfig.for_development(
                storage_path=str(Path(data_dir)),
                wtb_db_url=db_url,
            )
        outbox_repo = getattr(uow, 'outbox', None)
        wrapped_ctrl = OutboxExecutionControllerDecorator(
            exec_ctrl,
            outbox_repo,
            commit_fn=uow.commit,
            rollback_fn=uow.rollback,
        )
        
        if enable_ray:
            config.ray_enabled = True
            from wtb.config import RayConfig as InternalRayConfig
            config.ray_config = InternalRayConfig.for_local_development()
            if grpc_env_url:
                config.environment_provider = "grpc"
                config.grpc_env_manager_url = grpc_env_url
            batch_runner = BatchTestRunnerFactory.create_ray(config)
        else:
            batch_runner = BatchTestRunnerFactory.create_threadpool(config)
        
        variant_registry = NodeReplacerFactory.create_with_dependencies(uow)
        
        project_service = ProjectService(uow)
        variant_service = VariantService(uow, variant_registry)
        
        return WTBTestBench(
            project_service=project_service,
            variant_service=variant_service,
            execution_controller=wrapped_ctrl,
            batch_runner=batch_runner,
            owns_batch_runner=True,
            owns_execution_resources=True,
        )
    
    @staticmethod
    def create_with_langgraph(
        checkpointer_type: str = "sqlite",
        connection_string: str | None = None,
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
        import os
        from pathlib import Path

        from wtb.application.services import ProjectService, VariantService
        from wtb.sdk.test_bench import WTBTestBench

        supported_checkpointers = {"memory", "sqlite", "postgres"}
        if checkpointer_type not in supported_checkpointers:
            raise ValueError(
                f"Unsupported checkpointer type: {checkpointer_type!r}"
            )
        if checkpointer_type == "postgres" and not connection_string:
            raise ValueError(
                "connection_string is required for postgres checkpointer"
            )

        try:
            from wtb.infrastructure.adapters.langgraph_state_adapter import (
                LANGGRAPH_AVAILABLE,
                LangGraphConfig,
                LangGraphStateAdapter,
            )
        except ImportError as error:
            raise ImportError(
                "LangGraph state adapter dependencies are unavailable"
            ) from error

        if not LANGGRAPH_AVAILABLE:
            raise ImportError("LangGraph state adapter is unavailable")

        if checkpointer_type == "memory":
            state_config = LangGraphConfig.for_testing()
            bench_config = WTBConfig.for_testing()
            bench_config.state_adapter_mode = "langgraph"
            uow = InMemoryUnitOfWork()
        elif checkpointer_type == "sqlite":
            os.makedirs(data_dir, exist_ok=True)
            checkpoint_path = connection_string or os.path.join(
                data_dir,
                "wtb_checkpoints.db",
            )
            state_config = LangGraphConfig.for_development(checkpoint_path)
            bench_config = WTBConfig.for_development(data_dir)
            bench_config.langgraph_checkpoint_path = checkpoint_path
            uow = UnitOfWorkFactory.create(
                mode="sqlalchemy",
                db_url=bench_config.wtb_db_url,
            )
        else:
            state_config = LangGraphConfig.for_production(connection_string)
            bench_config = WTBConfig.for_production(
                db_url=connection_string,
                data_dir=data_dir,
            )
            uow = UnitOfWorkFactory.create(
                mode="sqlalchemy",
                db_url=connection_string,
            )

        state_adapter = LangGraphStateAdapter(state_config)

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
        
        outbox_repo = getattr(uow, 'outbox', None)
        wrapped_ctrl = OutboxExecutionControllerDecorator(
            exec_ctrl,
            outbox_repo,
            commit_fn=uow.commit,
            rollback_fn=uow.rollback,
        )
        
        batch_runner = BatchTestRunnerFactory.create_threadpool(bench_config)
        
        variant_registry = NodeReplacerFactory.create_with_dependencies(uow)
        
        project_service = ProjectService(uow)
        variant_service = VariantService(uow, variant_registry)
        
        return WTBTestBench(
            project_service=project_service,
            variant_service=variant_service,
            execution_controller=wrapped_ctrl,
            batch_runner=batch_runner,
            owns_batch_runner=True,
            owns_execution_resources=True,
        )
