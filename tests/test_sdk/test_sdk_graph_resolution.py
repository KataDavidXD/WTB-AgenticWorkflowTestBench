"""Fail-closed SDK graph resolution for persisted executions."""

from contextlib import contextmanager, ExitStack
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from wtb.domain.models import Execution, ExecutionState, ExecutionStatus
from wtb.sdk import BatchTestResult, WTBTestBench, WorkflowProject


def _bench_with_single_project(*, execution_workflow_id: str):
    graph = object()
    graph_factory = MagicMock(return_value=graph)
    project = WorkflowProject(
        id="project-id",
        name="project-name",
        graph_factory=graph_factory,
    )
    execution = Execution(
        id="persisted-execution",
        workflow_id=execution_workflow_id,
        status=ExecutionStatus.PAUSED,
        state=ExecutionState(workflow_variables={}),
    )

    controller = MagicMock()
    controller._inner = controller
    controller._state_adapter = MagicMock()
    controller.supports_time_travel.return_value = True
    controller.get_status.return_value = execution
    controller.resume.return_value = execution
    controller.rollback.return_value = execution
    controller.fork.return_value = Execution(workflow_id=execution_workflow_id)

    bench = WTBTestBench(
        project_service=MagicMock(),
        variant_service=MagicMock(),
        execution_controller=controller,
    )
    bench._project_cache = {project.name: project}
    return bench, controller, project, graph_factory, graph


def test_run_persists_graph_identity_in_metadata_without_polluting_user_state():
    bench, controller, project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    selected_graph = object()
    project.build_graph = MagicMock(return_value=selected_graph)
    created_execution = Execution(id="created-execution", workflow_id=project.id)
    controller.create_execution.return_value = created_execution
    controller.run.return_value = created_execution
    source_state = {"nested": {"value": 1}}
    variant_config = {"node-a": "fast"}

    result = bench.run(
        project=project.name,
        initial_state=source_state,
        variant_config=variant_config,
        workflow_variant="branching",
    )

    create_kwargs = controller.create_execution.call_args.kwargs
    assert create_kwargs["initial_state"] == {
        **source_state,
        "_variant_config": {"node-a": "fast"},
        "_workflow_variant": "branching",
    }
    assert create_kwargs["initial_state"]["_variant_config"] is not variant_config
    assert create_kwargs["metadata"] == {
        "_variant_config": {"node-a": "fast"},
        "_workflow_variant": "branching",
    }
    assert source_state == {"nested": {"value": 1}}
    project.build_graph.assert_called_once_with(
        variant_config={"node-a": "fast"},
        workflow_variant="branching",
    )
    controller.run.assert_called_once_with(
        created_execution.id,
        graph=selected_graph,
    )
    assert result is created_execution


def test_resolver_prefers_persisted_metadata_graph_identity():
    bench, _controller, project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    rebuilt_graph = object()
    project.build_graph = MagicMock(return_value=rebuilt_graph)
    persisted_execution = Execution(
        id="persisted-variant-execution",
        workflow_id=project.id,
        metadata={
            "_variant_config": {"node-a": "fast"},
            "_workflow_variant": "branching",
        },
        state=ExecutionState(
            workflow_variables={
                "_variant_config": {"node-a": "legacy"},
                "_workflow_variant": "legacy",
            }
        ),
    )

    resolved = bench._resolve_graph_for_execution(
        persisted_execution.id,
        execution=persisted_execution,
    )

    assert resolved is rebuilt_graph
    project.build_graph.assert_called_once_with(
        variant_config={"node-a": "fast"},
        workflow_variant="branching",
    )


def test_resolver_supports_legacy_graph_identity_from_state():
    bench, _controller, project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    rebuilt_graph = object()
    project.build_graph = MagicMock(return_value=rebuilt_graph)
    persisted_execution = Execution(
        id="legacy-variant-execution",
        workflow_id=project.id,
        state=ExecutionState(
            workflow_variables={
                "_variant_config": {"node-a": "fast"},
                "_workflow_variant": "branching",
            }
        ),
    )

    resolved = bench._resolve_graph_for_execution(
        persisted_execution.id,
        execution=persisted_execution,
    )

    assert resolved is rebuilt_graph
    project.build_graph.assert_called_once_with(
        variant_config={"node-a": "fast"},
        workflow_variant="branching",
    )


