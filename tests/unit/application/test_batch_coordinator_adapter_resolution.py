"""Fail-closed tests for execution-specific checkpoint adapter resolution."""

from unittest.mock import MagicMock, patch

import pytest

from wtb.application.services.batch_execution_coordinator import (
    BatchExecutionCoordinator,
    StateAdapterResolutionError,
)
from wtb.domain.models.workflow import Execution, ExecutionState, ExecutionStatus


def _execution(metadata):
    return Execution(
        id="exec-resolution",
        workflow_id="workflow-resolution",
        status=ExecutionStatus.PAUSED,
        state=ExecutionState(current_node_id="start"),
        session_id="wtb-exec-resolution",
        metadata=metadata,
    )


def _harness(metadata):
    execution = _execution(metadata)
    uow = MagicMock(name="resolution-uow")
    uow.__enter__.return_value = uow
    uow.__exit__.return_value = None
    uow.executions.get.return_value = execution
    controller_factory = MagicMock(name="controller-factory")
    shared_adapter = MagicMock(name="shared-adapter")
    coordinator = BatchExecutionCoordinator(
        uow_factory=MagicMock(return_value=uow),
        controller_factory=controller_factory,
        state_adapter=shared_adapter,
        file_tracking=None,
    )
    return coordinator, uow, controller_factory, shared_adapter


def _invoke(coordinator, operation):
    if operation == "history":
        return coordinator.get_checkpoints("exec-resolution")
    return coordinator.rollback("exec-resolution", "checkpoint-resolution")


@pytest.mark.parametrize("operation", ["history", "rollback"])
@pytest.mark.parametrize(
    "metadata",
    [
        {"checkpoint_db_path": "actor-checkpoints.db"},
        {"state_adapter_backend": "node_sqlite"},
        {
            "checkpoint_db_path": "actor-checkpoints.db",
            "state_adapter_backend": "unknown_backend",
        },
    ],
)
def test_missing_or_unknown_explicit_backend_never_uses_shared_adapter(
    operation,
    metadata,
):
    coordinator, uow, controller_factory, shared_adapter = _harness(metadata)

    with pytest.raises(StateAdapterResolutionError):
        _invoke(coordinator, operation)

    controller_factory.create.assert_not_called()
    uow.commit.assert_not_called()
    assert uow.rollback.call_count == (1 if operation == "rollback" else 0)
    assert shared_adapter.method_calls == []


@pytest.mark.parametrize("operation", ["history", "rollback"])
def test_node_backend_constructor_failure_never_uses_shared_adapter(operation):
    coordinator, uow, controller_factory, shared_adapter = _harness(
        {
            "checkpoint_db_path": "actor-checkpoints.db",
            "state_adapter_backend": "node_sqlite",
        }
    )

    with patch(
        "wtb.infrastructure.adapters.sqlite_state_adapter.SqliteStateAdapter",
        side_effect=RuntimeError("node store unavailable"),
    ) as adapter_class:
        with pytest.raises(StateAdapterResolutionError, match="node store unavailable"):
            _invoke(coordinator, operation)

    adapter_class.assert_called_once()
    controller_factory.create.assert_not_called()
    uow.commit.assert_not_called()
    assert uow.rollback.call_count == (1 if operation == "rollback" else 0)
    assert shared_adapter.method_calls == []


@pytest.mark.parametrize("operation", ["history", "rollback"])
def test_unavailable_langgraph_backend_never_uses_shared_adapter(operation):
    import wtb.infrastructure.adapters.langgraph_state_adapter as langgraph_module

    coordinator, uow, controller_factory, shared_adapter = _harness(
        {
            "checkpoint_db_path": "actor-checkpoints.db",
            "state_adapter_backend": "langgraph_sqlite",
        }
    )

    with patch.object(langgraph_module, "LANGGRAPH_AVAILABLE", False):
        with pytest.raises(StateAdapterResolutionError, match="unavailable"):
            _invoke(coordinator, operation)

    controller_factory.create.assert_not_called()
    uow.commit.assert_not_called()
    assert uow.rollback.call_count == (1 if operation == "rollback" else 0)
    assert shared_adapter.method_calls == []


def test_exact_backend_and_normalized_path_reuses_shared_adapter(tmp_path):
    checkpoint_path = tmp_path / "actor-checkpoints.db"
    expected_history = [{"checkpoint_id": "checkpoint-resolution"}]
    coordinator, _, controller_factory, shared_adapter = _harness(
        {
            "checkpoint_db_path": str(checkpoint_path),
            "state_adapter_backend": "node_sqlite",
        }
    )
    shared_adapter.state_adapter_backend = "node_sqlite"
    shared_adapter.storage_path = (
        checkpoint_path.parent / "nested" / ".." / checkpoint_path.name
    )
    controller = MagicMock(name="history-controller")
    controller.get_checkpoint_history.return_value = expected_history
    controller_factory.create.return_value = controller

    assert coordinator.get_checkpoints("exec-resolution") == expected_history

    assert (
        controller_factory.create.call_args.kwargs["state_adapter"]
        is shared_adapter
    )
    shared_adapter.close.assert_not_called()


def test_same_path_wrong_backend_constructs_execution_specific_adapter(tmp_path):
    checkpoint_path = tmp_path / "actor-checkpoints.db"
    expected_history = [{"checkpoint_id": "checkpoint-resolution"}]
    coordinator, _, controller_factory, shared_adapter = _harness(
        {
            "checkpoint_db_path": str(checkpoint_path),
            "state_adapter_backend": "node_sqlite",
        }
    )
    shared_adapter.state_adapter_backend = "langgraph_sqlite"
    shared_adapter.storage_path = checkpoint_path
    replacement = MagicMock(name="execution-specific-adapter")
    controller = MagicMock(name="history-controller")
    controller.get_checkpoint_history.return_value = expected_history
    controller_factory.create.return_value = controller

    with patch(
        "wtb.infrastructure.adapters.sqlite_state_adapter.SqliteStateAdapter",
        return_value=replacement,
    ) as adapter_class:
        assert coordinator.get_checkpoints("exec-resolution") == expected_history

    adapter_class.assert_called_once_with(str(checkpoint_path))
    assert (
        controller_factory.create.call_args.kwargs["state_adapter"]
        is replacement
    )
    replacement.close.assert_called_once_with()
    shared_adapter.close.assert_not_called()


def test_checkpoint_query_storage_error_is_not_reported_as_empty_history():
    coordinator, uow, controller_factory, _ = _harness({})
    controller = MagicMock(name="history-controller")
    controller.get_checkpoint_history.side_effect = OSError("checkpoint db unreadable")
    controller_factory.create.return_value = controller

    with pytest.raises(OSError, match="checkpoint db unreadable"):
        coordinator.get_checkpoints("exec-resolution")

    uow.__exit__.assert_called_once_with(None, None, None)
