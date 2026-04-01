#!/usr/bin/env python
"""
run_demo.py - Simplified WTB Demo Script

Demonstrates the key WTB SDK methods:
1. Project Setup & Registration
2. Execution with Checkpointing
3. Rollback to Checkpoint
4. Forking for A/B Testing
5. Batch Testing

Uses SYNCHRONOUS SDK methods (no async).

Usage:
    cd <project-root>
    python examples/wtb_presentation/scripts/run_demo.py
    
    # Run specific demo:
    python examples/wtb_presentation/scripts/run_demo.py --demo=rollback
    python examples/wtb_presentation/scripts/run_demo.py --demo=fork
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ═══════════════════════════════════════════════════════════════════════════════
# SDK Imports
# ═══════════════════════════════════════════════════════════════════════════════

from wtb.sdk import (
    WTBTestBench,
    WorkflowProject,
    FileTrackingConfig,
    ExecutionConfig,
    ExecutionStatus,
)

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
# Utility Functions
# ═══════════════════════════════════════════════════════════════════════════════


def print_header(title: str, char: str = "=") -> None:
    """Print formatted section header."""
    print(f"\n{char * 70}")
    print(f"  {title}")
    print(f"{char * 70}")


def print_step(step: int, description: str) -> None:
    """Print formatted step."""
    print(f"\n[Step {step}] {description}")


def format_duration(seconds: float) -> str:
    """Format duration for display."""
    if seconds < 1:
        return f"{seconds*1000:.1f}ms"
    return f"{seconds:.2f}s"


# ═══════════════════════════════════════════════════════════════════════════════
# Demo 1: Basic Execution
# ═══════════════════════════════════════════════════════════════════════════════


def demo_basic_execution():
    """Demonstrate basic workflow execution with WTB SDK."""
    print_header("DEMO 1: BASIC EXECUTION")
    
    print("""
    This demo shows:
    1. Creating a WTBTestBench with SQLite persistence
    2. Registering a WorkflowProject
    3. Running a workflow execution
    4. Retrieving checkpoints
    """)
    
    # Import graph factory
    from examples.wtb_presentation.graphs.unified_graph import create_rag_only_graph
    
    # Step 1: Create WTBTestBench with development mode (SQLite)
    print_step(1, "Creating WTBTestBench with SQLite persistence...")
    
    bench = WTBTestBench.create(
        mode="development",
        data_dir=str(DATA_DIR),
        enable_file_tracking=False,
    )
    print(f"  - Bench created with data_dir: {DATA_DIR}")
    
    # Step 2: Create and register WorkflowProject
    print_step(2, "Creating and registering WorkflowProject...")
    
    project = WorkflowProject(
        name="basic_demo",
        graph_factory=create_rag_only_graph,
        description="Basic execution demo",
    )
    
    try:
        bench.register_project(project)
        print(f"  - Project '{project.name}' registered")
    except ValueError as e:
        if "already registered" in str(e).lower():
            bench.unregister_project(project.name)
            bench.register_project(project)
            print(f"  - Project '{project.name}' re-registered")
        else:
            raise
    
    # Step 3: Run execution
    print_step(3, "Running workflow execution...")
    
    initial_state = {
        "query": "What is the revenue?",
        "messages": [],
    }
    
    start_time = time.time()
    result = bench.run(
        project=project.name,
        initial_state=initial_state,
    )
    duration = time.time() - start_time
    
    print(f"  - Execution ID: {result.id}")
    print(f"  - Status: {result.status}")
    print(f"  - Duration: {format_duration(duration)}")
    
    # Step 4: Get checkpoints
    print_step(4, "Retrieving checkpoints...")
    
    checkpoints = bench.get_checkpoints(result.id)
    print(f"  - Found {len(checkpoints)} checkpoints")
    
    for i, cp in enumerate(checkpoints[:5]):  # Show first 5
        print(f"    [{i}] Step {cp.step}, Next: {cp.next_nodes[:2] if cp.next_nodes else '(end)'}")
    
    return bench, project, result


# ═══════════════════════════════════════════════════════════════════════════════
# Demo 2: Rollback
# ═══════════════════════════════════════════════════════════════════════════════


def demo_rollback():
    """Demonstrate rollback functionality."""
    print_header("DEMO 2: ROLLBACK TO CHECKPOINT")
    
    print("""
    This demo shows:
    1. Running an execution
    2. Getting checkpoints
    3. Rolling back to an earlier checkpoint
    4. Verifying state restoration
    """)
    
    from examples.wtb_presentation.graphs.unified_graph import create_rag_only_graph
    
    bench = WTBTestBench.create(mode="development", data_dir=str(DATA_DIR))
    
    project = WorkflowProject(
        name="rollback_demo",
        graph_factory=create_rag_only_graph,
        description="Rollback demonstration",
    )
    bench.register_project(project)
    
    # Step 1: Run execution
    print_step(1, "Running initial execution...")
    
    result = bench.run(
        project=project.name,
        initial_state={"query": "Test query", "messages": []},
    )
    print(f"  - Execution completed: {result.id}")
    
    # Step 2: Get checkpoints
    print_step(2, "Getting checkpoints...")
    
    checkpoints = bench.get_checkpoints(result.id)
    print(f"  - Found {len(checkpoints)} checkpoints")
    
    if not checkpoints:
        print("  - No checkpoints available for rollback demo")
        return
    
    # Step 3: Rollback to earlier checkpoint
    print_step(3, "Rolling back to earlier checkpoint...")
    
    target_cp = checkpoints[-1]  # Oldest checkpoint (list is newest-first)
    print(f"  - Target checkpoint: {target_cp.id}")
    print(f"  - Target step: {target_cp.step}")
    
    rollback_result = bench.rollback(
        execution_id=result.id,
        checkpoint_id=str(target_cp.id),
    )
    
    print(f"  - Rollback success: {rollback_result.success}")
    if not rollback_result.success:
        print(f"  - Error: {rollback_result.error}")
    
    # Step 4: Verify state
    print_step(4, "Verifying rolled-back state...")
    
    state = bench.get_state(result.id)
    print(f"  - State retrieved: {state is not None}")


# ═══════════════════════════════════════════════════════════════════════════════
# Demo 3: Forking
# ═══════════════════════════════════════════════════════════════════════════════


def demo_forking():
    """Demonstrate forking for A/B testing."""
    print_header("DEMO 3: FORKING FOR A/B TESTING")
    
    print("""
    This demo shows:
    1. Running a base execution
    2. Creating forks from a checkpoint
    3. Each fork is an independent execution
    """)
    
    from examples.wtb_presentation.graphs.unified_graph import create_rag_only_graph
    
    bench = WTBTestBench.create(mode="development", data_dir=str(DATA_DIR))
    
    project = WorkflowProject(
        name="fork_demo",
        graph_factory=create_rag_only_graph,
        description="Forking demonstration",
    )
    bench.register_project(project)
    
    # Step 1: Run base execution
    print_step(1, "Running base execution...")
    
    result = bench.run(
        project=project.name,
        initial_state={"query": "Base query", "messages": []},
    )
    print(f"  - Base execution: {result.id}")
    
    # Step 2: Get checkpoints
    print_step(2, "Getting fork point...")
    
    checkpoints = bench.get_checkpoints(result.id)
    
    if not checkpoints:
        print("  - No checkpoints available for forking")
        return
    
    fork_point = checkpoints[0]  # Newest checkpoint (list is newest-first)
    print(f"  - Fork point: {fork_point.id}")
    
    # Step 3: Create forks
    print_step(3, "Creating forks...")
    
    variants = [
        {"query": "Variant A query", "variant": "A"},
        {"query": "Variant B query", "variant": "B"},
    ]
    
    forks = []
    for variant in variants:
        try:
            fork_result = bench.fork(
                execution_id=result.id,
                checkpoint_id=str(fork_point.id),
                new_initial_state=variant,
            )
            forks.append(fork_result)
            print(f"  - Created fork: {fork_result.fork_execution_id[:12]}... (variant {variant['variant']})")
        except Exception as e:
            print(f"  - Fork failed: {e}")
    
    print(f"\n  Total forks created: {len(forks)}")


# ═══════════════════════════════════════════════════════════════════════════════
# Demo 4: Batch Testing
# ═══════════════════════════════════════════════════════════════════════════════


def demo_batch_testing():
    """Demonstrate batch testing with multiple variants."""
    print_header("DEMO 4: BATCH TESTING")
    
    print("""
    This demo shows:
    1. Setting up variant matrix
    2. Running multiple test cases
    3. Collecting results
    """)
    
    from examples.wtb_presentation.graphs.unified_graph import create_rag_only_graph
    
    bench = WTBTestBench.create(mode="development", data_dir=str(DATA_DIR))
    
    project = WorkflowProject(
        name="batch_demo",
        graph_factory=create_rag_only_graph,
        description="Batch testing demonstration",
    )
    bench.register_project(project)
    
    # Step 1: Define variant matrix
    print_step(1, "Defining variant matrix...")
    
    variant_matrix = [
        {"retriever": "default"},
        {"retriever": "bm25"},
    ]
    print(f"  - Variants: {len(variant_matrix)}")
    
    # Step 2: Define test cases
    print_step(2, "Defining test cases...")
    
    test_cases = [
        {"query": "Test query 1", "messages": []},
        {"query": "Test query 2", "messages": []},
    ]
    print(f"  - Test cases: {len(test_cases)}")
    
    # Step 3: Run batch test
    print_step(3, "Running batch test...")
    
    start_time = time.time()
    batch_result = bench.run_batch_test(
        project=project.name,
        variant_matrix=variant_matrix,
        test_cases=test_cases,
    )
    duration = time.time() - start_time
    
    print(f"  - Batch ID: {batch_result.id}")
    print(f"  - Status: {batch_result.status}")
    print(f"  - Duration: {format_duration(duration)}")
    print(f"  - Results: {len(batch_result.results)}")
    
    # Step 4: Show results summary
    print_step(4, "Results summary...")
    
    for i, result in enumerate(batch_result.results[:5]):
        status = "SUCCESS" if result.success else "FAILED"
        print(f"    [{i}] {result.combination_name}: {status}")


# ═══════════════════════════════════════════════════════════════════════════════
# Demo 5: Pause/Resume
# ═══════════════════════════════════════════════════════════════════════════════


def demo_pause_resume():
    """Demonstrate pause/resume functionality."""
    print_header("DEMO 5: PAUSE/RESUME")
    
    print("""
    This demo shows:
    1. Running with breakpoints
    2. Pausing at breakpoint
    3. Inspecting paused state
    4. Resuming execution
    """)
    
    from examples.wtb_presentation.graphs.unified_graph import create_rag_only_graph
    
    bench = WTBTestBench.create(mode="development", data_dir=str(DATA_DIR))
    
    project = WorkflowProject(
        name="pause_resume_demo",
        graph_factory=create_rag_only_graph,
        description="Pause/resume demonstration",
    )
    bench.register_project(project)
    
    # Step 1: Run with breakpoint
    print_step(1, "Running with breakpoint at 'rag_grade'...")
    
    result = bench.run(
        project=project.name,
        initial_state={"query": "Test query", "messages": []},
        breakpoints=["rag_grade"],
    )
    
    print(f"  - Execution ID: {result.id}")
    print(f"  - Status: {result.status}")
    
    if result.status == ExecutionStatus.PAUSED:
        # Step 2: Inspect paused state
        print_step(2, "Inspecting paused state...")
        
        state = bench.get_state(result.id)
        print(f"  - State retrieved: {state is not None}")
        
        # Step 3: Resume
        print_step(3, "Resuming execution...")
        
        resumed = bench.resume(result.id)
        print(f"  - Resumed status: {resumed.status}")
    else:
        print("  - Execution completed without pausing (breakpoint may not be supported)")


# ═══════════════════════════════════════════════════════════════════════════════
# Main Entry Point
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    """Run the demo."""
    parser = argparse.ArgumentParser(description="WTB SDK Demo")
    parser.add_argument(
        "--demo",
        type=str,
        choices=["basic", "rollback", "fork", "batch", "pause", "all"],
        default="all",
        help="Run specific demo",
    )
    
    args = parser.parse_args()
    
    print_header("WTB SDK DEMO", char="*")
    print("""
    Welcome to the WTB SDK Demo!
    
    This demonstrates the key SDK methods:
    - WTBTestBench.create() - Create test bench
    - bench.register_project() - Register workflow
    - bench.run() - Execute workflow
    - bench.get_checkpoints() - Get checkpoints
    - bench.rollback() - Rollback to checkpoint
    - bench.fork() - Fork for A/B testing
    - bench.run_batch_test() - Batch testing
    - bench.pause() / bench.resume() - Pause/resume
    """)
    
    demos = {
        "basic": demo_basic_execution,
        "rollback": demo_rollback,
        "fork": demo_forking,
        "batch": demo_batch_testing,
        "pause": demo_pause_resume,
    }
    
    if args.demo == "all":
        for name, demo_func in demos.items():
            try:
                demo_func()
            except Exception as e:
                print(f"\n[ERROR] Demo {name} failed: {e}")
    else:
        try:
            demos[args.demo]()
        except Exception as e:
            print(f"\n[ERROR] Demo {args.demo} failed: {e}")
    
    print_header("DEMO COMPLETE", char="*")


if __name__ == "__main__":
    main()
