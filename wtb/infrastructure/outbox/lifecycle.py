"""
Outbox Processor Lifecycle Management.

ISS-006 Resolution: Provides managed lifecycle for OutboxProcessor with:
- Auto-start on application initialization
- Health endpoint for monitoring
- Graceful shutdown on SIGTERM/SIGINT

Design Principles:
- SOLID: Single responsibility (lifecycle management only)
- ACID: Ensures processor completes pending work before shutdown

Usage:
    from wtb.infrastructure.outbox.lifecycle import OutboxLifecycleManager
    
    # Auto-start with application
    manager = OutboxLifecycleManager(
        wtb_db_url="sqlite:///data/wtb.db",
        auto_start=True,
    )
    
    # Health check
    health = manager.get_health()
    print(health)  # {"status": "healthy", "events_processed": 100, ...}
    
    # Graceful shutdown (automatic on SIGTERM)
    manager.shutdown()
"""

from typing import Dict, Any, Optional, Callable, List
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import signal
import threading
import logging
import atexit

from .processor import OutboxProcessor

logger = logging.getLogger(__name__)


class LifecycleStatus(Enum):
    """Lifecycle status for OutboxProcessor."""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class HealthStatus:
    """Health status information for monitoring."""
    status: str
    lifecycle_status: LifecycleStatus
    is_running: bool
    uptime_seconds: float
    events_processed: int
    events_failed: int
    last_check_time: datetime
    error_message: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            "status": self.status,
            "lifecycle_status": self.lifecycle_status.value,
            "is_running": self.is_running,
            "uptime_seconds": self.uptime_seconds,
            "events_processed": self.events_processed,
            "events_failed": self.events_failed,
            "last_check_time": self.last_check_time.isoformat(),
            "error_message": self.error_message,
            **self.details,
        }