def _structured_graph(marker):
    pytest.importorskip("langgraph")
    from langgraph.graph import END, StateGraph
    from typing_extensions import TypedDict

    class StructuredState(TypedDict):
        value: int
        route: list[str]

    def step(state):
        return {
            "value": state["value"] + 1,
            "route": [*state["route"], marker],
        }

    graph = StateGraph(StructuredState)
    graph.add_node("step", step)
    graph.add_edge("__start__", "step")
    graph.add_edge("step", END)
    return graph


def test_structured_state_identity_survives_sqlite_reload_and_control(tmp_path):
    data_dir = str(tmp_path / "graph-identity")
    project_id = "structured-project-id"
    project_name = "structured-project"

    first_project = WorkflowProject(
        id=project_id,
        name=project_name,
        graph_factory=lambda: _structured_graph("default"),
    )
    first_project.register_workflow_variant(
        "alternate",
        lambda: _structured_graph("alternate"),
    )
    first_bench = WTBTestBench.create(mode="development", data_dir=data_dir)
    try:
        first_bench.register_project(first_project)
        completed = first_bench.run(
            project=project_name,
            initial_state={"value": 0, "route": []},
            variant_config={"step": "fast"},
            workflow_variant="alternate",
        )
        execution_id = completed.id
        checkpoint_id = completed.checkpoint_id
        assert completed.state.workflow_variables == {
            "value": 1,
            "route": ["alternate"],
        }
        assert checkpoint_id
    finally:
        first_bench.close()

    default_factory = MagicMock(side_effect=lambda: _structured_graph("wrong"))
    alternate_factory = MagicMock(
        side_effect=lambda: _structured_graph("alternate")
    )
    reloaded_project = WorkflowProject(
        id=project_id,
        name=project_name,
        graph_factory=default_factory,
    )
    reloaded_project.register_workflow_variant("alternate", alternate_factory)
    second_bench = WTBTestBench.create(mode="development", data_dir=data_dir)
    second_bench._project_cache = {project_name: reloaded_project}
    try:
        reloaded = second_bench.get_execution(execution_id)
        assert reloaded.metadata["_variant_config"] == {"step": "fast"}
        assert reloaded.metadata["_workflow_variant"] == "alternate"
        assert "_variant_config" not in reloaded.state.workflow_variables
        assert "_workflow_variant" not in reloaded.state.workflow_variables

        forked = second_bench.fork(execution_id, checkpoint_id)
        fork_execution = second_bench.get_execution(forked.fork_execution_id)
        assert fork_execution.metadata["_variant_config"] == {"step": "fast"}
        assert fork_execution.metadata["_workflow_variant"] == "alternate"

        rollback = second_bench.rollback(execution_id, checkpoint_id)
        assert rollback.success is True, rollback.error
        resumed = second_bench.resume(execution_id)
        assert resumed.status == ExecutionStatus.COMPLETED
    finally:
        second_bench.close()

    default_factory.assert_not_called()
    alternate_factory.assert_called_once_with()


@contextmanager
def _patched_actor_local_adapter(temporary_adapter):
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "wtb.application.services.external_storage."
                "resolve_execution_storage_paths",
                return_value=SimpleNamespace(
                    checkpoint_db_path="actor-checkpoints.db"
                ),
            )
        )
        stack.enter_context(
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LANGGRAPH_AVAILABLE",
                True,
            )
        )
        stack.enter_context(
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LangGraphConfig.for_development",
                return_value=object(),
            )
        )
        stack.enter_context(
            patch(
                "wtb.infrastructure.adapters.langgraph_state_adapter."
                "LangGraphStateAdapter",
                return_value=temporary_adapter,
            )
        )
        yield


def _control_execution(execution_id: str, *, actor_local: bool) -> Execution:
    metadata = (
        {
            "actor_id": "actor-a",
            "checkpoint_db_path": "actor-checkpoints.db",
            "state_adapter_backend": "langgraph_sqlite",
        }
        if actor_local
        else {}
    )
    return Execution(
        id=execution_id,
        workflow_id="project-id",
        status=ExecutionStatus.PAUSED,
        state=ExecutionState(workflow_variables={}),
        metadata=metadata,
    )


