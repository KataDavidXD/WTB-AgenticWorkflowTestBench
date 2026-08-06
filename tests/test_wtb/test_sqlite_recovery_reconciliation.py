"""Crash recovery and single-claim tests for durable graphless execution."""

from __future__ import annotations

import copy
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from wtb.application.services.execution_controller import (
    ExecutionController,
    NodeBoundaryClaimConflict,
)
from wtb.domain.interfaces.node_executor import NodeExecutionResult
from wtb.domain.interfaces.state_adapter import CheckpointTrigger
from wtb.domain.models import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow as WorkflowDefinition,
    WorkflowEdge,
    WorkflowNode,
)
from wtb.infrastructure.adapters.sqlite_state_adapter import SqliteStateAdapter
from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork


class _RecordingNodeExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(
        self,
        node: WorkflowNode,
        context: dict[str, object],
    ) -> NodeExecutionResult:
        self.calls.append(node.id)
        return NodeExecutionResult(
            success=True,
            output={f"{node.id}_runs": self.calls.count(node.id)},
        )


class _HardCrashNodeExecutor(_RecordingNodeExecutor):
    def execute(
        self,
        node: WorkflowNode,
        context: dict[str, object],
    ) -> NodeExecutionResult:
        self.calls.append(node.id)
        raise KeyboardInterrupt("injected hard node interruption")


def _workflow() -> WorkflowDefinition:
    workflow = WorkflowDefinition(id="wf-sqlite-recovery", name="SQLite recovery")
    workflow.add_node(WorkflowNode(id="A", name="A", type="action"))
    workflow.add_node(WorkflowNode(id="B", name="B", type="end"))
    workflow.add_edge(WorkflowEdge(source_id="A", target_id="B"))
    workflow.entry_point = "A"
    return workflow


def _db_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@contextmanager
def _opened_controller(
    db_url: str,
    checkpoint_db: Path,
    recorder: _RecordingNodeExecutor,
) -> Iterator[tuple[ExecutionController, SQLAlchemyUnitOfWork, SqliteStateAdapter]]:
    adapter = SqliteStateAdapter(checkpoint_db)
    try:
        with SQLAlchemyUnitOfWork(db_url) as uow:
            yield (
                ExecutionController(
                    execution_repository=uow.executions,
                    workflow_repository=uow.workflows,
                    state_adapter=adapter,
                    node_executor=recorder,
                    unit_of_work=uow,
                ),
                uow,
                adapter,
            )
    finally:
        adapter.close()


def _create_execution(
    controller: ExecutionController,
    uow: SQLAlchemyUnitOfWork,
    adapter: SqliteStateAdapter,
) -> Execution:
    workflow = _workflow()
    uow.workflows.add(workflow)
    uow.commit()
    return controller.create_execution(
        workflow,
        metadata={
            "state_adapter_backend": "node_sqlite",
            "checkpoint_db_path": str(adapter.storage_path),
        },
    )


def _seed_boundary(
    adapter: SqliteStateAdapter,
    execution: Execution,
    node_id: str,
    status: str,
    *,
    continuation: str | None = None,
    error_message: str = "seeded node failure",
) -> str | None:
    assert adapter.set_current_session(
        execution.session_id or "",
        execution_id=execution.id,
    )
    entry_id = adapter.save_checkpoint(
        copy.deepcopy(execution.state),
        node_id,
        CheckpointTrigger.AUTO,
        name=f"Before: {node_id}",
    )
    assert adapter.mark_node_started(node_id, entry_id)
    if status == "started":
        return None
    if status == "failed":
        assert adapter.mark_node_failed(node_id, error_message) is True
        return None

    exit_state = copy.deepcopy(execution.state)
    exit_state.execution_path.append(node_id)
    exit_state.node_results[node_id] = {"seeded": True}
    exit_state.current_node_id = continuation
    exit_id = adapter.save_checkpoint(
        exit_state,
        node_id,
        CheckpointTrigger.AUTO,
        name=f"After: {node_id}",
    )
    assert adapter.mark_node_completed(node_id, exit_id) is True
    return exit_id


