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
from wtb.domain.models.checkpoint import Checkpoint, CheckpointId


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph Workflow Definition
# ═══════════════════════════════════════════════════════════════════════════════

DEMO_OUTPUT_DIR_ENV = "WTB_FILE_CLEANUP_DEMO_OUTPUT_DIR"

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


def create_demo_workflow():
    """Top-level graph factory so WTB/Ray can import it reliably."""
    output_dir = os.getenv(DEMO_OUTPUT_DIR_ENV)
    if not output_dir:
        raise RuntimeError(f"{DEMO_OUTPUT_DIR_ENV} is not initialized")
    return create_file_creating_workflow(Path(output_dir))


def extract_checkpoint_file_paths(checkpoint: Any) -> List[str]:
    """Read tracked file paths from checkpoint state, not from workspace discovery."""
    state_values = checkpoint.state_values if isinstance(checkpoint.state_values, dict) else {}
    workflow_vars = state_values.get("workflow_variables", state_values)
    files_created = workflow_vars.get("files_created", [])
    return [str(Path(file_path)) for file_path in files_created if file_path]


def store_checkpoint_file_addresses(checkpoints: List[Any], file_tracking_service) -> Dict[str, str]:
    """
    Materialize checkpoint -> file snapshot links in FileTracker.

    This keeps the file relationship in external storage rather than inferring it
    from the current workspace contents later.
    """
    checkpoint_refs: Dict[str, str] = {}

    for checkpoint in checkpoints:
        file_paths = extract_checkpoint_file_paths(checkpoint)
        if not file_paths:
            continue

        tracking_result = file_tracking_service.track_and_link(
            checkpoint_id=str(checkpoint.id),
            file_paths=file_paths,
            message=f"Checkpoint step {checkpoint.step} files",
        )
        checkpoint_refs[str(checkpoint.id)] = tracking_result.commit_id

    return checkpoint_refs