@pytest.mark.parametrize(
    "operation",
    ["pause", "resume", "rollback", "rollback_to_node", "fork", "get_checkpoints"],
)
def test_actor_local_langgraph_unavailable_fails_before_controller_call(operation):
    bench, controller, project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    actor_execution = _control_execution("actor-execution", actor_local=True)
    if operation == "pause":
        actor_execution.status = ExecutionStatus.RUNNING
    controller.get_status.return_value = actor_execution
    project.build_graph = MagicMock(return_value=object())

    with patch(
        "wtb.infrastructure.adapters.langgraph_state_adapter.LANGGRAPH_AVAILABLE",
        False,
    ):
        if operation == "rollback":
            result = bench.rollback(actor_execution.id, "checkpoint-id")
            assert result.success is False
        elif operation == "rollback_to_node":
            result = bench.rollback_to_node(actor_execution.id, "node-a")
            assert result.success is False
        elif operation in {"fork", "pause", "resume", "get_checkpoints"}:
            with pytest.raises(RuntimeError, match="execution-specific"):
                if operation == "fork":
                    bench.fork(actor_execution.id, "checkpoint-id")
                else:
                    getattr(bench, operation)(actor_execution.id)

    for method_name in (
        "pause",
        "resume",
        "rollback",
        "rollback_to_node",
        "fork",
        "get_checkpoint_history",
    ):
        getattr(controller, method_name).assert_not_called()


def test_actor_local_langgraph_import_failure_fails_closed():
    bench, _controller, _project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    execution = _control_execution("actor-execution", actor_local=True)
    real_import = __import__

    def fail_langgraph_import(name, *args, **kwargs):
        if name == "wtb.infrastructure.adapters.langgraph_state_adapter":
            raise ImportError("langgraph adapter import failed")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fail_langgraph_import):
        with pytest.raises(RuntimeError, match="execution-specific"):
            bench._state_adapter_for_execution(execution, fallback=MagicMock())


@pytest.mark.parametrize("backend", [None, "memory"])
def test_actor_local_missing_or_unknown_backend_fails_before_controller_call(
    backend,
):
    bench, controller, _project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    bench._project_cache = {}
    execution = _control_execution("actor-execution", actor_local=True)
    if backend is None:
        execution.metadata.pop("state_adapter_backend")
    else:
        execution.metadata["state_adapter_backend"] = backend
    controller.get_status.return_value = execution

    with pytest.raises(RuntimeError, match="state.adapter.backend"):
        bench.resume(execution.id)

    controller.resume.assert_not_called()


def test_actor_local_langgraph_constructor_failure_fails_closed():
    bench, _controller, _project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    execution = _control_execution("actor-execution", actor_local=True)

    with patch(
        "wtb.infrastructure.adapters.langgraph_state_adapter.LANGGRAPH_AVAILABLE",
        True,
    ), patch(
        "wtb.infrastructure.adapters.langgraph_state_adapter."
        "LangGraphConfig.for_development",
        return_value=object(),
    ), patch(
        "wtb.infrastructure.adapters.langgraph_state_adapter.LangGraphStateAdapter",
        side_effect=RuntimeError("constructor failed"),
    ):
        with pytest.raises(RuntimeError, match="execution-specific"):
            bench._state_adapter_for_execution(execution, fallback=MagicMock())


def test_actor_local_node_sqlite_constructor_failure_fails_closed():
    bench, _controller, _project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    execution = _control_execution("actor-execution", actor_local=True)
    execution.metadata["state_adapter_backend"] = "node_sqlite"

    with patch(
        "wtb.infrastructure.adapters.sqlite_state_adapter.SqliteStateAdapter",
        side_effect=RuntimeError("constructor failed"),
    ):
        with pytest.raises(RuntimeError, match="execution-specific"):
            bench._state_adapter_for_execution(execution, fallback=MagicMock())


