"""
Integration tests for Ray Batch mode.

Tests the RayBatchTestRunner with actual Ray when available.
Skips gracefully when Ray is not installed.
"""

import pytest
import os
import socket
import tempfile
from typing import Dict, Any, Optional, List

from wtb.domain.models.batch_test import (
    BatchTest,
    BatchTestStatus,
    VariantCombination,
)
from wtb.domain.models.workflow import (
    TestWorkflow,
    WorkflowNode,
    WorkflowEdge,
)

try:
    import ray
    RAY_AVAILABLE = True
except ImportError:
    RAY_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not RAY_AVAILABLE,
    reason="Ray not installed, skipping Ray integration tests",
)


def _try_import_runner():
    from wtb.application.services.ray_batch_runner import (
        RayBatchTestRunner,
        RAY_AVAILABLE as WTB_RAY,
    )
    if not WTB_RAY:
        pytest.skip("Ray not available in wtb")
    return RayBatchTestRunner


def _try_import_config():
    from wtb.config import RayConfig
    return RayConfig


def _make_workflow() -> TestWorkflow:
    wf = TestWorkflow(id="wf-ray", name="ray-batch-test", entry_point="start")
    wf.add_node(WorkflowNode(id="start", name="Start", type="start"))
    wf.add_node(WorkflowNode(id="end", name="End", type="end"))
    wf.add_edge(WorkflowEdge(source_id="start", target_id="end"))
    return wf


def _make_batch_test(
    workflow_id: str = "wf-ray",
    combinations: Optional[List[VariantCombination]] = None,
    initial_state: Optional[Dict[str, Any]] = None,
) -> BatchTest:
    if combinations is None:
        combinations = [
            VariantCombination(name="variant_a", variants={"key": "a"}),
            VariantCombination(name="variant_b", variants={"key": "b"}),
        ]
    return BatchTest(
        name="test-batch",
        workflow_id=workflow_id,
        variant_combinations=combinations,
        initial_state=initial_state or {"value": 0, "messages": [], "route": None},
        parallel_count=2,
    )


def _assert_completed_batch(result: BatchTest, expected_count: int) -> None:
    assert result.status == BatchTestStatus.COMPLETED
    assert len(result.results) == expected_count
    assert all(r.success for r in result.results), [
        (r.combination_name, r.error_message) for r in result.results if not r.success
    ]
    assert len(result.execution_ids) == expected_count


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def ray_init():
    """Initialize Ray once per module."""
    if not ray.is_initialized():
        ray.init(
            num_cpus=2,
            ignore_reinit_error=True,
            include_dashboard=False,
            _metrics_export_port=_find_free_port(),
        )
    yield
    ray.shutdown()


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        yield tmpdir


# ═══════════════════════════════════════════════════════════════
# Ray Availability & Setup
# ═══════════════════════════════════════════════════════════════


class TestRayAvailability:

    def test_ray_is_importable(self):
        import ray
        assert ray is not None

    def test_runner_reports_available(self):
        RayBatchTestRunner = _try_import_runner()
        assert RayBatchTestRunner.is_available() is True

    def test_docker_internal_venv_paths_stay_out_of_ray_runtime_env(self):
        RayBatchTestRunner = _try_import_runner()

        runtime_env = RayBatchTestRunner._build_ray_runtime_env(
            "actor_0",
            {
                "type": "grpc_uv",
                "env_path": "/data/envs/actor_0",
                "python_path": "/data/envs/actor_0/.venv/bin/python",
                "py_executable": "/data/envs/actor_0/.venv/bin/python",
                "venv_path": "/data/envs/actor_0/.venv",
                "env_vars": {"CUSTOM": "1"},
            },
        )

        assert set(runtime_env) == {"env_vars"}
        assert runtime_env["env_vars"]["WTB_UV_ENV_PATH"] == "/data/envs/actor_0"
        assert runtime_env["env_vars"]["WTB_UV_PYTHON_PATH"] == (
            "/data/envs/actor_0/.venv/bin/python"
        )
        assert "py_executable" not in runtime_env
        assert "VIRTUAL_ENV" not in runtime_env["env_vars"]


# ═══════════════════════════════════════════════════════════════
# Batch Run Tests
# ═══════════════════════════════════════════════════════════════


