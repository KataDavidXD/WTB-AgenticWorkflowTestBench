"""
Batch Test Runner Interface.

Defines the abstract interface for batch test execution,
enabling multiple implementations (ThreadPool, Ray, etc.).

Usage:
    runner: IBatchTestRunner = factory.create(config)
    result = runner.run_batch_test(batch_test)
    status = runner.get_status(batch_test.id)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any

from wtb.domain.models.batch_test import BatchTest


class BatchRunnerStatus(Enum):
    """Status of a batch test runner."""
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"
    ERROR = "error"


@dataclass
class BatchRunnerProgress:
    """Progress information for a running batch test."""
    batch_test_id: str
    total_variants: int
    completed_variants: int
    failed_variants: int
    in_progress_variants: int
    elapsed_ms: float
    estimated_remaining_ms: float | None = None
    
    @property
    def progress_pct(self) -> float:
        """Get progress as percentage (0-100)."""
        if self.total_variants == 0:
            return 100.0
        return (self.completed_variants / self.total_variants) * 100.0


class IBatchTestRunner(ABC):
    """
    Abstract interface for batch test execution.
    
    Implementations:
    - ThreadPoolBatchTestRunner: Local multithreaded execution
    - RayBatchTestRunner: Distributed execution via Ray
    
    Design Principles:
    - Async-friendly but sync-first API
    - Progress tracking for long-running tests
    - Graceful cancellation support
    - Resource-aware execution
    """
    
    @abstractmethod
    def run_batch_test(self, batch_test: BatchTest) -> BatchTest:
        """
        Execute a batch test synchronously.
        
        Executes all variant combinations according to the batch test
        configuration and returns the completed batch test with results.
        
        Args:
            batch_test: BatchTest with variant combinations to execute
            
        Returns:
            BatchTest with results populated
            
        Raises:
            BatchRunnerError: If execution fails
        """
        pass
    
    @abstractmethod
    def get_status(self, batch_test_id: str) -> BatchRunnerStatus:
        """
        Get the current status of a batch test.
        
        Args:
            batch_test_id: ID of the batch test
            
        Returns:
            Current status
        """
        pass
    
    @abstractmethod
    def get_progress(self, batch_test_id: str) -> BatchRunnerProgress | None:
        """
        Get progress information for a running batch test.
        
        Args:
            batch_test_id: ID of the batch test
            
        Returns:
            Progress info if running, None if not found
        """
        pass
    
    @abstractmethod
    def cancel(self, batch_test_id: str) -> bool:
        """
        Cancel a running batch test.
        
        Args:
            batch_test_id: ID of the batch test to cancel
            
        Returns:
            True if the cancellation request was accepted, False if the batch
            was not found or the request was rejected. Implementations may
            document stronger completion guarantees.
        """
        pass
    
    @abstractmethod
    def shutdown(self) -> None:
        """
        Shutdown the runner and release resources.
        
        Should be called when the runner is no longer needed.
        """
        pass


class BatchRunnerError(Exception):
    """Base exception for batch runner errors."""
    pass


class BatchRunnerConfigError(BatchRunnerError):
    """Configuration error for batch runner."""
    pass


class BatchRunnerExecutionError(BatchRunnerError):
    """Execution error during batch test."""
    
    def __init__(
        self,
        message: str,
        batch_test_id: str,
        failed_variant: str | None = None,
        cause: Exception | None = None,
    ):
        super().__init__(message)
        self.batch_test_id = batch_test_id
        self.failed_variant = failed_variant
        self.cause = cause


class IEnvironmentProvider(ABC):
    """
    Abstract interface for execution environment isolation.
    
    Implementations:
    - InProcessEnvironmentProvider: No isolation (development/testing)
    - RayEnvironmentProvider: Ray runtime_env isolation
    - GrpcEnvironmentProvider: External gRPC environment manager
    
    Purpose:
    - Isolate variant executions from each other
    - Manage dependencies and configuration per variant
    - Support deterministic execution
    """
    
    @abstractmethod
    def create_environment(
        self,
        variant_id: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Create an isolated execution environment.
        
        Args:
            variant_id: Unique identifier for the variant
            config: Environment configuration
            
        Returns:
            Environment specification (format depends on implementation)
        """
        pass
    
    @abstractmethod
    def cleanup_environment(
        self,
        variant_id: str,
        timeout: float | None = None,
    ) -> None:
        """
        Cleanup an execution environment.
        
        Args:
            variant_id: Environment to cleanup
            timeout: Optional maximum seconds for remote cleanup

        Raises:
            Exception: If cleanup cannot be confirmed. Implementations must
                retain enough state for a later retry.
        """
        pass
    
    @abstractmethod
    def get_runtime_env(self, variant_id: str) -> dict[str, Any] | None:
        """
        Get the runtime environment specification.
        
        For Ray, returns the runtime_env dict.
        For gRPC, returns the environment service response.
        
        Args:
            variant_id: Variant identifier
            
        Returns:
            Runtime environment specification or None
        """
        pass