@pytest.mark.parametrize("backend", ["langgraph_sqlite", "node_sqlite"])
def test_actor_local_reuses_only_exact_backend_and_normalized_path(
    backend,
    tmp_path,
):
    bench, _controller, _project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    checkpoint_path = tmp_path / "actor" / "checkpoints.db"
    execution = _control_execution("actor-execution", actor_local=True)
    execution.metadata.update(
        {
            "checkpoint_db_path": str(checkpoint_path),
            "state_adapter_backend": backend,
        }
    )
    fallback = SimpleNamespace(
        state_adapter_backend=backend,
        storage_path=str(checkpoint_path.parent / "." / checkpoint_path.name),
    )

    assert bench._state_adapter_for_execution(execution, fallback) is fallback


def test_actor_local_does_not_reuse_same_path_with_wrong_backend():
    bench, _controller, _project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    execution = _control_execution("actor-execution", actor_local=True)
    fallback = SimpleNamespace(
        state_adapter_backend="node_sqlite",
        storage_path="actor-checkpoints.db",
    )
    temporary_adapter = MagicMock(name="langgraph_actor_adapter")

    with _patched_actor_local_adapter(temporary_adapter):
        assert (
            bench._state_adapter_for_execution(execution, fallback)
            is temporary_adapter
        )


@pytest.mark.parametrize(
    "operation",
    ["pause", "resume", "rollback", "fork", "get_checkpoints"],
)
def test_node_sqlite_actor_control_binds_adapter_without_registered_graph(
    operation,
):
    bench, controller, _project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    bench._project_cache = {}
    base_adapter = MagicMock(name="base_adapter")
    temporary_adapter = MagicMock(name="node_actor_adapter")
    controller._state_adapter = base_adapter
    execution = _control_execution("node-execution", actor_local=True)
    execution.metadata["state_adapter_backend"] = "node_sqlite"
    if operation == "pause":
        execution.status = ExecutionStatus.RUNNING
    controller.get_status.return_value = execution
    forked_execution = Execution(
        id="forked-execution",
        workflow_id=execution.workflow_id,
    )
    adapters_at_controller_call = []

    def record_adapter(result):
        def invoke(*_args, **_kwargs):
            adapters_at_controller_call.append(controller._state_adapter)
            return result

        return invoke

    controller.pause.side_effect = record_adapter(execution)
    controller.resume.side_effect = record_adapter(execution)
    controller.rollback.side_effect = record_adapter(execution)
    controller.fork.side_effect = record_adapter(forked_execution)
    controller.get_checkpoint_history.side_effect = record_adapter([])

    with patch.object(
        bench,
        "_state_adapter_for_execution",
        return_value=temporary_adapter,
    ) as resolve_adapter:
        if operation == "rollback":
            result = bench.rollback(execution.id, "checkpoint-id")
            assert result.success is True, result.error
        elif operation == "fork":
            result = bench.fork(execution.id, "checkpoint-id")
            assert result.fork_execution_id == forked_execution.id
        elif operation == "get_checkpoints":
            assert bench.get_checkpoints(execution.id) == []
        else:
            assert getattr(bench, operation)(execution.id) is execution

    assert adapters_at_controller_call == [temporary_adapter]
    assert controller._state_adapter is base_adapter
    resolve_adapter.assert_called_once_with(execution, fallback=base_adapter)
    temporary_adapter.set_workflow_graph.assert_not_called()
    temporary_adapter.close.assert_called_once_with()


def test_actor_local_adapter_is_restored_before_next_normal_execution():
    bench, controller, project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    base_adapter = MagicMock(name="base_adapter")
    temporary_adapter = MagicMock(name="actor_adapter")
    controller._state_adapter = base_adapter
    controller._file_tracking = None
    controller._uow = None
    bench._owns_execution_resources = True
    actor_execution = _control_execution("actor-execution", actor_local=True)
    normal_execution = _control_execution("normal-execution", actor_local=False)
    executions = {
        actor_execution.id: actor_execution,
        normal_execution.id: normal_execution,
    }
    controller.get_status.side_effect = executions.__getitem__
    controller.resume.side_effect = lambda execution_id, _state: executions[execution_id]
    actor_graph = object()
    normal_graph = object()
    project.build_graph = MagicMock(side_effect=[actor_graph, normal_graph])

    with _patched_actor_local_adapter(temporary_adapter):
        assert bench.resume(actor_execution.id) is actor_execution
        assert controller._state_adapter is base_adapter
        temporary_adapter.set_workflow_graph.assert_called_once_with(
            actor_graph,
            force_recompile=True,
        )
        temporary_adapter.close.assert_called_once_with()

        assert bench.resume(normal_execution.id) is normal_execution

    assert controller._state_adapter is base_adapter
    base_adapter.set_workflow_graph.assert_called_once_with(
        normal_graph,
        force_recompile=True,
    )
    assert controller.resume.call_args_list == [
        call(actor_execution.id, None),
        call(normal_execution.id, None),
    ]

    bench.close()
    base_adapter.close.assert_called_once_with()
    temporary_adapter.close.assert_called_once_with()