@pytest.mark.parametrize("failure_point", ["update", "commit"])
def test_completed_nodes_are_not_replayed_after_execution_persist_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    db_url = _db_url(tmp_path / "executions.db")
    checkpoint_db = tmp_path / "checkpoints.db"
    recorder = _RecordingNodeExecutor()

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        uow,
        adapter,
    ):
        execution = _create_execution(controller, uow, adapter)
        if failure_point == "update":
            def fail_update(_execution: Execution) -> None:
                uow.rollback()
                raise RuntimeError("injected final execution update failure")

            monkeypatch.setattr(uow.executions, "update", fail_update)
        else:
            def fail_commit() -> None:
                uow.rollback()
                raise RuntimeError("injected final execution commit failure")

            monkeypatch.setattr(uow, "commit", fail_commit)

        with pytest.raises(RuntimeError, match="injected final execution"):
            controller.run(execution.id)
        assert recorder.calls == ["A", "B"]
        execution_id = execution.id

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        uow,
        _adapter,
    ):
        stored_before = uow.executions.get(execution_id)
        assert stored_before is not None
        assert stored_before.status is ExecutionStatus.PENDING
        recovered = controller.run(execution_id)
        assert recovered.status is ExecutionStatus.COMPLETED
        assert recovered.state.execution_path == ["A", "B"]
        assert recorder.calls == ["A", "B"]


def test_persisted_running_continues_from_completed_head(tmp_path: Path) -> None:
    db_url = _db_url(tmp_path / "running-executions.db")
    checkpoint_db = tmp_path / "running-checkpoints.db"
    recorder = _RecordingNodeExecutor()

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        uow,
        adapter,
    ):
        execution = _create_execution(controller, uow, adapter)
        _seed_boundary(adapter, execution, "A", "completed", continuation="B")
        execution.status = ExecutionStatus.RUNNING
        uow.executions.update(execution)
        uow.commit()
        execution_id = execution.id

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        _uow,
        _adapter,
    ):
        recovered = controller.run(execution_id)
        assert recovered.status is ExecutionStatus.COMPLETED
        assert recovered.state.execution_path == ["A", "B"]
        assert recovered.state.node_results["A"] == {"seeded": True}
        assert recorder.calls == ["B"]


def test_recovery_commit_failure_propagates_before_successor_executes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_url = _db_url(tmp_path / "reconcile-commit-executions.db")
    checkpoint_db = tmp_path / "reconcile-commit-checkpoints.db"
    recorder = _RecordingNodeExecutor()

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        uow,
        adapter,
    ):
        execution = _create_execution(controller, uow, adapter)
        _seed_boundary(adapter, execution, "A", "completed", continuation="B")
        execution_id = execution.id

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        uow,
        _adapter,
    ):
        def fail_commit() -> None:
            uow.rollback()
            raise RuntimeError("injected recovery commit failure")

        monkeypatch.setattr(uow, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="injected recovery commit failure"):
            controller.run(execution_id)
        assert recorder.calls == []


def test_hard_crash_after_recovery_commit_is_not_treated_as_explicit_pause(
    tmp_path: Path,
) -> None:
    db_url = _db_url(tmp_path / "second-crash-executions.db")
    checkpoint_db = tmp_path / "second-crash-checkpoints.db"
    seed_recorder = _RecordingNodeExecutor()

    with _opened_controller(db_url, checkpoint_db, seed_recorder) as (
        controller,
        uow,
        adapter,
    ):
        execution = _create_execution(controller, uow, adapter)
        _seed_boundary(adapter, execution, "A", "completed", continuation="B")
        execution_id = execution.id

    crash_executor = _HardCrashNodeExecutor()
    with _opened_controller(db_url, checkpoint_db, crash_executor) as (
        controller,
        _uow,
        _adapter,
    ):
        with pytest.raises(KeyboardInterrupt, match="hard node interruption"):
            controller.run(execution_id)
        assert crash_executor.calls == ["B"]

    retry_recorder = _RecordingNodeExecutor()
    with _opened_controller(db_url, checkpoint_db, retry_recorder) as (
        controller,
        _uow,
        _adapter,
    ):
        recovered = controller.run(execution_id)
        assert recovered.status is ExecutionStatus.FAILED
        assert recovered.error_node_id == "B"
        assert recovered.metadata["recovery_reason"] == "node_outcome_unknown"
        assert retry_recorder.calls == []


def test_latest_started_boundary_fails_closed(tmp_path: Path) -> None:
    db_url = _db_url(tmp_path / "started-executions.db")
    checkpoint_db = tmp_path / "started-checkpoints.db"
    recorder = _RecordingNodeExecutor()

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        uow,
        adapter,
    ):
        execution = _create_execution(controller, uow, adapter)
        _seed_boundary(adapter, execution, "A", "completed", continuation="B")
        _seed_boundary(adapter, execution, "B", "started")
        execution_id = execution.id

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        _uow,
        _adapter,
    ):
        recovered = controller.run(execution_id)
        assert recovered.status is ExecutionStatus.FAILED
        assert recovered.error_node_id == "B"
        assert recovered.metadata["recovery_required"] is True
        assert recovered.metadata["recovery_reason"] == "node_outcome_unknown"
        assert recorder.calls == []


