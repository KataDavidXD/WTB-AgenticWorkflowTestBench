"""
Parallel Execution Integration Tests for Workspace Isolation.

Tests parallel execution isolation across all systems:
- Thread-based parallel workspace creation
- Concurrent file writes in isolated workspaces
- Actor pool workspace management patterns
- Race condition prevention
- Resource cleanup under parallel load
- Batch test parallelism patterns

Design Reference: docs/issues/WORKSPACE_ISOLATION_DESIGN.md (Sections 4, 11)
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
import uuid

import pytest

from wtb.domain.models.workspace import (
    Workspace,
    WorkspaceConfig,
    WorkspaceStrategy,
    compute_venv_spec_hash,
)
from wtb.domain.events.workspace_events import WorkspaceCreatedEvent
from wtb.infrastructure.workspace.manager import (
    WorkspaceManager,
    create_file_link,
)
from wtb.infrastructure.events import WTBEventBus


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Concurrent Workspace Creation
# ═══════════════════════════════════════════════════════════════════════════════


class TestConcurrentWorkspaceCreation:
    """Tests for thread-safe concurrent workspace creation."""
    
    def test_concurrent_creation_unique_ids(
        self,
        workspace_config,
        event_bus,
    ):
        """Test that concurrent creation produces unique IDs."""
        manager = WorkspaceManager(
            config=workspace_config,
            event_bus=event_bus,
            session_id="concurrent-test",
        )
        
        workspaces = []
        errors = []
        lock = threading.Lock()
        
        def create_workspace(i):
            try:
                ws = manager.create_workspace(
                    batch_id="batch-concurrent",
                    variant_name=f"v{i}",
                    execution_id=f"exec-{i}",
                )
                with lock:
                    workspaces.append(ws)
            except Exception as e:
                with lock:
                    errors.append(e)
        
        # Create 20 workspaces concurrently
        threads = []
        for i in range(20):
            t = threading.Thread(target=create_workspace, args=(i,))
            threads.append(t)
            t.start()
        
        for t in threads:
            t.join()
        
        # No errors
        assert len(errors) == 0, f"Errors during creation: {errors}"
        
        # All unique IDs
        workspace_ids = [ws.workspace_id for ws in workspaces]
        assert len(workspace_ids) == len(set(workspace_ids)), "All IDs should be unique"
        
        # All unique paths
        root_paths = [str(ws.root_path) for ws in workspaces]
        assert len(root_paths) == len(set(root_paths)), "All paths should be unique"
        
        # Cleanup
        manager.cleanup_batch("batch-concurrent")
    
    def test_concurrent_creation_with_source_files(
        self,
        workspace_config,
        event_bus,
        sample_project_files,
    ):
        """Test concurrent creation with source file linking."""
        manager = WorkspaceManager(
            config=workspace_config,
            event_bus=event_bus,
            session_id="concurrent-files-test",
        )
        
        source_file = sample_project_files["csv"]
        workspaces = []
        errors = []
        lock = threading.Lock()
        
        def create_with_files(i):
            try:
                ws = manager.create_workspace(
                    batch_id="batch-files-concurrent",
                    variant_name=f"v{i}",
                    execution_id=f"exec-{i}",
                    source_paths=[source_file],
                )
                with lock:
                    workspaces.append(ws)
            except Exception as e:
                with lock:
                    errors.append(e)
        
        threads = [
            threading.Thread(target=create_with_files, args=(i,))
            for i in range(10)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0
        assert len(workspaces) == 10
        
        # All should have the source file
        for ws in workspaces:
            input_file = ws.input_dir / source_file.name
            assert input_file.exists()
        
        manager.cleanup_batch("batch-files-concurrent")


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Concurrent File Writes
# ═══════════════════════════════════════════════════════════════════════════════


class TestConcurrentFileWrites:
    """Tests for concurrent file writes in isolated workspaces."""
    
    def test_parallel_writes_isolated(
        self,
        workspace_manager,
        sample_project_files,
    ):
        """Test that parallel writes to same filename are isolated."""
        # Create workspaces for each "worker"
        workspaces = []
        for i in range(5):
            ws = workspace_manager.create_workspace(
                batch_id="batch-parallel-writes",
                variant_name=f"worker_{i}",
                execution_id=f"exec-{i}",
            )
            workspaces.append(ws)
        
        results = {}
        errors = []
        lock = threading.Lock()
        
        def write_result(ws: Workspace, worker_id: int):
            original_cwd = os.getcwd()
            try:
                ws.activate()
                
                # All write to same filename "result.json"
                output_file = ws.output_dir / "result.json"
                output_file.parent.mkdir(parents=True, exist_ok=True)
                
                # Include unique content
                content = {
                    "worker_id": worker_id,
                    "workspace_id": ws.workspace_id,
                    "timestamp": datetime.now().isoformat(),
                }
                output_file.write_text(json.dumps(content))
                
                with lock:
                    results[worker_id] = str(output_file)
                
            except Exception as e:
                with lock:
                    errors.append((worker_id, e))
            finally:
                ws.deactivate()
                os.chdir(original_cwd)
        
        # Execute in parallel
        threads = []
        for i, ws in enumerate(workspaces):
            t = threading.Thread(target=write_result, args=(ws, i))
            threads.append(t)
            t.start()
        
        for t in threads:
            t.join()
        
        assert len(errors) == 0, f"Errors during writes: {errors}"
        assert len(results) == 5
        
        # Verify each file has correct content
        for worker_id, file_path in results.items():
            content = json.loads(Path(file_path).read_text())
            assert content["worker_id"] == worker_id
    
    def test_high_concurrency_writes(
        self,
        workspace_config,
        event_bus,
    ):
        """Test high concurrency file writes."""
        manager = WorkspaceManager(
            config=workspace_config,
            event_bus=event_bus,
            session_id="high-concurrency-test",
        )
        
        num_workers = 50
        workspaces = []
        
        # Create all workspaces first
        for i in range(num_workers):
            ws = manager.create_workspace(
                batch_id="batch-high-concurrency",
                variant_name=f"w{i}",
                execution_id=f"exec-{i}",
            )
            workspaces.append(ws)
        
        success_count = [0]
        lock = threading.Lock()
        
        def execute_and_write(ws: Workspace, worker_id: int):
            original_cwd = os.getcwd()
            try:
                ws.activate()
                
                # Write multiple files
                for j in range(5):
                    output_file = ws.output_dir / f"output_{j}.txt"
                    output_file.parent.mkdir(parents=True, exist_ok=True)
                    output_file.write_text(f"Worker {worker_id}, file {j}")
                
                with lock:
                    success_count[0] += 1
                
            finally:
                ws.deactivate()
                os.chdir(original_cwd)
        
        # Use ThreadPoolExecutor for controlled concurrency
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [
                executor.submit(execute_and_write, ws, i)
                for i, ws in enumerate(workspaces)
            ]
            
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    print(f"Worker failed: {e}")
        
        assert success_count[0] == num_workers
        
        # Cleanup
        manager.cleanup_batch("batch-high-concurrency")


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Concurrent Cleanup
# ═══════════════════════════════════════════════════════════════════════════════


class TestConcurrentCleanup:
    """Tests for concurrent workspace cleanup."""
    
    def test_concurrent_cleanup_safe(
        self,
        workspace_config,
        event_bus,
    ):
        """Test that concurrent cleanup is thread-safe."""
        manager = WorkspaceManager(
            config=workspace_config,
            event_bus=event_bus,
            session_id="cleanup-test",
        )
        
        # Create workspaces
        workspace_ids = []
        for i in range(10):
            ws = manager.create_workspace(
                batch_id="batch-cleanup-concurrent",
                variant_name=f"v{i}",
                execution_id=f"exec-{i}",
            )
            workspace_ids.append(ws.workspace_id)
        
        results = []
        lock = threading.Lock()
        
        def cleanup_workspace(ws_id):
            result = manager.cleanup_workspace(ws_id)
            with lock:
                results.append(result)
        
        # Cleanup in parallel
        threads = [
            threading.Thread(target=cleanup_workspace, args=(ws_id,))
            for ws_id in workspace_ids
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All should have succeeded
        assert sum(results) == 10
    
    def test_double_cleanup_safe(
        self,
        workspace_manager,
    ):
        """Test that double cleanup is safe."""
        ws = workspace_manager.create_workspace(
            batch_id="batch-double-cleanup",
            variant_name="test",
            execution_id="exec-double",
        )
        ws_id = ws.workspace_id
        
        # First cleanup
        result1 = workspace_manager.cleanup_workspace(ws_id)
        assert result1 is True
        
        # Second cleanup (already cleaned)
        result2 = workspace_manager.cleanup_workspace(ws_id)
        assert result2 is False  # Already cleaned


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Race Condition Prevention
# ═══════════════════════════════════════════════════════════════════════════════


class TestRaceConditionPrevention:
    """Tests for race condition prevention."""
    
    def test_no_workspace_id_collision(
        self,
        workspace_config,
        event_bus,
    ):
        """Test that workspace IDs never collide under high concurrency."""
        manager = WorkspaceManager(
            config=workspace_config,
            event_bus=event_bus,
            session_id="collision-test",
        )
        
        all_ids = []
        lock = threading.Lock()
        
        def create_and_record(i):
            ws = manager.create_workspace(
                batch_id="batch-collision-test",
                variant_name=f"v{i}",
                execution_id=f"exec-{i}",
            )
            with lock:
                all_ids.append(ws.workspace_id)
        
        # Very high concurrency
        threads = [
            threading.Thread(target=create_and_record, args=(i,))
            for i in range(100)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All should be unique
        assert len(all_ids) == 100
        assert len(set(all_ids)) == 100
        
        manager.cleanup_batch("batch-collision-test")
    
    def test_activation_single_threaded_per_workspace(
        self,
        workspace_manager,
    ):
        """Test that activation is safe (single-threaded per workspace)."""
        ws = workspace_manager.create_workspace(
            batch_id="batch-activation-test",
            variant_name="test",
            execution_id="exec-activation",
        )
        
        original_cwd = os.getcwd()
        
        try:
            # Activate
            ws.activate()
            assert ws.is_active
            
            # Double activation should be idempotent
            ws.activate()
            assert ws.is_active
            
            # Deactivate
            ws.deactivate()
            assert not ws.is_active
            
            # Double deactivation should be idempotent
            ws.deactivate()
            assert not ws.is_active
            
        finally:
            os.chdir(original_cwd)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Batch Test Parallelism
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchTestParallelism:
    """Tests for batch test parallelism patterns."""
    
    def test_variant_execution_pattern(
        self,
        workspace_config,
        event_bus,
        sample_project_files,
    ):
        """Test the variant execution parallelism pattern."""
        manager = WorkspaceManager(
            config=workspace_config,
            event_bus=event_bus,
            session_id="variant-pattern-test",
        )
        
        batch_id = f"batch-{uuid.uuid4().hex[:8]}"
        variants = ["bm25", "dense", "hybrid", "sparse", "neural"]
        
        results = {}
        lock = threading.Lock()
        
        def execute_variant(variant_name: str, input_data: dict):
            # Create workspace
            ws = manager.create_workspace(
                batch_id=batch_id,
                variant_name=variant_name,
                execution_id=f"exec-{variant_name}",
                source_paths=[sample_project_files["csv"]],
            )
            
            original_cwd = os.getcwd()
            try:
                ws.activate()
                
                # Simulate execution
                result = {
                    "variant": variant_name,
                    "workspace_id": ws.workspace_id,
                    "input_query": input_data["query"],
                    "score": 0.80 + (hash(variant_name) % 20) / 100,
                }
                
                # Write result
                output_file = ws.output_dir / "result.json"
                output_file.parent.mkdir(parents=True, exist_ok=True)
                output_file.write_text(json.dumps(result))
                
                with lock:
                    results[variant_name] = result
                
            finally:
                ws.deactivate()
                os.chdir(original_cwd)
        
        # Execute all variants in parallel
        test_input = {"query": "test query", "k": 10}
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(execute_variant, variant, test_input)
                for variant in variants
            ]
            
            for future in as_completed(futures):
                future.result()  # Raise any exceptions
        
        # All variants should have results
        assert len(results) == len(variants)
        
        for variant in variants:
            assert variant in results
            assert results[variant]["variant"] == variant
        
        # Cleanup
        cleaned = manager.cleanup_batch(batch_id)
        assert cleaned == len(variants)
    
    def test_parallel_parity_check_pattern(
        self,
        workspace_config,
        event_bus,
    ):
        """Test parallel execution for parity checking pattern."""
        manager = WorkspaceManager(
            config=workspace_config,
            event_bus=event_bus,
            session_id="parity-test",
        )
        
        batch_id = f"batch-parity-{uuid.uuid4().hex[:8]}"
        
        # Create two workspaces for parity check
        ws_threadpool = manager.create_workspace(
            batch_id=batch_id,
            variant_name="threadpool",
            execution_id="exec-threadpool",
        )
        
        ws_ray = manager.create_workspace(
            batch_id=batch_id,
            variant_name="ray",
            execution_id="exec-ray",
        )
        
        results = {}
        
        def execute_runner(ws: Workspace, runner_type: str):
            original_cwd = os.getcwd()
            try:
                ws.activate()
                
                # Simulate execution with deterministic result
                result = {
                    "runner": runner_type,
                    "output": [1, 2, 3, 4, 5],  # Same output
                    "metrics": {"accuracy": 0.95, "latency_ms": 100},
                }
                
                output_file = ws.output_dir / "result.json"
                output_file.parent.mkdir(parents=True, exist_ok=True)
                output_file.write_text(json.dumps(result))
                
                results[runner_type] = result
                
            finally:
                ws.deactivate()
                os.chdir(original_cwd)
        
        # Execute both in parallel
        threads = [
            threading.Thread(target=execute_runner, args=(ws_threadpool, "threadpool")),
            threading.Thread(target=execute_runner, args=(ws_ray, "ray")),
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Compare results (parity check)
        assert results["threadpool"]["output"] == results["ray"]["output"]
        
        manager.cleanup_batch(batch_id)


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Resource Pressure
# ═══════════════════════════════════════════════════════════════════════════════


class TestResourcePressure:
    """Tests for system behavior under resource pressure."""
    
    def test_many_concurrent_workspaces(
        self,
        workspace_config,
        event_bus,
    ):
        """Test system with many concurrent workspaces."""
        manager = WorkspaceManager(
            config=workspace_config,
            event_bus=event_bus,
            session_id="pressure-test",
        )
        
        batch_id = f"batch-pressure-{uuid.uuid4().hex[:8]}"
        num_workspaces = 100
        
        # Create many workspaces
        workspaces = []
        for i in range(num_workspaces):
            ws = manager.create_workspace(
                batch_id=batch_id,
                variant_name=f"v{i}",
                execution_id=f"exec-{i}",
            )
            workspaces.append(ws)
        
        # All should exist
        assert len(workspaces) == num_workspaces
        
        # Stats should be accurate
        stats = manager.get_stats()
        assert stats["active_workspaces"] == num_workspaces
        
        # Cleanup should handle all
        cleaned = manager.cleanup_batch(batch_id)
        assert cleaned == num_workspaces
    
    def test_rapid_create_cleanup_cycles(
        self,
        workspace_config,
        event_bus,
    ):
        """Test rapid create-cleanup cycles."""
        manager = WorkspaceManager(
            config=workspace_config,
            event_bus=event_bus,
            session_id="cycle-test",
        )
        
        for cycle in range(10):
            batch_id = f"batch-cycle-{cycle}"
            
            # Create
            for i in range(10):
                manager.create_workspace(
                    batch_id=batch_id,
                    variant_name=f"v{i}",
                    execution_id=f"exec-{cycle}-{i}",
                )
            
            # Cleanup
            cleaned = manager.cleanup_batch(batch_id)
            assert cleaned == 10
        
        # Manager should be in clean state
        stats = manager.get_stats()
        assert stats["active_workspaces"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Event Bus Under Parallel Load
# ═══════════════════════════════════════════════════════════════════════════════


class TestEventBusParallelLoad:
    """Tests for event bus behavior under parallel load."""
    
    def test_events_received_under_parallel_load(
        self,
        workspace_config,
        event_bus,
    ):
        """Test that all events are received under parallel load."""
        received_events = []
        lock = threading.Lock()
        
        def capture_event(event):
            with lock:
                received_events.append(event)
        
        event_bus.subscribe(WorkspaceCreatedEvent, capture_event)
        
        manager = WorkspaceManager(
            config=workspace_config,
            event_bus=event_bus,
            session_id="event-load-test",
        )
        
        # Create workspaces in parallel
        num_workspaces = 20
        threads = []
        
        for i in range(num_workspaces):
            def create(idx):
                manager.create_workspace(
                    batch_id="batch-event-load",
                    variant_name=f"v{idx}",
                    execution_id=f"exec-{idx}",
                )
            
            t = threading.Thread(target=create, args=(i,))
            threads.append(t)
            t.start()
        
        for t in threads:
            t.join()
        
        # Give event processing a moment
        time.sleep(0.1)
        
        # Should have received all creation events
        creation_events = [
            e for e in received_events
            if isinstance(e, WorkspaceCreatedEvent)
        ]
        
        assert len(creation_events) == num_workspaces
        
        manager.cleanup_batch("batch-event-load")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
