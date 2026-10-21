from __future__ import annotations

import copy
import threading
from pathlib import Path

from wtb.domain.models.outbox import OutboxEvent, OutboxEventType
from wtb.domain.models.workflow import (
    Execution,
    ExecutionState,
    ExecutionStatus,
    TestWorkflow as Workflow,
)
from wtb.domain.interfaces.workflow_runtime import (
    RuntimeAttemptPlan,
    RuntimeBindingEnvelope,
)
from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork
from wtb.sdk.test_bench import WTBTestBench


def _db_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _binding(execution_id: str, attempt_id: str, input_sha256: str):
    return RuntimeBindingEnvelope(
        exact_session_id=f"runtime-{execution_id}",
        persistence_instance_id="runtime-store-1",
        runtime_config_sha256="c" * 64,
        protocol_version=1,
        schema_version=1,
        input_sha256=input_sha256,
        attempt_id=attempt_id,
    )


def test_sqlite_runtime_attempt_compare_and_swap_has_one_winner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime-attempt-cas.db"
    first = SQLAlchemyUnitOfWork(_db_url(database))
    second = SQLAlchemyUnitOfWork(_db_url(database))
    first.__enter__()
    second.__enter__()
    try:
        workflow = Workflow(id="wf-cas", name="CAS", entry_point="start")
        first.workflows.add(workflow)
        first.executions.add(
            Execution(
                id="exec-cas",
                workflow_id=workflow.id,
                status=ExecutionStatus.PENDING,
                state=ExecutionState(workflow_variables={"prompt": "once"}),
            )
        )
        first.commit()

        first_loaded = first.executions.get("exec-cas")
        second_loaded = second.executions.get("exec-cas")
        assert first_loaded is not None
        assert second_loaded is not None
        expected_metadata = copy.deepcopy(first_loaded.metadata)

        first_loaded.start()
        first_loaded.metadata = {
            "runtime_attempt": {"attempt_id": "attempt-winner"},
            "recovery_required": True,
        }
        second_loaded.start()
        second_loaded.metadata = {
            "runtime_attempt": {"attempt_id": "attempt-loser"},
            "recovery_required": True,
        }

        assert first.executions.compare_and_swap(
            first_loaded,
            expected_status=ExecutionStatus.PENDING,
            expected_metadata=expected_metadata,
        )
        first.outbox.add(
            OutboxEvent.create(
                event_type=OutboxEventType.EXECUTION_DISPATCH_PREPARED,
                aggregate_type="Execution",
                aggregate_id="exec-cas",
                idempotency_key="runtime-dispatch:attempt-winner",
                payload={"attempt_id": "attempt-winner"},
            )
        )
        first.commit()

        assert not second.executions.compare_and_swap(
            second_loaded,
            expected_status=ExecutionStatus.PENDING,
            expected_metadata=expected_metadata,
        )
        second.rollback()

        persisted = second.executions.get("exec-cas")
        assert persisted is not None
        assert persisted.status is ExecutionStatus.RUNNING
        assert persisted.metadata["runtime_attempt"]["attempt_id"] == (
            "attempt-winner"
        )
        assert [event.idempotency_key for event in second.outbox.list_all()] == [
            "runtime-dispatch:attempt-winner"
        ]
    finally:
        second.__exit__(None, None, None)
        first.__exit__(None, None, None)
        second.dispose()
        first.dispose()


