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

WTB follows **Domain-Driven Design (DDD)** with **SOLID principles**, organized in a layered architecture:

```
wtb/
 ├── sdk/                     # SDK Layer (User-facing API)
 │    ├── workflow_project.py  # WorkflowProject configuration
 │    └── test_bench.py        # WTBTestBench facade
 │
 ├── application/              # Application Layer (Use Cases)
 │    ├── services/
 │    │    ├── ray_batch_runner.py          # RayBatchTestRunner + VariantExecutionActor
 │    │    ├── execution_controller.py      # Workflow lifecycle management
 │    │    ├── batch_execution_coordinator.py  # Batch rollback/fork coordinator
 │    │    └── langgraph_node_replacer.py   # Node variant substitution
 │    └── factories.py                      # DI composition root
 │
 ├── domain/                   # Domain Layer (Business Logic)
 │    ├── models/
 │    │    ├── workflow.py          # Execution, ExecutionState, TestWorkflow
 │    │    ├── checkpoint.py        # Checkpoint, CheckpointId
 │    │    ├── batch_test.py        # BatchTest, BatchTestResult
 │    │    └── file_processing/     # BlobId (SHA-256), CommitId, FileMemento
 │    └── interfaces/              # Abstractions (IStateAdapter, IBatchTestRunner, ...)
 │
 ├── infrastructure/           # Infrastructure Layer (Implementations)
 │    ├── adapters/
 │    │    └── langgraph_state_adapter.py   # LangGraph checkpointer integration
 │    ├── file_tracking/                    # Content-addressable file storage
 │    ├── database/                         # SQLAlchemy UoW + repositories
 │    ├── events/                           # Event bus, Ray event bridge, audit trail
 │    └── workspace/                        # Workspace isolation manager
 │
 └── api/                      # API Layer (REST, WebSocket, gRPC)
```

### Content-Addressable Storage (CAS)

WTB uses a Git-like content-addressable storage system for file versioning:

- **BlobId**: SHA-256 hash of file content, used as the storage key (e.g., `objects/b9/4d27b9...`)
- **FileMemento**: Immutable snapshot of a file's metadata (path, hash, size) following the Memento Pattern
- **FileCommit**: Aggregate root grouping multiple FileMementos into an atomic commit
- **CommitId**: UUID identifier for each commit, linked to WTB checkpoints for rollback

When a checkpoint is created, all tracked output files are hashed and stored. On rollback, files are restored from blob storage to their original paths, ensuring exact byte-level reproduction of the file system state.

### Ray Actor Model

