"""Real LangGraph and DSH SDK runtimes, deterministic local model transport.

No alternate subprocess launcher is counted as a second runtime. DSH helper
composition and prerequisite gates are shared with the existing operator E2E.
"""

from typing import TypedDict

import pytest

from wtb.application.services.evidence_collector import (
    EvidenceCollector,
    deepseek_native_reference,
    langgraph_native_reference,
)
from wtb.domain.execution_evidence import AttemptIdentity
from wtb.domain.models.workflow import ExecutionState, ExecutionStatus
from wtb.domain.runtime_identity import canonical_json_sha256
from wtb.infrastructure.evidence.sqlite_store import SQLiteEvidenceStore


@pytest.fixture
def evidence_store_factory(tmp_path):
    return lambda: SQLiteEvidenceStore(tmp_path / "events.sqlite")


def exercise(runtime, store, native_reference, input_state, reopen_store):
    plan = runtime.prepare_attempt(
        "execution-1", "attempt-1", input_sha256=canonical_json_sha256(input_state)
    )
    identity = AttemptIdentity(
        plan.execution_id, type(runtime).__name__, "scope", plan.binding
    )
    store.register(identity, expected_attempt=None)
    result = runtime.invoke(
        input_state,
        execution_id=plan.execution_id,
        attempt_id=plan.attempt_id,
        binding=plan.binding,
        attempt_metadata=plan.metadata,
    )
    collector = EvidenceCollector(store)
    receipt = collector.collect(
        runtime, identity, native_reference=native_reference, input_state=input_state
    )
    assert receipt is not None
    projected = collector.execution(plan.execution_id, plan.attempt_id)
    assert projected.status is ExecutionStatus.COMPLETED
    assert projected.metadata["last_runtime_attempt"]["receipt"][
        "nativeReference"
    ] == native_reference(receipt)
    # Reopen only the WTB collector store; this does not promise runtime recovery.
    reopened = EvidenceCollector(reopen_store())
    assert (
        reopened.execution(plan.execution_id, plan.attempt_id).metadata
        == projected.metadata
    )
    return result


def test_real_langgraph_collector(tmp_path, evidence_store_factory):
    from langgraph.graph import END, START, StateGraph

    from wtb.infrastructure.adapters.langgraph_state_adapter import (
        LangGraphConfig,
        LangGraphStateAdapter,
    )
    from wtb.infrastructure.adapters.langgraph_workflow_runtime import (
        LangGraphWorkflowRuntime,
    )

    class State(TypedDict):
        answer: str

    graph = StateGraph(State)
    graph.add_node("writer", lambda state: {"answer": state["answer"] + "-completed"})
    graph.add_edge(START, "writer")
    graph.add_edge("writer", END)
    adapter = LangGraphStateAdapter(LangGraphConfig.for_testing())
    adapter.initialize_session("execution-1", ExecutionState())
    runtime = LangGraphWorkflowRuntime(adapter)
    runtime.bind(graph)
    try:
        result = exercise(
            runtime,
            evidence_store_factory(),
            langgraph_native_reference,
            {"answer": "real-langgraph"},
            evidence_store_factory,
        )
        assert result["answer"] == "real-langgraph-completed"
    finally:
        adapter.close()


def test_real_deepseek_sdk_collector(tmp_path, evidence_store_factory):
    from tests.integration.test_deepseek_operator_real_e2e import (
        _completion_server,
        _dsh_checkout,
        _runtime,
    )
    from wtb.infrastructure.runtimes.deepseek_harness import DeepSeekHarnessWorkflow

    checkout = _dsh_checkout()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with _completion_server() as (base_url, requests):
        runtime = _runtime(
            checkout=checkout,
            base_url=base_url,
            session_root=tmp_path / "sessions",
            workspace=workspace,
        )
        runtime.bind(DeepSeekHarnessWorkflow())
        try:
            result = exercise(
                runtime,
                evidence_store_factory(),
                deepseek_native_reference,
                {"prompt": "Return a deterministic completion."},
                evidence_store_factory,
            )
            assert result["_dsh"]["message_id"]
            assert len(requests) == 1
        finally:
            runtime.close()
