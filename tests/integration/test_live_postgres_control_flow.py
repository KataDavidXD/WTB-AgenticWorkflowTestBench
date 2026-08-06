"""Opt-in E2E coverage against a live PostgreSQL service."""

from __future__ import annotations

import os
from typing import TypedDict

import pytest
from langgraph.graph import END, StateGraph

from tests.integration.test_real_file_control_flow_modes import (
    _exercise_single_like_file_flow,
    _project,
)
from wtb.application.factories import WTBTestBenchFactory
from wtb.domain.models.workflow import ExecutionState
from wtb.infrastructure.adapters.async_langgraph_state_adapter import (
    AsyncLangGraphStateAdapter,
)
from wtb.infrastructure.adapters.langgraph_state_adapter import LangGraphConfig


def _live_postgres_url() -> str:
    url = os.environ.get("WTB_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("WTB_TEST_POSTGRES_URL is required for live PostgreSQL E2E")
    return url


def _postgres_counts(url: str) -> tuple[int, int]:
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM wtb_executions")
        execution_count = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM checkpoints")
        checkpoint_count = cursor.fetchone()[0]
    return execution_count, checkpoint_count


def test_live_postgres_factory_rollback_resume_fork(tmp_path):
    """Use PostgreSQL for both WTB core metadata and LangGraph checkpoints."""
    url = _live_postgres_url()
    bench = WTBTestBenchFactory.create_with_langgraph(
        checkpointer_type="postgres",
        connection_string=url,
        data_dir=str(tmp_path),
        enable_file_tracking=True,
    )
    try:
        project = _project("live_postgres_files")
        bench.register_project(project)
        _exercise_single_like_file_flow(
            bench=bench,
            project_name=project.name,
            output_file=tmp_path / "outputs" / "result.txt",
            batch=False,
        )
    finally:
        bench.close()

    execution_count, checkpoint_count = _postgres_counts(url)
    assert execution_count >= 2  # original plus fork
    assert checkpoint_count > 0


class AsyncState(TypedDict):
    count: int
    trace: list[str]


def _async_graph() -> StateGraph:
    graph = StateGraph(AsyncState)
    graph.add_node(
        "increment",
        lambda state: {
            "count": state["count"] + 1,
            "trace": state["trace"] + ["increment"],
        },
    )
    graph.set_entry_point("increment")
    graph.add_edge("increment", END)
    return graph


@pytest.mark.asyncio
async def test_live_async_postgres_saver_lifecycle_and_fork():
    """Exercise lazy setup, checkpoints, fork sharing, and final async close."""
    url = _live_postgres_url()
    adapter = AsyncLangGraphStateAdapter(LangGraphConfig.for_production(url))
    fork = None
    try:
        await adapter.aset_workflow_graph(_async_graph())
        await adapter.ainitialize_session(
            "live-async-postgres",
            ExecutionState(workflow_variables={"count": 0, "trace": []}),
        )
        result = await adapter.aexecute({"count": 0, "trace": []})
        assert result == {"count": 1, "trace": ["increment"]}

        checkpoints = await adapter.aget_checkpoints()
        assert checkpoints
        checkpoint_id = checkpoints[0]["checkpoint_id"]
        assert checkpoint_id

        fork = await adapter.acreate_fork(
            "wtb-live-async-postgres-fork",
            from_checkpoint_id=checkpoint_id,
        )
        assert await fork.aget_current_state() == result
    finally:
        if fork is not None:
            await fork.aclose()
        await adapter.aclose()

    _, checkpoint_count = _postgres_counts(url)
    assert checkpoint_count > 0
