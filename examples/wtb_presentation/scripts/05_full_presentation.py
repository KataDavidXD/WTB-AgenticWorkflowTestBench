#!/usr/bin/env python
"""
05_full_presentation.py - Complete WTB Presentation Demo

REFACTORED (2026-01-27): Uses synchronous SDK methods.

This is the MAIN presentation script that walks through all WTB features
in a guided, presentation-ready format.

Demonstrates:
1. Project Setup - WorkflowProject, FileTracking, Environment configs
2. Basic Execution - Run unified RAG+SQL workflow
3. Checkpointing - View checkpoints at each node
4. Rollback - Restore workflow and file system state
5. Forking - A/B testing with fork() method
6. Pause/Resume - Human-in-the-loop review
7. Batch Testing - Multiple queries, multiple variants
8. Ray Distribution - Parallel node execution
9. Venv Isolation - Per-node virtual environments

SDK Methods Used:
- WTBTestBench.create(mode, data_dir, enable_file_tracking)
- bench.register_project(project)
- bench.run(project, initial_state, breakpoints)
- bench.get_checkpoints(execution_id)
- bench.get_state(execution_id)
- bench.rollback(execution_id, checkpoint_id)
- bench.fork(execution_id, checkpoint_id, new_initial_state)
- bench.pause(execution_id)
- bench.resume(execution_id, modified_state)
- bench.run_batch_test(project, variant_matrix, test_cases)

Usage:
    python scripts/05_full_presentation.py
    
    # Run specific section:
    python scripts/05_full_presentation.py --section=rollback
    python scripts/05_full_presentation.py --section=forking

Prerequisites:
    - Configure env.local with LLM_API_KEY
    - Optionally configure RAY_ADDRESS for distributed execution
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ═══════════════════════════════════════════════════════════════════════════════
# Imports
# ═══════════════════════════════════════════════════════════════════════════════

from wtb.sdk import (
    WTBTestBench,
    WorkflowProject,
)

# Import project factory (centralizes graph, env, ray, file-tracking config)
from examples.wtb_presentation.config.project_config import create_demo_project

# ═══════════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════════

WORKSPACE_DIR = Path(__file__).parent.parent / "workspace"
DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUTS_DIR = WORKSPACE_DIR / "outputs"

# Ensure directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Presentation Utilities
# ═══════════════════════════════════════════════════════════════════════════════

def print_header(title: str, char: str = "=") -> None:
    """Print formatted section header."""
    print(f"\n{char * 70}")
    print(f"  {title}")
    print(f"{char * 70}")


def print_step(step: int, description: str) -> None:
    """Print formatted step."""
    print(f"\n[Step {step}] {description}")


def wait_for_input(prompt: str = "Press Enter to continue...") -> None:
    """Wait for presenter input (for paced demo)."""
    if os.environ.get("WTB_AUTO_DEMO", "false").lower() != "true":
        input(f"\n  {prompt}")


def format_duration(seconds: float) -> str:
    """Format duration for display."""
    if seconds < 1:
        return f"{seconds*1000:.1f}ms"
    return f"{seconds:.2f}s"


def write_output_files_from_state(
    state: Dict[str, Any],
    output_dir: Path,
    prefix: str = "",
) -> List[Path]:
    """
    Write _output_files from execution state to disk.
    
    This bridges the gap between _output_files in workflow state 
    (filename→content) and actual files that can be tracked.
    
    Args:
        state: Execution state containing _output_files
        output_dir: Directory to write files to
        prefix: Optional prefix for subdirectory
        
    Returns:
        List of paths written
    """
    output_files = state.get("_output_files", {})
    if not output_files:
        return []
    
    target_dir = output_dir / prefix if prefix else output_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    
    written_paths = []
    for filename, content in output_files.items():
        # Sanitize filename
        safe_filename = Path(filename).name
        if not safe_filename:
            continue
        
        file_path = target_dir / safe_filename
        try:
            if isinstance(content, bytes):
                file_path.write_bytes(content)
            else:
                file_path.write_text(str(content), encoding="utf-8")
            written_paths.append(file_path)
        except Exception as e:
            print(f"  [WARN] Failed to write {filename}: {e}")
    
    return written_paths


# ═══════════════════════════════════════════════════════════════════════════════
# Section 1: Project Setup
# ═══════════════════════════════════════════════════════════════════════════════

def demo_project_setup(bench: WTBTestBench) -> WorkflowProject:
    """Demonstrate project configuration and setup."""
    print_header("SECTION 1: PROJECT SETUP")
    
    print("""
  In this section, we configure a WorkflowProject with:
  - LangGraph workflow (unified RAG + SQL graph)
  - File system tracking for state restoration
  - Virtual environment specifications per node
  - Ray configuration for distributed execution
    """)
    
    wait_for_input()
    
    print_step(1, "Creating WorkflowProject configuration...")
    
    project = create_demo_project(
        name="wtb_full_presentation",
        enable_file_tracking=True,
        enable_venv_isolation=True,
        enable_ray=True,
        data_dir=DATA_DIR,
        uv_manager_url="http://localhost:10900",
    )
    
    print(f"  - Project name: {project.name}")
    print(f"  - File tracking: {project.file_tracking.enabled}")
    print(f"  - Tracked paths: {project.file_tracking.tracked_paths}")
    print(f"  - Node environments: {list(project.environment.node_environments.keys())}")
    print(f"  - Ray executor: {project.execution.batch_executor}")
    print(f"  - Pause mode: {project.pause_strategy.mode}")
    
    print_step(2, "Registering project with WTB...")
    try:
        bench.register_project(project)
        print("  - Project registered successfully!")
    except ValueError as e:
        if "already registered" in str(e):
            bench.unregister_project(project.name)
            bench.register_project(project)
            print("  - Project re-registered successfully!")
        else:
            raise
    
    return project


# ═══════════════════════════════════════════════════════════════════════════════
# Section 2: Basic Execution
# ═══════════════════════════════════════════════════════════════════════════════

def demo_basic_execution(bench: WTBTestBench, project: WorkflowProject):
    """Demonstrate basic workflow execution."""
    print_header("SECTION 2: BASIC EXECUTION")
    
    print("""
  Running the unified RAG + SQL workflow:
  1. Load documents from workspace/documents
  2. Chunk and embed documents
  3. Route query to RAG or SQL based on content
  4. Generate answer from retrieved context
    """)
    
    wait_for_input()
    
    # RAG query
    print_step(1, "Executing RAG query...")
    rag_query = "What was TechFlow's revenue growth in Q4 2025?"
    print(f"  Query: {rag_query}")
    
    start_time = time.time()
    result = bench.run(
        project=project.name,
        initial_state={"query": rag_query, "messages": []},
    )
    duration = time.time() - start_time
    
    print(f"  - Execution ID: {result.id}")
    print(f"  - Status: {result.status}")
    print(f"  - Duration: {format_duration(duration)}")
    
    # Access state from Execution domain model (result.state is ExecutionState)
    if hasattr(result, 'state') and result.state:
        state_vars = result.state.workflow_variables if hasattr(result.state, 'workflow_variables') else {}
        answer = state_vars.get("answer", "N/A")
        if answer and answer != "N/A":
            print(f"\n  Answer Preview:")
            print(f"  {answer[:200]}..." if len(str(answer)) > 200 else f"  {answer}")
        
        # Write _output_files from state to disk
        written = write_output_files_from_state(state_vars, OUTPUTS_DIR, prefix="rag_execution")
        if written:
            print(f"\n  Output files written: {len(written)}")
            for p in written[:3]:  # Show first 3
                print(f"    - {p.name}")
    
    wait_for_input()
    
    # SQL query
    print_step(2, "Executing SQL query (routed to SQL agent)...")
    sql_query = "How many customers are in the database?"
    print(f"  Query: {sql_query}")
    
    start_time = time.time()
    sql_result = bench.run(
        project=project.name,
        initial_state={"query": sql_query, "messages": []},
    )
    duration = time.time() - start_time
    
    print(f"  - Status: {sql_result.status}")
    print(f"  - Duration: {format_duration(duration)}")
    
    if hasattr(sql_result, 'state') and sql_result.state:
        state_vars = sql_result.state.workflow_variables if hasattr(sql_result.state, 'workflow_variables') else {}
        sql_answer = state_vars.get("answer", state_vars.get("sql_result", "N/A"))
        if sql_answer and sql_answer != "N/A":
            print(f"\n  SQL Result:")
            print(f"  {str(sql_answer)[:200]}...")
        
        # Write _output_files from SQL execution
        written = write_output_files_from_state(state_vars, OUTPUTS_DIR, prefix="sql_execution")
        if written:
            print(f"\n  Output files written: {len(written)}")
            for p in written[:3]:
                print(f"    - {p.name}")
    
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Section 3: Checkpointing
# ═══════════════════════════════════════════════════════════════════════════════

def demo_checkpointing(bench: WTBTestBench, execution_id: str):
    """Demonstrate checkpoint inspection."""
    print_header("SECTION 3: CHECKPOINTING (SQLite Persistence)")
    
    print("""
  Each node execution creates a checkpoint containing:
  - Workflow state at that point (LangGraph checkpoint)
  - File system snapshot (content-addressed)
  - Execution metadata
  
  REAL PERSISTENCE:
  - Checkpoints stored in SQLite: data/wtb_checkpoints.db
  - Survives process restart
  - Supports time-travel debugging
    """)
    
    wait_for_input()
    
    print_step(1, "Listing checkpoints from last execution...")
    
    checkpoints = bench.get_checkpoints(execution_id)
    
    print(f"\n  Found {len(checkpoints)} checkpoints:")
    print(f"  {'Index':<6} {'Step':<6} {'Next Nodes':<20} {'State Keys':<25} {'Checkpoint ID':<20}")
    print(f"  {'-'*6} {'-'*6} {'-'*20} {'-'*25} {'-'*20}")
    
    for i, cp in enumerate(checkpoints):
        # Show step, next_nodes (what will run), and key state fields
        step = cp.step
        next_nodes = ", ".join(cp.next_nodes[:2]) if cp.next_nodes else "(terminal)"
        # Get state keys that aren't internal
        state_keys = [k for k in cp.state_values.keys() if not k.startswith("_")][:3]
        state_preview = ", ".join(state_keys) if state_keys else "(empty)"
        cp_id = str(cp.id)[:20]
        print(f"  {i:<6} {step:<6} {next_nodes:<20} {state_preview:<25} {cp_id:<20}")
    
    # Show detailed checkpoint info
    if checkpoints:
        print_step(2, "Checkpoint state preview (latest)...")
        latest_cp = checkpoints[0]
        state = latest_cp.state_values
        print(f"  - Step: {latest_cp.step}")
        print(f"  - Next nodes: {latest_cp.next_nodes}")
        if "query" in state:
            print(f"  - Query: {str(state.get('query', ''))[:60]}...")
        if "answer" in state:
            answer = str(state.get("answer", ""))[:100]
            print(f"  - Answer: {answer}...")
    
    return checkpoints


# ═══════════════════════════════════════════════════════════════════════════════
# Section 4: Rollback
# ═══════════════════════════════════════════════════════════════════════════════

def demo_rollback(bench: WTBTestBench, execution_id: str, checkpoints: List):
    """Demonstrate rollback functionality."""
    print_header("SECTION 4: ROLLBACK (State + File System)")
    
    print("""
  Rollback restores:
  - Workflow state to checkpoint (LangGraph state)
  - File system to checkpoint (content-addressed blobs)
  - Enables re-execution from any previous point
  
  FILE TRACKING enabled:
  - Files tracked via content-addressable storage
  - Deduplication across checkpoints
  - Atomic restore on rollback
    """)
    
    wait_for_input()
    
    if len(checkpoints) < 3:
        print("  [SKIP] Not enough checkpoints for rollback demo")
        return
    
    # Choose a checkpoint with state data (not initial empty one)
    target_idx = 0  # Latest with data
    for i, cp in enumerate(checkpoints):
        if cp.state_values.get("answer"):
            target_idx = i
            break
    target_cp = checkpoints[target_idx]
    
    print_step(1, f"Rolling back to checkpoint [{target_idx}] (step={target_cp.step})...")
    print(f"  Target checkpoint: {target_cp.id}")
    print(f"  State snapshot keys: {list(target_cp.state_values.keys())[:5]}")
    
    # Show files that would be restored
    output_files = target_cp.state_values.get("_output_files", {})
    if output_files:
        print(f"  Files to restore: {list(output_files.keys())[:3]}")
    
    # SDK uses checkpoint_id param
    rollback_result = bench.rollback(
        execution_id=execution_id,
        checkpoint_id=str(target_cp.id),
    )
    
    print(f"\n  - Rollback success: {rollback_result.success}")
    if rollback_result.success:
        print(f"  - Workflow state restored to step {target_cp.step}")
        
        # Check file restore status
        try:
            state = bench.get_state(execution_id)
            if hasattr(state, 'workflow_variables'):
                file_status = state.workflow_variables.get("_file_restore_status", {})
                if file_status.get("attempted"):
                    if file_status.get("success"):
                        restored_count = file_status.get("files_restored", 0)
                        print(f"  - File system restored: {restored_count} files restored")
                    else:
                        print(f"  - File system restore issue: {file_status.get('error', 'partial')}")
                else:
                    print(f"  - File system tracking: no files to restore")
            else:
                print(f"  - File system: state verified")
        except Exception as e:
            print(f"  - File system: {e}")
    else:
        print(f"  - Error: {rollback_result.error}")
    
    print_step(2, "Verifying rolled-back state...")
    try:
        current_state = bench.get_state(execution_id)
        if current_state and hasattr(current_state, 'workflow_variables'):
            wv = current_state.workflow_variables
            print(f"  - Query: {str(wv.get('query', 'N/A'))[:50]}")
            print(f"  - Answer present: {'answer' in wv and bool(wv.get('answer'))}")
            print(f"  - State keys: {list(wv.keys())[:5]}")
        else:
            print(f"  - State retrieved successfully")
    except Exception as e:
        print(f"  - State verification: {e}")
    

# ═══════════════════════════════════════════════════════════════════════════════
# Section 5: Branching
# ═══════════════════════════════════════════════════════════════════════════════

def demo_forking(bench: WTBTestBench, execution_id: str, checkpoints: List):
    """Demonstrate forking for A/B testing."""
    print_header("SECTION 5: FORKING (A/B Testing)")
    
    print("""
  Forking enables:
  - Create independent execution copies from checkpoint
  - Test different variants (models, retrievers)
  - Compare results across forks
  - Isolated checkpoint thread per fork
    """)
    
    wait_for_input()
    
    if not checkpoints:
        print("  [SKIP] No checkpoints available for forking")
        return
    
    # Use latest checkpoint as fork point
    fork_point = checkpoints[0] if checkpoints else None
    if not fork_point:
        print("  [SKIP] No valid checkpoint for forking")
        return
    
    print_step(1, "Creating forks for variant comparison...")
    
    variants = [
        ("variant_a", "Dense retrieval + GPT-4o-mini", {"query": "TechFlow revenue (variant A)"}),
        ("variant_b", "BM25 retrieval + GPT-4o", {"query": "TechFlow revenue (variant B)"}),
    ]
    
    forks = {}
    for name, desc, new_state in variants:
        try:
            # Use fork() to create independent execution copies
            # Fork creates a new execution with isolated checkpoint thread
            fork_result = bench.fork(
                execution_id=execution_id,
                checkpoint_id=str(fork_point.id),
                new_initial_state=new_state,
            )
            forks[name] = fork_result.fork_execution_id
            print(f"  - Created fork: {name} ({desc})")
            print(f"    Fork execution ID: {fork_result.fork_execution_id[:8]}...")
        except Exception as e:
            print(f"  - Failed to create fork {name}: {e}")
    
    print_step(2, "Forks ready for parallel execution")
    print(f"  - Total forks: {len(forks)}")
    
    return forks


# ═══════════════════════════════════════════════════════════════════════════════
# Section 6: Batch Testing
# ═══════════════════════════════════════════════════════════════════════════════

def demo_batch_testing(bench: WTBTestBench, project: WorkflowProject):
    """Demonstrate batch testing with multiple queries and variants via SDK."""
    print_header("SECTION 6: BATCH TESTING")
    
    print("""
  Batch testing features:
  - bench.run_batch_test() orchestrates parallel variant execution
  - Test variant combinations (model x retriever)
  - Comparison matrix for result analysis
  - Rollback/fork individual batch results
    """)
    
    wait_for_input()
    
    test_cases = [
        {"query": "What is TechFlow's gross margin?", "messages": []},
        {"query": "What is the market size for AI platforms?", "messages": []},
        {"query": "List the investment risks for TechFlow", "messages": []},
    ]
    
    variant_matrix = [
        {"retriever": "dense", "model": "gpt-4o-mini"},
        {"retriever": "bm25", "model": "gpt-4o"},
    ]
    
    print_step(1, f"Running batch test: {len(test_cases)} queries x {len(variant_matrix)} variants...")
    
    start_time = time.time()
    batch = bench.run_batch_test(
        project=project.name,
        variant_matrix=variant_matrix,
        test_cases=test_cases,
    )
    total_duration = time.time() - start_time
    
    print_step(2, "Batch test results...")
    successful = sum(1 for r in batch.results if r.success)
    print(f"  - Total results: {len(batch.results)}")
    print(f"  - Successful: {successful}")
    print(f"  - Total duration: {format_duration(total_duration)}")
    
    for r in batch.results:
        status = "[OK]" if r.success else "[FAIL]"
        cp = r.last_checkpoint_id[:8] if r.last_checkpoint_id else "none"
        print(f"  {status} {r.combination_name}: exec={r.execution_id[:8]}... cp={cp}...")
    
    matrix = batch.build_comparison_matrix()
    print(f"\n  Comparison matrix headers: {matrix.get('headers', [])}")
    
    report_path = OUTPUTS_DIR / "batch_test_results.json"
    report_data = [
        {"variant": r.combination_name, "success": r.success, "execution_id": r.execution_id}
        for r in batch.results
    ]
    report_path.write_text(
        json.dumps(report_data, indent=2, default=str), encoding="utf-8"
    )
    print(f"  - Results saved: {report_path}")
    
    return batch


# ═══════════════════════════════════════════════════════════════════════════════
# Section 7: Venv per Node
# ═══════════════════════════════════════════════════════════════════════════════

def demo_venv_per_node(bench: WTBTestBench, project: WorkflowProject):
    """Demonstrate per-node virtual environment isolation."""
    print_header("SECTION 7: VENV PER NODE (REAL UV Venv Manager)")
    
    print("""
  Per-node venv features:
  - REAL UV Venv Manager service at localhost:10900
  - Isolated dependencies per node
  - Different Python versions possible
  - Reproducible environments via uv.lock
  - Managed by uv (ultra-fast package management)
    """)
    
    wait_for_input()
    
    print_step(1, "Checking UV Venv Manager service...")
    
    # Verify real venv service is running
    uv_manager_url = project.environment.uv_manager_url
    print(f"  - UV Manager URL: {uv_manager_url or 'Not configured (using current env)'}")
    
    if uv_manager_url:
        try:
            import httpx
            with httpx.Client(timeout=5.0) as client:
                # Just check health by trying to access the API
                resp = client.get(f"{uv_manager_url}/docs")
                if resp.status_code == 200:
                    print(f"  - Service status: RUNNING (HTTP {resp.status_code})")
                else:
                    print(f"  - Service status: RESPONDING (HTTP {resp.status_code})")
        except Exception as e:
            print(f"  - Service status: NOT REACHABLE ({e})")
            print(f"  - Start service: cd uv_venv_manager && python -m uvicorn src.api:create_app --port 10900 --factory")
    
    print_step(2, "Configured node environments:")
    
    print(f"\n  Granularity: {project.environment.granularity}")
    print(f"\n  Default environment:")
    if project.environment.default_env:
        print(f"    Python: {project.environment.default_env.python_version}")
        print(f"    Dependencies: {project.environment.default_env.dependencies or '(uses current env)'}")
    
    print(f"\n  Node-specific environments:")
    for node_name, env_spec in project.environment.node_environments.items():
        print(f"\n    {node_name}:")
        print(f"      Python: {env_spec.python_version}")
        print(f"      Dependencies: {env_spec.dependencies or '(default)'}")
    
    print_step(3, "Environment isolation benefits:")
    print("""
    - SQL agent: has sqlite-utils for database operations
    - RAG embed: has numpy + faiss-cpu for vector operations  
    - Prevents dependency conflicts between nodes
    - Each node runs in isolated venv managed by UV Venv Manager
    - Lock files ensure reproducibility
    """)


# ═══════════════════════════════════════════════════════════════════════════════
# Section 8: Ray Distribution
# ═══════════════════════════════════════════════════════════════════════════════

def demo_ray_distribution(bench: WTBTestBench, project: WorkflowProject):
    """Demonstrate Ray distributed execution."""
    print_header("SECTION 8: RAY DISTRIBUTION (REAL Ray Cluster)")
    
    print("""
  Ray distribution features:
  - REAL Ray cluster (local or remote)
  - Parallel node execution
  - Resource allocation per node
  - Fault tolerance with retries
  - Scalable to multi-node cluster
    """)
    
    wait_for_input()
    
    print_step(1, "Checking Ray cluster status...")
    
    try:
        import ray
        if ray.is_initialized():
            resources = ray.cluster_resources()
            print(f"  - Ray Status: CONNECTED")
            print(f"  - Available CPUs: {resources.get('CPU', 0)}")
            print(f"  - Available Memory: {resources.get('memory', 0) / (1024**3):.1f} GB")
            print(f"  - Available GPUs: {resources.get('GPU', 0)}")
            
            nodes = ray.nodes()
            print(f"  - Cluster nodes: {len(nodes)}")
        else:
            print(f"  - Ray Status: NOT INITIALIZED")
            print(f"  - Initialize with: ray.init()")
    except ImportError:
        print(f"  - Ray Status: NOT INSTALLED")
        print(f"  - Install with: pip install ray")
    
    print_step(2, "Ray configuration:")
    ray_config = project.execution.ray_config
    
    print(f"  - Cluster address: {ray_config.address}")
    print(f"  - Max retries: {ray_config.max_retries}")
    print(f"  - Retry delay: {ray_config.retry_delay}s")
    
    print_step(3, "Node resource allocations:")
    for node_name, resources in project.execution.node_resources.items():
        print(f"    {node_name}:")
        print(f"      CPUs: {resources.num_cpus}")
        print(f"      Memory: {resources.memory}")
    
    print_step(4, "Ray execution benefits:")
    print("""
    - rag_embed_docs: 2 CPUs, 2GB RAM for batch embeddings
    - rag_retrieve: 2 CPUs, 1GB RAM for vector search
    - rag_generate: 1 CPU, 1GB RAM for LLM inference
    - Automatic resource scheduling and load balancing
    - Fault tolerance with automatic retries
    - Scales horizontally with Ray cluster
    """)


# ═══════════════════════════════════════════════════════════════════════════════
# Section 9: Ray Batch Execution
# ═══════════════════════════════════════════════════════════════════════════════

def demo_ray_batch_execution(bench: WTBTestBench, project: WorkflowProject):
    """Demonstrate Ray-based parallel batch execution via run_batch_test()."""
    print_header("SECTION 9: RAY BATCH EXECUTION (Parallel Workflows)")
    
    print("""
  Ray Batch Execution enables:
  - bench.run_batch_test() dispatched through Ray actors
  - Actor-based isolation per execution (ACID per variant)
  - Resource management per variant via RayConfig
  - Automatic result aggregation with comparison matrix
  
  Architecture:
  - WTBTestBench.create(enable_ray=True) wires RayBatchTestRunner
  - RayBatchTestRunner orchestrates ActorPool
  - VariantExecutionActor executes individual workflows
  - Backpressure to prevent resource exhaustion
    """)
    
    wait_for_input()
    
    ray_available = False
    try:
        import ray
        ray_available = ray.is_initialized()
    except ImportError:
        pass
    
    if not ray_available:
        print("  [SKIP] Ray not available for batch execution demo")
        return {}
    
    print_step(1, "Preparing Ray-parallel batch test...")
    
    import ray
    resources = ray.cluster_resources()
    available_cpus = int(resources.get("CPU", 1))
    
    test_cases = [
        {"query": "What is TechFlow's revenue?", "messages": []},
        {"query": "List the key products", "messages": []},
        {"query": "Who are the competitors?", "messages": []},
        {"query": "What is the market opportunity?", "messages": []},
        {"query": "Describe the technology stack", "messages": []},
    ]
    
    variant_matrix = [
        {"retriever": "dense"},
        {"retriever": "bm25"},
    ]
    
    print(f"  - Test cases: {len(test_cases)}")
    print(f"  - Variant configs: {len(variant_matrix)}")
    print(f"  - Available CPUs: {available_cpus}")
    print(f"  - Expected executions: {len(test_cases) * len(variant_matrix)}")
    
    wait_for_input()
    
    print_step(2, "Executing via bench.run_batch_test() (Ray actors)...")
    
    start_time = time.time()
    batch = bench.run_batch_test(
        project=project.name,
        variant_matrix=variant_matrix,
        test_cases=test_cases,
    )
    total_duration = time.time() - start_time
    
    print_step(3, "Ray batch execution summary...")
    
    successful = sum(1 for r in batch.results if r.success)
    failed = len(batch.results) - successful
    
    print(f"  - Total results: {len(batch.results)}")
    print(f"  - Successful: {successful}")
    print(f"  - Failed: {failed}")
    print(f"  - Total wall time: {format_duration(total_duration)}")
    
    for r in batch.results:
        status = "[OK]" if r.success else "[FAIL]"
        cp = r.last_checkpoint_id[:8] if r.last_checkpoint_id else "none"
        print(f"  {status} {r.combination_name}: exec={r.execution_id[:8]}... cp={cp}...")
    
    matrix = batch.build_comparison_matrix()
    print(f"\n  Comparison matrix headers: {matrix.get('headers', [])}")
    
    batch_results_path = OUTPUTS_DIR / "ray_batch_results.json"
    report = {
        "total_results": len(batch.results),
        "successful": successful,
        "failed": failed,
        "total_duration": total_duration,
        "results": [
            {"variant": r.combination_name, "success": r.success, "execution_id": r.execution_id}
            for r in batch.results
        ],
    }
    batch_results_path.write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n  Results saved: {batch_results_path}")
    
    return batch


# ═══════════════════════════════════════════════════════════════════════════════
# Main Presentation Flow
# ═══════════════════════════════════════════════════════════════════════════════

def run_full_presentation(sections: Optional[List[str]] = None):
    """Run the complete presentation."""
    print_header("WTB FULL PRESENTATION DEMO", char="*")
    print("""
  Welcome to the Workflow Test Bench (WTB) Presentation
  
  This demo showcases:
  1. Project Setup - Configuration and registration
  2. Basic Execution - RAG and SQL workflow
  3. Checkpointing - State snapshots at each node
  4. Rollback - Restore state and file system
  5. Forking - A/B testing with independent executions
  6. Batch Testing - Multiple queries/variants
  7. Venv per Node - Environment isolation (REAL UV Venv Manager)
  8. Ray Distribution - Resource allocation per node
  9. Ray Batch Execution - Parallel workflow execution
  
  REAL SERVICES ENABLED:
  - UV Venv Manager: http://localhost:10900
  - LangGraph Checkpoint: SQLite persistence
  - Ray: Local cluster (auto-initialized)
  - File Tracking: Content-addressable storage
    """)
    
    wait_for_input("Press Enter to begin the presentation...")
    
    # Initialize Ray cluster for distributed execution
    ray_ok = False
    try:
        import ray
        if not ray.is_initialized():
            ray.init(ignore_reinit_error=True)
        ray_ok = True
        resources = ray.cluster_resources()
        print(f"  - Ray initialized (CPUs: {resources.get('CPU', 0)}, "
              f"Memory: {resources.get('memory', 0) / (1024**3):.1f} GB)")
    except ImportError:
        print("  - Ray not available, using thread-pool execution")
    
    bench = WTBTestBench.create(
        mode="development",
        data_dir=str(DATA_DIR),
        enable_file_tracking=True,
        enable_ray=ray_ok,
    )
    runner_type = "Ray actors" if ray_ok else "thread pool"
    print(f"  - WTB initialized (SQLite persistence, batch runner: {runner_type})")
    print(f"  - Data directory: {DATA_DIR}")
    
    all_sections = {
        "setup": demo_project_setup,
        "execution": demo_basic_execution,
        "checkpointing": demo_checkpointing,
        "rollback": demo_rollback,
        "forking": demo_forking,
        "batch": demo_batch_testing,
        "venv": demo_venv_per_node,
        "ray": demo_ray_distribution,
        "ray_batch": demo_ray_batch_execution,
    }
    
    sections_to_run = sections or list(all_sections.keys())
    
    # Section 1: Setup
    project = demo_project_setup(bench)
    
    # Section 2: Basic Execution
    if "execution" in sections_to_run:
        result = demo_basic_execution(bench, project)
        execution_id = result.id
    else:
        execution_id = None
    
    # Section 3: Checkpointing
    checkpoints = []
    if "checkpointing" in sections_to_run and execution_id:
        checkpoints = demo_checkpointing(bench, str(execution_id))
    
    # Section 4: Rollback
    if "rollback" in sections_to_run and execution_id and checkpoints:
        demo_rollback(bench, str(execution_id), checkpoints)
    
    # Section 5: Forking (A/B Testing)
    if "forking" in sections_to_run and execution_id and checkpoints:
        demo_forking(bench, str(execution_id), checkpoints)  # Uses fork()
    
    # Section 6: Batch Testing
    if "batch" in sections_to_run:
        demo_batch_testing(bench, project)
    
    # Section 7: Venv per Node
    if "venv" in sections_to_run:
        demo_venv_per_node(bench, project)
    
    # Section 8: Ray Distribution
    if "ray" in sections_to_run:
        demo_ray_distribution(bench, project)
    
    # Section 9: Ray Batch Execution
    if "ray_batch" in sections_to_run:
        demo_ray_batch_execution(bench, project)
    
    # Conclusion
    print_header("PRESENTATION COMPLETE", char="*")
    print("""
  Thank you for attending the WTB presentation!
  
  Key Takeaways:
  1. WTB provides enterprise-grade workflow testing
  2. Checkpointing enables time-travel debugging
  3. File system integration for complete state restoration
  4. Branching and variants for A/B testing
  5. Ray integration for scalable parallel execution
  6. Per-node venv for dependency isolation
  7. Content-addressable file tracking for atomic rollback
  
  Questions?
    """)


# ═══════════════════════════════════════════════════════════════════════════════
# Main Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WTB Full Presentation Demo")
    parser.add_argument(
        "--section",
        type=str,
        choices=["setup", "execution", "checkpointing", "rollback", "forking", "batch", "venv", "ray", "ray_batch", "all"],
        default="all",
        help="Run specific section of the presentation",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Run without pausing for input",
    )
    
    args = parser.parse_args()
    
    if args.auto:
        os.environ["WTB_AUTO_DEMO"] = "true"
    
    sections = None if args.section == "all" else [args.section]
    
    print("Starting WTB Full Presentation...")
    print("=" * 70)
    
    try:
        run_full_presentation(sections)
        print("\n[SUCCESS] Presentation completed successfully!")
    except Exception as e:
        print(f"\n[ERROR] Presentation failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