class OutboxLifecycleManager:
    """
    Managed lifecycle for OutboxProcessor.
    
    ISS-006 Resolution:
    - Auto-start: Set auto_start=True to start processor on init
    - Health endpoint: get_health() returns monitoring data
    - Graceful shutdown: register_signal_handlers() + atexit hooks
    
    Thread Safety:
    - Uses Lock for state transitions
    - Safe to call from signal handlers
    
    Usage:
        # Auto-start with signal handling
        manager = OutboxLifecycleManager(
            wtb_db_url="sqlite:///data/wtb.db",
            auto_start=True,
            register_signals=True,
        )
        
        # Manual control
        manager = OutboxLifecycleManager(wtb_db_url="sqlite:///data/wtb.db")
        manager.start()
        health = manager.get_health()
        manager.shutdown()
    """
    
    def __init__(
        self,
        wtb_db_url: str,
        checkpoint_repo=None,
        file_tracking_service=None,
        file_cleanup_service=None,
        poll_interval_seconds: float = 1.0,
        batch_size: int = 50,
        auto_start: bool = False,
        register_signals: bool = False,
        shutdown_timeout_seconds: float = 10.0,
        on_start: Optional[Callable[[], None]] = None,
        on_stop: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ):
        """
        Initialize lifecycle manager.
        
        Args:
            wtb_db_url: WTB database connection URL
            checkpoint_repo: Optional checkpoint repository for verification
            file_tracking_service: Optional file tracking service
            file_cleanup_service: Optional file cleanup service for rollback cleanup (v1.9)
            poll_interval_seconds: How often processor polls for events
            batch_size: Maximum events per batch
            auto_start: If True, start processor immediately
            register_signals: If True, register SIGTERM/SIGINT handlers
            shutdown_timeout_seconds: How long to wait for graceful shutdown
            on_start: Optional callback when processor starts
            on_stop: Optional callback when processor stops
            on_error: Optional callback on processor error
        """
        self._wtb_db_url = wtb_db_url
        self._checkpoint_repo = checkpoint_repo
        self._file_tracking_service = file_tracking_service
        self._file_cleanup_service = file_cleanup_service
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._shutdown_timeout = shutdown_timeout_seconds
        
        # Callbacks
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_error = on_error
        
        # State
        self._processor: Optional[OutboxProcessor] = None
        self._status = LifecycleStatus.STOPPED
        self._started_at: Optional[datetime] = None
        self._error: Optional[str] = None
        self._lock = threading.Lock()
        
        # Signal handling
        self._original_sigterm = None
        self._original_sigint = None
        
        # Register signal handlers if requested
        if register_signals:
            self.register_signal_handlers()
        
        # Register atexit hook for cleanup
        atexit.register(self._atexit_cleanup)
        
        # Auto-start if requested
        if auto_start:
            self.start()
    
    def start(self) -> bool:
        """
        Start the outbox processor.
        
        Returns:
            True if started successfully, False if already running
        """
        with self._lock:
            if self._status in (LifecycleStatus.RUNNING, LifecycleStatus.STARTING):
                logger.warning("OutboxProcessor already running or starting")
                return False
            
            self._status = LifecycleStatus.STARTING
        
        try:
            # Create processor
            self._processor = OutboxProcessor(
                wtb_db_url=self._wtb_db_url,
                checkpoint_repo=self._checkpoint_repo,
                file_tracking_service=self._file_tracking_service,
                file_cleanup_service=self._file_cleanup_service,
                poll_interval_seconds=self._poll_interval,
                batch_size=self._batch_size,
            )
            
            # Start processor
            self._processor.start()
            
            with self._lock:
                self._status = LifecycleStatus.RUNNING
                self._started_at = datetime.now()
                self._error = None
            
            logger.info("OutboxLifecycleManager: Processor started")
            
            if self._on_start:
                try:
                    self._on_start()
                except Exception as e:
                    logger.warning(f"on_start callback error: {e}")
            
            return True
            
        except Exception as e:
            with self._lock:
                self._status = LifecycleStatus.ERROR
                self._error = str(e)
            
            logger.error(f"Failed to start OutboxProcessor: {e}")
            
            if self._on_error:
                try:
                    self._on_error(e)
                except Exception as callback_error:
                    logger.warning(f"on_error callback error: {callback_error}")
            
            return False
    
    def shutdown(self, timeout: Optional[float] = None) -> bool:
        """
        Gracefully shutdown the outbox processor.
        
        Args:
            timeout: Override default shutdown timeout
            
        Returns:
            True if shutdown cleanly, False if timeout/error
        """
        timeout = timeout or self._shutdown_timeout
        
        with self._lock:
            if self._status == LifecycleStatus.STOPPED:
                return True
            
            if self._status == LifecycleStatus.STOPPING:
                logger.warning("OutboxProcessor already stopping")
                return False
            
            self._status = LifecycleStatus.STOPPING
        
        logger.info("OutboxLifecycleManager: Initiating graceful shutdown...")
        
        success = True
        try:
            if self._processor:
                self._processor.stop(timeout=timeout)
                
                # Check if processor stopped cleanly
                if self._processor.is_running():
                    logger.warning("OutboxProcessor did not stop within timeout")
                    success = False
            
            with self._lock:
                self._status = LifecycleStatus.STOPPED
                self._processor = None
            
            logger.info("OutboxLifecycleManager: Processor stopped")
            
            if self._on_stop:
                try:
                    self._on_stop()
                except Exception as e:
                    logger.warning(f"on_stop callback error: {e}")
            
        except Exception as e:
            with self._lock:
                self._status = LifecycleStatus.ERROR
                self._error = str(e)
            
            logger.error(f"Error during shutdown: {e}")
            success = False
        
        return success
    
    def restart(self) -> bool:
        """
        Restart the outbox processor.
        
        Returns:
            True if restarted successfully
        """
        logger.info("OutboxLifecycleManager: Restarting processor...")
        
        # Stop if running
        if self._status in (LifecycleStatus.RUNNING, LifecycleStatus.STARTING):
            self.shutdown()
        
        # Start fresh
        return self.start()
    
    def get_health(self) -> HealthStatus:
        """
        Get health status for monitoring.
        
        Returns:
            HealthStatus with current processor state
        """
        with self._lock:
            status = self._status
            error = self._error
            started_at = self._started_at
            processor = self._processor
        
        # Calculate uptime
        uptime = 0.0
        if started_at and status == LifecycleStatus.RUNNING:
            uptime = (datetime.now() - started_at).total_seconds()
        
        # Get processor stats
        events_processed = 0
        events_failed = 0
        details = {}
        
        if processor:
            stats = processor.get_stats()
            events_processed = stats.get("events_processed", 0)
            events_failed = stats.get("events_failed", 0)
            details = processor.get_extended_stats()
        
        # Determine health status string
        if status == LifecycleStatus.RUNNING:
            health_status = "healthy"
        elif status == LifecycleStatus.STOPPED:
            health_status = "stopped"
        elif status == LifecycleStatus.ERROR:
            health_status = "unhealthy"
        else:
            health_status = "transitioning"
        
        return HealthStatus(
            status=health_status,
            lifecycle_status=status,
            is_running=status == LifecycleStatus.RUNNING,
            uptime_seconds=uptime,
            events_processed=events_processed,
            events_failed=events_failed,
            last_check_time=datetime.now(),
            error_message=error,
            details=details,
        )
    
    def is_healthy(self) -> bool:
        """Quick health check."""
        return self._status == LifecycleStatus.RUNNING
    
    def register_signal_handlers(self) -> None:
        """
        Register signal handlers for graceful shutdown.
        
        Handles SIGTERM and SIGINT for graceful shutdown.
        """
        def signal_handler(signum, frame):
            sig_name = signal.Signals(signum).name
            logger.info(f"Received {sig_name}, initiating graceful shutdown...")
            
            # Run shutdown in thread to avoid blocking signal handler
            shutdown_thread = threading.Thread(target=self.shutdown)
            shutdown_thread.start()
            shutdown_thread.join(timeout=self._shutdown_timeout)
            
            # Call original handler if exists
            if signum == signal.SIGTERM and self._original_sigterm:
                if callable(self._original_sigterm):
                    self._original_sigterm(signum, frame)
            elif signum == signal.SIGINT and self._original_sigint:
                if callable(self._original_sigint):
                    self._original_sigint(signum, frame)
        
        # Store original handlers
        self._original_sigterm = signal.getsignal(signal.SIGTERM)
        self._original_sigint = signal.getsignal(signal.SIGINT)
        
        # Register new handlers
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        
        logger.info("OutboxLifecycleManager: Signal handlers registered")
    
    def _atexit_cleanup(self) -> None:
        """Cleanup handler for atexit."""
        if self._status == LifecycleStatus.RUNNING:
            logger.info("OutboxLifecycleManager: atexit cleanup triggered")
            self.shutdown(timeout=5.0)  # Short timeout for atexit
    
    @property
    def processor(self) -> Optional[OutboxProcessor]:
        """Get underlying processor (for advanced use)."""
        return self._processor
    
    @property
    def status(self) -> LifecycleStatus:
        """Get current lifecycle status."""
        return self._status
    
    def __enter__(self) -> "OutboxLifecycleManager":
        """Context manager entry - start processor."""
        self.start()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - shutdown processor."""
        self.shutdown()
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Factory Functions
# ═══════════════════════════════════════════════════════════════════════════════


def create_managed_processor(
    wtb_db_url: str,
    auto_start: bool = True,
    register_signals: bool = True,
    **kwargs,
) -> OutboxLifecycleManager:
    """
    Create a managed OutboxProcessor with lifecycle management.
    
    This is the recommended way to create an OutboxProcessor for production.
    
    Args:
        wtb_db_url: WTB database connection URL
        auto_start: If True, start processor immediately
        register_signals: If True, register SIGTERM/SIGINT handlers
        **kwargs: Additional arguments for OutboxLifecycleManager
        
    Returns:
        OutboxLifecycleManager with running processor
        
    Usage:
        manager = create_managed_processor("sqlite:///data/wtb.db")
        # Processor is now running and will shutdown on SIGTERM
    """
    return OutboxLifecycleManager(
        wtb_db_url=wtb_db_url,
        auto_start=auto_start,
        register_signals=register_signals,
        **kwargs,
    )