class TestBatchRun:

    def test_batch_run_multiple_variants(self, ray_init, temp_data_dir):
        """Run 2 variants in parallel, verify both complete."""
        RayBatchTestRunner = _try_import_runner()
        RayConfig = _try_import_config()

        agentgit_db = os.path.join(temp_data_dir, "agentgit.db")
        wtb_db = f"sqlite:///{os.path.join(temp_data_dir, 'wtb.db')}"

        runner = RayBatchTestRunner(
            config=RayConfig.for_local_development(),
            agentgit_db_url=agentgit_db,
            wtb_db_url=wtb_db,
        )

        batch = _make_batch_test()

        result = runner.run_batch_test(batch)
        _assert_completed_batch(result, expected_count=2)

    def test_batch_with_graph_factory(self, ray_init, temp_data_dir):
        """Variants with graph_factory_module/name pass factory references to actors."""
        RayBatchTestRunner = _try_import_runner()
        RayConfig = _try_import_config()

        combos = [
            VariantCombination(
                name="graph_variant",
                variants={"key": "a"},
                graph_factory_module="wtb.testing.fixtures",
                graph_factory_name="create_minimal_graph",
            ),
        ]

        agentgit_db = os.path.join(temp_data_dir, "agentgit.db")
        wtb_db = f"sqlite:///{os.path.join(temp_data_dir, 'wtb.db')}"

        runner = RayBatchTestRunner(
            config=RayConfig.for_local_development(),
            agentgit_db_url=agentgit_db,
            wtb_db_url=wtb_db,
        )

        batch = _make_batch_test(combinations=combos)

        result = runner.run_batch_test(batch)
        _assert_completed_batch(result, expected_count=1)


# ═══════════════════════════════════════════════════════════════
# Checkpoint DB Path in Metadata
# ═══════════════════════════════════════════════════════════════


class TestCheckpointMetadata:

    def test_checkpoint_db_path_is_stored(self, ray_init, temp_data_dir):
        """Verify that actor stores checkpoint_db_path in execution.metadata."""
        RayBatchTestRunner = _try_import_runner()
        RayConfig = _try_import_config()

        agentgit_db = os.path.join(temp_data_dir, "agentgit.db")
        wtb_db = f"sqlite:///{os.path.join(temp_data_dir, 'wtb.db')}"

        runner = RayBatchTestRunner(
            config=RayConfig.for_local_development(),
            agentgit_db_url=agentgit_db,
            wtb_db_url=wtb_db,
        )

        batch = _make_batch_test()

        result = runner.run_batch_test(batch)

        _assert_completed_batch(result, expected_count=2)
        for r in result.results:
            assert r.execution_id


# ═══════════════════════════════════════════════════════════════
# Event Bridge Tests
# ═══════════════════════════════════════════════════════════════


class TestEventBridge:

    def test_variant_started_event_emitted(self, ray_init, temp_data_dir):
        """Verify on_variant_execution_started is called when event_bridge is provided."""
        RayBatchTestRunner = _try_import_runner()
        RayConfig = _try_import_config()

        class FakeEventBridge:
            def __init__(self):
                self.actor_pool_events = []
                self.started_events = []
                self.completed_events = []

            @property
            def event_bus(self):
                return None

            def on_variant_execution_started(self, **kwargs):
                self.started_events.append(kwargs)

            def on_variant_completed(self, **kwargs):
                self.completed_events.append(kwargs)

            def on_variant_execution_completed(self, **kwargs):
                self.completed_events.append(kwargs)

            def on_actor_pool_created(self, **kwargs):
                self.actor_pool_events.append(kwargs)

            def on_variant_failed(self, **kwargs):
                pass

            def on_batch_test_started(self, **kwargs):
                pass

            def on_batch_test_completed(self, **kwargs):
                pass

            def on_batch_test_failed(self, **kwargs):
                pass

            def on_batch_test_cancelled(self, **kwargs):
                pass

            def cleanup_batch(self, batch_test_id):
                pass

        agentgit_db = os.path.join(temp_data_dir, "agentgit.db")
        wtb_db = f"sqlite:///{os.path.join(temp_data_dir, 'wtb.db')}"

        bridge = FakeEventBridge()
        runner = RayBatchTestRunner(
            config=RayConfig.for_local_development(),
            agentgit_db_url=agentgit_db,
            wtb_db_url=wtb_db,
            event_bridge=bridge,
        )

        batch = _make_batch_test()

        result = runner.run_batch_test(batch)

        _assert_completed_batch(result, expected_count=2)
        assert len(bridge.actor_pool_events) == 1
        assert len(bridge.started_events) == 2
        assert len(bridge.completed_events) == 2


# ═══════════════════════════════════════════════════════════════
# Cancellation Test
# ═══════════════════════════════════════════════════════════════


class TestBatchCancellation:

    def test_cancel_returns_false_when_not_running(self, ray_init, temp_data_dir):
        RayBatchTestRunner = _try_import_runner()
        RayConfig = _try_import_config()

        agentgit_db = os.path.join(temp_data_dir, "agentgit.db")
        wtb_db = f"sqlite:///{os.path.join(temp_data_dir, 'wtb.db')}"

        runner = RayBatchTestRunner(
            config=RayConfig.for_local_development(),
            agentgit_db_url=agentgit_db,
            wtb_db_url=wtb_db,
        )

        result = runner.cancel("non-existent-batch")
        assert result is False