def test_actor_local_adapter_is_restored_and_closed_when_resume_raises():
    bench, controller, project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    base_adapter = MagicMock(name="base_adapter")
    temporary_adapter = MagicMock(name="actor_adapter")
    controller._state_adapter = base_adapter
    actor_execution = _control_execution("actor-execution", actor_local=True)
    controller.get_status.return_value = actor_execution
    controller.resume.side_effect = RuntimeError("resume failed")
    project.build_graph = MagicMock(return_value=object())

    with _patched_actor_local_adapter(temporary_adapter):
        with pytest.raises(RuntimeError, match="resume failed"):
            bench.resume(actor_execution.id)

    assert controller._state_adapter is base_adapter
    temporary_adapter.close.assert_called_once_with()
    base_adapter.close.assert_not_called()


def test_actor_local_adapter_is_restored_when_graph_preparation_raises():
    bench, controller, project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    base_adapter = MagicMock(name="base_adapter")
    temporary_adapter = MagicMock(name="actor_adapter")
    temporary_adapter.set_workflow_graph.side_effect = RuntimeError(
        "graph preparation failed"
    )
    controller._state_adapter = base_adapter
    actor_execution = _control_execution("actor-execution", actor_local=True)
    controller.get_status.return_value = actor_execution
    project.build_graph = MagicMock(return_value=object())

    with _patched_actor_local_adapter(temporary_adapter):
        with pytest.raises(RuntimeError, match="graph preparation failed"):
            bench.resume(actor_execution.id)

    assert controller._state_adapter is base_adapter
    temporary_adapter.close.assert_called_once_with()
    base_adapter.close.assert_not_called()
    controller.resume.assert_not_called()


def test_actor_local_pause_uses_temporary_adapter_and_restores_base():
    bench, controller, project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    base_adapter = MagicMock(name="base_adapter")
    temporary_adapter = MagicMock(name="actor_adapter")
    controller._state_adapter = base_adapter
    actor_execution = _control_execution("actor-execution", actor_local=True)
    actor_execution.status = ExecutionStatus.RUNNING
    controller.get_status.return_value = actor_execution
    adapters_at_pause = []

    def pause(_execution_id):
        adapters_at_pause.append(controller._state_adapter)
        return actor_execution

    controller.pause.side_effect = pause
    actor_graph = object()
    project.build_graph = MagicMock(return_value=actor_graph)

    with _patched_actor_local_adapter(temporary_adapter):
        assert bench.pause(actor_execution.id) is actor_execution

    assert adapters_at_pause == [temporary_adapter]
    assert controller._state_adapter is base_adapter
    temporary_adapter.set_workflow_graph.assert_called_once_with(
        actor_graph,
        force_recompile=True,
    )
    temporary_adapter.close.assert_called_once_with()


def test_actor_local_get_checkpoints_uses_temporary_adapter_and_restores_base():
    bench, controller, project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    base_adapter = MagicMock(name="base_adapter")
    temporary_adapter = MagicMock(name="actor_adapter")
    controller._state_adapter = base_adapter
    actor_execution = _control_execution("actor-execution", actor_local=True)
    controller.get_status.return_value = actor_execution
    controller.supports_time_travel.return_value = True
    adapters_at_history = []

    def get_checkpoint_history(_execution_id):
        adapters_at_history.append(controller._state_adapter)
        return []

    controller.get_checkpoint_history.side_effect = get_checkpoint_history
    actor_graph = object()
    project.build_graph = MagicMock(return_value=actor_graph)

    with _patched_actor_local_adapter(temporary_adapter):
        assert bench.get_checkpoints(actor_execution.id) == []

    assert adapters_at_history == [temporary_adapter]
    assert controller._state_adapter is base_adapter
    temporary_adapter.set_workflow_graph.assert_called_once_with(
        actor_graph,
        force_recompile=True,
    )
    temporary_adapter.close.assert_called_once_with()