def test_latest_failed_boundary_materializes_failed(tmp_path: Path) -> None:
    db_url = _db_url(tmp_path / "failed-executions.db")
    checkpoint_db = tmp_path / "failed-checkpoints.db"
    recorder = _RecordingNodeExecutor()

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        uow,
        adapter,
    ):
        execution = _create_execution(controller, uow, adapter)
        _seed_boundary(
            adapter,
            execution,
            "A",
            "failed",
            error_message="durable node failure",
        )
        execution_id = execution.id

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        _uow,
        _adapter,
    ):
        recovered = controller.run(execution_id)
        assert recovered.status is ExecutionStatus.FAILED
        assert recovered.error_node_id == "A"
        assert recovered.error_message == "durable node failure"
        assert recovered.metadata.get("recovery_required") is not True
        assert recorder.calls == []


def test_paused_execution_is_never_fast_forwarded(tmp_path: Path) -> None:
    db_url = _db_url(tmp_path / "paused-executions.db")
    checkpoint_db = tmp_path / "paused-checkpoints.db"
    recorder = _RecordingNodeExecutor()

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        uow,
        adapter,
    ):
        execution = _create_execution(controller, uow, adapter)
        _seed_boundary(adapter, execution, "A", "completed", continuation="B")
        execution.status = ExecutionStatus.PAUSED
        execution.state = ExecutionState(current_node_id="A")
        resume_token = "explicit-replay-token"
        execution.metadata["node_resume_claim_token"] = resume_token
        assert adapter.prepare_resume(resume_token) is True
        uow.executions.update(execution)
        uow.commit()
        execution_id = execution.id

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        _uow,
        _adapter,
    ):
        resumed = controller.resume(execution_id)
        assert resumed.status is ExecutionStatus.COMPLETED
        assert resumed.state.execution_path == ["A", "B"]
        assert recorder.calls == ["A", "B"]


def test_completed_head_requires_owned_exit_checkpoint(tmp_path: Path) -> None:
    db_url = _db_url(tmp_path / "missing-executions.db")
    checkpoint_db = tmp_path / "missing-checkpoints.db"
    recorder = _RecordingNodeExecutor()

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        uow,
        adapter,
    ):
        execution = _create_execution(controller, uow, adapter)
        _seed_boundary(adapter, execution, "A", "completed", continuation="B")
        execution_id = execution.id

    with sqlite3.connect(checkpoint_db) as connection:
        connection.execute(
            "UPDATE wtb_node_state_boundaries "
            "SET exit_checkpoint_id = 'missing-checkpoint' WHERE node_id = 'A'"
        )
        connection.commit()

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        _uow,
        _adapter,
    ):
        with pytest.raises(RuntimeError, match="valid owned exit checkpoint"):
            controller.run(execution_id)
        assert recorder.calls == []


def test_recovery_rejects_mismatched_explicit_path(tmp_path: Path) -> None:
    db_url = _db_url(tmp_path / "mismatch-executions.db")
    checkpoint_db = tmp_path / "mismatch-checkpoints.db"
    recorder = _RecordingNodeExecutor()

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        uow,
        adapter,
    ):
        execution = _create_execution(controller, uow, adapter)
        _seed_boundary(adapter, execution, "A", "started")
        execution.metadata["checkpoint_db_path"] = str(tmp_path / "wrong.db")
        uow.executions.update(execution)
        uow.commit()
        execution_id = execution.id

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        _uow,
        _adapter,
    ):
        with pytest.raises(RuntimeError, match="does not match"):
            controller.run(execution_id)
        assert recorder.calls == []


def test_two_connections_cannot_claim_same_open_node(tmp_path: Path) -> None:
    checkpoint_db = tmp_path / "claim-checkpoints.db"
    initial = ExecutionState(current_node_id="A")
    first = SqliteStateAdapter(checkpoint_db)
    second = SqliteStateAdapter(checkpoint_db)
    try:
        session_id = first.initialize_session("exec-claim", initial)
        assert session_id
        entry_id = first.save_checkpoint(
            initial,
            "A",
            CheckpointTrigger.AUTO,
            name="Before: A",
        )
        assert second.set_current_session(session_id, execution_id="exec-claim")
        first_claim = first.mark_node_started("A", entry_id)
        second_claim = second.mark_node_started("A", entry_id)
        assert isinstance(first_claim, str)
        assert second_claim is False
        boundaries = second.get_node_boundaries(session_id)
        assert len(boundaries) == 1
        assert boundaries[0].id == first_claim
    finally:
        first.close()
        second.close()


