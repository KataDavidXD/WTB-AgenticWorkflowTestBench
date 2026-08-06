"""A closed development bench must release every SQLite file immediately."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, StateGraph

from wtb.sdk import WorkflowProject, WTBTestBench


class _CloseState(TypedDict, total=False):
    route: list[str]
    branch: str


def _close_graph():
    def prefix(state: _CloseState):
        return {"route": [*state.get("route", []), "prefix"]}

    def suffix(state: _CloseState):
        return {
            "route": [
                *state.get("route", []),
                f"suffix:{state.get('branch', 'source')}",
            ]
        }

    graph = StateGraph(_CloseState)
    graph.add_node("prefix", prefix)
    graph.add_node("suffix", suffix)
    graph.set_entry_point("prefix")
    graph.add_edge("prefix", "suffix")
    graph.add_edge("suffix", END)
    return graph


def test_close_releases_sqlite_after_run_fork_resume(tmp_path):
    bench = WTBTestBench.create(mode="development", data_dir=str(tmp_path))
    try:
        project = WorkflowProject(name="close-sqlite", graph_factory=_close_graph)
        bench.register_project(project)
        source = bench.run(project.name, {"route": [], "branch": "source"})
        checkpoint = next(
            item
            for item in bench.get_checkpoints(source.id)
            if item.next_nodes == ["suffix"]
        )
        fork = bench.fork(
            source.id,
            str(checkpoint.id),
            new_initial_state={"branch": "fork"},
        )
        resumed = bench.resume(fork.fork_execution_id)
        assert resumed.state.workflow_variables["route"] == [
            "prefix",
            "suffix:fork",
        ]
    finally:
        bench.close()

    database_files = sorted(Path(tmp_path).glob("*.db"))
    assert {path.name for path in database_files} >= {
        "wtb.db",
        "wtb_checkpoints.db",
    }
    for database_path in database_files:
        renamed = database_path.with_suffix(".closed")
        database_path.rename(renamed)
        renamed.unlink()


def test_closing_one_bench_keeps_a_shared_database_bench_usable(tmp_path):
    first = WTBTestBench.create(mode="development", data_dir=str(tmp_path))
    second = WTBTestBench.create(mode="development", data_dir=str(tmp_path))
    try:
        first_project = WorkflowProject(
            name="shared-database-first",
            graph_factory=_close_graph,
        )
        second_project = WorkflowProject(
            name="shared-database-second",
            graph_factory=_close_graph,
        )
        first.register_project(first_project)
        second.register_project(second_project)
        first.run(first_project.name, {"route": [], "branch": "first"})
        second.run(second_project.name, {"route": [], "branch": "before-close"})

        first.close()

        continued = second.run(
            second_project.name,
            {"route": [], "branch": "after-close"},
        )
        assert continued.state.workflow_variables["route"] == [
            "prefix",
            "suffix:after-close",
        ]
    finally:
        first.close()
        second.close()

    for database_path in sorted(Path(tmp_path).glob("*.db")):
        renamed = database_path.with_suffix(".closed")
        database_path.rename(renamed)
        renamed.unlink()
