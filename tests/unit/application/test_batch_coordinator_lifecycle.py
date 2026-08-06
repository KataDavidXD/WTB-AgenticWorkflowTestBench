"""Resource-ownership tests for BatchExecutionCoordinator."""

from unittest.mock import MagicMock, patch

from wtb.application.factories import (
    BatchCoordinatorFactory,
    ExecutionControllerFactory,
)
from wtb.application.services.batch_execution_coordinator import (
    BatchExecutionCoordinator,
)
from wtb.config import WTBConfig


def _coordinator(adapter, tracker, **ownership):
    return BatchExecutionCoordinator(
        uow_factory=MagicMock(),
        state_adapter=adapter,
        file_tracking=tracker,
        **ownership,
    )


def test_coordinator_borrows_injected_resources_by_default():
    adapter = MagicMock()
    tracker = MagicMock()
    coordinator = _coordinator(adapter, tracker)

    coordinator.close()

    adapter.close.assert_not_called()
    tracker.close.assert_not_called()


def test_coordinator_closes_owned_resources_once_and_deduplicates_aliases():
    shared_resource = MagicMock()
    coordinator = _coordinator(
        shared_resource,
        shared_resource,
        owns_state_adapter=True,
        owns_file_tracking=True,
    )

    coordinator.close()
    coordinator.close()

    shared_resource.close.assert_called_once_with()


def test_default_factory_owns_adapter_and_file_tracker_it_creates():
    config = WTBConfig.for_testing()
    config.file_tracking_config = MagicMock(
        enabled=True,
        postgres_url=None,
        storage_path="unused",
    )
    adapter = MagicMock()
    tracker = MagicMock()

    with (
        patch.object(
            ExecutionControllerFactory,
            "_create_state_adapter",
            return_value=adapter,
        ),
        patch(
            "wtb.infrastructure.file_tracking.SqliteFileTrackingService",
            return_value=tracker,
        ),
    ):
        coordinator = BatchCoordinatorFactory.create_default(config)

    coordinator.close()

    adapter.close.assert_called_once_with()
    tracker.close.assert_called_once_with()


def test_testing_factory_owns_created_adapter():
    coordinator = BatchCoordinatorFactory.create_for_testing()
    try:
        assert coordinator._owns_state_adapter is True
    finally:
        coordinator.close()