def test_stale_claim_is_rejected_after_competing_boundary_completed(
    tmp_path: Path,
) -> None:
    checkpoint_db = tmp_path / "stale-claim-checkpoints.db"
    initial = ExecutionState(current_node_id="A")
    winner = SqliteStateAdapter(checkpoint_db)
    stale = SqliteStateAdapter(checkpoint_db)
    try:
        session_id = winner.initialize_session("exec-stale-claim", initial)
        assert session_id
        assert stale.set_current_session(
            session_id,
            execution_id="exec-stale-claim",
        )
        stale_entry = stale.save_checkpoint(
            initial,
            "A",
            CheckpointTrigger.AUTO,
            name="Stale before: A",
        )
        winner_entry = winner.save_checkpoint(
            initial,
            "A",
            CheckpointTrigger.AUTO,
            name="Winner before: A",
        )
        assert winner.mark_node_started(
            "A",
            winner_entry,
            expected_predecessor_checkpoint_id=None,
            enforce_predecessor=True,
        )
        exit_state = copy.deepcopy(initial)
        exit_state.current_node_id = "B"
        winner_exit = winner.save_checkpoint(
            exit_state,
            "A",
            CheckpointTrigger.AUTO,
            name="Winner after: A",
        )
        assert winner.mark_node_completed("A", winner_exit) is True

        assert stale.mark_node_started(
            "A",
            stale_entry,
            expected_predecessor_checkpoint_id=None,
            enforce_predecessor=True,
        ) is False

        successor_entry = winner.save_checkpoint(
            exit_state,
            "B",
            CheckpointTrigger.AUTO,
            name="Before: B",
        )
        assert winner.mark_node_started(
            "B",
            successor_entry,
            expected_predecessor_checkpoint_id=winner_exit,
            enforce_predecessor=True,
        )
    finally:
        winner.close()
        stale.close()


def test_controller_requires_claim_before_node_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_url = _db_url(tmp_path / "claim-executions.db")
    checkpoint_db = tmp_path / "claim-controller-checkpoints.db"
    recorder = _RecordingNodeExecutor()

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        uow,
        adapter,
    ):
        execution = _create_execution(controller, uow, adapter)
        monkeypatch.setattr(
            adapter,
            "mark_node_started",
            lambda _node_id, _checkpoint_id, **_kwargs: False,
        )
        with pytest.raises(NodeBoundaryClaimConflict, match="claim"):
            controller.run(execution.id)
        uow.rollback()
        stored = uow.executions.get(execution.id)
        assert stored is not None
        assert stored.status is ExecutionStatus.PENDING
        assert stored.checkpoint_id is None
        assert recorder.calls == []


def test_stale_paused_controller_cannot_reconsume_resume_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_url = _db_url(tmp_path / "paused-race-executions.db")
    checkpoint_db = tmp_path / "paused-race-checkpoints.db"
    recorder = _RecordingNodeExecutor()

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        uow,
        adapter,
    ):
        execution = _create_execution(controller, uow, adapter)
        completed = controller.run(execution.id)
        before_a = next(
            checkpoint.id
            for checkpoint in adapter.get_checkpoints(completed.session_id or "")
            if checkpoint.name == "Before: A"
        )
        paused = controller.rollback(completed.id, before_a)
        assert paused.status is ExecutionStatus.PAUSED
        execution_id = paused.id

    with SQLAlchemyUnitOfWork(db_url) as snapshot_uow:
        stale_paused = snapshot_uow.executions.get(execution_id)
        assert stale_paused is not None
        assert stale_paused.status is ExecutionStatus.PAUSED

    recorder.calls.clear()
    with _opened_controller(db_url, checkpoint_db, recorder) as (
        winner,
        _winner_uow,
        _winner_adapter,
    ):
        resumed = winner.resume(execution_id)
        assert resumed.status is ExecutionStatus.COMPLETED
        assert recorder.calls == ["A", "B"]

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        stale,
        stale_uow,
        _stale_adapter,
    ):
        monkeypatch.setattr(
            stale_uow.executions,
            "get",
            lambda _execution_id: copy.deepcopy(stale_paused),
        )
        with pytest.raises(NodeBoundaryClaimConflict, match="claim"):
            stale.resume(execution_id)
        stale_uow.rollback()
        assert recorder.calls == ["A", "B"]

    with SQLAlchemyUnitOfWork(db_url) as verification_uow:
        stored = verification_uow.executions.get(execution_id)
        assert stored is not None
        assert stored.status is ExecutionStatus.COMPLETED


