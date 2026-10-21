<h1 align="center">WTB</h1>

<p align="center"><strong>Governance, recovery, and experiment control for agentic workflows.</strong></p>

<p align="center"><strong>English</strong> · <a href="README_ZH.md">简体中文</a></p>

<div align="center">

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11--3.13-blue.svg)](https://www.python.org/)

</div>

WTB (Workflow Test Bench) runs a workflow under a durable governance layer. It
records execution identity, checkpoints, file versions, variants, lifecycle
transitions, and audit evidence so a run can be inspected, resumed, rolled
back, forked, or compared without making the workflow engine the source of
governance truth.

WTB includes a native Python runtime. **LangGraph is optional**, not a core
dependency. DeepSeek Harness and other engines connect through an explicit
runtime port.

## Why WTB

Suppose an agent writes `report.md`, pauses after the second tool call, and is
then restarted with a different prompt or model. A normal test runner can tell
you whether the final call returned. It usually cannot answer:

- Which workflow, input, runtime, and attempt produced this file?
- Which checkpoint can be resumed without silently replaying a side effect?
- Can we branch from the same state and compare two variants?
- Did rollback restore both workflow state and checkpoint-linked files?
- Is a recovery target the exact durable attempt, or only a guessed session?

WTB makes those questions explicit. It is designed for workflows whose state
and side effects matter, not only for stateless input/output assertions.

### What WTB owns

| WTB governs | The workflow runtime owns |
|---|---|
| Execution, attempt, project, and variant identity | Model invocation and tool semantics |
| Pause, resume, rollback, fork, cancellation, and reconciliation policy | Node/agent-loop execution |
| Checkpoint references and durable state adapters | Runtime-native state production |
| Workspace snapshots, file manifests, and content-addressable storage | The actual workspace mutations |
| Batch orchestration, evaluation records, audit events, and outbox receipts | Framework-specific streaming or interrupts |

WTB is not a new agent framework and does not replace LangGraph, DeepSeek
Harness, or a custom runtime. It wraps them behind a governance contract.

## Quick start

Install this development checkout. The examples below describe `0.4.0.dev0`;
an earlier package registry release may not expose these APIs:

```bash
python -m pip install -e "."
```

`WTBTestBench.create(mode="development")` requires only the base package. The
example below runs real Python nodes and stores metadata plus checkpoints in
SQLite; it does not import LangGraph.

### Native Python workflow

```python
from dataclasses import dataclass

from wtb.sdk import WorkflowProject, WTBTestBench


@dataclass(frozen=True)
class Edge:
    source: str
    target: str


def prepare(state):
    return {"value": 20}


def compute(state):
    return {"answer": state["value"] * 2 + 2}


class NativeGraph:
    nodes = {"prepare": prepare, "compute": compute}
    edges = [
        Edge("__start__", "prepare"),
        Edge("prepare", "compute"),
        Edge("compute", "__end__"),
    ]
    entry_point = "prepare"


project = WorkflowProject(name="answer-42", graph_factory=NativeGraph)

with WTBTestBench.create(mode="development", data_dir=".wtb-data") as bench:
    bench.register_project(project)
    execution = bench.run(project.name, {})
    assert execution.state.workflow_variables["answer"] == 42
```

The native graph contract is deliberately small: `nodes` maps IDs to
synchronous callables, `edges` exposes `source` and `target`, and a node returns
a mapping of state updates.

### Pause, reopen, and resume

Executable Python code is never serialized into the state database. After a
process restart, reattach the reviewed project definition before continuing:

```python
first = WTBTestBench.create(mode="development", data_dir=".wtb-data")
first.register_project(project)
paused = first.run(project.name, {}, breakpoints=["compute"])
execution_id = paused.id
first.close()

second = WTBTestBench.create(mode="development", data_dir=".wtb-data")
second.attach_project(project)
completed = second.resume(execution_id)
second.close()
```

This is a security boundary: persisted state alone is not treated as trusted
graph code. A missing executable binding fails closed.

## Choose a runtime

| Need | Runtime/state choice | Install |
|---|---|---|
| Local Python workflow, durable lifecycle, no framework dependency | Native runtime + native SQLite | `pip install -e "."` |
| Distributed variant batches | Native runtime + Ray | `pip install -e ".[ray]"` |
| LangGraph reducer/superstep/interrupt semantics | LangGraph + SQLite | `pip install -e ".[langgraph-sqlite]"` |
| LangGraph with shared PostgreSQL checkpoints | LangGraph + PostgreSQL | `pip install -e ".[langgraph-postgres]"` |
| Ray batch plus LangGraph | Explicit combined composition | `pip install -e ".[ray,langgraph-sqlite]"` |
| Another workflow engine | Implement `WorkflowRuntime` | Base package plus the engine |

The default native SQLite adapter is a single-host state backend. Cluster-wide
workflow state needs an explicitly injected `IStateAdapter` or the optional
LangGraph/PostgreSQL composition; a PostgreSQL metadata URL alone does not
change the workflow-state backend.

### Optional LangGraph example

```python
from langgraph.graph import END, StateGraph
from wtb.sdk import WorkflowProject, WTBTestBench


def create_graph():
    graph = StateGraph(dict)
    graph.add_node("answer", lambda state: {"answer": state["value"] * 2})
    graph.set_entry_point("answer")
    graph.add_edge("answer", END)
    return graph


project = WorkflowProject(
    name="langgraph-example",
    graph_factory=create_graph,
    runtime_backend="langgraph",
)

with WTBTestBench.create(
    mode="development",
    data_dir=".wtb-data",
    state_adapter_mode="langgraph",
    checkpointer="sqlite",
) as bench:
    bench.register_project(project)
    execution = bench.run(project.name, {"value": 21})
```

Selecting `runtime_backend="langgraph"` is explicit. The base installation
does not attempt a LangGraph import.

## Architecture

```text
User / experiment runner
          |
          v
WTBTestBench SDK
          |
          v
Governance application layer
  lifecycle | variants | audit/outbox | batch | CAS/file policy
          |
          v
Framework-neutral ports
  WorkflowRuntime | IStateAdapter | repositories | node executor
          |
          +----------------------+----------------------+------------------+
          |                      |                      |
     Native defaults       Optional adapters      Execution substrate
     Python runtime        LangGraph              thread pool / Ray
     SQLite state          DeepSeek Harness       UV environments
     SQLAlchemy metadata   custom runtimes        external services
```

The dependency direction points inward: domain and application code depend on
interfaces, while runtime and persistence integrations implement those
interfaces. Governance policy therefore remains usable when LangGraph is not
installed.

### Lifecycle semantics

| Operation | Result | Execution ID | Runs immediately? |
|---|---|---|---|
| `resume(id, modified_state=...)` | Continue the selected recovery head | Same | Yes |
| `rollback(id, checkpoint_id)` | Move the execution to historical state | Same | No |
| `fork(id, checkpoint_id, new_initial_state=...)` | Create an isolated branch | New | No |

Rollback and fork select state; call `resume` to execute subsequent nodes.
With file tracking enabled, synchronous rollback validates and restores the
checkpoint-linked file snapshot before committing the rewound state. Required
but missing CAS content fails closed. Workflow state and external file writes
are not a distributed transaction, so operators must still handle failures
that occur after a physical restore.

## DeepSeek Harness integration

WTB integrates with DeepSeek Harness as an **external workflow runtime**, not
as a replacement for its agent loop:

```text
WTB governance
  -> WorkflowRuntime / DeepSeekHarnessRuntime
    -> DeepSeek Harness Python SDK
      -> JSON-RPC runtime
        -> Cordis agent composition
```

DeepSeek Harness owns model sessions, the agent loop, tool execution, and
Cordis lifecycle. WTB owns execution/attempt identity, durable admission,
recovery policy, checkpoint references, workspace evidence, audit receipts,
and experiment comparison.

The current `0.4.0.dev0` adapter includes durable attempt control and optional
S0/M0 reference-memory receipts. It is source-only and release-gated because the
published SDK does not expose the complete durable-attempt contract required
by WTB. It fails closed before model/tool dispatch when durable identity,
binding, or process-generation evidence is missing. There is no supported
`wtb[deepseek-harness]` distribution extra yet.

Read [DeepSeek Harness integration and operator safety](DEEPSEEK_HARNESS.md)
for source setup, protocol mapping, durable attempt identity, recovery rules,
platform limits, and release sequencing.

## Execution evidence and publication

The opt-in `EvidenceCollector` and `SQLiteEvidenceStore` preserve exact attempt
bindings across duplicate, delayed, and conflicting runtime observations.
`SQLitePublicationSink` checks current authority and commits approval consumption
with a stored-byte effect. `ExternalDelivery` uses durable requests and lookup;
external providers must enforce their own atomic authority and idempotency guard.
These stores and sinks are explicitly composed by the application. See the
[module and runtime guide](AUTHORITY_RUNTIME.md), [architecture (中文)](docs/RUNTIME_EVIDENCE_ARCHITECTURE_ZH.md),
and [material-generation migration guide](docs/e1-material-version-upgrade.md).

PostgreSQL evidence, approval consumption and external delivery are available with
`pip install -e ".[postgres-evidence]"`. Use `PostgresEvidenceStore` and
`PostgresPublicationSink`; see [setup and transaction guarantees](docs/POSTGRES_EVIDENCE_ZH.md).
This is separate from the optional LangGraph PostgreSQL checkpointer.

## Core operations

```python
# Run and inspect checkpoints.
execution = bench.run(project="my-workflow", initial_state={"query": "hello"})
checkpoints = bench.get_checkpoints(execution.id)

# Roll back the existing execution, then continue it.
rollback = bench.rollback(execution.id, checkpoint_id=str(checkpoints[0].id))
if rollback.success:
    resumed = bench.resume(execution.id, modified_state={"query": "revised"})

# Fork without changing the source execution, then run the branch.
fork = bench.fork(
    execution.id,
    checkpoint_id=str(checkpoints[0].id),
    new_initial_state={"query": "alternative"},
)
forked = bench.resume(fork.fork_execution_id)
```

### Batch experiments

```python
batch = bench.run_batch_test(
    project="my-workflow",
    variant_matrix=[
        {"retriever": "bm25", "generator": "large"},
        {"retriever": "dense", "generator": "small"},
    ],
    test_cases=[{"query": "What changed?"}],
)

for result in batch.results:
    print(result.combination_name, result.success, result.overall_score)
```

Use `enable_ray=True` and the `ray` extra when the batch should execute through
Ray. The same output root remains a governed resource; do not start independent
matrix orchestrators as competing writers to one result journal.

## Installation

```bash
# Base: native runtime and native SQLite state
python -m pip install -e "."

# Optional execution and state integrations
python -m pip install -e ".[ray]"
python -m pip install -e ".[langgraph-sqlite]"
python -m pip install -e ".[langgraph-postgres]"
python -m pip install -e ".[ray,langgraph-sqlite]"

# Combined optional features in this source tree
python -m pip install -e ".[all]"
```

For source development, check out the reviewed revision before installing:

```bash
git clone https://github.com/KataDavidXD/WTB-AgenticWorkflowTestBench.git
cd WTB-AgenticWorkflowTestBench
python -m pip install -e ".[dev,api]"
python -m pytest tests/unit tests/test_api/test_console_real.py -q
```

WTB supports Python 3.11 through 3.13. Optional runtimes can impose narrower
platform constraints.

## Guarantees and limits

- Native development and standalone compositions run without LangGraph.
- WTB stores data and executable bindings separately; reattachment is explicit.
- Rollback, fork, reconcile, and cancel validate durable identity and fail
  closed when the target cannot be proven.
- Content-addressable file tracking detects missing or mismatched snapshots.
- Native SQLite is local durable state, not a cluster consensus mechanism.
- WTB does not reimplement LangGraph reducers, supersteps, streaming, or
  distributed checkpoint semantics.
- WTB cannot make its database, an external runtime, and arbitrary tool side
  effects one atomic transaction. Unknown outcomes remain recovery-required;
  they are not blindly replayed.

## Project status

The package version is `0.4.0.dev0`. Native execution, optional LangGraph
execution, Ray batch orchestration, lifecycle control, SQLite persistence,
file/CAS tracking, and the framework-neutral runtime boundary are covered by
the test suite. DeepSeek Harness support remains a source-only development
integration until its required durable-attempt SDK contract is published and
pinned.

For a real-provider smoke test, see `examples/modes_quick_demo.py`. It requires
an OpenAI-compatible endpoint and never falls back to a mock. Live PostgreSQL,
external Ray Client, and Docker UV-environment checks are opt-in integration
tests under `tests/integration/` and `scripts/`.

## Contributing

Contributions are welcome for runtime/state adapters, lifecycle correctness,
file governance, evaluation, documentation, and platform validation. Run the
focused tests for your change before opening a pull request. Runtime, PostgreSQL,
Ray, and real-provider suites require their documented optional dependencies.

WTB is built from the ideas explored in
[Agent Git](https://github.com/HKU-MAS-Infra-Layer/Agent-Git). Partner: HKU CAMO
Lab.

## License

Apache License 2.0. See [LICENSE](LICENSE).
