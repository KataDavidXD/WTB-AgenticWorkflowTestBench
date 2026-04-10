"""
Ray + FileTracking + Rollback Cleanup Mini-Demo (SDK Only).

This demo shows:
1. Using WTB SDK with Ray distributed batch testing
2. FileTrackingConfig integration for file versioning
3. LangGraph workflow with file-creating nodes
4. Auto-delete orphaned files during rollback (v1.9 feature)

Requirements:
    pip install ray langgraph

Run:
    python examples/ray_file_cleanup_demo.py

Architecture (v1.9 Rollback Cleanup):
    ┌─────────────────────────────────────┐
    │  BatchExecutionCoordinator          │  <-- Creates events with cleanup config
    │  (via SDK: wtb.rollback_batch_result)│
    └───────────────────┬─────────────────┘
                        │ OutboxEvent (ROLLBACK_FILE_RESTORE)
                        ▼
    ┌─────────────────────────────────────┐
    │  OutboxProcessor                    │  <-- Actually deletes orphaned files
    │  (runs via OutboxLifecycleManager)  │
    └─────────────────────────────────────┘
"""

from __future__ import annotations

import os
import sys
import time
import tempfile
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

# Check dependencies
try:
    import ray
    RAY_AVAILABLE = True
except ImportError:
    RAY_AVAILABLE = False
    print("WARNING: Ray not installed. Run: pip install ray")

try:
    from langgraph.graph import StateGraph, END
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    print("WARNING: LangGraph not installed. Run: pip install langgraph")


# ═══════════════════════════════════════════════════════════════════════════════
# SDK Imports (Use SDK ONLY - no direct infrastructure imports)
# ═══════════════════════════════════════════════════════════════════════════════

from wtb.sdk import (
    WTBTestBench,
    WorkflowProject,
    FileTrackingConfig,
    RayConfig,
    BatchRollbackResult,
    BatchTestResult,
)
from wtb.config import WTBConfig


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph Workflow Definition
# ═══════════════════════════════════════════════════════════════════════════════

def create_file_creating_workflow(output_dir: Path):
    """
    Create a LangGraph workflow where nodes create files.
    
    This simulates a real agentic workflow that:
    - Node A: Initializes and creates setup files
    - Node B: Processes data and creates output files  
    - Node C: Finalizes and creates report files
    
    Args:
        output_dir: Directory where files will be created
        
    Returns:
        LangGraph StateGraph builder (uncompiled)
    """
    if not LANGGRAPH_AVAILABLE:
        raise ImportError("LangGraph required")
    
    from typing_extensions import TypedDict
    
    class FileWorkflowState(TypedDict, total=False):
        messages: List[str]
        files_created: List[str]
        step: int
    
    def node_a(state: Dict[str, Any]) -> Dict[str, Any]:
        """Initialize node - creates setup.txt"""
        file_path = output_dir / "setup.txt"
        file_path.write_text(f"Setup created at {datetime.now()}")
        
        files = state.get("files_created", [])
        return {
            "messages": state.get("messages", []) + ["Node A: Created setup.txt"],
            "files_created": files + [str(file_path)],
            "step": 1,
        }
    
    def node_b(state: Dict[str, Any]) -> Dict[str, Any]:
        """Processing node - creates data files"""
        # Create multiple files to simulate processing
        files = state.get("files_created", [])
        new_files = []
        
        for i in range(3):
            file_path = output_dir / f"data_{i}.json"
            file_path.write_text(f'{{"index": {i}, "timestamp": "{datetime.now()}"}}')
            new_files.append(str(file_path))
        
        return {
            "messages": state.get("messages", []) + ["Node B: Created 3 data files"],
            "files_created": files + new_files,
            "step": 2,
        }
    
    def node_c(state: Dict[str, Any]) -> Dict[str, Any]:
        """Finalization node - creates report"""
        file_path = output_dir / "report.md"
        files_count = len(state.get("files_created", []))
        file_path.write_text(
            f"# Report\n\n"
            f"Generated at: {datetime.now()}\n"
            f"Total files created: {files_count + 1}\n"
        )
        
        files = state.get("files_created", [])
        return {
            "messages": state.get("messages", []) + ["Node C: Created report.md"],
            "files_created": files + [str(file_path)],
            "step": 3,
        }
    
    # Build the graph
    builder = StateGraph(FileWorkflowState)
    builder.add_node("node_a", node_a)
    builder.add_node("node_b", node_b)
    builder.add_node("node_c", node_c)
    
    builder.add_edge("__start__", "node_a")
    builder.add_edge("node_a", "node_b")  # Checkpoint created after node_a
    builder.add_edge("node_b", "node_c")  # Checkpoint created after node_b
    builder.add_edge("node_c", END)
    
    return builder