def test_stale_paused_controller_is_rejected_when_winner_repauses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_url = _db_url(tmp_path / "repaused-race-executions.db")
    checkpoint_db = tmp_path / "repaused-race-checkpoints.db"
    recorder = _RecordingNodeExecutor()

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        uow,
        adapter,
    ):
        workflow = _workflow()
        uow.workflows.add(workflow)
        uow.commit()
        execution = controller.create_execution(
            workflow,
            breakpoints=["A"],
            metadata={
                "state_adapter_backend": "node_sqlite",
                "checkpoint_db_path": str(adapter.storage_path),
            },
        )
        paused = controller.run(execution.id)
        assert paused.status is ExecutionStatus.PAUSED
        paused.breakpoints = ["A"]
        uow.executions.update(paused)
        uow.commit()
        execution_id = paused.id

    with SQLAlchemyUnitOfWork(db_url) as snapshot_uow:
        stale_paused = snapshot_uow.executions.get(execution_id)
        assert stale_paused is not None

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        winner,
        _winner_uow,
        _winner_adapter,
    ):
        repaused = winner.resume(execution_id)
        assert repaused.status is ExecutionStatus.PAUSED
        assert recorder.calls == []

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        stale,
        stale_uow,
        _stale_adapter,
    ):
        monkeypatch.setattr(
            stale_uow.executions,
            "get",
            lambda _execution_id: copy.deepcopy(stale_paused),
        )
        with pytest.raises(NodeBoundaryClaimConflict, match="claim"):
            stale.resume(execution_id)
        stale_uow.rollback()
        assert recorder.calls == []


def test_new_rollback_token_invalidates_unconsumed_stale_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_url = _db_url(tmp_path / "rollback-token-executions.db")
    checkpoint_db = tmp_path / "rollback-token-checkpoints.db"
    recorder = _RecordingNodeExecutor()

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        uow,
        adapter,
    ):
        execution = _create_execution(controller, uow, adapter)
        completed = controller.run(execution.id)
        checkpoints = adapter.get_checkpoints(completed.session_id or "")
        before_a = next(cp.id for cp in checkpoints if cp.name == "Before: A")
        after_a = next(cp.id for cp in checkpoints if cp.name == "After: A")

        first_pause = controller.rollback(completed.id, before_a)
        stale_paused = copy.deepcopy(first_pause)
        first_token = stale_paused.metadata.get("node_resume_claim_token")
        assert isinstance(first_token, str) and first_token

        second_pause = controller.rollback(completed.id, after_a)
        second_token = second_pause.metadata.get("node_resume_claim_token")
        assert isinstance(second_token, str) and second_token != first_token
        execution_id = second_pause.id

    recorder.calls.clear()
    with _opened_controller(db_url, checkpoint_db, recorder) as (
        winner,
        _winner_uow,
        _winner_adapter,
    ):
        resumed = winner.resume(execution_id)
        assert resumed.status is ExecutionStatus.COMPLETED
        assert recorder.calls == ["B"]

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        stale,
        stale_uow,
        _stale_adapter,
    ):
        monkeypatch.setattr(
            stale_uow.executions,
            "get",
            lambda _execution_id: copy.deepcopy(stale_paused),
        )
        with pytest.raises(NodeBoundaryClaimConflict, match="claim"):
            stale.resume(execution_id)
        stale_uow.rollback()
        assert recorder.calls == ["B"]


def test_paused_execution_without_durable_resume_token_fails_closed(
    tmp_path: Path,
) -> None:
    db_url = _db_url(tmp_path / "missing-token-executions.db")
    checkpoint_db = tmp_path / "missing-token-checkpoints.db"
    recorder = _RecordingNodeExecutor()

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        uow,
        adapter,
    ):
        execution = _create_execution(controller, uow, adapter)
        execution.status = ExecutionStatus.PAUSED
        uow.executions.update(execution)
        uow.commit()
        execution_id = execution.id

    with _opened_controller(db_url, checkpoint_db, recorder) as (
        controller,
        _uow,
        _adapter,
    ):
        with pytest.raises(RuntimeError, match="durable resume token"):
            controller.resume(execution_id)
        assert recorder.calls == []