def test_two_sqlite_controllers_dispatch_one_runtime_attempt_and_one_outbox(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "integrated-runtime-cas"
    data_dir.mkdir()
    database = data_dir / "wtb.db"
    execution = Execution(
        id="exec-integrated-cas",
        workflow_id="wf-integrated-cas",
        status=ExecutionStatus.PENDING,
        session_id="wtb-exec-integrated-cas",
        state=ExecutionState(workflow_variables={"prompt": "exactly once"}),
    )
    seed = SQLAlchemyUnitOfWork(_db_url(database))
    seed.__enter__()
    try:
        seed.workflows.add(
            Workflow(
                id=execution.workflow_id,
                name="Integrated CAS",
                entry_point="start",
            )
        )
        seed.executions.add(execution)
        seed.commit()
    finally:
        seed.__exit__(None, None, None)
        seed.dispose()

    cas_barrier = threading.Barrier(2)
    invoke_started = threading.Event()
    release_invoke = threading.Event()
    loser_returned = threading.Event()
    invocation_attempts: list[str] = []
    invocation_lock = threading.Lock()

    class BlockingRuntime:
        def __init__(self) -> None:
            self._bound = False

        def is_bound(self) -> bool:
            return self._bound

        def bind(self, workflow, *, force_rebind=True) -> None:
            del workflow, force_rebind
            self._bound = True

        def prepare_attempt(
            self,
            execution_id,
            attempt_id,
            *,
            input_sha256,
            checkpoint_id=None,
        ) -> RuntimeAttemptPlan:
            del checkpoint_id
            return RuntimeAttemptPlan(
                execution_id=execution_id,
                attempt_id=attempt_id,
                session_id=f"runtime-{execution_id}",
                binding=_binding(execution_id, attempt_id, input_sha256),
                metadata={"input_sha256": input_sha256},
            )

        def invoke(
            self,
            input_state,
            *,
            execution_id,
            checkpoint_id=None,
            attempt_id=None,
            binding=None,
        ):
            del execution_id, checkpoint_id
            assert attempt_id is not None
            assert binding is not None
            with invocation_lock:
                invocation_attempts.append(attempt_id)
            invoke_started.set()
            assert release_invoke.wait(timeout=5)
            return {"answer": "winner", "input": input_state}

        def snapshot(self, execution_id, checkpoint_id=None):
            del execution_id, checkpoint_id
            return None

        def history(self, execution_id):
            del execution_id
            return []

        def close(self) -> None:
            return None

    benches = []
    for _ in range(2):
        bench = WTBTestBench.create_with_runtime(
            BlockingRuntime(),
            data_dir=str(data_dir),
        )
        inner = bench._exec_ctrl._inner
        inner._state_adapter.initialize_session(execution.id, execution.state)
        repository = inner._exec_repo
        original_cas = repository.compare_and_swap

        def barrier_cas(
            entity,
            *,
            expected_status,
            expected_metadata,
            _original=original_cas,
        ):
            if expected_status is ExecutionStatus.PENDING:
                cas_barrier.wait(timeout=3)
            return _original(
                entity,
                expected_status=expected_status,
                expected_metadata=expected_metadata,
            )

        repository.compare_and_swap = barrier_cas
        benches.append(bench)

    results: list[Execution] = []
    errors: list[BaseException] = []

    def run(bench: WTBTestBench) -> None:
        try:
            result = bench._exec_ctrl.run(execution.id, workflow=object())
            results.append(result)
            if result.status is ExecutionStatus.RUNNING:
                loser_returned.set()
        except BaseException as error:  # noqa: BLE001 - asserted below
            errors.append(error)

    workers = [threading.Thread(target=run, args=(bench,)) for bench in benches]
    try:
        for worker in workers:
            worker.start()
        assert invoke_started.wait(timeout=3)
        assert loser_returned.wait(timeout=3)

        observer = SQLAlchemyUnitOfWork(_db_url(database))
        observer.__enter__()
        try:
            fenced = observer.executions.get(execution.id)
            assert fenced is not None
            assert fenced.status is ExecutionStatus.RUNNING
            assert fenced.metadata["runtime_attempt"]["dispatch_state"] == (
                "prepared"
            )
            prepared = [
                event
                for event in observer.outbox.list_all(limit=100)
                if event.event_type
                is OutboxEventType.EXECUTION_DISPATCH_PREPARED
            ]
            assert len(prepared) == 1
        finally:
            observer.__exit__(None, None, None)
            observer.dispose()

        with invocation_lock:
            assert len(invocation_attempts) == 1
        release_invoke.set()
        for worker in workers:
            worker.join(timeout=5)

        assert all(not worker.is_alive() for worker in workers)
        assert errors == []
        assert sorted(result.status.value for result in results) == [
            ExecutionStatus.COMPLETED.value,
            ExecutionStatus.RUNNING.value,
        ]

        final_observer = SQLAlchemyUnitOfWork(_db_url(database))
        final_observer.__enter__()
        try:
            terminal = final_observer.executions.get(execution.id)
            assert terminal is not None
            assert terminal.status is ExecutionStatus.COMPLETED
            prepared = [
                event
                for event in final_observer.outbox.list_all(limit=100)
                if event.event_type
                is OutboxEventType.EXECUTION_DISPATCH_PREPARED
            ]
            assert len(prepared) == 1
        finally:
            final_observer.__exit__(None, None, None)
            final_observer.dispose()
    finally:
        release_invoke.set()
        for worker in workers:
            worker.join(timeout=1)
        for bench in benches:
            bench.close()
