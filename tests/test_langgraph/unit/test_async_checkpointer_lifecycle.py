"""Regression tests for durable async LangGraph saver lifecycle."""

import asyncio
import sys
from types import ModuleType
from typing import TypedDict

import pytest

from wtb.domain.models.workflow import ExecutionState
from wtb.infrastructure.adapters.async_langgraph_state_adapter import (
    AsyncLangGraphStateAdapter,
)
from wtb.infrastructure.adapters.langgraph_state_adapter import LangGraphConfig


class CounterState(TypedDict):
    value: int


@pytest.mark.asyncio
async def test_async_sqlite_saver_is_entered_used_and_closed(tmp_path):
    from langgraph.graph import StateGraph

    graph = StateGraph(CounterState)
    graph.add_node("increment", lambda state: {"value": state["value"] + 1})
    graph.set_entry_point("increment")
    graph.set_finish_point("increment")

    adapter = AsyncLangGraphStateAdapter(
        LangGraphConfig.for_development(str(tmp_path / "async-checkpoints.db"))
    )

    adapter.set_workflow_graph(graph)
    assert adapter._compiled_graph is None

    await adapter.ainitialize_session("execution-1", ExecutionState())

    assert type(adapter._checkpointer).__name__ == "AsyncSqliteSaver"
    assert adapter._compiled_graph is not None
    assert await adapter.aexecute({"value": 1}) == {"value": 2}

    await adapter.aclose()

    assert adapter._checkpointer is None
    assert adapter._checkpointer_context is None


class _RecordingGraph:
    def __init__(self):
        self.compile_calls = []

    def compile(self, *, checkpointer):
        self.compile_calls.append(checkpointer)
        return object()


class _FakeAsyncSaver:
    def __init__(self):
        self.setup_called = False

    async def setup(self):
        self.setup_called = True


class _FakeAsyncContext:
    def __init__(self, saver):
        self.saver = saver
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self.saver

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True


@pytest.mark.asyncio
async def test_async_postgres_never_compiles_with_missing_saver(monkeypatch):
    connection_string = "postgresql://user:pass@localhost/wtb"
    saver = _FakeAsyncSaver()
    context = _FakeAsyncContext(saver)

    class FakeAsyncPostgresSaver:
        @staticmethod
        def from_conn_string(value):
            assert value == connection_string
            return context

    postgres_module = ModuleType("langgraph.checkpoint.postgres")
    postgres_module.__path__ = []
    aio_module = ModuleType("langgraph.checkpoint.postgres.aio")
    aio_module.AsyncPostgresSaver = FakeAsyncPostgresSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", postgres_module)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", aio_module)

    adapter = AsyncLangGraphStateAdapter(
        LangGraphConfig.for_production(connection_string)
    )
    graph = _RecordingGraph()

    adapter.set_workflow_graph(graph)

    assert graph.compile_calls == []
    assert adapter._compiled_graph is None

    await adapter.ainitialize_session("execution-2", ExecutionState())

    assert context.entered is True
    assert saver.setup_called is True
    assert graph.compile_calls == [saver]
    assert adapter._checkpointer is saver

    await adapter.aclose()

    assert context.exited is True
    assert adapter._checkpointer is None


class _BlockingAsyncContext:
    def __init__(self, saver):
        self.saver = saver
        self.enter_started = asyncio.Event()
        self.allow_enter = asyncio.Event()
        self.exit_calls = 0

    async def __aenter__(self):
        self.enter_started.set()
        await self.allow_enter.wait()
        return self.saver

    async def __aexit__(self, exc_type, exc, traceback):
        self.exit_calls += 1


@pytest.mark.asyncio
async def test_close_serializes_with_inflight_durable_initialization(monkeypatch):
    connection_string = "postgresql://user:pass@localhost/wtb"
    saver = _FakeAsyncSaver()
    context = _BlockingAsyncContext(saver)

    class FakeAsyncPostgresSaver:
        @staticmethod
        def from_conn_string(value):
            assert value == connection_string
            return context

    postgres_module = ModuleType("langgraph.checkpoint.postgres")
    postgres_module.__path__ = []
    aio_module = ModuleType("langgraph.checkpoint.postgres.aio")
    aio_module.AsyncPostgresSaver = FakeAsyncPostgresSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", postgres_module)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", aio_module)

    adapter = AsyncLangGraphStateAdapter(
        LangGraphConfig.for_production(connection_string)
    )
    graph = _RecordingGraph()
    adapter.set_workflow_graph(graph)

    initialize_task = asyncio.create_task(
        adapter.ainitialize_session("execution-race", ExecutionState())
    )
    await asyncio.wait_for(context.enter_started.wait(), timeout=1.0)

    close_task = asyncio.create_task(adapter.aclose())
    done, _ = await asyncio.wait({close_task}, timeout=0.05)
    close_waited_for_initialization = close_task not in done

    context.allow_enter.set()
    await asyncio.gather(initialize_task, close_task)

    assert close_waited_for_initialization is True
    assert context.exit_calls == 1
    assert adapter._closed is True
    assert adapter._checkpointer is None
    assert adapter._checkpointer_context is None

class _CountingAsyncContext:
    def __init__(self, saver):
        self.saver = saver
        self.exit_calls = 0

    async def __aenter__(self):
        return self.saver

    async def __aexit__(self, exc_type, exc, traceback):
        self.exit_calls += 1


class _ForkSnapshot:
    values = {"value": 1}


class _ForkCompiledGraph:
    def __init__(self):
        self.seeded = []

    async def aget_state(self, config):
        return _ForkSnapshot()

    async def aupdate_state(self, config, values):
        self.seeded.append((config, values))

    async def ainvoke(self, state, config):
        return dict(state)


class _ForkGraph:
    def __init__(self, compiled):
        self.compiled = compiled
        self.checkpointers = []

    def compile(self, *, checkpointer):
        self.checkpointers.append(checkpointer)
        return self.compiled


@pytest.mark.asyncio
async def test_async_fork_keeps_shared_saver_alive_after_parent_close(monkeypatch):
    connection_string = "postgresql://user:pass@localhost/wtb"
    saver = _FakeAsyncSaver()
    context = _CountingAsyncContext(saver)

    class FakeAsyncPostgresSaver:
        @staticmethod
        def from_conn_string(value):
            assert value == connection_string
            return context

    postgres_module = ModuleType("langgraph.checkpoint.postgres")
    postgres_module.__path__ = []
    aio_module = ModuleType("langgraph.checkpoint.postgres.aio")
    aio_module.AsyncPostgresSaver = FakeAsyncPostgresSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", postgres_module)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", aio_module)

    compiled = _ForkCompiledGraph()
    graph = _ForkGraph(compiled)
    parent = AsyncLangGraphStateAdapter(
        LangGraphConfig.for_production(connection_string)
    )
    parent.set_workflow_graph(graph)
    await parent.ainitialize_session("parent", ExecutionState())

    fork = await parent.acreate_fork("wtb-fork")
    await parent.aclose()

    assert context.exit_calls == 0
    assert await fork.aexecute({"value": 2}) == {"value": 2}

    await fork.aclose()
    await fork.aclose()

    assert context.exit_calls == 1
