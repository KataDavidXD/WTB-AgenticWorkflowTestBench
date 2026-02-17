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

# 6. Fork for A/B comparison
if checkpoints:
    fork = bench.fork(execution.id, checkpoint_id=str(checkpoints[0].id),
                      new_initial_state={"query": "Alternative input", "result": ""})
    print(f"Fork ID: {fork.fork_execution_id}")
```

## Core Operations

### Checkpointing

```python
execution = bench.run(project="my_workflow", initial_state={...})
checkpoints = bench.get_checkpoints(execution.id)

for cp in checkpoints:
    print(f"Step {cp.step}: next={cp.next_nodes}, keys={list(cp.state_values.keys())}")
```

### Rollback

```python
result = bench.rollback(execution_id=execution.id, checkpoint_id=str(cp.id))

# Rollback to after a specific node
result = bench.rollback_to_node(execution_id=execution.id, node_id="retriever")
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
