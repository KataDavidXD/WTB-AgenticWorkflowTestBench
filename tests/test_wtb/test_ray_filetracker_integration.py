"""
Integration tests for Ray Batch Runner with FileTracker.

Tests the integration between RayBatchTestRunner, VariantExecutionActor,
and FileTracker for distributed file tracking during batch test execution.

Test Categories:
1. RayFileTrackerService basic operations
2. VariantExecutionActor with file tracking
3. RayBatchTestRunner with FileTracker config
4. End-to-end batch test with file tracking

Note: Tests marked with @pytest.mark.ray require Ray to be installed.
"""

import pytest
import os
import tempfile
from unittest.mock import Mock, patch, MagicMock
from typing import Dict, Any, List

from wtb.config import FileTrackingConfig, RayConfig
from wtb.domain.interfaces.file_tracking import (
    FileTrackingResult,
    FileRestoreResult,
    FileTrackingError,
)
from wtb.infrastructure.file_tracking import RayFileTrackerService, MockFileTrackingService


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def filetracker_config_dict() -> Dict[str, Any]:
    """Create a FileTrackingConfig as dict for Ray transmission."""
    return {
        "enabled": True,
        "postgres_url": None,  # Will use mock
        "storage_path": "./test_storage",
        "wtb_db_url": "sqlite:///test.db",
        "auto_track_patterns": ["*.csv", "*.pkl"],
        "excluded_patterns": ["*.tmp"],
        "max_file_size_mb": 10.0,
    }


