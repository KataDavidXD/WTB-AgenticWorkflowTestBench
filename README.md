<h1 align="center">WTB: Workflow Test Bench for Agentic Workflows</h1>

<p align="center">Built as the production release of <a href="https://github.com/HKU-MAS-Infra-Layer/Agent-Git">Agent Git</a>.</p>

<div align="center">

[![GitHub stars](https://img.shields.io/github/stars/KataDavidXD/WTB-AgenticWorkflowTestBench?logo=github&logoColor=auto)](https://github.com/KataDavidXD/WTB-AgenticWorkflowTestBench/stargazers)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)

</div>

<p align="center">
  <a href="#overview">Overview</a>
  &nbsp;&nbsp;&bull;&nbsp;&nbsp;
  <a href="#architecture">Architecture</a>
  &nbsp;&nbsp;&bull;&nbsp;&nbsp;
  <a href="#installation">Installation</a>
  &nbsp;&nbsp;&bull;&nbsp;&nbsp;
  <a href="#quick-start">Quick Start</a>
  &nbsp;&nbsp;&bull;&nbsp;&nbsp;
  <a href="#core-operations">Core Operations</a>
</p>

##

**WTB (Workflow Test Bench)** is a production-grade testing, debugging, and benchmarking framework for agentic workflows. It ensures **transactional integrity**, **reproducibility**, and **observability** for complex AI agent systems by combining LangGraph orchestration, Ray distributed computing, content-addressable storage, and UV environment isolation.

> **The Problem:** Modern agentic systems (RAG, autonomous agents) are not just "read-only" chat interfaces. They persist state, modify data, and evolve. Testing them requires more than simple input/output matching -- it requires a rig that understands state, side effects, and concurrency.

## Overview

- **Checkpoint & Rollback**: Create restore points at every node boundary and travel back in execution history
- **Forking (A/B Testing)**: Create independent execution branches from any checkpoint for variant comparison
- **Batch Testing**: Run multiple test cases and variant combinations in parallel via Ray
- **File Version Control**: Track all generated files with content-addressable storage (SHA-256 hashing)
- **Environment Isolation**: Per-node virtual environments via UV for dependency safety

## Architecture

```
                         ┌──────────────────────────┐
                         │      WTBTestBench         │
                         │  (SDK Entry Point)        │
                         └─────┬──────────┬─────────┘
                               │          │
                  single run   │          │   batch test
                               ▼          ▼
              ┌─────────────────┐   ┌──────────────────────┐
              │  ExecutionCtrl   │   │  RayBatchTestRunner   │
              │  (run, pause,   │   │                       │
              │   rollback,     │   │   Actor 0 │ Actor 1   │
              │   fork)         │   │   Actor 2 │ Actor N   │
              └───────┬─────────┘   └──────────┬───────────┘
                      │                        │
         ┌────────────┴────────────────────────┘
         ▼
┌──────────────────────────────────────────────────────────┐
│                    Infrastructure                         │
│                                                          │
│   LangGraph          CAS                UV               │
│   Checkpointer       (SHA-256           Venv Manager     │
│   ┌──────────┐       File Hashing)      ┌──────────┐    │
│   │ Memory   │       ┌──────────┐       │ per-node │    │
│   │ SQLite   │       │ BlobId   │       │ per-var  │    │
│   │ Postgres │       │ CommitId │       │ isolated │    │
│   └──────────┘       └──────────┘       └──────────┘    │
│                                                          │
│   SQLAlchemy Unit of Work (ACID transactions)            │
└──────────────────────────────────────────────────────────┘
```

## Timeline

[Jan 2026]: v0.2.0 -- Ray batch execution, LangGraph checkpoint integration, content-addressable file tracking, workspace isolation, and batch rollback/fork coordination.

## Installation

### Using uv (Recommended)

```bash
# Install core package
uv pip install wtb

# Install with Ray support for distributed batch testing
uv pip install "wtb[ray]"

# Install with all features (Ray, LangGraph SQLite/Postgres, API, Observability)
uv pip install "wtb[all]"
```

### Using pip

```bash
pip install wtb

# With Ray support
pip install "wtb[ray]"
```

### From Source

```bash
git clone https://github.com/KataDavidXD/WTB-AgenticWorkflowTestBench.git
cd WTB-AgenticWorkflowTestBench

# Install with uv
uv pip install -e ".[all]"

# Or with pip
pip install -e ".[all]"
```

## Quick Start

> All examples import only from `wtb.sdk`. The Ray batch runner and LangGraph adapter are configured internally by the SDK.

### Runnable Mode Demo

The quickest way to verify the main execution modes is the runnable demo in
`examples/modes_quick_demo.py`. Each mode proves the same control-flow contract:
`real LLM call -> variant -> _output_files -> outputs/ -> checkpoint_id -> file_commit_id -> rollback -> resume -> fork/resume`.

The demo requires a real OpenAI-compatible LLM provider. Configure either
environment variables or a local `.env` file:

```bash
LLM_API_KEY=...
LLM_BASE_URL=https://your-openai-compatible-endpoint/v1
LLM_MODEL=your-model-name
```

`OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL` are also accepted. The
demo fails fast when no real LLM is configured; it does not fall back to mocks.

```bash
# Single workflow execution with a real LLM call, real output file, and registered node variant
python -m examples.modes_quick_demo --mode single

# Local batch mode with the same real LLM graph and CAS-tracked file restore
python -m examples.modes_quick_demo --mode batch

# Ray batch mode with the same real LLM graph and CAS-tracked file restore
python -m examples.modes_quick_demo --mode ray

# Ray batch mode plus Docker uv_venv_manager gRPC venv provisioning
python -m examples.modes_quick_demo --mode venv --grpc-url localhost:50051

# Run single + batch + Ray; add --grpc-url to include venv mode
python -m examples.modes_quick_demo --mode all --grpc-url localhost:50051
```

For the venv mode, start `uv_venv_manager` first:

```bash
cd C:\Users\asus\Documents\uv_venv_manager
docker compose up -d
```

### Mode Recipes

These snippets use the demo graph so they stay short and executable.
Run them from the repository root. If you save a snippet outside the repo,
set `PYTHONPATH` to the repository root first.

#### Single Mode: real LLM file output, variant, rollback, resume, fork

```python
import tempfile

from examples.modes_quick_demo import run_single

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data_dir:
    result = run_single(data_dir)
    print(result["execution_id"])
```

#### Batch Mode: local batch file output with rollback/resume/fork

```python
import tempfile

from examples.modes_quick_demo import run_batch

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data_dir:
    result = run_batch(data_dir)
    print(result["fork_execution_id"])
```

#### Ray Batch Mode: distributed file output with rollback/resume/fork

```python
import tempfile

import ray

from examples.modes_quick_demo import run_ray

try:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data_dir:
        result = run_ray(data_dir)
        print(result["execution_id"])
finally:
    ray.shutdown()
```

#### Venv Mode: Ray batch plus Docker uv_venv_manager

```python
import tempfile

import ray

from examples.modes_quick_demo import run_ray

try:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data_dir:
        result = run_ray(data_dir, grpc_url="localhost:50051")
        print(result["actor_id"])
finally:
    ray.shutdown()
```

### E2E Validation with Docker uv_venv_manager

When `uv_venv_manager` is running via Docker compose, use the strict E2E checker:

```powershell
.\scripts\verify_wtb_uv_e2e.ps1 -UvVenvManagerPath C:\Users\asus\Documents\uv_venv_manager
```

The script builds/starts compose, waits for REST and gRPC, runs the strict Ray
pytest, then runs `install_checker.py --grpc-url localhost:50051`.

### Opt-in PostgreSQL and external Ray Client E2E

The live PostgreSQL tests use PostgreSQL for both WTB core metadata and
LangGraph checkpoints. They cover synchronous rollback/resume/fork plus the
async saver lifecycle:

```powershell
$env:WTB_TEST_POSTGRES_URL = "postgresql://user:password@host:5432/wtb"
python -m pytest tests/integration/test_live_postgres_control_flow.py -v
```

To test a Ray Client endpoint together with the live gRPC venv manager and the
SQLite/CAS file flow, start the Ray head and venv manager independently, then
run:

```powershell
$env:WTB_TEST_RAY_ADDRESS = "ray://127.0.0.1:10001"
$env:UV_VENV_GRPC_ADDRESS = "localhost:50051"
python -m pytest tests/integration/test_external_ray_client_file_flow.py -v
```

Using an independently started local head validates the Ray Client transport
and worker boundary. It is not evidence for a multi-host remote deployment;
run the same opt-in test against the actual cluster address for that claim.

### 1. Batch Testing with Ray (Recommended)

`bench.run_batch_test()` internally delegates to `RayBatchTestRunner`, which distributes variant combinations across a Ray ActorPool. Configure Ray through `ExecutionConfig` on your `WorkflowProject`.

```python
from wtb.sdk import (
    WTBTestBench,
    WorkflowProject,
    ExecutionConfig,
    RayConfig,
    FileTrackingConfig,
    EnvironmentConfig,
    EnvSpec,
)

# 1. Create bench (LangGraph checkpointer + SQLite configured internally)
bench = WTBTestBench.create(mode="development", data_dir="data")

# 2. Register project with Ray configuration
project = WorkflowProject(
    name="rag_pipeline",
    graph_factory=create_rag_graph,   # your LangGraph factory function
    execution=ExecutionConfig(
        batch_executor="ray",
        ray_config=RayConfig(address="auto", max_retries=3),
        checkpoint_strategy="per_node",
        checkpoint_storage="sqlite",
    ),
    file_tracking=FileTrackingConfig(enabled=True, tracked_paths=["workspace/"]),
    environment=EnvironmentConfig(
        granularity="node",
        default_env=EnvSpec(python_version="3.12", dependencies=["openai>=1.0.0"]),
    ),
)
bench.register_project(project)

# 3. Run batch test (Ray actors execute variants in parallel)
batch = bench.run_batch_test(
    project="rag_pipeline",
    variant_matrix=[
        {"retriever": "bm25", "generator": "gpt4"},
        {"retriever": "dense", "generator": "gpt4"},
        {"retriever": "hybrid", "generator": "gpt4o-mini"},
    ],
    test_cases=[
        {"query": "What is the revenue?", "result": ""},
        {"query": "List the competitors", "result": ""},
    ],
)

# 4. Inspect results
print(f"Batch status: {batch.status}")
for r in batch.results:
    print(f"  {r.combination_name}: success={r.success}, score={r.overall_score}")

# 5. Rollback or fork any result
bench.rollback_batch_result(batch.results[0])
fork = bench.fork_batch_result(batch.results[0], new_state={"temperature": 0.5})
```

### 2. Single Execution with LangGraph Checkpointing

`WTBTestBench.create(mode="development")` automatically configures a `LangGraphStateAdapter` with SQLite persistence. You never need to import the adapter directly.

```python
from langgraph.graph import StateGraph, END
from wtb.sdk import WTBTestBench, WorkflowProject

# 1. Define your LangGraph workflow
def create_graph():
    from typing import TypedDict

    class State(TypedDict):
        query: str
        result: str

    def process_node(state: State) -> dict:
        return {"result": f"Processed: {state['query']}"}

    graph = StateGraph(State)
    graph.add_node("process", process_node)
    graph.set_entry_point("process")
    graph.add_edge("process", END)
    return graph

# 2. Create bench and register project
bench = WTBTestBench.create(mode="development", data_dir="data")
project = WorkflowProject(name="my_workflow", graph_factory=create_graph)
bench.register_project(project)

# 3. Run workflow (LangGraph checkpoints at each super-step automatically)
execution = bench.run(
    project="my_workflow",
    initial_state={"query": "Hello, WTB!", "result": ""},
)
print(f"Status: {execution.status}")

# 4. Inspect checkpoints
checkpoints = bench.get_checkpoints(execution.id)
for cp in checkpoints:
    print(f"  Step {cp.step}: next={cp.next_nodes}")

# 5. Rollback
if checkpoints:
    result = bench.rollback(execution.id, checkpoint_id=str(checkpoints[0].id))
    print(f"Rollback success: {result.success}")
    if result.success:
        execution = bench.resume(execution.id)

# 6. Fork for A/B comparison
if checkpoints:
    fork = bench.fork(execution.id, checkpoint_id=str(checkpoints[0].id),
                      new_initial_state={"query": "Alternative input", "result": ""})
    print(f"Fork ID: {fork.fork_execution_id}")
    forked_execution = bench.resume(fork.fork_execution_id)
```

## Core Operations

### Checkpointing

```python
execution = bench.run(project="my_workflow", initial_state={...})
checkpoints = bench.get_checkpoints(execution.id)

for cp in checkpoints:
    print(f"Step {cp.step}: next={cp.next_nodes}, keys={list(cp.state_values.keys())}")
```

### Resume vs. Rollback vs. Fork

These operations control related but different lifecycle transitions:

| Operation | What it does | Execution ID | Selects a checkpoint? | Runs immediately? |
|---|---|---|---|---|
| `resume(execution_id, modified_state=...)` | Continues a paused or rolled-back execution from its current recovery head and may overlay state | Same | No | Yes |
| `rollback(execution_id, checkpoint_id)` | Moves the existing execution to a selected checkpoint and leaves it paused | Same | Yes | No; call `resume` |
| `fork(execution_id, checkpoint_id, new_initial_state=...)` | Creates an isolated paused branch from a checkpoint without changing the source execution | New | Yes | No; resume the returned fork ID |

`rollback` and `fork` select historical state; `resume` continues whichever
state is already selected. Neither rollback nor fork executes subsequent nodes
automatically.

```python
rollback = bench.rollback(execution.id, checkpoint_id=str(cp.id))
if not rollback.success:
    raise RuntimeError(rollback.error)

# Continue the same execution from the rolled-back checkpoint.
resumed = bench.resume(execution.id, modified_state={"query": "revised"})

# Keep the source execution unchanged and create an independent branch.
fork = bench.fork(
    execution.id,
    checkpoint_id=str(cp.id),
    new_initial_state={"query": "alternative"},
)
forked_execution = bench.resume(fork.fork_execution_id)
```

With file tracking enabled, synchronous rollback restores checkpoint-linked
files before committing the rewound state. A synchronous fork validates any
required CAS link and restores linked files when the fork is resumed. Missing
or partial required restores fail closed. Without file tracking, these control
operations affect workflow state only. File restoration and database updates
are not one distributed transaction; a later state or database failure cannot
automatically undo physical file writes that already succeeded.

The async controller has a narrower contract: calling
`arollback_to_checkpoint()` without `restore_output_dir` is state-only; pass a
destination directory to request file restoration. Missing or partial restores
abort before the adapter state is moved, but later adapter or database failures
do not compensate already-restored files. `afork()` currently forks state and
thread history, and does not provide the synchronous file-level fork/resume
contract. The async controller also does not currently expose an `aresume()`
operation equivalent to the synchronous SDK.

### Rollback

```python
rollback = bench.rollback(execution_id=execution.id, checkpoint_id=str(cp.id))
if rollback.success:
    resumed = bench.resume(execution.id)

# Alternative: select the checkpoint after a specific node, then resume.
node_rollback = bench.rollback_to_node(execution_id=execution.id, node_id="retriever")
if node_rollback.success:
    resumed = bench.resume(execution.id)
```

### Forking (A/B Testing)

```python
fork_a = bench.fork(execution.id, checkpoint_id=str(cp.id), new_initial_state={"model": "gpt-4o"})
fork_b = bench.fork(execution.id, checkpoint_id=str(cp.id), new_initial_state={"model": "gpt-4o-mini"})

exec_a = bench.resume(fork_a.fork_execution_id)
exec_b = bench.resume(fork_b.fork_execution_id)
```

### Batch Testing

```python
batch = bench.run_batch_test(
    project="my_workflow",
    variant_matrix=[
        {"retriever": "bm25", "generator": "gpt4"},
        {"retriever": "dense", "generator": "gpt4"},
    ],
    test_cases=[{"query": "What is the revenue?"}],
)

for r in batch.results:
    print(f"  {r.combination_name}: score={r.overall_score}")

bench.rollback_batch_result(batch.results[0])
```

## Environment Configuration

```bash
# Required (if your workflows use OpenAI)
export OPENAI_API_KEY="sk-..."

# Optional: Ray cluster
export RAY_ADDRESS="auto"

# Optional: Database
export WTB_DB_URL="sqlite:///data/wtb.db"
export WTB_CHECKPOINT_DB="data/wtb_checkpoints.db"
```

## Contributing

We welcome contributions! WTB is open source and actively seeking:

- Bug reports and feature requests
- New state adapter implementations
- Documentation improvements
- Performance optimizations

Partner: HKU CAMO Lab

## License

Apache License 2.0. See [LICENSE](LICENSE) for details.

---