def test_actor_local_resume_blocks_ordinary_run_until_adapter_is_restored():
    bench, controller, project, _graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="project-id")
    )
    base_adapter = MagicMock(name="base_adapter")
    temporary_adapter = MagicMock(name="actor_adapter")
    controller._state_adapter = base_adapter
    actor_execution = _control_execution("actor-execution", actor_local=True)
    run_execution = Execution(id="run-execution", workflow_id=project.id)
    controller.get_status.return_value = actor_execution
    project.build_graph = MagicMock(side_effect=[object(), object()])

    resume_entered = Event()
    release_resume = Event()
    run_entered = Event()
    run_adapters = []
    errors = []

    def blocking_resume(_execution_id, _modified_state):
        resume_entered.set()
        assert release_resume.wait(timeout=5)
        return actor_execution

    def create_execution(**_kwargs):
        run_adapters.append(controller._state_adapter)
        run_entered.set()
        return run_execution

    controller.resume.side_effect = blocking_resume
    controller.create_execution.side_effect = create_execution
    controller.run.return_value = run_execution

    def capture(callable_):
        try:
            callable_()
        except BaseException as error:
            errors.append(error)

    with _patched_actor_local_adapter(temporary_adapter):
        resume_thread = Thread(
            target=capture,
            args=(lambda: bench.resume(actor_execution.id),),
        )
        resume_thread.start()
        assert resume_entered.wait(timeout=5)

        run_thread = Thread(
            target=capture,
            args=(
                lambda: bench.run(
                    project=project.name,
                    initial_state={"value": 1},
                ),
            ),
        )
        run_thread.start()

        try:
            assert not run_entered.wait(timeout=0.2)
        finally:
            release_resume.set()
            resume_thread.join(timeout=5)
            run_thread.join(timeout=5)

    assert not resume_thread.is_alive()
    assert not run_thread.is_alive()
    assert errors == []
    assert run_adapters == [base_adapter]


def test_resume_rejects_the_only_cached_project_when_workflow_id_differs():
    """A sole cached project is not evidence that it owns an execution."""
    bench, controller, _project, graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="foreign-workflow")
    )

    with pytest.raises(ValueError, match="No registered project matches"):
        bench.resume("persisted-execution")

    graph_factory.assert_not_called()
    controller._state_adapter.set_workflow_graph.assert_not_called()
    controller.resume.assert_not_called()


def test_rollback_rejects_a_foreign_workflow_before_controller_mutation():
    bench, controller, _project, graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="foreign-workflow")
    )

    result = bench.rollback("persisted-execution", "checkpoint-id")

    assert result.success is False
    assert "No registered project matches" in (result.error or "")
    graph_factory.assert_not_called()
    controller.rollback.assert_not_called()


def test_fork_rejects_a_foreign_workflow_before_controller_mutation():
    bench, controller, _project, graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id="foreign-workflow")
    )

    with pytest.raises(ValueError, match="No registered project matches"):
        bench.fork("persisted-execution", "checkpoint-id")

    graph_factory.assert_not_called()
    controller.fork.assert_not_called()


def _batch_control_case(resolution_case: str):
    workflow_id = "foreign-workflow" if resolution_case == "foreign" else "project-id"
    bench, controller, _project, graph_factory, _graph = (
        _bench_with_single_project(execution_workflow_id=workflow_id)
    )
    graph_factories = [graph_factory]

    if resolution_case == "zero":
        bench._project_cache = {}
    elif resolution_case == "ambiguous":
        collision_factory = MagicMock(return_value=object())
        collision = WorkflowProject(
            id="collision-id",
            name="project-id",
            graph_factory=collision_factory,
        )
        bench._project_cache[collision.name] = collision
        graph_factories.append(collision_factory)

    coordinator = MagicMock()
    coordinator.rollback.return_value = controller.get_status.return_value
    coordinator.fork.return_value = Execution(
        id="forked-execution",
        workflow_id=workflow_id,
    )
    bench._batch_coordinator = coordinator
    result = BatchTestResult(
        combination_name="variant-0",
        execution_id="persisted-execution",
        success=True,
        last_checkpoint_id="checkpoint-id",
    )
    return bench, controller, coordinator, graph_factories, result


