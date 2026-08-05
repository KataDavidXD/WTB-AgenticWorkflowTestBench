import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from wtb.application.services.batch_execution_coordinator import BatchExecutionCoordinator
from wtb.application.services.external_storage import (
    resolve_actor_local_storage_paths,
    resolve_execution_storage_paths,
)
from wtb.application.services.ray_batch_runner import RayBatchTestRunner
from wtb.domain.models.workflow import Execution, ExecutionState, ExecutionStatus


def _make_execution(execution_id: str, status: ExecutionStatus = ExecutionStatus.PAUSED) -> Execution:
    execution = MagicMock(spec=Execution)
    execution.id = execution_id
    execution.workflow_id = "wf-1"
    execution.status = status
    execution.state = ExecutionState(
        current_node_id="start",
        workflow_variables={},
        execution_path=[],
        node_results={},
    )
    execution.metadata = {}
    execution.session_id = "session-1"
    return execution


def test_resolve_actor_local_storage_paths_is_actor_scoped(tmp_path):
    root = tmp_path / "ray_actors"

    first = resolve_actor_local_storage_paths("actor_1", storage_root=root)
    second = resolve_actor_local_storage_paths("actor_2", storage_root=root)

    assert first.bundle_path == root / "actor_1"
    assert first.checkpoint_db_path == root / "actor_1" / "wtb_checkpoints.db"
    assert first.llm_cache_path == root / "actor_1" / "llm_response_cache.db"
    assert first.checkpoint_db_path.parent == first.llm_cache_path.parent
    assert second.checkpoint_db_path != first.checkpoint_db_path
    assert second.llm_cache_path != first.llm_cache_path


def test_resolve_execution_storage_paths_rehydrates_from_metadata(tmp_path):
    metadata = {
        "actor_id": "actor_9",
        "checkpoint_db_path": str(tmp_path / "ray_actors" / "actor_9" / "wtb_checkpoints.db"),
    }

    paths = resolve_execution_storage_paths(metadata)

    assert paths.actor_id == "actor_9"
    assert paths.checkpoint_db_path == tmp_path / "ray_actors" / "actor_9" / "wtb_checkpoints.db"
    assert paths.llm_cache_path == tmp_path / "ray_actors" / "actor_9" / "llm_response_cache.db"
    assert paths.cache_storage_scope == "actor_local"


