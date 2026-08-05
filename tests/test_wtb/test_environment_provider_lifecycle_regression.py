"""Focused lifecycle regressions for the gRPC environment provider."""

import threading
from unittest.mock import MagicMock

import pytest

from wtb.infrastructure.environment.providers import GrpcEnvironmentProvider


def _response(*, status="READY", env_path="/data/envs/workflow_node_version"):
    return MagicMock(
        workflow_id="workflow",
        node_id="node",
        version_id="version",
        env_path=env_path,
        python_version="3.12",
        status=status,
    )


def _remote_identity():
    return {
        "type": "grpc_uv",
        "workflow_id": "workflow",
        "node_id": "node",
        "version_id": "version",
    }


def test_create_response_loss_retains_remote_identity_for_cleanup():
    provider = GrpcEnvironmentProvider("localhost:50051")
    provider._stub = MagicMock()
    provider._stub.CreateEnv.side_effect = TimeoutError("response lost")
    provider._stub.DeleteEnv.return_value = MagicMock(status="DELETED")

    with pytest.raises(TimeoutError, match="response lost"):
        provider.create_environment(
            "lost-response",
            {
                "workflow_id": "workflow",
                "node_id": "node",
                "version_id": "version",
            },
        )

    pending = provider._environments["lost-response"]
    assert pending["type"] == "grpc_uv_pending"
    assert pending["workflow_id"] == "workflow"
    assert pending["node_id"] == "node"
    assert pending["version_id"] == "version"

    provider.cleanup_environment("lost-response")

    request = provider._stub.DeleteEnv.call_args.args[0]
    assert request.workflow_id == "workflow"
    assert request.node_id == "node"
    assert request.version_id == "version"
    assert "lost-response" not in provider._environments


@pytest.mark.parametrize(
    ("status", "env_path", "message"),
    [
        ("FAILED", "/data/envs/failed", "status FAILED"),
        ("READY", "", "empty env_path"),
    ],
)
def test_invalid_create_response_is_rejected_but_remains_cleanup_safe(
    status,
    env_path,
    message,
):
    provider = GrpcEnvironmentProvider("localhost:50051")
    provider._stub = MagicMock()
    provider._stub.CreateEnv.return_value = _response(
        status=status,
        env_path=env_path,
    )
    provider._stub.DeleteEnv.return_value = MagicMock(status="DELETED")

    with pytest.raises(RuntimeError, match=message):
        provider.create_environment(
            "invalid-response",
            {
                "workflow_id": "workflow",
                "node_id": "node",
                "version_id": "version",
            },
        )

    assert provider._environments["invalid-response"]["type"] == "grpc_uv_pending"
    provider.cleanup_environment("invalid-response")
    assert "invalid-response" not in provider._environments


def test_remote_delete_failure_keeps_retry_state():
    provider = GrpcEnvironmentProvider("localhost:50051")
    provider._stub = MagicMock()
    provider._stub.DeleteEnv.side_effect = [
        RuntimeError("service unavailable"),
        MagicMock(status="DELETED"),
    ]
    provider._environments["retry"] = _remote_identity()

    with pytest.raises(RuntimeError, match="service unavailable"):
        provider.cleanup_environment("retry")

    assert "retry" in provider._environments
    provider.cleanup_environment("retry")
    assert "retry" not in provider._environments


def test_failed_remote_delete_status_keeps_retry_state():
    provider = GrpcEnvironmentProvider("localhost:50051")
    provider._stub = MagicMock()
    provider._stub.DeleteEnv.return_value = MagicMock(status="FAILED")
    provider._environments["retry-status"] = _remote_identity()

    with pytest.raises(RuntimeError, match="FAILED"):
        provider.cleanup_environment("retry-status")

    assert "retry-status" in provider._environments


def test_failed_cleanup_blocks_same_key_replacement_create():
    provider = GrpcEnvironmentProvider("localhost:50051")
    provider._stub = MagicMock()
    provider._stub.DeleteEnv.side_effect = RuntimeError("delete unavailable")
    provider._stub.CreateEnv.return_value = _response()
    old_identity = _remote_identity()
    provider._environments["same-key"] = old_identity

    with pytest.raises(RuntimeError, match="delete unavailable"):
        provider.create_environment(
            "same-key",
            {
                "workflow_id": "new-workflow",
                "node_id": "new-node",
                "version_id": "new-version",
            },
        )

    assert provider._environments["same-key"] is old_identity
    provider._stub.CreateEnv.assert_not_called()


def test_cleanup_waits_for_same_key_create_to_commit():
    provider = GrpcEnvironmentProvider("localhost:50051")
    create_entered = threading.Event()
    release_create = threading.Event()
    cleanup_finished = threading.Event()
    errors = []

    def create_env(_request, timeout):
        create_entered.set()
        assert release_create.wait(timeout=2.0)
        return _response()

    provider._stub = MagicMock()
    provider._stub.CreateEnv.side_effect = create_env
    provider._stub.DeleteEnv.return_value = MagicMock(status="DELETED")

    def create():
        try:
            provider.create_environment(
                "shared-key",
                {
                    "workflow_id": "workflow",
                    "node_id": "node",
                    "version_id": "version",
                },
            )
        except BaseException as error:
            errors.append(error)

    def cleanup():
        try:
            provider.cleanup_environment("shared-key")
        except BaseException as error:
            errors.append(error)
        finally:
            cleanup_finished.set()

    create_thread = threading.Thread(target=create)
    cleanup_thread = threading.Thread(target=cleanup)
    create_thread.start()
    assert create_entered.wait(timeout=2.0)
    cleanup_thread.start()
    assert not cleanup_finished.wait(timeout=0.1)
    release_create.set()
    create_thread.join(timeout=2.0)
    cleanup_thread.join(timeout=2.0)

    assert errors == []
    assert not create_thread.is_alive()
    assert not cleanup_thread.is_alive()
    provider._stub.DeleteEnv.assert_called_once()
    assert "shared-key" not in provider._environments

    assert "shared-key" not in provider._operation_locks


def test_high_cardinality_environment_keys_do_not_leak_operation_locks():
    provider = GrpcEnvironmentProvider("localhost:50051")
    provider._stub = None

    for index in range(100):
        environment_id = f"ephemeral-{index}"
        provider.create_environment(environment_id, {})
        provider.cleanup_environment(environment_id)

    assert provider._environments == {}
    assert provider._operation_locks == {}


def test_runtime_env_preserves_provider_type_and_omits_empty_paths():
    provider = GrpcEnvironmentProvider("localhost:50051")
    provider._environments["pending"] = {
        "type": "grpc_uv_pending",
        "workflow_id": "workflow",
        "node_id": "node",
        "version_id": "version",
    }

    runtime_env = provider.get_runtime_env("pending")

    assert runtime_env == {
        "type": "grpc_uv_pending",
        "env_path": "",
        "python_path": "",
        "venv_path": "",
    }