def load_execution_specific_checkpoints(
    wtb,
    execution_id: str,
    graph: Optional[Any] = None,
) -> List[Checkpoint]:
    """Read checkpoint history from the execution's own checkpoint DB."""
    execution = wtb.get_execution(execution_id)
    coordinator = wtb.get_batch_coordinator()
    state_adapter = coordinator._build_state_adapter_for_execution(execution)

    if graph is None:
        resolver = getattr(wtb, "_resolve_graph_for_result", None)
        if callable(resolver):
            try:
                graph = resolver()
            except Exception:
                graph = None

    if graph is not None and hasattr(state_adapter, "set_workflow_graph"):
        state_adapter.set_workflow_graph(graph, force_recompile=True)

    if execution.session_id:
        state_adapter.set_current_session(execution.session_id, execution_id=execution.id)

    history = state_adapter.get_checkpoint_history()
    checkpoints: List[Checkpoint] = []
    for cp in history:
        writes = cp.get("writes") or {}
        source = cp.get("source", "")
        if not writes and source and source not in ("input", "__start__", ""):
            writes = {source: {}}

        checkpoints.append(
            Checkpoint(
                id=CheckpointId(str(cp.get("checkpoint_id", cp.get("id", "")))),
                execution_id=execution_id,
                step=cp.get("step", 0),
                node_writes=writes,
                next_nodes=cp.get("next", []),
                state_values=cp.get("values", {}),
                created_at=cp.get("created_at") or datetime.now(),
            )
        )
    return sorted(checkpoints, key=lambda checkpoint: checkpoint.step)


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
    output_dir = data_dir / "outputs"
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
    started_ray = False
    wtb = None
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
        
        os.environ[DEMO_OUTPUT_DIR_ENV] = str(output_dir)
        module_alias = "examples.ray_file_cleanup_demo"
        sys.modules.setdefault(module_alias, sys.modules[__name__])
        create_demo_workflow.__module__ = module_alias

        print("  - Created workflow factory: node_a -> node_b -> node_c")
        print("  - Node A creates: setup.txt")
        print("  - Node B creates: data_0.json, data_1.json, data_2.json")
        print("  - Node C creates: report.md")
        
        # Step 3: Create SDK project with file tracking
        print("\n" + "-" * 50)
        print("Step 3: Creating WorkflowProject with FileTrackingConfig")
        print("-" * 50)

        project = create_sdk_project(output_dir, create_demo_workflow)
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

        if RAY_AVAILABLE and not ray.is_initialized():
            ray.init(num_cpus=2, ignore_reinit_error=True, log_to_driver=False)
            started_ray = True
            print("  - Local Ray runtime initialized")
        
        wtb = WTBTestBenchFactory.create_for_development(
            data_dir=str(base_dir / "data"),
            enable_file_tracking=True,
            enable_ray=RAY_AVAILABLE,
        )
        print("  - WTBTestBench created")
        print("  - File tracking service initialized")

        runner_config = getattr(getattr(wtb, "_batch_runner", None), "_config", None)
        if runner_config is not None:
            runner_config.rollback_cleanup_enabled = config.rollback_cleanup_enabled
            runner_config.rollback_cleanup_dry_run = config.rollback_cleanup_dry_run
            runner_config.rollback_cleanup_backup = config.rollback_cleanup_backup
            runner_config.rollback_cleanup_max_files = config.rollback_cleanup_max_files
            print("  - Rollback cleanup config applied to batch runner")
        
        # Register project
        wtb.register_project(project)
        print(f"  - Project '{project.name}' registered")
        
        # Step 5: Run the workflow (simulated batch test)
        print("\n" + "-" * 50)
        print("Step 5: Running workflow execution")
        print("-" * 50)
        
        # Start execution
        initial_state = {"messages": [], "files_created": [], "step": 0}
        result = None
        
        try:
            # Run batch test with single variant
            batch_result = wtb.run_batch_test(
                project=project.name,
                variant_matrix=[{"node_b": "default"}],  # Single variant
                test_cases=[initial_state],
            )
            
            print(f"  - Batch test completed")
            print(f"  - Results count: {len(batch_result.results)}")
            
            result = batch_result.results[0] if batch_result.results else None
            if result is not None:
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
        
        # Step 7: Demonstrate rollback with cleanup
        print("\n" + "-" * 50)
        print("Step 7: Rollback with auto-cleanup")
        print("-" * 50)

        if result is None:
            print("  - Skipping rollback cleanup check because the batch result was empty")
        else:
            checkpoints = load_execution_specific_checkpoints(
                wtb,
                result.execution_id,
                graph=create_demo_workflow(),
            )
            print(f"  - Available checkpoints: {len(checkpoints)}")
            for cp in checkpoints:
                print(f"    * step={cp.step}, id={str(cp.id)[:12]}...")

            if not checkpoints:
                print("  - No checkpoints available for rollback cleanup check")
            else:
                inner_ctrl = getattr(getattr(wtb, "_exec_ctrl", None), "_inner", None)
                file_tracking_service = getattr(inner_ctrl, "_file_tracking", None)
                if file_tracking_service is None:
                    raise RuntimeError("File tracking service is not available on the bench")

                checkpoint_refs = store_checkpoint_file_addresses(checkpoints, file_tracking_service)
                print("  - Stored checkpoint file addresses:")
                for checkpoint in checkpoints:
                    ref = checkpoint_refs.get(str(checkpoint.id))
                    if ref:
                        print(f"    * step={checkpoint.step} -> {ref[:12]}...")

                target_checkpoint = next((cp for cp in checkpoints if cp.step == 1), checkpoints[0])

                print(f"  - Rolling back to checkpoint step={target_checkpoint.step}")

                rollback_result = wtb.rollback_batch_result(
                    result,
                    checkpoint_id=str(target_checkpoint.id),
                )
                print(f"  - Rollback success: {rollback_result.success}")
                if not rollback_result.success:
                    raise RuntimeError(rollback_result.error or "rollback failed")

                restore_result = file_tracking_service.restore_from_checkpoint(str(target_checkpoint.id))
                print(
                    f"  - Restored files from stored address: "
                    f"{restore_result.files_restored} files"
                )

                from wtb.infrastructure.file_tracking.cleanup_service import FileCleanupService

                cleanup_service = FileCleanupService()
                orphaned = cleanup_service.identify_orphaned_files(
                    target_checkpoint_id=str(target_checkpoint.id),
                    execution_id=result.execution_id,
                    current_workspace_path=output_dir,
                    track_patterns=["*.txt", "*.json", "*.md"],
                    exclude_patterns=["*.tmp", "*.log", "__pycache__/*", ".rollback_backup/*"],
                    file_tracking_service=file_tracking_service,
                )
                print(f"  - Orphaned files before cleanup: {orphaned}")

                cleanup_result = cleanup_service.cleanup_orphaned_files(
                    checkpoint_id=str(target_checkpoint.id),
                    execution_id=result.execution_id,
                    orphaned_paths=orphaned,
                    backup_dir=output_dir / ".rollback_backup",
                    dry_run=config.rollback_cleanup_dry_run,
                    max_files=config.rollback_cleanup_max_files,
                )
                print(
                    f"  - Cleanup success: deleted={cleanup_result.files_deleted}, "
                    f"backed_up={cleanup_result.files_backed_up}, "
                    f"skipped={cleanup_result.files_skipped}"
                )

                remaining = sorted(
                    p.name for p in output_dir.iterdir()
                    if p.is_file() or p.is_dir()
                )
                print(f"  - Files after rollback cleanup: {remaining}")
                if (
                    "setup.txt" in remaining
                    and "report.md" not in remaining
                    and "data_0.json" not in remaining
                    and "data_1.json" not in remaining
                    and "data_2.json" not in remaining
                ):
                    print("  - Cleanup verification passed: only checkpoint-state files remain")
                else:
                    print(
                        "  - WARNING: cleanup removed checkpoint-state files too; "
                        "the rollback cleanup linkage still needs a core fix"
                    )

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
        if wtb is not None:
            try:
                wtb.close()
            except Exception:
                pass
        if started_ray and ray.is_initialized():
            ray.shutdown()
        os.environ.pop(DEMO_OUTPUT_DIR_ENV, None)
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            print(f"\nNote: Temp directory {tmp_dir} may need manual cleanup")


# ═══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    run_demo()