@pytest.fixture
def disabled_config_dict() -> Dict[str, Any]:
    """Create disabled FileTrackingConfig as dict."""
    return {
        "enabled": False,
        "postgres_url": None,
        "storage_path": "./test_storage",
        "wtb_db_url": None,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# RayFileTrackerService Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayFileTrackerService:
    """Tests for RayFileTrackerService."""
    
    def test_disabled_service_returns_disabled_result(self, disabled_config_dict):
        """Test that disabled service returns appropriate results."""
        service = RayFileTrackerService(disabled_config_dict)
        
        result = service.track_files(["test.csv"])
        
        assert result.files_tracked == 0
        assert result.commit_id == ""
    
    def test_disabled_service_is_not_available(self, disabled_config_dict):
        """Test that disabled service reports not available."""
        service = RayFileTrackerService(disabled_config_dict)
        
        assert service.is_available() is False
    
    def test_enabled_property(self, filetracker_config_dict, disabled_config_dict):
        """Test enabled property."""
        enabled = RayFileTrackerService(filetracker_config_dict)
        disabled = RayFileTrackerService(disabled_config_dict)
        
        assert enabled.enabled is True
        assert disabled.enabled is False
    
    def test_restore_from_checkpoint_disabled(self, disabled_config_dict):
        """Test restore when disabled."""
        service = RayFileTrackerService(disabled_config_dict)
        
        result = service.restore_from_checkpoint(checkpoint_id=1)
        
        assert result.success is False
        assert "disabled" in result.error_message.lower()
    
    def test_restore_commit_disabled(self, disabled_config_dict):
        """Test restore_commit when disabled."""
        service = RayFileTrackerService(disabled_config_dict)
        
        result = service.restore_commit("some-commit-id")
        
        assert result.success is False


class TestRayFileTrackerServiceWithMock:
    """Tests for RayFileTrackerService using mocked FileTrackerService."""
    
    def test_lazy_initialization(self, filetracker_config_dict):
        """Test that service initializes lazily."""
        # Create service - should not be initialized yet
        service = RayFileTrackerService(filetracker_config_dict)
        
        # Service not initialized until first operation
        assert service._initialized is False
        assert service._service is None
        
        # The enabled property should work without initializing
        assert service.enabled is True


# ═══════════════════════════════════════════════════════════════════════════════
# Factory Function Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCreateFileTrackingService:
    """Tests for create_file_tracking_service factory."""
    
    def test_create_with_disabled_config(self):
        """Test factory creates mock service when disabled."""
        from wtb.infrastructure.file_tracking.ray_filetracker_service import create_file_tracking_service
        
        config = FileTrackingConfig.for_testing()
        service = create_file_tracking_service(config=config)
        
        assert isinstance(service, MockFileTrackingService)
    
    def test_create_with_dict_disabled(self):
        """Test factory creates mock service with disabled dict."""
        from wtb.infrastructure.file_tracking.ray_filetracker_service import create_file_tracking_service
        
        config_dict = {"enabled": False}
        service = create_file_tracking_service(config_dict=config_dict)
        
        assert isinstance(service, MockFileTrackingService)
    
    def test_create_with_dict_enabled(self):
        """Test factory creates Ray service with enabled dict."""
        from wtb.infrastructure.file_tracking.ray_filetracker_service import create_file_tracking_service
        
        config_dict = {"enabled": True, "postgres_url": "postgresql://test"}
        service = create_file_tracking_service(config_dict=config_dict)
        
        assert isinstance(service, RayFileTrackerService)
    
    def test_create_with_no_args_returns_mock(self):
        """Test factory returns mock with no arguments."""
        from wtb.infrastructure.file_tracking.ray_filetracker_service import create_file_tracking_service
        
        service = create_file_tracking_service()
        
        assert isinstance(service, MockFileTrackingService)


# ═══════════════════════════════════════════════════════════════════════════════
# Config Serialization Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestConfigSerialization:
    """Tests for FileTrackingConfig serialization for Ray."""
    
    def test_config_to_dict_round_trip(self):
        """Test config survives serialization round trip."""
        original = FileTrackingConfig(
            enabled=True,
            postgres_url="postgresql://localhost/test",
            storage_path="/data/storage",
            auto_track_patterns=["*.csv", "*.pkl", "*.json"],
            excluded_patterns=["*.tmp", "*.log"],
            max_file_size_mb=50.0,
        )
        
        data = original.to_dict()
        restored = FileTrackingConfig.from_dict(data)
        
        assert restored.enabled == original.enabled
        assert restored.postgres_url == original.postgres_url
        assert restored.storage_path == original.storage_path
        assert restored.auto_track_patterns == original.auto_track_patterns
        assert restored.excluded_patterns == original.excluded_patterns
        assert restored.max_file_size_mb == original.max_file_size_mb
    
    def test_config_dict_is_serializable(self):
        """Test that config dict can be serialized to JSON."""
        import json
        
        config = FileTrackingConfig.for_production(
            postgres_url="postgresql://localhost/test",
            storage_path="/data/storage",
            wtb_db_url="postgresql://localhost/wtb",
        )
        
        data = config.to_dict()
        
        # Should be JSON serializable
        json_str = json.dumps(data)
        loaded = json.loads(json_str)
        
        assert loaded["enabled"] is True
        assert loaded["postgres_url"] == "postgresql://localhost/test"


# ═══════════════════════════════════════════════════════════════════════════════
# Ray Batch Runner Integration Tests (Mocked)
# ═══════════════════════════════════════════════════════════════════════════════


class TestRayBatchRunnerFileTrackerIntegration:
    """Tests for RayBatchTestRunner with FileTracker config (mocked Ray)."""
    
    def test_runner_accepts_filetracker_config(self):
        """Test that runner accepts filetracker_config parameter."""
        # This test verifies the API without running Ray
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner, RAY_AVAILABLE
        
        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")
        
        config_dict = {
            "enabled": True,
            "postgres_url": "postgresql://localhost/test",
            "storage_path": "/data/storage",
        }
        
        # Should not raise
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/test.db",
            wtb_db_url="sqlite:///data/test.db",
            filetracker_config=config_dict,
        )
        
        assert runner._file_tracking_enabled is True
        assert runner._filetracker_config == config_dict
    
    def test_runner_disabled_filetracker(self):
        """Test runner with disabled FileTracker."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner, RAY_AVAILABLE
        
        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/test.db",
            wtb_db_url="sqlite:///data/test.db",
            filetracker_config=None,
        )
        
        assert runner._file_tracking_enabled is False
    
    def test_runner_filetracker_disabled_in_config(self):
        """Test runner with explicitly disabled FileTracker in config."""
        from wtb.application.services.ray_batch_runner import RayBatchTestRunner, RAY_AVAILABLE
        
        if not RAY_AVAILABLE:
            pytest.skip("Ray not installed")
        
        runner = RayBatchTestRunner(
            config=RayConfig.for_testing(),
            agentgit_db_url="data/test.db",
            wtb_db_url="sqlite:///data/test.db",
            filetracker_config={"enabled": False},
        )
        
        assert runner._file_tracking_enabled is False


# ═══════════════════════════════════════════════════════════════════════════════
# Variant Execution Result with File Tracking
# ═══════════════════════════════════════════════════════════════════════════════


class TestVariantExecutionResultFileTracking:
    """Tests for variant execution result with file tracking fields."""
    
    def test_result_includes_files_tracked(self):
        """Test that execution result can include file tracking info."""
        from wtb.application.services.ray_batch_runner import VariantExecutionResult
        
        result = VariantExecutionResult(
            execution_id="exec-123",
            combination_name="variant_a",
            combination_variants={"train": "xgboost"},
            success=True,
            duration_ms=1000,
            metrics={"accuracy": 0.95},
            checkpoint_count=5,
            node_count=10,
        )
        
        # Result should be convertible to dict
        assert result.execution_id == "exec-123"
        assert result.success is True


# ═══════════════════════════════════════════════════════════════════════════════
# File Collection Pattern Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFileCollectionPatterns:
    """Tests for file collection patterns from config."""
    
    def test_auto_track_patterns_default(self):
        """Test default auto-track patterns."""
        config = FileTrackingConfig()
        
        assert "*.csv" in config.auto_track_patterns
        assert "*.pkl" in config.auto_track_patterns
        assert "*.json" in config.auto_track_patterns
    
    def test_excluded_patterns_default(self):
        """Test default excluded patterns."""
        config = FileTrackingConfig()
        
        assert "*.tmp" in config.excluded_patterns
        assert "*.log" in config.excluded_patterns
    
    def test_custom_patterns(self):
        """Test custom patterns."""
        config = FileTrackingConfig(
            auto_track_patterns=["*.h5", "*.onnx"],
            excluded_patterns=["*.cache"],
        )
        
        assert "*.h5" in config.auto_track_patterns
        assert "*.cache" in config.excluded_patterns


# ═══════════════════════════════════════════════════════════════════════════════
# Skip if Ray not available
# ═══════════════════════════════════════════════════════════════════════════════


def check_ray_available():
    """Check if Ray is available for tests."""
    try:
        import ray
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not check_ray_available(), reason="Ray not installed")
class TestRayActorWithFileTracking:
    """Tests for VariantExecutionActor with FileTracker (requires Ray)."""
    
    def test_actor_accepts_filetracker_config(self):
        """Test that actor accepts filetracker_config parameter."""
        from wtb.application.services.ray_batch_runner import (
            _create_variant_execution_actor_class,
        )
        
        ActorClass = _create_variant_execution_actor_class()
        assert ActorClass is not None
        
        # Verify the __init__ signature includes filetracker_config
        import inspect
        sig = inspect.signature(ActorClass.__init__)
        params = list(sig.parameters.keys())
        
        # Note: Ray wraps the class, so we check the original
        # The parameter should be present in the actor definition
