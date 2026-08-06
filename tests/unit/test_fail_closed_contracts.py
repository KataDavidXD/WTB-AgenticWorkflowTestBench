"""Focused fail-closed lifecycle, fork, and checkpoint-history contracts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from wtb.application.services.execution_controller import ExecutionController
from wtb.domain.models.workflow import Execution, ExecutionState, ExecutionStatus
from wtb.infrastructure.adapters.langgraph_state_adapter import LangGraphStateAdapter
from wtb.sdk.test_bench import WTBTestBench


class _ControllerResources:
    def __init__(self, adapter, file_tracking, uow):
        self._state_adapter = adapter
        self._file_tracking = file_tracking
        self._uow = uow


def test_bench_close_propagates_all_owned_failures_and_remains_retryable():
    adapter = MagicMock()
    file_tracking = MagicMock()
    uow = MagicMock()
    runner = MagicMock()
    coordinator = MagicMock()
    runner.shutdown.side_effect = [RuntimeError("runner close failed"), None]
    coordinator.close.side_effect = [RuntimeError("coordinator close failed"), None]
    adapter.close.side_effect = [RuntimeError("adapter close failed"), None]
    file_tracking.close.side_effect = [RuntimeError("file tracking close failed"), None]
    uow.__exit__.side_effect = [RuntimeError("uow close failed"), None]
    bench = WTBTestBench(
        project_service=object(),
        variant_service=object(),
        execution_controller=_ControllerResources(adapter, file_tracking, uow),
        batch_runner=runner,
        owns_batch_runner=True,
        owns_execution_resources=True,
    )
    bench._batch_coordinator = coordinator

    with pytest.raises(ExceptionGroup) as raised:
        bench.close()

    assert {str(error) for error in raised.value.exceptions} == {
        "runner close failed",
        "coordinator close failed",
        "adapter close failed",
        "file tracking close failed",
        "uow close failed",
    }
    assert bench._closed is False
    assert bench._batch_coordinator is coordinator

    bench.close()

    assert bench._closed is True
    assert bench._batch_coordinator is None
    assert runner.shutdown.call_count == 2
    assert coordinator.close.call_count == 2
    assert adapter.close.call_count == 2
    assert file_tracking.close.call_count == 2
    assert uow.__exit__.call_count == 2


@pytest.mark.parametrize("failure", ["create_fork", "update_state"])
def test_fork_setup_failure_restores_source_without_cleanup_commit(failure):
    source = Execution(
        id="source-execution",
        workflow_id="workflow",
        status=ExecutionStatus.COMPLETED,
        state=ExecutionState(workflow_variables={}),
    )
    source.session_id = "wtb-source-execution"
    execution_repository = MagicMock()
    execution_repository.get.return_value = source
    adapter = MagicMock()
    adapter.set_current_session.return_value = True
    uow = MagicMock()
    controller = ExecutionController(
        execution_repository=execution_repository,
        workflow_repository=MagicMock(),
        state_adapter=adapter,
        unit_of_work=uow,
    )
    controller._fork_impl = MagicMock(
        side_effect=RuntimeError(f"{failure} failed")
    )

    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        controller.fork(source.id, "checkpoint")

    execution_repository.add.assert_not_called()
    execution_repository.delete.assert_not_called()
    uow.commit.assert_not_called()
    adapter.set_current_session.assert_called_once_with(
        source.session_id,
        execution_id=source.id,
    )


class _PartiallyFailingHistoryGraph:
    def get_state_history(self, config):
        yield SimpleNamespace(
            config={"configurable": {"checkpoint_id": "cp-partial"}},
            metadata={"step": 1, "source": "graph", "writes": {}},
            next=(),
            values={"partial": True},
        )
        raise RuntimeError("graph history failed after partial result")


class _FallbackHistorySaver:
    def list(self, config):
        return iter(
            [
                SimpleNamespace(
                    config={
                        "configurable": {"checkpoint_id": "cp-fallback"}
                    },
                    metadata={"step": 3, "source": "fallback"},
                )
            ]
        )


def test_history_discards_partial_graph_results_before_fallback():
    adapter = object.__new__(LangGraphStateAdapter)
    adapter._closed = False
    adapter._current_thread_id = "wtb-history"
    adapter._compiled_graph = _PartiallyFailingHistoryGraph()
    adapter._checkpointer = _FallbackHistorySaver()

    history = adapter.get_checkpoint_history()

    assert [item["checkpoint_id"] for item in history] == ["cp-fallback"]


def test_bench_run_error_identifies_the_created_execution() -> None:
    project_service = MagicMock()
    project_service.get_workflow_by_name.return_value = object()
    controller = MagicMock()
    controller.create_execution.return_value = SimpleNamespace(id="exec-created")
    controller.run.side_effect = RuntimeError("persistence failed after run")
    bench = WTBTestBench(
        project_service=project_service,
        variant_service=MagicMock(),
        execution_controller=controller,
    )
    bench._project_cache["paper-rag"] = SimpleNamespace(
        build_graph=MagicMock(return_value=object())
    )

    with pytest.raises(RuntimeError, match="persistence failed") as raised:
        bench.run(project="paper-rag", initial_state={"item_id": "q0"})

    assert raised.value.wtb_execution_id == "exec-created"
