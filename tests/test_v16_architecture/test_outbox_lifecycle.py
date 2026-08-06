"""
Unit Tests for OutboxLifecycleManager (ISS-006).

Tests lifecycle management for OutboxProcessor including:
- Auto-start on initialization
- Health endpoint
- Graceful shutdown
- Signal handling

Test Coverage:
- LifecycleStatus transitions
- HealthStatus generation
- Context manager protocol
- Callback invocations
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

from wtb.infrastructure.outbox.lifecycle import (
    HealthStatus,
    LifecycleStatus,
    OutboxLifecycleManager,
    create_managed_processor,
)


class TestLifecycleStatus:
    """Tests for LifecycleStatus enum."""
    
    def test_lifecycle_status_values(self):
        """LifecycleStatus should have expected values."""
        assert LifecycleStatus.STOPPED.value == "stopped"
        assert LifecycleStatus.STARTING.value == "starting"
        assert LifecycleStatus.RUNNING.value == "running"
        assert LifecycleStatus.STOPPING.value == "stopping"
        assert LifecycleStatus.ERROR.value == "error"


class TestHealthStatus:
    """Tests for HealthStatus dataclass."""
    
    def test_health_status_to_dict(self):
        """HealthStatus.to_dict() should return proper dictionary."""
        # Arrange
        health = HealthStatus(
            status="healthy",
            lifecycle_status=LifecycleStatus.RUNNING,
            is_running=True,
            uptime_seconds=100.5,
            events_processed=50,
            events_failed=2,
            last_check_time=datetime(2026, 1, 27, 12, 0, 0),
            error_message=None,
            details={"extra": "value"},
        )
        
        # Act
        result = health.to_dict()
        
        # Assert
        assert result["status"] == "healthy"
        assert result["lifecycle_status"] == "running"
        assert result["is_running"] is True
        assert result["uptime_seconds"] == 100.5
        assert result["events_processed"] == 50
        assert result["events_failed"] == 2
        assert result["extra"] == "value"


class TestOutboxLifecycleManagerInit:
    """Tests for OutboxLifecycleManager initialization."""
    
    @patch('wtb.infrastructure.outbox.lifecycle.OutboxProcessor')
    def test_auto_start_creates_and_starts_processor(self, mock_processor_class):
        """auto_start=True should start processor immediately."""
        # Arrange
        mock_processor = MagicMock()
        mock_processor.is_running.return_value = True
        mock_processor.get_stats.return_value = {}
        mock_processor_class.return_value = mock_processor
        
        # Act
        manager = OutboxLifecycleManager(
            wtb_db_url="sqlite:///test.db",
            auto_start=True,
        )
        
        # Assert
        mock_processor_class.assert_called_once()
        mock_processor.start.assert_called_once()
        assert manager.status == LifecycleStatus.RUNNING
        
        # Cleanup
        manager.shutdown()
    
    @patch('wtb.infrastructure.outbox.lifecycle.OutboxProcessor')
    def test_no_auto_start_does_not_start_processor(self, mock_processor_class):
        """auto_start=False should not start processor."""
        # Act
        manager = OutboxLifecycleManager(
            wtb_db_url="sqlite:///test.db",
            auto_start=False,
        )
        
        # Assert
        mock_processor_class.assert_not_called()
        assert manager.status == LifecycleStatus.STOPPED


class TestOutboxLifecycleManagerStart:
    """Tests for OutboxLifecycleManager.start()."""
    
    @patch('wtb.infrastructure.outbox.lifecycle.OutboxProcessor')
    def test_start_transitions_to_running(self, mock_processor_class):
        """start() should transition to RUNNING status."""
        # Arrange
        mock_processor = MagicMock()
        mock_processor.is_running.return_value = True
        mock_processor_class.return_value = mock_processor
        
        manager = OutboxLifecycleManager(
            wtb_db_url="sqlite:///test.db",
        )
        
        # Act
        result = manager.start()
        
        # Assert
        assert result is True
        assert manager.status == LifecycleStatus.RUNNING
        
        # Cleanup
        manager.shutdown()
    
    @patch('wtb.infrastructure.outbox.lifecycle.OutboxProcessor')
    def test_start_returns_false_if_already_running(self, mock_processor_class):
        """start() should return False if already running."""
        # Arrange
        mock_processor = MagicMock()
        mock_processor.is_running.return_value = True
        mock_processor_class.return_value = mock_processor
        
        manager = OutboxLifecycleManager(
            wtb_db_url="sqlite:///test.db",
            auto_start=True,
        )
        
        # Act
        result = manager.start()
        
        # Assert
        assert result is False
        
        # Cleanup
        manager.shutdown()
    
    @patch('wtb.infrastructure.outbox.lifecycle.OutboxProcessor')
    def test_start_calls_on_start_callback(self, mock_processor_class):
        """start() should call on_start callback."""
        # Arrange
        mock_processor = MagicMock()
        mock_processor_class.return_value = mock_processor
        
        on_start_called = []
        
        manager = OutboxLifecycleManager(
            wtb_db_url="sqlite:///test.db",
            on_start=lambda: on_start_called.append(True),
        )
        
        # Act
        manager.start()
        
        # Assert
        assert len(on_start_called) == 1
        
        # Cleanup
        manager.shutdown()


class TestOutboxLifecycleManagerShutdown:
    """Tests for OutboxLifecycleManager.shutdown()."""
    
    @patch('wtb.infrastructure.outbox.lifecycle.OutboxProcessor')
    def test_shutdown_transitions_to_stopped(self, mock_processor_class):
        """shutdown() should transition to STOPPED status."""
        # Arrange
        mock_processor = MagicMock()
        mock_processor.is_running.return_value = False
        mock_processor_class.return_value = mock_processor
        
        manager = OutboxLifecycleManager(
            wtb_db_url="sqlite:///test.db",
            auto_start=True,
        )
        
        # Act
        result = manager.shutdown()
        
        # Assert
        assert result is True
        assert manager.status == LifecycleStatus.STOPPED
        mock_processor.stop.assert_called_once()
    
    @patch('wtb.infrastructure.outbox.lifecycle.OutboxProcessor')
    def test_shutdown_calls_on_stop_callback(self, mock_processor_class):
        """shutdown() should call on_stop callback."""
        # Arrange
        mock_processor = MagicMock()
        mock_processor.is_running.return_value = False
        mock_processor_class.return_value = mock_processor
        
        on_stop_called = []
        
        manager = OutboxLifecycleManager(
            wtb_db_url="sqlite:///test.db",
            auto_start=True,
            on_stop=lambda: on_stop_called.append(True),
        )
        
        # Act
        manager.shutdown()
        
        # Assert
        assert len(on_stop_called) == 1


class TestOutboxLifecycleManagerHealth:
    """Tests for OutboxLifecycleManager.get_health()."""
    
    @patch('wtb.infrastructure.outbox.lifecycle.OutboxProcessor')
    def test_get_health_returns_healthy_when_running(self, mock_processor_class):
        """get_health() should return healthy status when running."""
        # Arrange
        mock_processor = MagicMock()
        mock_processor.get_stats.return_value = {
            "events_processed": 100,
            "events_failed": 5,
        }
        mock_processor.get_extended_stats.return_value = {
            "events_processed": 100,
            "events_failed": 5,
        }
        mock_processor_class.return_value = mock_processor
        
        manager = OutboxLifecycleManager(
            wtb_db_url="sqlite:///test.db",
            auto_start=True,
        )
        
        # Act
        health = manager.get_health()
        
        # Assert
        assert health.status == "healthy"
        assert health.is_running is True
        assert health.events_processed == 100
        assert health.events_failed == 5
        
        # Cleanup
        manager.shutdown()
    
    def test_get_health_returns_stopped_when_not_running(self):
        """get_health() should return stopped status when not running."""
        # Arrange
        manager = OutboxLifecycleManager(
            wtb_db_url="sqlite:///test.db",
            auto_start=False,
        )
        
        # Act
        health = manager.get_health()
        
        # Assert
        assert health.status == "stopped"
        assert health.is_running is False
    
    def test_is_healthy_returns_true_when_running(self):
        """is_healthy() should return True when running."""
        # Arrange
        with patch('wtb.infrastructure.outbox.lifecycle.OutboxProcessor'):
            manager = OutboxLifecycleManager(
                wtb_db_url="sqlite:///test.db",
                auto_start=True,
            )
            
            # Act & Assert
            assert manager.is_healthy() is True
            
            # Cleanup
            manager.shutdown()
    
    def test_is_healthy_returns_false_when_stopped(self):
        """is_healthy() should return False when stopped."""
        # Arrange
        manager = OutboxLifecycleManager(
            wtb_db_url="sqlite:///test.db",
            auto_start=False,
        )
        
        # Act & Assert
        assert manager.is_healthy() is False


class TestOutboxLifecycleManagerContextManager:
    """Tests for OutboxLifecycleManager context manager protocol."""
    
    @patch('wtb.infrastructure.outbox.lifecycle.OutboxProcessor')
    def test_context_manager_starts_and_stops(self, mock_processor_class):
        """Context manager should start on enter and stop on exit."""
        # Arrange
        mock_processor = MagicMock()
        mock_processor.is_running.return_value = False
        mock_processor_class.return_value = mock_processor
        
        # Act
        with OutboxLifecycleManager(wtb_db_url="sqlite:///test.db") as manager:
            assert manager.status == LifecycleStatus.RUNNING
        
        # Assert
        mock_processor.start.assert_called_once()
        mock_processor.stop.assert_called_once()


class TestCreateManagedProcessor:
    """Tests for create_managed_processor factory function."""
    
    @patch('wtb.infrastructure.outbox.lifecycle.OutboxProcessor')
    def test_create_managed_processor_auto_starts(self, mock_processor_class):
        """create_managed_processor() should auto-start by default."""
        # Arrange
        mock_processor = MagicMock()
        mock_processor_class.return_value = mock_processor
        
        # Act
        manager = create_managed_processor(
            wtb_db_url="sqlite:///test.db",
        )
        
        # Assert
        assert manager.status == LifecycleStatus.RUNNING
        
        # Cleanup
        manager.shutdown()
    
    @patch('wtb.infrastructure.outbox.lifecycle.OutboxProcessor')
    def test_create_managed_processor_with_options(self, mock_processor_class):
        """create_managed_processor() should accept additional options."""
        # Arrange
        mock_processor = MagicMock()
        mock_processor_class.return_value = mock_processor
        
        # Act
        manager = create_managed_processor(
            wtb_db_url="sqlite:///test.db",
            poll_interval_seconds=2.0,
            batch_size=100,
        )
        
        # Assert
        mock_processor_class.assert_called_once()
        call_kwargs = mock_processor_class.call_args[1]
        assert call_kwargs['poll_interval_seconds'] == 2.0
        assert call_kwargs['batch_size'] == 100
        
        # Cleanup
        manager.shutdown()


# ═══════════════════════════════════════════════════════════════════════════════
# v1.9 File Cleanup Service Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileCleanupServiceIntegration:
    """
    Tests for v1.9 file cleanup service integration.
    
    Key Architecture:
    - OutboxLifecycleManager accepts file_cleanup_service parameter
    - OutboxLifecycleManager passes file_cleanup_service to OutboxProcessor
    - This follows the Outbox Pattern: processor handles the actual work
    """
    
    @patch('wtb.infrastructure.outbox.lifecycle.OutboxProcessor')
    def test_lifecycle_manager_accepts_file_cleanup_service(self, mock_processor_class):
        """OutboxLifecycleManager should accept file_cleanup_service parameter."""
        # Arrange
        mock_processor = MagicMock()
        mock_processor_class.return_value = mock_processor
        mock_cleanup_service = MagicMock()
        
        # Act
        manager = OutboxLifecycleManager(
            wtb_db_url="sqlite:///test.db",
            file_cleanup_service=mock_cleanup_service,
            auto_start=True,
        )
        
        # Assert - file_cleanup_service should be stored
        assert manager._file_cleanup_service == mock_cleanup_service
        
        # Cleanup
        manager.shutdown()
    
    @patch('wtb.infrastructure.outbox.lifecycle.OutboxProcessor')
    def test_file_cleanup_service_passed_to_processor(self, mock_processor_class):
        """file_cleanup_service should be passed to OutboxProcessor."""
        # Arrange
        mock_processor = MagicMock()
        mock_processor_class.return_value = mock_processor
        mock_cleanup_service = MagicMock()
        mock_tracking_service = MagicMock()
        
        # Act
        manager = OutboxLifecycleManager(
            wtb_db_url="sqlite:///test.db",
            file_tracking_service=mock_tracking_service,
            file_cleanup_service=mock_cleanup_service,
            auto_start=True,
        )
        
        # Assert - verify OutboxProcessor was called with file_cleanup_service
        mock_processor_class.assert_called_once()
        call_kwargs = mock_processor_class.call_args[1]
        assert call_kwargs.get('file_cleanup_service') == mock_cleanup_service
        assert call_kwargs.get('file_tracking_service') == mock_tracking_service
        
        # Cleanup
        manager.shutdown()
    
    @patch('wtb.infrastructure.outbox.lifecycle.OutboxProcessor')
    def test_file_cleanup_service_is_optional(self, mock_processor_class):
        """file_cleanup_service should be optional (opt-in feature)."""
        # Arrange
        mock_processor = MagicMock()
        mock_processor_class.return_value = mock_processor
        
        # Act - create without file_cleanup_service
        manager = OutboxLifecycleManager(
            wtb_db_url="sqlite:///test.db",
            auto_start=True,
        )
        
        # Assert - should work without file_cleanup_service
        assert manager._file_cleanup_service is None
        
        # Verify OutboxProcessor was called with file_cleanup_service=None
        call_kwargs = mock_processor_class.call_args[1]
        assert call_kwargs.get('file_cleanup_service') is None
        
        # Cleanup
        manager.shutdown()
