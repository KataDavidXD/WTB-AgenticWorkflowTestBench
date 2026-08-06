"""Real-Ray durability gate for graphless batch checkpoints."""

import socket

import pytest

from wtb.domain.models.batch_test import (
    BatchTest,
    BatchTestStatus,
    VariantCombination,
)
from wtb.domain.models.workflow import ExecutionStatus

try:
    import ray

    RAY_AVAILABLE = True
except ImportError:
    RAY_AVAILABLE = False


pytestmark = pytest.mark.skipif(not RAY_AVAILABLE, reason="Ray is not installed")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def ray_runtime():
    owned = not ray.is_initialized()
    if owned:
        ray.init(
            num_cpus=2,
            include_dashboard=False,
            ignore_reinit_error=True,
            _metrics_export_port=_free_port(),
        )
    yield
    if owned:
        ray.shutdown()


def test_graphless_batch_history_and_rollback_survive_actor_return(
    ray_runtime,
    tmp_path,
    monkeypatch,
):
    from wtb.application.services.ray_batch_runner import (
        RayBatchTestRunner,
        RayConfig,
    )

    monkeypatch.setenv("WTB_RAY_STORAGE_ROOT", str(tmp_path / "ray-storage"))
    wtb_db_url = f"sqlite:///{(tmp_path / 'wtb.db').as_posix()}"
    runner = RayBatchTestRunner(
        config=RayConfig.for_testing(),
        agentgit_db_url=str(tmp_path / "agentgit.db"),
        wtb_db_url=wtb_db_url,
    )
    coordinator = None
    try:
        batch = BatchTest(
            name="graphless-durable-batch",
            workflow_id="wf-ray-graphless-durable",
            variant_combinations=[
                VariantCombination(name="node-mode", variants={})
            ],
            initial_state={"request": "durable"},
            parallel_count=1,
        )

        result = runner.run_batch_test(batch)
        assert result.status is BatchTestStatus.COMPLETED
        assert len(result.results) == 1
        case = result.results[0]
        assert case.success is True
        assert case.checkpoint_count == 4
        assert case.last_checkpoint_id

        coordinator = runner.create_rollback_coordinator()
        history = coordinator.get_checkpoints(case.execution_id)
        assert len(history) == case.checkpoint_count
        assert history[0]["checkpoint_id"] == case.last_checkpoint_id

        target = history[-1]
        forked = coordinator.fork(
            case.execution_id,
            target["checkpoint_id"],
            new_state={"fork_marker": "real-ray"},
        )
        assert forked.status is ExecutionStatus.PAUSED
        assert forked.id != case.execution_id
        assert forked.session_id == f"wtb-{forked.id}"
        assert forked.session_id != f"wtb-{case.execution_id}"
        assert forked.metadata["forked_from"] == case.execution_id
        assert forked.metadata["source_checkpoint_id"] == target["checkpoint_id"]
        assert forked.metadata["state_adapter_backend"] == "node_sqlite"
        assert forked.state.workflow_variables["fork_marker"] == "real-ray"

        rolled_back = coordinator.rollback(
            case.execution_id,
            target["checkpoint_id"],
        )
        assert rolled_back.status is ExecutionStatus.PAUSED
        assert rolled_back.checkpoint_id == target["checkpoint_id"]
        assert rolled_back.state.to_dict() == target["values"]
    finally:
        if coordinator is not None:
            coordinator.close()
        runner.shutdown()
