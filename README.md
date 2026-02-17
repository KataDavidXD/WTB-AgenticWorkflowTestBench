<h1 align="center">WTB: Agentic Workflow Test Bench<br>Agent Git Production Version</h1>

<div align="center">

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Build Status](https://img.shields.io/badge/build-passing-brightgreen)](https://github.com/HKU-MAS-Infra-Layer/Agent-Git)

</div>

<p align="center">
  <a href="#-overview">Overview</a>
  &nbsp;&nbsp;&bull;&nbsp;&nbsp;
  <a href="#-architecture">Architecture</a>
  &nbsp;&nbsp;&bull;&nbsp;&nbsp;
  <a href="#-installation">Installation</a>
  &nbsp;&nbsp;&bull;&nbsp;&nbsp;
  <a href="#-quick-start">Quick Start</a>
  &nbsp;&nbsp;&bull;&nbsp;&nbsp;
  <a href="#-core-operations">Core Operations</a>
</p>

##

**WTB (Workflow Test Bench)** is a production-grade testing, debugging, and benchmarking framework for agentic workflows. Built as the testing layer for [Agent Git](https://github.com/HKU-MAS-Infra-Layer/Agent-Git), WTB ensures **transactional integrity**, **reproducibility**, and **observability** for complex AI agent systems by combining LangGraph orchestration, Ray distributed computing, content-addressable storage, and UV environment isolation.

> **The Problem:** Modern agentic systems (RAG, autonomous agents) are not just "read-only" chat interfaces. They persist state, modify data, and evolve. Testing them requires more than simple input/output matching -- it requires a rig that understands state, side effects, and concurrency.

## Overview

WTB provides a comprehensive workflow testing framework that wraps around LangGraph agent graphs and enables:

- **Checkpoint & Rollback**: Create restore points at every node boundary and travel back in execution history
- **Forking (A/B Testing)**: Create independent execution branches from any checkpoint for variant comparison
- **Batch Testing**: Run multiple test cases and variant combinations in parallel via Ray
- **File Version Control**: Track all generated files with content-addressable storage (SHA-256 hashing)
- **Environment Isolation**: Per-node virtual environments via UV for dependency safety

### Core Concepts

* **WorkflowProject**: Configuration container holding a LangGraph graph factory, file tracking settings, environment specs, and Ray resource allocations.
* **WTBTestBench**: Main SDK entry point (Facade pattern) that orchestrates project registration, execution, checkpointing, rollback, forking, and batch testing.
* **Execution**: A single run of a workflow graph, tracked with status, state, and checkpoints.
* **Checkpoint**: A snapshot of workflow state at a node boundary, enabling time-travel and rollback.
* **BatchTest**: A collection of variant combinations executed in parallel, producing a comparison matrix.

### Key Features

- **ACID-Compliant Execution**: Guarantees workflow state and database side-effects are always consistent, even when agents crash or hallucinate.
- **Time-Travel Debugging**: Step backward through your agent's reasoning traces. Replay specific checkpoints with different inputs to isolate bugs.
- **Content-Addressable Artifacts**: All generated files and outputs are SHA-256 hashed and stored immutably in blob storage.
- **Variant Testing**: Define multiple versions of a node (e.g., "Prompt A" vs "Prompt B") and run them competitively in the same pipeline.
- **Distributed Execution**: Scale from a single laptop to a Ray cluster with per-node resource allocation.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              SDK Layer                                          │
│                                                                                 │
│   WorkflowProject ──────► WTBTestBench ◄──── WTBTestBenchBuilder               │
│   (graph_factory,          (run, rollback,    (for_testing,                      │
│    file_tracking,           fork, batch,       for_development,                  │
│    env_config,              get_checkpoints)   for_production)                   │
│    ray_config)                                                                  │
└───────────────────────────────────┬─────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
┌──────────────────────┐ ┌──────────────────┐ ┌──────────────────────────────────┐
│   ExecutionController│ │  ProjectService  │ │     RayBatchTestRunner           │
│   (run, pause,       │ │  VariantService  │ │                                  │
│    resume, rollback, │ │                  │ │  ┌─────────────────────────────┐  │
│    fork)             │ │                  │ │  │  Ray ActorPool              │  │
│                      │ │                  │ │  │  ┌───────┐ ┌───────┐       │  │
│                      │ │                  │ │  │  │Actor 0│ │Actor 1│  ...  │  │
│                      │ │                  │ │  │  │ UoW   │ │ UoW   │       │  │
│                      │ │                  │ │  │  │ State │ │ State │       │  │
│                      │ │                  │ │  │  │ Files │ │ Files │       │  │
│                      │ │                  │ │  │  └───────┘ └───────┘       │  │
│                      │ │                  │ │  └─────────────────────────────┘  │
└──────────┬───────────┘ └──────────────────┘ └────────────────┬─────────────────┘
           │                                                   │
           └──────────────────┬────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         Infrastructure Layer                                    │
│                                                                                 │
│  ┌─────────────────────┐  ┌──────────────────────┐  ┌───────────────────────┐  │
│  │   LangGraph         │  │  Content-Addressable  │  │   UV Environment      │  │
│  │   State Adapter     │  │  Storage (CAS)        │  │   Manager             │  │
│  │                     │  │                       │  │                       │  │
│  │  StateGraph ──►     │  │  File ──► SHA-256     │  │  EnvSpec ──►          │  │
│  │  compile(           │  │           hash        │  │  uv venv create      │  │
│  │   checkpointer)     │  │           │           │  │   --python 3.12      │  │
│  │       │             │  │           ▼           │  │       │              │  │
│  │       ▼             │  │  BlobId: objects/     │  │       ▼              │  │
│  │  Checkpointer       │  │    b9/4d27b9...      │  │  Isolated venv       │  │
│  │  ┌────────────────┐ │  │           │           │  │  per node/variant    │  │
│  │  │ Memory         │ │  │           ▼           │  │                       │  │
│  │  │ SQLite         │ │  │  FileCommit           │  │  Granularity:         │  │
│  │  │ PostgreSQL     │ │  │   ├── FileMemento     │  │  - workflow           │  │
│  │  └────────────────┘ │  │   ├── FileMemento     │  │  - node              │  │
│  │       │             │  │   └── ...             │  │  - variant            │  │
│  │       ▼             │  │           │           │  │                       │  │
│  │  checkpoint_id ◄────┼──┼── CommitId            │  │                       │  │
│  │  (time-travel)      │  │  (rollback restore)   │  │                       │  │
│  └─────────────────────┘  └──────────────────────┘  └───────────────────────┘  │
│                                                                                 │
│  ┌─────────────────────────────────────────────────────────────────────────────┐│
│  │  SQLAlchemy Unit of Work  ──►  Executions | Workflows | Checkpoints | Audit ││
│  └─────────────────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────────────────┘
```

**Data flow for a single execution:**

```
User                    SDK                      LangGraph               CAS
 │                       │                          │                     │
 │── bench.run() ──────► │                          │                     │
 │                       │── compile(checkpointer) ►│                     │
 │                       │── invoke(state) ────────►│                     │
 │                       │                          │── node A ──────────►│
 │                       │                          │   checkpoint ───────│── hash files
 │                       │                          │── node B ──────────►│   store blobs
 │                       │                          │   checkpoint ───────│── hash files
 │                       │◄── Execution result ─────│                     │   store blobs
 │◄── Execution ─────────│                          │                     │
 │                       │                          │                     │
 │── bench.rollback() ──►│                          │                     │
 │                       │── load_checkpoint() ────►│                     │
 │                       │                          │◄── restore state    │
 │                       │── restore_files() ──────────────────────────► │
 │                       │                          │    ◄── restore blobs│
 │◄── RollbackResult ───│                          │                     │
```

**Batch testing with Ray actors:**

```
                   WTBTestBench
                       │
              bench.run_batch_test()
                       │
                       ▼
               RayBatchTestRunner
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
    ┌──────────┐ ┌──────────┐ ┌──────────┐
    │ Actor 0  │ │ Actor 1  │ │ Actor 2  │     Ray ActorPool
    │ variant_a│ │ variant_b│ │ variant_c│     (parallel execution)
    │          │ │          │ │          │
    │ LangGraph│ │ LangGraph│ │ LangGraph│     Each actor has isolated:
    │ Adapter  │ │ Adapter  │ │ Adapter  │     - LangGraph checkpointer
    │ UoW      │ │ UoW      │ │ UoW      │     - Unit of Work
    │ CAS      │ │ CAS      │ │ CAS      │     - File tracking
    └────┬─────┘ └────┬─────┘ └────┬─────┘
         │            │            │
         └────────────┼────────────┘
                      ▼
               BatchTest result
               (comparison matrix)
```

## Timeline

[Jan 2026]: v0.2.0 -- Introduced Ray batch execution, LangGraph checkpoint integration, content-addressable file tracking, workspace isolation, and batch rollback/fork coordination.

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
git clone https://github.com/HKU-MAS-Infra-Layer/Agent-Git.git
cd Agent-Git

# Install with uv
uv pip install -e ".[all]"

# Or with pip
pip install -e ".[all]"
```

### Required Dependencies

```bash
# Core dependencies (installed automatically)
# sqlalchemy>=2.0.0, langgraph>=2.0.60, langgraph-checkpoint>=2.0.10

# Optional: Set OpenAI API key if your workflow uses LLMs
export OPENAI_API_KEY="your-api-key-here"
```

## Quick Start

> **Note:** Use `RayBatchTestRunner` and `LangGraphStateAdapter` only via the SDK for development. All examples below import only from `wtb.sdk`.

### 1. Run a Workflow with LangGraph Checkpointing

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

# 2. Create WTBTestBench (uses LangGraphStateAdapter + SQLite checkpointer internally)
bench = WTBTestBench.create(mode="development", data_dir="data")

# 3. Register your workflow project
project = WorkflowProject(
    name="my_workflow",
    graph_factory=create_graph,
    description="Simple processing workflow",
)
bench.register_project(project)

# 4. Run the workflow (LangGraph checkpoints at each super-step automatically)
execution = bench.run(
    project="my_workflow",
    initial_state={"query": "Hello, WTB!", "result": ""},
)
print(f"Execution ID: {execution.id}")
print(f"Status: {execution.status}")

# 5. Inspect checkpoints created by LangGraph
checkpoints = bench.get_checkpoints(execution.id)
for cp in checkpoints:
    print(f"  Checkpoint {cp.id} at step {cp.step}, next={cp.next_nodes}")

# 6. Rollback to any checkpoint
if checkpoints:
    result = bench.rollback(execution.id, checkpoint_id=str(checkpoints[0].id))
    print(f"Rollback success: {result.success}")

# 7. Fork for A/B comparison
if checkpoints:
    fork = bench.fork(
        execution.id,
        checkpoint_id=str(checkpoints[0].id),
        new_initial_state={"query": "Alternative input", "result": ""},
    )
    print(f"Fork execution ID: {fork.fork_execution_id}")
```

### 2. Batch Testing with Ray (via SDK)

`bench.run_batch_test()` internally delegates to `RayBatchTestRunner` when Ray is available. Configure Ray through `ExecutionConfig` on your `WorkflowProject`.

```python
from wtb.sdk import (
    WTBTestBench,
    WTBTestBenchBuilder,
    WorkflowProject,
    ExecutionConfig,
    RayConfig,
    FileTrackingConfig,
    EnvironmentConfig,
    EnvSpec,
)

# 1. Create bench via builder (configures Ray batch runner internally)
bench = WTBTestBenchBuilder().for_development("data").build()

# 2. Register project with Ray and per-node environment configuration
project = WorkflowProject(
    name="rag_pipeline",
    graph_factory=create_rag_graph,
    execution=ExecutionConfig(
        batch_executor="ray",
        ray_config=RayConfig(address="auto", max_retries=3),
        checkpoint_strategy="per_node",
        checkpoint_storage="sqlite",
    ),
    file_tracking=FileTrackingConfig(
        enabled=True,
        tracked_paths=["workspace/"],
    ),
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

# 4. Inspect comparison results
print(f"Batch status: {batch.status}")
for result in batch.results:
    print(f"  {result.combination_name}: success={result.success}, score={result.overall_score}")

# 5. Rollback or fork any batch result (via SDK)
bench.rollback_batch_result(batch.results[0])
fork = bench.fork_batch_result(batch.results[0], new_state={"temperature": 0.5})
print(f"Forked execution: {fork.fork_execution_id}")
```

## Core Operations

### Checkpointing

```python
# LangGraph creates checkpoints at each super-step automatically
execution = bench.run(project="my_workflow", initial_state={...})
checkpoints = bench.get_checkpoints(execution.id)

# Inspect checkpoint state
for cp in checkpoints:
    print(f"Step {cp.step}: next={cp.next_nodes}, keys={list(cp.state_values.keys())}")
```

### Rollback

```python
# Rollback to any checkpoint (restores workflow state + file system)
result = bench.rollback(execution_id=execution.id, checkpoint_id=str(cp.id))
print(f"Success: {result.success}")

# Rollback to after a specific node
result = bench.rollback_to_node(execution_id=execution.id, node_id="retriever")
```

### Forking (A/B Testing)

```python
# Fork creates an independent execution from a checkpoint
fork_a = bench.fork(execution.id, checkpoint_id=str(cp.id), new_initial_state={"model": "gpt-4o"})
fork_b = bench.fork(execution.id, checkpoint_id=str(cp.id), new_initial_state={"model": "gpt-4o-mini"})

# Resume forked executions independently
exec_a = bench.resume(fork_a.fork_execution_id)
exec_b = bench.resume(fork_b.fork_execution_id)
```

### Batch Testing

```python
# Run batch test with variant matrix (uses Ray actors internally)
batch = bench.run_batch_test(
    project="my_workflow",
    variant_matrix=[
        {"retriever": "bm25", "generator": "gpt4"},
        {"retriever": "dense", "generator": "gpt4"},
    ],
    test_cases=[
        {"query": "What is the revenue?"},
        {"query": "List the competitors"},
    ],
)

# Inspect results and comparison matrix
for result in batch.results:
    print(f"  {result.combination_name}: score={result.overall_score}")

# Rollback or fork any batch result
bench.rollback_batch_result(batch.results[0])
fork = bench.fork_batch_result(batch.results[0], new_state={"temperature": 0.5})
```

## Environment Configuration

```bash
# Required (if your workflows use OpenAI)
export OPENAI_API_KEY="sk-..."

# Optional: Ray cluster
export RAY_ADDRESS="auto"            # Local cluster
# export RAY_ADDRESS="ray://host:10001"  # Remote cluster

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

Partners: **HKU Lab for AI Agents in Business and Economics**

## License

Apache License 2.0. See [LICENSE](LICENSE) for details.

---

Built for the LangChain/LangGraph community.
