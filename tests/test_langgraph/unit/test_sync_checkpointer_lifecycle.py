"""Regression tests for synchronous durable saver context ownership."""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from wtb.infrastructure.adapters.langgraph_state_adapter import (
    LangGraphConfig,
    LangGraphStateAdapter,
)


class _FakePostgresSaver:
    def __init__(self, setup_error=None):
        self.setup_calls = 0
        self.setup_error = setup_error

    def setup(self):
        self.setup_calls += 1
        if self.setup_error is not None:
            raise self.setup_error


class _FakeSyncContext:
    def __init__(self, saver, exit_error=None):
        self.saver = saver
        self.exit_error = exit_error
        self.enter_calls = 0
        self.exit_calls = 0
        self.exit_args = []

    def __enter__(self):
        self.enter_calls += 1
        return self.saver

    def __exit__(self, exc_type, exc, traceback):
        self.exit_calls += 1
        self.exit_args.append((exc_type, exc, traceback))
        if self.exit_error is not None:
            raise self.exit_error


def _install_postgres_saver(monkeypatch, context, connection_string):
    class FakePostgresSaver:
        @staticmethod
        def from_conn_string(value):
            assert value == connection_string
            return context

    module = ModuleType("langgraph.checkpoint.postgres")
    module.PostgresSaver = FakePostgresSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", module)


def test_sync_postgres_enters_context_and_closes_exactly_once(monkeypatch):
    connection_string = "postgresql://user:pass@localhost/wtb"
    saver = _FakePostgresSaver()
    context = _FakeSyncContext(saver)
    _install_postgres_saver(monkeypatch, context, connection_string)

    adapter = LangGraphStateAdapter(
        LangGraphConfig.for_production(connection_string)
    )

    assert adapter.get_checkpointer() is saver
    assert context.enter_calls == 1
    assert saver.setup_calls == 1

    adapter.close()
    adapter.close()

    assert context.exit_calls == 1
    assert adapter._checkpointer is None
    assert adapter._checkpointer_context is None


def test_sync_postgres_setup_failure_exits_context(monkeypatch):
    connection_string = "postgresql://user:pass@localhost/wtb"
    saver = _FakePostgresSaver(RuntimeError("setup failed"))
    context = _FakeSyncContext(saver)
    _install_postgres_saver(monkeypatch, context, connection_string)

    with pytest.raises(RuntimeError, match="setup failed"):
        LangGraphStateAdapter(LangGraphConfig.for_production(connection_string))

    assert context.enter_calls == 1
    assert saver.setup_calls == 1
    assert context.exit_calls == 1
    assert context.exit_args[0][0] is RuntimeError


class _SyncForkCompiledGraph:
    def __init__(self):
        self.checkpointer = None

    def invoke(self, state, config):
        return dict(state)

    def get_state(self, config):
        return None


class _SyncForkGraph:
    def __init__(self, compiled):
        self.compiled = compiled

    def compile(self, *, checkpointer):
        self.compiled.checkpointer = checkpointer
        return self.compiled


def test_sync_fork_keeps_shared_saver_alive_after_parent_close(monkeypatch):
    connection_string = "postgresql://user:pass@localhost/wtb"
    saver = _FakePostgresSaver()
    context = _FakeSyncContext(saver)
    _install_postgres_saver(monkeypatch, context, connection_string)

    parent = LangGraphStateAdapter(
        LangGraphConfig.for_production(connection_string)
    )
    compiled = _SyncForkCompiledGraph()
    parent.set_workflow_graph(_SyncForkGraph(compiled))
    parent.initialize_session("parent", object())

    fork = parent.create_fork("wtb-fork")
    parent.close()

    assert context.exit_calls == 0
    with pytest.raises(RuntimeError, match="closed"):
        parent.execute({"value": 1})
    with pytest.raises(RuntimeError, match="closed"):
        parent.get_current_state()
    assert fork.execute({"value": 2}) == {"value": 2}

    fork.close()
    fork.close()

    assert context.exit_calls == 1


def test_explicit_sync_close_propagates_context_exit_failure(monkeypatch):
    connection_string = "postgresql://user:pass@localhost/wtb"
    saver = _FakePostgresSaver()
    context = _FakeSyncContext(saver, RuntimeError("close failed"))
    _install_postgres_saver(monkeypatch, context, connection_string)
    adapter = LangGraphStateAdapter(
        LangGraphConfig.for_production(connection_string)
    )

    with pytest.raises(RuntimeError, match="close failed"):
        adapter.close()

    adapter.close()
    assert context.exit_calls == 1


class _FailingHistoryGraph:
    def get_state_history(self, config):
        raise RuntimeError("graph history failed")


class _HistorySaver:
    def __init__(self, *, entries=(), error=None):
        self.entries = entries
        self.error = error

    def list(self, config):
        if self.error is not None:
            raise self.error
        return iter(self.entries)


def _history_adapter(*, graph, saver):
    adapter = object.__new__(LangGraphStateAdapter)
    adapter._closed = False
    adapter._current_thread_id = "wtb-history"
    adapter._compiled_graph = graph
    adapter._checkpointer = saver
    return adapter


def test_sync_history_uses_saver_when_graph_history_fails():
    entry = SimpleNamespace(
        config={"configurable": {"checkpoint_id": "cp-fallback"}},
        metadata={"step": 3, "source": "fallback"},
    )
    adapter = _history_adapter(
        graph=_FailingHistoryGraph(),
        saver=_HistorySaver(entries=[entry]),
    )

    history = adapter.get_checkpoint_history()

    assert history == [
        {
            "checkpoint_id": "cp-fallback",
            "step": 3,
            "source": "fallback",
            "writes": {},
            "next": [],
            "values": {},
            "created_at": None,
        }
    ]


def test_sync_history_raises_saver_error_with_graph_failure_note():
    adapter = _history_adapter(
        graph=_FailingHistoryGraph(),
        saver=_HistorySaver(error=OSError("saver history failed")),
    )

    with pytest.raises(OSError, match="saver history failed") as raised:
        adapter.get_checkpoint_history()

    assert any(
        "graph history failed" in note
        for note in getattr(raised.value, "__notes__", ())
    )


def test_sync_history_without_graph_propagates_saver_error():
    adapter = _history_adapter(
        graph=None,
        saver=_HistorySaver(error=OSError("saver history failed")),
    )

    with pytest.raises(OSError, match="saver history failed"):
        adapter.get_checkpoint_history()
