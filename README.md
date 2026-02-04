# Workflow Test Bench (WTB)

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Build Status](https://img.shields.io/badge/build-passing-brightgreen)](https://github.com/yourusername/wtb)

**Workflow Test Bench (WTB)** is a production-grade framework for testing, debugging, and benchmarking agentic workflows. It bridges the gap between prototype and production by ensuring **transactional integrity**, **reproducibility**, and **observability** for complex AI systems.

> **The Problem:** Modern agentic systems (RAG, autonomous agents) are not just "read-only" chat interfaces. They persist state, modify data, and evolve. Testing them requires more than simple input/output matching—it requires a rig that understands state, side effects, and concurrency.

## ⚡ The Stack

WTB is built on the shoulders of giants, integrating the best-in-class tools for modern Python engineering:

### 🕸️ LangGraph
**Orchestrate with Confidence.**
WTB leverages [LangGraph](https://langchain-ai.github.io/langgraph/) for state-native workflow definition. We treat your workflow graph as a first-class citizen, enabling deep introspection, checkpointing, and cyclic execution patterns that are impossible with linear DAGs.

### 🚀 Ray
**Scale Without Limits.**
Powered by [Ray](https://www.ray.io/), WTB scales your test execution from a single laptop to a massive cluster. Run thousands of concurrent agent simulations, variant A/B tests, and parameter sweeps with industrial-grade distributed computing capabilities.

### 🐍 uv
**Lightning-Fast Environments.**
We mandate [uv](https://github.com/astral-sh/uv) for environment management. WTB ensures that every test run happens in a pristine, reproducible, and isolated environment. No more "it works on my machine" – dependency resolution is instant and deterministic.

### 🧠 AgentGit
**Version Control for Agents.**
(Integrated Support) Treat your agent's behavior and prompts as versioned artifacts. WTB integrates with `agentgit` to track, diff, and rollback agent configurations, ensuring that you know exactly *which* version of your agent produced *which* result.

## ✨ Key Features

- **ACID-Compliant Execution**: Guarantees that your workflow state and database side-effects are always consistent, even when agents crash or hallucinate.
- **Time-Travel Debugging**: Step backward through your agent's reasoning traces. Replay specific checkpoints with different inputs to isolate bugs.
- **Content-Addressable Artifacts**: All generated files and outputs are hashed and stored immutably.
- **Variant Testing**: define multiple versions of a node (e.g., "Prompt A" vs "Prompt B") and run them competitively in the same pipeline.


note: use RayBatchTestRunner and LangGraphStateAdapter only for deveopment.

## 📦 Installation

WTB is optimized for the modern Python stack.

```bash
# Install with uv (Recommended)
uv pip install wtb

# Install with all power features (Ray, LangGraph, Observability)
uv pip install "wtb[all]"
```

## 🛠️ Quick Start

```python
from wtb.sdk import WorkflowProject, TestBench

# 1. Initialize your project
project = WorkflowProject.init("agentic-rag-v1")

# 2. Configure the Test Bench with Ray backend for parallel testing
bench = TestBench(
    project_id="agentic-rag-v1",
    storage_backend="sqlite",
    enable_ray=True  # 🚀 Enable distributed execution
)

# 3. Run a workflow variant
# WTB handles the environment isolation and state checkpointing automatically
result = bench.run_workflow(
    workflow_id="retrieval_chain",
    inputs={"query": "Explain the architecture"},
    config={
        "agent_version": "v1.2.0",  # Tracked via AgentGit
        "model": "gpt-4-turbo"
    }
)

print(f"Execution ID: {result.execution_id}")
print(f"Artifact Hash: {result.artifact_hash}")
```

## 📄 License

Apache License 2.0. See [LICENSE](LICENSE) for details.

---

*Built with ❤️ for the Open Source Community.*