@pytest.mark.parametrize("operation", ["rollback", "fork"])
@pytest.mark.parametrize("resolution_case", ["foreign", "zero", "ambiguous"])
def test_batch_control_without_explicit_graph_fails_closed(
    operation,
    resolution_case,
):
    bench, _controller, coordinator, graph_factories, result = (
        _batch_control_case(resolution_case)
    )

    outcome = getattr(bench, f"{operation}_batch_result")(result)

    if operation == "rollback":
        assert outcome.success is False
    else:
        assert outcome.fork_execution_id == ""
    assert "No registered project matches" in (outcome.error or "")
    getattr(coordinator, operation).assert_not_called()
    for graph_factory in graph_factories:
        graph_factory.assert_not_called()


@pytest.mark.parametrize("operation", ["rollback", "fork"])
def test_batch_control_with_explicit_graph_bypasses_project_resolution(operation):
    bench, controller, coordinator, graph_factories, result = (
        _batch_control_case("foreign")
    )
    explicit_graph = object()

    outcome = getattr(bench, f"{operation}_batch_result")(
        result,
        graph=explicit_graph,
    )

    if operation == "rollback":
        assert outcome.success is True
    else:
        assert outcome.error is None
        assert outcome.fork_execution_id == "forked-execution"
    getattr(coordinator, operation).assert_called_once()
    assert getattr(coordinator, operation).call_args.kwargs["graph"] is explicit_graph
    controller.get_status.assert_not_called()
    for graph_factory in graph_factories:
        graph_factory.assert_not_called()


@pytest.mark.parametrize("resolution_case", ["foreign", "zero", "ambiguous"])
def test_rollback_to_node_without_matching_project_fails_closed(resolution_case):
    bench, controller, _coordinator, graph_factories, _result = (
        _batch_control_case(resolution_case)
    )

    outcome = bench.rollback_to_node("persisted-execution", "node-a")

    assert outcome.success is False
    assert "No registered project matches" in (outcome.error or "")
    controller.rollback_to_node.assert_not_called()
    for graph_factory in graph_factories:
        graph_factory.assert_not_called()


@pytest.mark.parametrize("workflow_id", ["project-id", "project-name"])
def test_rollback_to_node_resolves_exact_project_id_or_name(workflow_id):
    bench, controller, _project, graph_factory, graph = (
        _bench_with_single_project(execution_workflow_id=workflow_id)
    )
    if workflow_id == "project-name":
        bench._project_cache["unrelated-name"] = WorkflowProject(
            id="unrelated-id",
            name="unrelated-name",
            graph_factory=MagicMock(return_value=object()),
        )
    controller.rollback_to_node.return_value = Execution(
        id="persisted-execution",
        workflow_id=workflow_id,
        checkpoint_id="node-checkpoint",
    )

    outcome = bench.rollback_to_node("persisted-execution", "node-a")

    assert outcome.success is True
    assert outcome.to_checkpoint_id == "node-checkpoint"
    graph_factory.assert_called_once_with()
    controller._state_adapter.set_workflow_graph.assert_called_once_with(
        graph,
        force_recompile=True,
    )
    controller.rollback_to_node.assert_called_once_with(
        "persisted-execution",
        "node-a",
    )


@pytest.mark.parametrize("workflow_id", ["project-id", "project-name"])
def test_resume_resolves_exact_project_id_or_name(workflow_id):
    bench, controller, _project, graph_factory, graph = (
        _bench_with_single_project(execution_workflow_id=workflow_id)
    )
    if workflow_id == "project-name":
        bench._project_cache["unrelated-name"] = WorkflowProject(
            id="unrelated-id",
            name="unrelated-name",
            graph_factory=MagicMock(return_value=object()),
        )

    resumed = bench.resume("persisted-execution")

    assert resumed is controller.resume.return_value
    graph_factory.assert_called_once_with()
    controller._state_adapter.set_workflow_graph.assert_called_once_with(
        graph,
        force_recompile=True,
    )
    controller.resume.assert_called_once_with("persisted-execution", None)
