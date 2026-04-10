# WTB Quick Installation & Test Guide

## Prerequisites

- Python 3.11+
- uv (recommended) or pip
- Ray-compatible system (Linux/macOS/Windows)

## Step 1: Build the Packages

```bash
# Build uv-venv-manager first
cd uv_venv_manager    # Windows: cd uv_venv_manager
uv build

# Build WTB
cd ..                 # back to project root
uv build
```

This produces wheel files under `uv_venv_manager/dist/` and `dist/`.

## Step 2: Create Fresh Test Workspace

```bash
# Create new workspace
mkdir test-wtb-install
cd test-wtb-install

# Create virtual environment
uv venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows
```

## Step 3: Install Packages

```bash
# Install uv-venv-manager first (adjust path to your dist dir)
uv pip install ../uv_venv_manager/dist/uv_venv_manager-0.1.0-py3-none-any.whl

# Install WTB with all required extras
uv pip install "../dist/wtb-0.1.0-py3-none-any.whl[ray,langgraph-sqlite,venv]"
```

### Installation Options

| Extra | Description |
|-------|-------------|
| `ray` | Ray for batch execution |
| `langgraph-sqlite` | SQLite checkpoint storage |
| `langgraph-postgres` | PostgreSQL checkpoint storage |
| `api` | FastAPI REST API |
| `grpc` | gRPC API |
| `venv` | UV Venv Manager integration |
| `all` | Everything |

## Step 4: Run Installation Checker

```bash
# Copy checker to test workspace
cp ../examples/quick_start/install_checker.py .   # Linux/macOS
# copy ..\examples\quick_start\install_checker.py .  # Windows

# Run the checker
python install_checker.py
```

## Expected Output

```
============================================================
WTB Installation Checker
============================================================
Python: 3.11.x (...)
Platform: win32
============================================================

[PASS] WTB Core Imports: WTB v0.1.0 imported successfully
[PASS] LangGraph + SQLite: Graph execution and checkpointing work correctly
[PASS] UV Venv Manager: uv-venv-manager v0.1.0 imported and configured
[PASS] Ray Integration: Ray 2.x.x working correctly with 8 CPUs
[PASS] Hash Rollback: Hash chain verified: abc123 -> def456 -> ghi789
[PASS] WTB + LangGraph Integration: WorkflowProject configured and graph executes correctly
[PASS] Full E2E (Ray + LangGraph + SQLite): Batch of 5 workflows executed with checkpointing

============================================================
Results: 7/7 tests passed
All tests passed! Installation verified.
```

## Troubleshooting

### Import Errors

```bash
# Check installed packages
uv pip list | grep -i "wtb\|langgraph\|ray"    # Linux/macOS
# uv pip list | findstr -i "wtb langgraph ray"  # Windows
```

### Ray Issues

```bash
python -c "import ray; ray.init(); print(ray.cluster_resources())"
```

### SQLite Issues

```bash
python -c "from langgraph.checkpoint.sqlite import SqliteSaver; print('OK')"
```

## Quick SDK Usage Example

```python
from wtb.sdk import WorkflowProject, WTBTestBench
from langgraph.graph import StateGraph, START, END

# Define your workflow
def create_my_graph():
    from typing_extensions import TypedDict
    
    class MyState(TypedDict):
        input: str
        output: str
    
    def process(state: MyState) -> dict:
        return {"output": f"Processed: {state['input']}"}
    
    builder = StateGraph(MyState)
    builder.add_node("process", process)
    builder.add_edge(START, "process")
    builder.add_edge("process", END)
    return builder.compile()

# Register with WTB
project = WorkflowProject(
    name="my-workflow",
    graph_factory=create_my_graph
)

# Use WTB TestBench
wtb = WTBTestBench.create()
wtb.register_project(project)

# Run workflow
result = wtb.run(project="my-workflow", initial_state={"input": "hello", "output": ""})
print(result)
```