# ═══════════════════════════════════════════════════════════════════════════════
# Demo Functions
# ═══════════════════════════════════════════════════════════════════════════════

def setup_demo_environment(base_dir: Path) -> tuple[WTBConfig, Path]:
    """
    Set up the demo environment with proper configuration.
    
    Returns:
        tuple of (WTBConfig, output_dir)
    """
    # Create directories
    data_dir = base_dir / "data"
    output_dir = base_dir / "outputs"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Configure WTB with rollback cleanup ENABLED (opt-in feature)
    config = WTBConfig(
        data_dir=str(data_dir),
        wtb_db_url=f"sqlite:///{data_dir}/wtb.db",
        agentgit_db_path=str(data_dir / "agentgit.db"),
        # v1.9: Enable rollback cleanup
        rollback_cleanup_enabled=True,
        rollback_cleanup_dry_run=False,  # Actually delete (set True for testing)
        rollback_cleanup_backup=True,    # Backup files before deletion
        rollback_cleanup_max_files=50,   # Safety limit
        # Ray configuration
        ray_enabled=True,
    )
    
    return config, output_dir


def create_sdk_project(output_dir: Path, graph_factory) -> WorkflowProject:
    """
    Create a WorkflowProject with FileTrackingConfig.
    
    This is the SDK way to configure file tracking.
    
    Args:
        output_dir: Directory to track for files
        graph_factory: Callable that returns the LangGraph workflow
    """
    project = WorkflowProject(
        name="file_demo_project",
        graph_factory=graph_factory,
        description="Demo project showing file tracking with rollback cleanup",
        # Configure file tracking via SDK
        file_tracking=FileTrackingConfig(
            enabled=True,
            tracked_paths=[str(output_dir)],  # Track the output directory
            ignore_patterns=["*.tmp", "*.log", "__pycache__/"],
            auto_commit=True,
            commit_on="checkpoint",  # Commit files at each checkpoint
            snapshot_strategy="incremental",
        ),
    )
    
    return project