WTB uses [Ray](https://www.ray.io/) Actors for distributed batch test execution:

- **RayBatchTestRunner**: Orchestrator that manages an ActorPool, submits variant combinations, and aggregates results with backpressure control via `ray.wait()`.
- **VariantExecutionActor**: A Ray Actor (`@ray.remote`) that executes a single workflow variant with isolated state. Each actor lazily initializes its own `LangGraphStateAdapter`, `UnitOfWork`, and `FileTrackingService` to ensure ACID isolation across parallel executions.
- **ObjectRef Tracking**: All pending executions are tracked as Ray ObjectRefs, enabling cancellation, progress monitoring, and timeout handling.

### LangGraph Integration

WTB is built on top of [LangGraph](https://langchain-ai.github.io/langgraph/) for state-native workflow orchestration:

- **LangGraphStateAdapter**: Primary state adapter that wraps LangGraph's `StateGraph` compilation with a checkpointer (Memory, SQLite, or PostgreSQL). Implements `IStateAdapter` for checkpoint save/load/rollback operations.
- **Automatic Checkpointing**: LangGraph saves a checkpoint at each super-step. WTB enriches these with node boundary tracking and file system snapshots.
- **Time-Travel**: Uses `get_state_history()` to retrieve the full checkpoint timeline for debugging and rollback target selection.

### UV Environment Isolation

WTB integrates with [uv](https://github.com/astral-sh/uv) for fast, reproducible environment management:

- **Per-Node Environments**: Each workflow node can specify its own Python version and dependencies via `EnvSpec`.
- **Granularity Levels**: Environment isolation at workflow, node, or variant level.
- **UV Venv Manager Service**: Optional gRPC/HTTP service for managing virtual environments across distributed workers.
- **Deterministic Resolution**: uv's lockfile ensures every test run uses the exact same dependency versions.

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

### Minimal Example: Run and Checkpoint a Workflow

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

# 2. Create WTBTestBench (development mode with SQLite persistence)
bench = WTBTestBench.create(mode="development", data_dir="data")

# 3. Register your workflow project
project = WorkflowProject(
    name="my_workflow",
    graph_factory=create_graph,
    description="Simple processing workflow",
)
bench.register_project(project)

# 4. Run the workflow
execution = bench.run(
    project="my_workflow",
    initial_state={"query": "Hello, WTB!", "result": ""},
)

print(f"Execution ID: {execution.id}")
print(f"Status: {execution.status}")

# 5. Inspect checkpoints (LangGraph creates one per super-step)
checkpoints = bench.get_checkpoints(execution.id)
for cp in checkpoints:
    print(f"  Checkpoint {cp.id} at step {cp.step}")
```

### Using RayBatchTestRunner for Distributed Batch Testing

```python
from wtb.sdk import WTBTestBench, WorkflowProject
from wtb.application.services.ray_batch_runner import RayBatchTestRunner
from wtb.config import RayConfig
from wtb.domain.models.batch_test import BatchTest, VariantCombination

# 1. Create your workflow project (same graph_factory as above)
project = WorkflowProject(
    name="batch_workflow",
    graph_factory=create_graph,
)

# 2. Initialize Ray batch runner
runner = RayBatchTestRunner(
    config=RayConfig.for_local_development(),
    agentgit_db_url="data/agentgit.db",
    wtb_db_url="sqlite:///data/wtb.db",
)

# 3. Define batch test with variant combinations
batch_test = BatchTest(workflow_id="batch_workflow")
batch_test.variant_combinations = [
    VariantCombination(name="variant_a", variants={"process": "default"}),
    VariantCombination(name="variant_b", variants={"process": "alternative"}),
]
batch_test.initial_state = {"query": "Test query", "result": ""}

# 4. Execute batch test (runs in parallel on Ray actors)
result = runner.run_batch_test(batch_test)

# 5. Inspect results
print(f"Batch status: {result.status}")
for r in result.results:
    print(f"  {r.combination_name}: success={r.success}, score={r.overall_score}")

# 6. Rollback a specific variant result
coordinator = runner.create_rollback_coordinator()
if result.results[0].last_checkpoint_id:
    coordinator.rollback(
        execution_id=result.results[0].execution_id,
        checkpoint_id=result.results[0].last_checkpoint_id,
    )
```

### Using LangGraphStateAdapter Directly

```python
from wtb.infrastructure.adapters.langgraph_state_adapter import (
    LangGraphStateAdapter,
    LangGraphConfig,
)
from wtb.domain.models.workflow import ExecutionState

# 1. Create adapter with SQLite checkpointer
adapter = LangGraphStateAdapter(
    LangGraphConfig.for_development("data/checkpoints.db")
)

# 2. Set your LangGraph workflow
adapter.set_workflow_graph(create_graph())  # Recompiles with checkpointer

# 3. Initialize a session
session_id = adapter.initialize_session(
    execution_id="exec-001",
    initial_state=ExecutionState(),
)

# 4. Execute with automatic checkpointing at each super-step
result = adapter.execute({"query": "Hello", "result": ""})

# 5. Time-travel: inspect checkpoint history
history = adapter.get_checkpoint_history()
for entry in history:
    print(f"  Step {entry['step']}: checkpoint={entry['checkpoint_id'][:8]}...")

# 6. Rollback to any checkpoint
if history:
    restored_state = adapter.rollback(history[-1]["checkpoint_id"])
    print(f"Rolled back to step {history[-1]['step']}")
```

## Usage Patterns

WTB offers three usage levels: **SDK** (recommended), **Application Services** (advanced), and **Infrastructure** (low-level).

### SDK: WTBTestBench (Recommended)

Full-featured facade for workflow testing with minimal setup.

```python
from wtb.sdk import WTBTestBench, WorkflowProject, ExecutionConfig, RayConfig

# Create bench with configuration
bench = WTBTestBench.create(mode="development", data_dir="data")

# Register project with Ray and checkpoint configuration
project = WorkflowProject(
    name="rag_pipeline",
    graph_factory=create_rag_graph,
    execution=ExecutionConfig(
        batch_executor="ray",
        ray_config=RayConfig(address="auto"),
        checkpoint_strategy="per_node",
        checkpoint_storage="sqlite",
    ),
)
bench.register_project(project)

# Run, checkpoint, rollback, fork
execution = bench.run(project="rag_pipeline", initial_state={"query": "..."})
checkpoints = bench.get_checkpoints(execution.id)
bench.rollback(execution.id, checkpoint_id=str(checkpoints[0].id))
fork = bench.fork(execution.id, checkpoint_id=str(checkpoints[0].id))
```

### Application: RayBatchTestRunner (Advanced)

Direct control over Ray actors and batch orchestration.

```python
from wtb.application.services.ray_batch_runner import RayBatchTestRunner
from wtb.config import RayConfig

runner = RayBatchTestRunner(
    config=RayConfig.for_local_development(),
    agentgit_db_url="data/agentgit.db",
    wtb_db_url="sqlite:///data/wtb.db",
    filetracker_config={"enabled": True, "tracked_paths": ["workspace/"]},
)

# Run batch, get actor stats, cancel, shutdown
result = runner.run_batch_test(batch_test)
stats = runner.get_actor_stats()
runner.shutdown()
```

### Infrastructure: LangGraphStateAdapter (Low-Level)

Direct checkpoint manipulation for custom integrations.

```python
from wtb.infrastructure.adapters.langgraph_state_adapter import (
    LangGraphStateAdapter, LangGraphConfig
)

adapter = LangGraphStateAdapter(LangGraphConfig.for_development())
adapter.set_workflow_graph(graph)
adapter.initialize_session("exec-id", initial_state)
result = adapter.execute(initial_state)
history = adapter.get_checkpoint_history()
adapter.rollback(checkpoint_id)
```

## Core Operations

### Checkpointing

```python
# Automatic checkpoints: LangGraph creates one per super-step
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
# Fork creates independent execution from checkpoint
fork_a = bench.fork(execution.id, checkpoint_id=str(cp.id), new_initial_state={"model": "gpt-4o"})
fork_b = bench.fork(execution.id, checkpoint_id=str(cp.id), new_initial_state={"model": "gpt-4o-mini"})

# Run forks independently
exec_a = bench.resume(fork_a.fork_execution_id)
exec_b = bench.resume(fork_b.fork_execution_id)
```

### Batch Testing

```python
# Run batch test with variant matrix
batch = bench.run_batch_test(
    project="my_workflow",
    variant_matrix=[
        {"retriever": "bm25", "generator": "gpt4"},
        {"retriever": "dense", "generator": "gpt4"},
        {"retriever": "hybrid", "generator": "gpt4o-mini"},
    ],
    test_cases=[
        {"query": "What is the revenue?"},
        {"query": "List the competitors"},
    ],
)

# Inspect comparison matrix
print(f"Status: {batch.status}")
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
