# WTB Presentation Demo

A comprehensive demonstration of the Workflow Test Bench (WTB) SDK, showcasing enterprise-grade workflow testing capabilities with LangGraph agents.

## Overview

This demo implements a unified RAG + SQL workflow with the following features:

- **RAG Pipeline** (7 nodes): load_docs → chunk_split → embed_docs → embed_query → retrieve → grade → generate
- **SQL Agent** (1 wrapped node): Natural language to SQL with validation
- **Conditional Routing**: Routes queries to RAG or SQL based on content

## Directory Structure

```
examples/wtb_presentation/
├── graphs/                     # LangGraph workflow definitions
│   ├── state_schemas.py        # TypedDict state definitions
│   ├── rag_nodes.py            # 7 RAG pipeline nodes + variants
│   ├── sql_agent_node.py       # SQL agent as single node
│   └── unified_graph.py        # Combined RAG+SQL workflow
├── config/                     # Configuration modules
│   ├── llm_config.py           # OpenAI-compatible LLM setup
│   ├── venv_specs.py           # Per-node environment specs
│   ├── ray_resources.py        # Ray resource allocation
│   └── project_config.py       # WorkflowProject configuration
├── workspace/                  # Business data (NOT system data)
│   ├── documents/              # RAG corpus (financial reports)
│   ├── sql/                    # SQLite demo database
│   ├── embeddings/             # Generated embeddings cache
│   └── outputs/                # Execution outputs
├── data/                       # System data (checkpoints, state)
│   └── *.db                    # SQLite databases
├── scripts/                    # Demo scripts
│   ├── clean_demo_data.py      # Clean up demo data and artifacts
│   ├── run_demo.py             # Quick demos (basic, rollback, fork, batch, pause/resume)
│   └── 05_full_presentation.py # Complete guided demo
├── env.local.example           # Environment template
└── README.md                   # This file
```

## Quick Start

### 1. Install Dependencies

```bash
# Windows
cd D:\12-22
uv pip install -e .

# Linux / macOS
cd /path/to/12-22
uv pip install -e .
```

### 2. Configure LLM API

```bash
# Copy template
cp examples/wtb_presentation/env.local.example examples/wtb_presentation/env.local

# Edit env.local with your API key
# LLM_API_KEY=sk-your-key-here
```

### 3. Run Demo Scripts

```bash
# Clean demo data
uv run python examples/wtb_presentation/scripts/clean_demo_data.py

# Run all demos
uv run python examples/wtb_presentation/scripts/run_demo.py --demo=all
```

### 05_full_presentation.py

Complete guided presentation:

- All features in one script
- Interactive pauses for presenter
- Can run specific sections:
  ```bash
  python 05_full_presentation.py --section=rollback
  python 05_full_presentation.py --section=forking
  ```

## Configuration Options

### LLM Configuration (env.local)

```bash
# OpenAI-compatible API
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-your-key

# Models for generation
DEFAULT_LLM=gpt-4o-mini    # Fast, cost-effective
ALT_LLM=gpt-4o             # Higher quality

# Models for embeddings
EMBEDDING_MODEL=text-embedding-3-small
ALT_EMBEDDING_MODEL=text-embedding-3-large
```

### Ray Configuration

```python
RayConfig(
    enabled=True,
    num_cpus=4,
    node_resources={
        "rag_embed": NodeResourceConfig(num_cpus=2, memory_mb=1024),
        "rag_generate": NodeResourceConfig(num_cpus=1, memory_mb=512),
    },
)
```

### Per-Node Environment

```python
EnvironmentConfig(
    default_env=EnvSpec(
        python_version="3.12",
        packages=["openai>=1.0.0"],
    ),
    node_envs={
        "sql_agent": EnvSpec(
            python_version="3.12",
            packages=["openai>=1.0.0", "sqlite-utils"],
        ),
        "rag_embed": EnvSpec(
            python_version="3.12",
            packages=["openai>=1.0.0", "numpy"],
        ),
    },
)
```

## RAG Node Variants

The demo includes multiple variants for A/B testing:

### Retriever Variants

- `rag_retrieve` (default) - Dense vector retrieval
- `rag_retrieve_bm25_v1` - BM25 sparse keyword retrieval
- `rag_retrieve_hybrid_v1` - Hybrid (dense + BM25 fusion)