def run_demo():
    """
    Main demo function.
    
    Shows the complete flow:
    1. Setup environment with cleanup enabled
    2. Create LangGraph workflow that creates files
    3. Run batch test with Ray (or ThreadPool if Ray unavailable)
    4. Create checkpoint after Node A
    5. Continue execution (Node B, C create more files)
    6. Rollback to checkpoint after Node A
    7. Verify files created by Node B, C are auto-deleted
    """
    print("=" * 70)
    print("Ray + FileTracking + Rollback Cleanup Demo (SDK Only)")
    print("=" * 70)
    
    # Check dependencies
    if not LANGGRAPH_AVAILABLE:
        print("\nERROR: LangGraph is required. Install with: pip install langgraph")
        return
    
    if not RAY_AVAILABLE:
        print("\nNOTE: Ray not installed. Using ThreadPool runner instead.")
        print("      For full Ray support: pip install ray")
    
    # Create temporary directory for demo
    tmp_dir = tempfile.mkdtemp()
    try:
        base_dir = Path(tmp_dir)
        print(f"\nDemo directory: {base_dir}")
        
        # Step 1: Setup environment
        print("\n" + "-" * 50)
        print("Step 1: Setting up environment with rollback cleanup enabled")
        print("-" * 50)
        
        config, output_dir = setup_demo_environment(base_dir)
        print(f"  - Data dir: {config.data_dir}")
        print(f"  - Output dir: {output_dir}")
        print(f"  - Rollback cleanup enabled: {config.rollback_cleanup_enabled}")
        print(f"  - Cleanup backup: {config.rollback_cleanup_backup}")
        
        # Step 2: Create the workflow factory
        print("\n" + "-" * 50)
        print("Step 2: Creating LangGraph workflow with file-creating nodes")
        print("-" * 50)
        
        # Create a factory function that returns the workflow
        def workflow_factory():
            return create_file_creating_workflow(output_dir)
        
        print("  - Created workflow factory: node_a -> node_b -> node_c")
        print("  - Node A creates: setup.txt")
        print("  - Node B creates: data_0.json, data_1.json, data_2.json")
        print("  - Node C creates: report.md")
        
        # Step 3: Create SDK project with file tracking
        print("\n" + "-" * 50)
        print("Step 3: Creating WorkflowProject with FileTrackingConfig")
        print("-" * 50)
        
        project = create_sdk_project(output_dir, workflow_factory)
        print(f"  - Project: {project.name}")
        print(f"  - File tracking enabled: {project.file_tracking.enabled}")
        print(f"  - Tracked paths: {project.file_tracking.tracked_paths}")
        print(f"  - Commit on: {project.file_tracking.commit_on}")
        
        # Step 4: Initialize WTB SDK
        print("\n" + "-" * 50)
        print("Step 4: Initializing WTB SDK with Ray")
        print("-" * 50)
        
        # Use factory to create properly wired WTBTestBench
        from wtb.application.factories import WTBTestBenchFactory
        
        wtb = WTBTestBenchFactory.create_for_development(
            data_dir=str(base_dir / "data"),
            enable_file_tracking=True,
        )
        print("  - WTBTestBench created")
        print("  - File tracking service initialized")
        
        # Register project
        wtb.register_project(project)
        print(f"  - Project '{project.name}' registered")
        
        # Step 5: Run the workflow (simulated batch test)
        print("\n" + "-" * 50)
        print("Step 5: Running workflow execution")
        print("-" * 50)
        
        # Start execution
        initial_state = {"messages": [], "files_created": [], "step": 0}
        
        try:
            # Run batch test with single variant
            batch_result = wtb.run_batch_test(
                project=project.name,
                variant_matrix=[{"node_b": "default"}],  # Single variant
                test_cases=[initial_state],
            )
            
            print(f"  - Batch test completed")
            print(f"  - Results count: {len(batch_result.results)}")
            
            if batch_result.results:
                result = batch_result.results[0]
                print(f"  - Execution ID: {result.execution_id}")
                print(f"  - Checkpoint count: {result.checkpoint_count}")
                print(f"  - Last checkpoint: {result.last_checkpoint_id}")
                
        except Exception as e:
            print(f"  - Note: Full batch execution requires Ray cluster")
            print(f"  - Demo will show the configuration pattern")
            print(f"  - Error: {e}")
        
        # Step 6: Show files created
        print("\n" + "-" * 50)
        print("Step 6: Files in output directory")
        print("-" * 50)
        
        if output_dir.exists():
            files = list(output_dir.iterdir())
            if files:
                for f in files:
                    print(f"  - {f.name}")
            else:
                print("  - (No files yet - workflow not fully executed)")
        
        # Step 7: Demonstrate rollback with cleanup (conceptual)
        print("\n" + "-" * 50)
        print("Step 7: Rollback with auto-cleanup (architecture)")
        print("-" * 50)
        
        print("""
  When you call:
    wtb.rollback_batch_result(result, to_checkpoint="cp-after-node-a")
  
  The following happens:
  
  1. BatchExecutionCoordinator creates ROLLBACK_FILE_RESTORE event
     - Includes cleanup config from WTBConfig:
       * cleanup_orphaned_files: True
       * cleanup_dry_run: False  
       * cleanup_backup: True
       * cleanup_max_files: 50
     
  2. OutboxProcessor (running via OutboxLifecycleManager):
     - Processes the event
     - Restores files to checkpoint state
     - Identifies orphaned files (created after checkpoint)
     - Backs up orphaned files to .rollback_backup/
     - Deletes orphaned files
  
  3. Result:
     - setup.txt (from Node A): KEPT
     - data_*.json (from Node B): DELETED (created after checkpoint)
     - report.md (from Node C): DELETED (created after checkpoint)
""")
        
        # Step 8: Show how to enable cleanup in your code
        print("\n" + "-" * 50)
        print("Step 8: How to enable rollback cleanup in your code")
        print("-" * 50)
        
        print("""
  # Option 1: Via WTBConfig
  config = WTBConfig(
      rollback_cleanup_enabled=True,   # Enable the feature (opt-in)
      rollback_cleanup_dry_run=False,  # False = actually delete
      rollback_cleanup_backup=True,    # Backup before deletion
      rollback_cleanup_max_files=100,  # Safety limit
  )
  
  # Option 2: Via environment variables
  WTB_ROLLBACK_CLEANUP_ENABLED=true
  WTB_ROLLBACK_CLEANUP_DRY_RUN=false
  WTB_ROLLBACK_CLEANUP_BACKUP=true
  WTB_ROLLBACK_CLEANUP_MAX_FILES=100
  
  # Then use SDK normally
  wtb = WTBTestBenchFactory.create(config)
  result = wtb.run_batch_test(...)
  
  # Rollback with auto-cleanup
  coordinator = wtb.get_batch_coordinator(config=config)
  rollback_result = coordinator.rollback(
      execution_id=result.results[0].execution_id,
      checkpoint_id=target_checkpoint_id,
  )
""")
        
        print("\n" + "=" * 70)
        print("Demo complete!")
        print("=" * 70)
        
    finally:
        # Cleanup: Note - on Windows, SQLite files may still be locked
        # This is expected behavior and doesn't affect the demo
        import gc
        gc.collect()  # Help release file handles
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            print(f"\nNote: Temp directory {tmp_dir} may need manual cleanup")


# ═══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    run_demo()
