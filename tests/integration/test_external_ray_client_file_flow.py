"""Opt-in real file flow through an independently started Ray Client cluster."""

from __future__ import annotations

import os

import pytest

from tests.integration.test_real_file_control_flow_modes import (
    _exercise_single_like_file_flow,
    _grpc_service_available,
    _project,
)
from wtb.sdk import WTBTestBench


def test_external_ray_client_grpc_real_file_control_flow(tmp_path):
    address = os.environ.get("WTB_TEST_RAY_ADDRESS")
    if not address:
        pytest.skip("WTB_TEST_RAY_ADDRESS is required for external Ray Client E2E")

    grpc_address = os.environ.get("UV_VENV_GRPC_ADDRESS")
    if not grpc_address or not _grpc_service_available(grpc_address):
        pytest.skip("a live UV_VENV_GRPC_ADDRESS is required for Ray + gRPC E2E")

    ray = pytest.importorskip("ray")
    ray.shutdown()
    ray.init(address=address, ignore_reinit_error=True, log_to_driver=False)

    os.environ["WTB_RAY_STORAGE_ROOT"] = str(tmp_path / "ray_actors")
    bench = WTBTestBench.create(
        mode="development",
        data_dir=str(tmp_path),
        enable_file_tracking=True,
        enable_ray=True,
        grpc_env_url=grpc_address,
    )
    try:
        project = _project("external_ray_client_grpc_files", executor="ray")
        bench.register_project(project)
        _exercise_single_like_file_flow(
            bench=bench,
            project_name=project.name,
            output_file=tmp_path / "outputs" / "result.txt",
            batch=True,
        )
    finally:
        bench.close()
        os.environ.pop("WTB_RAY_STORAGE_ROOT", None)
        ray.shutdown()