def test_build_ray_runtime_env_propagates_llm_and_storage_env_vars(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    monkeypatch.setenv("LLM_API_KEY", "sk-test-llm")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("DEFAULT_LLM", "gpt-4o-mini")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("WTB_LLM_RESPONSE_CACHE_ENABLED", "true")
    monkeypatch.setenv("WTB_LLM_DEBUG", "false")
    monkeypatch.setenv("DEBUG", "false")

    runtime_env = RayBatchTestRunner._build_ray_runtime_env(
        "actor_7",
        {
            "type": "docker",
            "env_path": "/tmp/env",
            "python_path": "/tmp/python",
            "py_executable": "/tmp/python",
            "venv_path": "/tmp/venv",
            "env_vars": {"EXISTING": "1"},
        },
    )

    env_vars = runtime_env["env_vars"]
    assert env_vars["EXISTING"] == "1"
    assert env_vars["WTB_UV_ACTOR_ID"] == "actor_7"
    assert env_vars["WTB_CACHE_ACTOR_ID"] == "actor_7"
    assert env_vars["WTB_CACHE_STORAGE_SCOPE"] == "actor_local"
    assert Path(env_vars["WTB_CHECKPOINT_DB_PATH"]) == Path(env_vars["WTB_CHECKPOINT_DB_PATH"]).parent / "wtb_checkpoints.db"
    assert "actor_7" in env_vars["WTB_CHECKPOINT_DB_PATH"].replace("\\", "/")
    assert Path(env_vars["WTB_LLM_CACHE_PATH"]) == Path(env_vars["WTB_LLM_CACHE_PATH"]).parent / "llm_response_cache.db"
    assert "actor_7" in env_vars["WTB_LLM_CACHE_PATH"].replace("\\", "/")
    assert env_vars["LLM_API_KEY"] == "sk-test-llm"
    assert env_vars["LLM_BASE_URL"] == "https://example.invalid/v1"
    assert env_vars["DEFAULT_LLM"] == "gpt-4o-mini"
    assert env_vars["WTB_LLM_RESPONSE_CACHE_ENABLED"] == "true"


def test_build_ray_runtime_env_uses_accessible_worker_python(tmp_path):
    from ray.runtime_env import RuntimeEnv

    python_path = tmp_path / "venv" / "Scripts" / "python.exe"
    python_path.parent.mkdir(parents=True)
    python_path.touch()

    runtime_env = RayBatchTestRunner._build_ray_runtime_env(
        "actor_local",
        {
            "type": "grpc_uv",
            "env_path": str(tmp_path),
            "python_path": str(python_path),
            "py_executable": str(python_path),
            "venv_path": str(python_path.parent.parent),
            "env_vars": {},
        },
    )

    if "py_executable" in RuntimeEnv.known_fields:
        assert runtime_env["py_executable"] == str(python_path)
        assert runtime_env["env_vars"]["VIRTUAL_ENV"] == str(
            python_path.parent.parent
        )
    else:
        assert "py_executable" not in runtime_env
        assert "VIRTUAL_ENV" not in runtime_env["env_vars"]


def test_build_ray_runtime_env_drops_inaccessible_provider_virtual_env(
    monkeypatch,
):
    monkeypatch.setattr(os.path, "isfile", lambda _path: False)

    runtime_env = RayBatchTestRunner._build_ray_runtime_env(
        "actor_remote",
        {
            "type": "grpc_uv",
            "env_path": "/container/env",
            "python_path": "/container/env/.venv/bin/python",
            "py_executable": "/container/env/.venv/bin/python",
            "venv_path": "/container/env/.venv",
            "env_vars": {
                "CUSTOM": "1",
                "VIRTUAL_ENV": "/container/env/.venv",
            },
        },
    )

    assert runtime_env["env_vars"]["CUSTOM"] == "1"
    assert "VIRTUAL_ENV" not in runtime_env["env_vars"]


def test_batch_execution_coordinator_uses_execution_specific_storage(monkeypatch):
    import wtb.infrastructure.adapters.langgraph_state_adapter as lg_module

    mock_uow = MagicMock()
    mock_uow.outbox = MagicMock()
    mock_uow.executions = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=None)
    mock_uow.commit = MagicMock()
    mock_uow.rollback = MagicMock()

    execution = _make_execution("exec-1", status=ExecutionStatus.PAUSED)
    execution.metadata = {
        "actor_id": "actor_3",
        "checkpoint_db_path": "/tmp/ray_actors/actor_3/wtb_checkpoints.db",
        "llm_cache_path": "/tmp/ray_actors/actor_3/llm_response_cache.db",
        "cache_storage_scope": "actor_local",
    }
    mock_uow.executions.get.return_value = execution

    mock_controller = MagicMock()
    mock_controller.rollback.return_value = execution
    mock_controller.set_deferred_commit = MagicMock()
    mock_controller_factory = MagicMock()
    mock_controller_factory.create.return_value = mock_controller

    captured_env = {}
    resolved_adapter = MagicMock()
    resolved_adapter.close = MagicMock()

    monkeypatch.setattr(lg_module, "LANGGRAPH_AVAILABLE", True)

    def fake_adapter(config):
        captured_env["checkpoint_db_path"] = os.getenv("WTB_CHECKPOINT_DB_PATH")
        captured_env["llm_cache_path"] = os.getenv("WTB_LLM_CACHE_PATH")
        captured_env["actor_id"] = os.getenv("WTB_CACHE_ACTOR_ID")
        captured_env["cache_storage_scope"] = os.getenv("WTB_CACHE_STORAGE_SCOPE")
        captured_env["config_connection_string"] = config.connection_string
        return resolved_adapter

    monkeypatch.setattr(lg_module, "LangGraphStateAdapter", fake_adapter)

    coordinator = BatchExecutionCoordinator(
        uow_factory=MagicMock(return_value=mock_uow),
        controller_factory=mock_controller_factory,
        state_adapter=MagicMock(),
        file_tracking=None,
    )

    result = coordinator.rollback("exec-1", "cp-123")

    assert result == execution
    mock_controller_factory.create.assert_called_once()
    assert mock_controller_factory.create.call_args.kwargs["state_adapter"] is resolved_adapter
    resolved_adapter.close.assert_called_once()
    assert captured_env["checkpoint_db_path"] == str(Path(execution.metadata["checkpoint_db_path"]))
    assert captured_env["llm_cache_path"] == str(Path(execution.metadata["llm_cache_path"]))
    assert captured_env["actor_id"] == execution.metadata["actor_id"]
    assert captured_env["cache_storage_scope"] == "actor_local"
    assert captured_env["config_connection_string"] == str(Path(execution.metadata["checkpoint_db_path"]))