### Embedding Variants

- `rag_embed` (default) - text-embedding-3-small
- `rag_embed_bge_v1` - text-embedding-3-large

### Generator Variants

- `rag_generate` (default) - gpt-4o-mini
- `rag_generate_gpt4_v1` - gpt-4o

## Business Documents

Sample financial documents for RAG demo:

1. **q4_2025_financial_report.md** - TechFlow Q4 2025 financial report

   - Revenue: $142.3M (23% YoY growth)
   - Cloud Services, Enterprise Software, Professional Services breakdown
2. **market_analysis_2026.md** - Enterprise AI Platform market analysis

   - $156.4B TAM, 28% CAGR
   - Competitive landscape, adoption trends
3. **investment_memo_techflow.md** - Series D investment memorandum

   - $75M investment thesis
   - Financial metrics, risk factors, exit analysis

## Manual Operations Guide

### View Checkpoints

```python
from wtb.sdk import WTBTestBench

bench = WTBTestBench.builder().with_sqlite("data/demo.db").build()
checkpoints = await bench.list_checkpoints(execution_id)
for cp in checkpoints:
    print(f"{cp.checkpoint_id}: {cp.node_id}")
```

### Rollback to Checkpoint

```python
await bench.rollback(
    execution_id=execution_id,
    target_checkpoint_id=checkpoint_id,
)
```

### Create Fork

```python
fork = await bench.fork(
    execution_id=execution_id,
    checkpoint_id=checkpoint_id,
    new_initial_state={"variant": "experiment_1"},
)
```

### Modify Paused State

```python
# Get current state
state = await bench.get_state(execution_id)

# Modify
await bench.modify_state(
    execution_id=execution_id,
    state_updates={"query": "modified query"},
)

# Resume
await bench.resume(execution_id)
```

## Key Concepts

### Data Separation

- **workspace/** - Business data (documents, outputs)
- **data/** - System data (checkpoints, databases)

This ensures rollback restores business state without affecting system metadata.

### File Tracking

Uses content-addressed storage (SHA-256):

- Each file snapshot is stored by content hash
- Rollback restores exact file contents
- Different from Git - integrated with workflow checkpoints

### Branching vs Rollback

- **Rollback**: Move back in time on same execution
- **Forking**: Create parallel execution path for experimentation

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        WTB Test Bench                                    │
├─────────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐         │
│  │ WorkflowProject │  │   FileTracker   │  │   RayExecutor   │         │
│  │                 │  │                 │  │                 │         │
│  │  - graph_factory│  │  - SHA-256 hash │  │  - num_cpus     │         │
│  │  - file_tracking│  │  - tracked_paths│  │  - memory       │         │
│  │  - environment  │  │  - snapshots    │  │  - node_configs │         │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘         │
├─────────────────────────────────────────────────────────────────────────┤
│                        Unified Graph (8 nodes)                           │
│                                                                          │
│  INDEXING PHASE (cached):                                                │
│  ┌──────────┐   ┌─────────────┐   ┌─────────────┐                      │
│  │load_docs │ → │ chunk_split │ → │ embed_docs  │                      │
│  └──────────┘   └─────────────┘   └──────┬──────┘                      │
│                                          │                               │
│  QUERY PHASE (per request):              ▼                               │
│                                   ┌─────────────┐                       │
│                                   │ embed_query │                       │
│                                   └──────┬──────┘                       │
│                                          ▼                               │
│                                   ┌───────────┐                         │
│                                   │ retrieve  │                         │
│                                   └─────┬─────┘                         │
│                                         ▼                                │
│                                   ┌───────────┐                         │
│                 ┌─────────────────│   grade   │─────────┐               │
│                 │                 └───────────┘         │               │
│                 │                       │               │               │
│            [rewrite]              [generate]         [sql]             │
│                 │                       │               │               │
│                 ▼                       ▼               ▼               │
│           ┌─────────────┐        ┌──────────┐   ┌───────────┐         │
│           │ embed_query │        │ generate │   │ sql_agent │         │
│           │   (loop)    │        └──────────┘   └───────────┘         │
│           └─────────────┘                                              │
└─────────────────────────────────────────────────────────────────────────┘
```

## License

Part of the WTB project. Internal use only.
