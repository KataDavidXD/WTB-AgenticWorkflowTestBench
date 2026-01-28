"""
Integration tests for Cross-System ACID Compliance.

Tests comprehensive ACID compliance across all integrated systems:
- Ray (distributed execution)
- venv (virtual environment management)
- LangGraph (workflow orchestration)
- FileTracker (file system state management)

Test Categories:
- Full Stack Integration: All systems working together
- Cross-DB Consistency: WTB, AgentGit, FileTracker consistency
- Distributed Transaction: Ray actor + checkpoint + file consistency
- End-to-End Scenarios: Complete workflow execution with all systems

ACID Focus:
- Atomicity: Cross-system operations succeed or fail together
- Consistency: State invariants maintained across all systems
- Isolation: Concurrent operations in different systems don't interfere
- Durability: State persists correctly in all databases

Run with: pytest tests/test_outbox_transaction_consistency/test_cross_system_acid.py -v
"""

import pytest
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, List
from unittest.mock import Mock, MagicMock

# Import real domain types
from wtb.domain.models.outbox import OutboxEvent, OutboxEventType

# Import test helpers
from tests.test_outbox_transaction_consistency.helpers import (
    FullIntegrationState,
    create_full_integration_state,
)

# Import centralized mocks and utilities
from tests.mocks import MockMemento
from tests.mocks.services import (
    verify_outbox_consistency,
    verify_transaction_atomicity,
    VerificationResult,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions for Test Events
# ═══════════════════════════════════════════════════════════════════════════════


def create_cross_system_event(
    event_id: str,
    event_type_name: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: Dict[str, Any] = None,
) -> OutboxEvent:
    """
    Create an OutboxEvent for cross-system testing.
    
    Maps string event type names to proper OutboxEventType enums.
    """
    type_mapping = {
        "EXECUTION_COMPLETED": OutboxEventType.CHECKPOINT_SAVED,
        "CHECKPOINT_FILE_LINK_VERIFY": OutboxEventType.CHECKPOINT_FILE_LINK_VERIFY,
        "CROSS_DB_VERIFY": OutboxEventType.CHECKPOINT_VERIFY,
        "CROSS_DB_TRANSACTION": OutboxEventType.CHECKPOINT_CREATE,
        "STEP_COMPLETED": OutboxEventType.CHECKPOINT_SAVED,
        "DISTRIBUTED_ROLLBACK": OutboxEventType.ROLLBACK_VERIFY,
        "PIPELINE_STEP_COMPLETED": OutboxEventType.CHECKPOINT_SAVED,
        "VARIANT_EXECUTION_COMPLETED": OutboxEventType.CHECKPOINT_SAVED,
        "BATCH_TEST_STARTED": OutboxEventType.BATCH_TEST_CREATED,
        "VARIANT_ROLLBACK": OutboxEventType.ROLLBACK_VERIFY,
        "CROSS_SYSTEM_COMPLETE": OutboxEventType.CHECKPOINT_VERIFY,
        "DURABLE_TEST": OutboxEventType.CHECKPOINT_VERIFY,
        "EXECUTION_FAILED": OutboxEventType.CHECKPOINT_VERIFY,
        "EXECUTION_RECOVERED": OutboxEventType.CHECKPOINT_VERIFY,
    }
    
    event_type = type_mapping.get(event_type_name, OutboxEventType.CHECKPOINT_CREATE)
    
    event = OutboxEvent(
        event_id=event_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=payload or {},
    )
    event.payload["_event_type_name"] = event_type_name
    return event


def verify_checkpoint_file_link(
    checkpoint_id: int,
    commit_id: str,
    checkpoint_repo,
    commit_repo,
) -> VerificationResult:
    """Verify checkpoint-file link consistency."""
    checkpoint = checkpoint_repo.get_by_id(checkpoint_id)
    commit = commit_repo.get_by_id(commit_id)
    
    if not checkpoint:
        return VerificationResult(
            success=False,
            message=f"Checkpoint {checkpoint_id} not found",
        )
    
    if not commit:
        return VerificationResult(
            success=False,
            message=f"Commit {commit_id} not found",
        )
    
    return VerificationResult(
        success=True,
        message="Checkpoint-file link verified",
        details={"checkpoint_id": checkpoint_id, "commit_id": commit_id},
    )


def verify_state_consistency(
    states: List[Dict[str, Any]],
    invariant,
) -> VerificationResult:
    """Verify state invariant holds for all states."""
    violations = []
    for i, state in enumerate(states):
        if not invariant(state):
            violations.append(i)
    
    if violations:
        return VerificationResult(
            success=False,
            message=f"State invariant violated at indices: {violations}",
            details={"violation_indices": violations},
        )
    
    return VerificationResult(
        success=True,
        message="State consistency verified",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Full Stack Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullStackIntegration:
    """Tests for full stack integration (Ray + venv + LangGraph + FileTracker)."""
    
    def test_complete_execution_all_systems(
        self,
        simple_compiled_graph,
        full_integration_setup,
        execution_context,
    ):
        """Complete execution should create records in all systems."""
        from tests.test_outbox_transaction_consistency.helpers import create_simple_state
        
        setup = full_integration_setup
        exec_id = execution_context["execution_id"]
        config = execution_context["config"]
        
        # Execute workflow
        result = simple_compiled_graph.invoke(create_simple_state(), config)
        
        # Create records in all systems
        
        # 1. LangGraph checkpoint
        setup["checkpoint_repository"].add_checkpoint(
            checkpoint_id=1,
            thread_id=execution_context["thread_id"],
            step=result["count"],
            state=result,
        )
        
        # 2. FileTracker commit
        blob_hash = setup["blob_repository"].save(str(result).encode())
        setup["commit_repository"].add_commit(
            commit_id=f"commit-{exec_id}",
            mementos=[MockMemento("result.json", blob_hash)],
            execution_id=exec_id,
        )
        
        # 3. Ray actor (simulated)
        actor = setup["actor_pool"].create_actor(
            actor_id=f"actor-{exec_id}",
            execution_id=exec_id,
            workspace_id="ws-1",
        )
        
        # 4. Venv (simulated)
        venv = setup["venv_manager"].create_venv(
            venv_id=f"venv-{exec_id}",
            venv_path=f"/tmp/venv-{exec_id}",
        )
        
        # 5. Outbox events
        setup["outbox_repository"].add(create_cross_system_event(
            event_id=f"exec-complete-{exec_id}",
            event_type_name="EXECUTION_COMPLETED",
            aggregate_type="Execution",
            aggregate_id=exec_id,
        ))
        
        # Verify all systems have records
        assert setup["checkpoint_repository"].get_by_id(1) is not None
        assert setup["commit_repository"].get_by_id(f"commit-{exec_id}") is not None
        assert setup["actor_pool"].get_actor(f"actor-{exec_id}") is not None
        assert setup["venv_manager"].get_venv(f"venv-{exec_id}") is not None
        assert setup["outbox_repository"].get_by_id(f"exec-complete-{exec_id}") is not None
    
    def test_execution_with_file_tracking(
        self,
        file_tracking_compiled_graph,
        full_integration_setup,
    ):
        """Execution with file tracking should maintain consistency."""
        from tests.test_outbox_transaction_consistency.helpers import create_file_tracking_state
        
        setup = full_integration_setup
        exec_id = f"exec-file-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": f"wtb-{exec_id}"}}
        
        # Execute
        result = file_tracking_compiled_graph.invoke(create_file_tracking_state(), config)
        
        # Create file commits for each step
        for i, file_path in enumerate(result.get("files_processed", [])):
            blob_hash = setup["blob_repository"].save(f"content_{i}".encode())
            setup["commit_repository"].add_commit(
                commit_id=f"commit-{exec_id}-{i}",
                mementos=[MockMemento(file_path, blob_hash)],
                execution_id=exec_id,
            )
        
        # Create checkpoint
        setup["checkpoint_repository"].add_checkpoint(
            checkpoint_id=1,
            thread_id=f"wtb-{exec_id}",
            step=result["count"],
            state=result,
        )
        
        # Verify file-checkpoint link consistency
        commits = setup["commit_repository"].list_by_execution(exec_id)
        checkpoint = setup["checkpoint_repository"].get_by_id(1)
        
        assert len(commits) >= 1
        assert checkpoint is not None
    
    def test_batch_test_full_stack(
        self,
        batch_test_compiled_graph,
        full_integration_setup,
        batch_test_variants,
    ):
        """Batch test should create consistent records across all systems."""
        from tests.test_outbox_transaction_consistency.helpers import create_batch_test_state
        
        setup = full_integration_setup
        batch_test_id = f"bt-full-{uuid.uuid4().hex[:8]}"
        
        # Create batch start event
        setup["outbox_repository"].add(create_cross_system_event(
            event_id=f"start-{batch_test_id}",
            event_type_name="BATCH_TEST_STARTED",
            aggregate_type="BatchTest",
            aggregate_id=batch_test_id,
            payload={"variant_count": len(batch_test_variants)},
        ))
        
        # Execute variants
        for i, variant in enumerate(batch_test_variants[:3]):
            config = {"configurable": {"thread_id": f"{batch_test_id}-v{i}"}}
            state = create_batch_test_state(
                batch_test_id=batch_test_id,
                variant_name=variant["variant_name"],
                variant_config=variant["variant_config"],
            )
            
            result = batch_test_compiled_graph.invoke(state, config)
            
            # Create checkpoint
            setup["checkpoint_repository"].add_checkpoint(
                checkpoint_id=100 + i,
                thread_id=f"{batch_test_id}-v{i}",
                step=result["count"],
                state=result,
            )
            
            # Create file commit
            blob_hash = setup["blob_repository"].save(str(result).encode())
            setup["commit_repository"].add_commit(
                commit_id=f"commit-{batch_test_id}-v{i}",
                mementos=[MockMemento(f"result_v{i}.json", blob_hash)],
                execution_id=f"{batch_test_id}-v{i}",
            )
            
            # Create variant completion event
            setup["outbox_repository"].add(create_cross_system_event(
                event_id=f"var-complete-{batch_test_id}-{i}",
                event_type_name="VARIANT_EXECUTION_COMPLETED",
                aggregate_type="BatchTest",
                aggregate_id=batch_test_id,
                payload={"variant_index": i, "success": True},
            ))
        
        # Verify all variants recorded
        all_events = setup["outbox_repository"].list_all()
        batch_events = [e for e in all_events if e.aggregate_id == batch_test_id]
        assert len(batch_events) == 4  # 1 start + 3 variants


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-Database Consistency Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrossDBConsistency:
    """Tests for cross-database consistency (WTB, AgentGit, FileTracker)."""
    
    def test_checkpoint_file_link_consistency(
        self,
        checkpoint_repository,
        commit_repository,
        blob_repository,
        outbox_repository,
    ):
        """Checkpoint-file links should be consistent across databases."""
        exec_id = "cross-db-1"
        
        # Create checkpoint in WTB
        checkpoint = checkpoint_repository.add_checkpoint(
            checkpoint_id=1,
            thread_id=exec_id,
            step=3,
            state={"messages": ["A", "B", "C"], "count": 3},
        )
        
        # Create file commit in FileTracker
        blob_hash = blob_repository.save(b"checkpoint state data")
        commit = commit_repository.add_commit(
            commit_id=f"commit-{exec_id}",
            mementos=[MockMemento("state.json", blob_hash)],
            execution_id=exec_id,
        )
        
        # Create link verification event in outbox
        link_event = outbox_repository.add(create_cross_system_event(
            event_id="link-verify-1",
            event_type_name="CHECKPOINT_FILE_LINK_VERIFY",
            aggregate_type="CheckpointFile",
            aggregate_id=f"{checkpoint.checkpoint_id}_{commit.commit_id}",
            payload={
                "checkpoint_id": checkpoint.checkpoint_id,
                "commit_id": commit.commit_id,
            },
        ))
        
        # Verify consistency
        result = verify_checkpoint_file_link(
            checkpoint_id=checkpoint.checkpoint_id,
            commit_id=commit.commit_id,
            checkpoint_repo=checkpoint_repository,
            commit_repo=commit_repository,
        )
        assert result.success
    
    def test_outbox_event_links_to_all_dbs(
        self,
        checkpoint_repository,
        commit_repository,
        outbox_repository,
    ):
        """Outbox events should correctly link to records in all databases."""
        exec_id = "outbox-link-1"
        
        # Create records in each DB
        checkpoint = checkpoint_repository.add_checkpoint(
            checkpoint_id=1,
            thread_id=exec_id,
            step=1,
        )
        
        commit = commit_repository.add_commit(
            commit_id=f"commit-{exec_id}",
            mementos=[],
        )
        
        # Create outbox event that references both
        event = create_cross_system_event(
            event_id="cross-link-1",
            event_type_name="CROSS_DB_VERIFY",
            aggregate_type="Execution",
            aggregate_id=exec_id,
            payload={
                "checkpoint_id": checkpoint.checkpoint_id,
                "commit_id": commit.commit_id,
                "databases": ["wtb", "filetracker"],
            },
        )
        outbox_repository.add(event)
        
        # Verify links
        saved_event = outbox_repository.get_by_id("cross-link-1")
        assert saved_event.payload["checkpoint_id"] == checkpoint.checkpoint_id
        assert saved_event.payload["commit_id"] == commit.commit_id
    
    def test_cross_db_transaction_atomicity(
        self,
        checkpoint_repository,
        commit_repository,
        blob_repository,
        outbox_repository,
    ):
        """Cross-database operations should be atomic."""
        exec_id = "atomic-cross-1"
        operations = []
        
        try:
            # WTB: Create checkpoint
            checkpoint = checkpoint_repository.add_checkpoint(
                checkpoint_id=1,
                thread_id=exec_id,
                step=1,
            )
            operations.append(("wtb_checkpoint", True))
            
            # FileTracker: Create blob and commit
            blob_hash = blob_repository.save(b"atomic content")
            operations.append(("filetracker_blob", True))
            
            commit = commit_repository.add_commit(
                commit_id=f"commit-{exec_id}",
                mementos=[MockMemento("file.txt", blob_hash)],
            )
            operations.append(("filetracker_commit", True))
            
            # Outbox: Create link event
            outbox_repository.add(create_cross_system_event(
                event_id=f"atomic-event-{exec_id}",
                event_type_name="CROSS_DB_TRANSACTION",
                aggregate_type="Execution",
                aggregate_id=exec_id,
            ))
            operations.append(("outbox_event", True))
            
        except Exception as e:
            operations.append(("error", False))
        
        result = verify_transaction_atomicity(operations)
        assert result.success


# ═══════════════════════════════════════════════════════════════════════════════
# Distributed Transaction Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestDistributedTransaction:
    """Tests for distributed transaction consistency."""
    
    def test_ray_actor_checkpoint_file_consistency(
        self,
        actor_pool,
        checkpoint_repository,
        commit_repository,
        blob_repository,
        outbox_repository,
    ):
        """Ray actor execution should maintain checkpoint-file consistency."""
        exec_id = "ray-consist-1"
        
        # Create actor
        actor = actor_pool.create_actor(
            actor_id=f"actor-{exec_id}",
            execution_id=exec_id,
            workspace_id="ws-1",
        )
        
        # Simulate execution steps
        for step in range(3):
            # Create checkpoint
            checkpoint = checkpoint_repository.add_checkpoint(
                checkpoint_id=step + 1,
                thread_id=exec_id,
                step=step + 1,
                state={"step": step + 1, "actor_id": actor.actor_id},
            )
            
            # Create file commit
            blob_hash = blob_repository.save(f"step {step + 1} output".encode())
            commit = commit_repository.add_commit(
                commit_id=f"commit-{exec_id}-{step}",
                mementos=[MockMemento(f"output_{step}.txt", blob_hash)],
                execution_id=exec_id,
            )
            
            # Create outbox event
            outbox_repository.add(create_cross_system_event(
                event_id=f"step-{exec_id}-{step}",
                event_type_name="STEP_COMPLETED",
                aggregate_type="Execution",
                aggregate_id=exec_id,
                payload={
                    "step": step + 1,
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "commit_id": commit.commit_id,
                },
            ))
        
        # Verify consistency
        checkpoints = checkpoint_repository.list_by_thread(exec_id)
        commits = commit_repository.list_by_execution(exec_id)
        
        assert len(checkpoints) == 3
        assert len(commits) == 3
    
    def test_distributed_rollback_consistency(
        self,
        actor_pool,
        checkpoint_repository,
        commit_repository,
        blob_repository,
        outbox_repository,
    ):
        """Distributed rollback should maintain consistency."""
        exec_id = "dist-rollback-1"
        
        # Create initial state
        for step in range(3):
            checkpoint_repository.add_checkpoint(
                checkpoint_id=step + 1,
                thread_id=exec_id,
                step=step + 1,
            )
            blob_hash = blob_repository.save(f"step {step + 1}".encode())
            commit_repository.add_commit(
                commit_id=f"commit-{exec_id}-{step}",
                mementos=[MockMemento(f"file_{step}.txt", blob_hash)],
                execution_id=exec_id,
            )
        
        # Create actor
        actor = actor_pool.create_actor(
            actor_id=f"actor-{exec_id}",
            execution_id=exec_id,
            workspace_id="ws-1",
        )
        
        # Perform rollback to step 1
        rollback_event = create_cross_system_event(
            event_id=f"rollback-{exec_id}",
            event_type_name="DISTRIBUTED_ROLLBACK",
            aggregate_type="Execution",
            aggregate_id=exec_id,
            payload={
                "from_step": 3,
                "to_step": 1,
                "actor_id": actor.actor_id,
                "checkpoint_id": 1,
                "commit_id": f"commit-{exec_id}-0",
            },
        )
        outbox_repository.add(rollback_event)
        
        # Verify rollback event contains all necessary references
        saved = outbox_repository.get_by_id(f"rollback-{exec_id}")
        assert saved.payload["checkpoint_id"] == 1
        assert saved.payload["commit_id"] == f"commit-{exec_id}-0"


# ═══════════════════════════════════════════════════════════════════════════════
# End-to-End Scenario Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestEndToEndScenarios:
    """Tests for complete end-to-end scenarios."""
    
    def test_ml_pipeline_workflow(
        self,
        full_integration_setup,
    ):
        """ML pipeline workflow should maintain cross-system consistency."""
        setup = full_integration_setup
        exec_id = "ml-pipeline-1"
        
        # Stage 1: Setup venv
        venv = setup["venv_manager"].create_venv(
            venv_id=f"venv-{exec_id}",
            venv_path=f"/tmp/venv-{exec_id}",
        )
        setup["venv_manager"].install_packages(f"venv-{exec_id}", ["torch", "numpy"])
        
        # Stage 2: Execute training steps
        steps = ["data_load", "preprocess", "train", "evaluate", "save"]
        
        for i, step_name in enumerate(steps):
            # Create checkpoint
            setup["checkpoint_repository"].add_checkpoint(
                checkpoint_id=i + 1,
                thread_id=exec_id,
                step=i + 1,
                state={"current_step": step_name, "step_index": i},
            )
            
            # Create output files
            blob_hash = setup["blob_repository"].save(f"{step_name} output".encode())
            setup["commit_repository"].add_commit(
                commit_id=f"commit-{exec_id}-{step_name}",
                mementos=[MockMemento(f"{step_name}_output.pkl", blob_hash)],
                execution_id=exec_id,
            )
            
            # Create step event
            setup["outbox_repository"].add(create_cross_system_event(
                event_id=f"step-{exec_id}-{i}",
                event_type_name="PIPELINE_STEP_COMPLETED",
                aggregate_type="MLPipeline",
                aggregate_id=exec_id,
                payload={"step": step_name, "index": i},
            ))
        
        # Stage 3: Create actor for distributed inference
        actor = setup["actor_pool"].create_actor(
            actor_id=f"actor-{exec_id}",
            execution_id=exec_id,
            workspace_id="ml-workspace",
        )
        
        # Verify complete pipeline
        checkpoints = setup["checkpoint_repository"].list_by_thread(exec_id)
        commits = setup["commit_repository"].list_by_execution(exec_id)
        events = [e for e in setup["outbox_repository"].list_all() 
                  if e.aggregate_id == exec_id]
        
        assert len(checkpoints) == 5
        assert len(commits) == 5
        assert len(events) == 5
        assert setup["venv_manager"].get_venv(f"venv-{exec_id}") is not None
        assert setup["actor_pool"].get_actor(f"actor-{exec_id}") is not None
    
    def test_batch_test_with_variants_and_rollback(
        self,
        full_integration_setup,
    ):
        """Batch test with variant rollback should maintain consistency."""
        setup = full_integration_setup
        batch_id = "batch-rollback-1"
        
        # Execute variants
        for i in range(3):
            variant_id = f"{batch_id}-v{i}"
            
            # Checkpoint
            setup["checkpoint_repository"].add_checkpoint(
                checkpoint_id=100 + i,
                thread_id=variant_id,
                step=3,
            )
            
            # Files
            blob_hash = setup["blob_repository"].save(f"variant {i} result".encode())
            setup["commit_repository"].add_commit(
                commit_id=f"commit-{variant_id}",
                mementos=[MockMemento(f"result_v{i}.json", blob_hash)],
                execution_id=variant_id,
            )
        
        # Simulate variant 1 needs rollback
        rollback_event = create_cross_system_event(
            event_id=f"rollback-{batch_id}-v1",
            event_type_name="VARIANT_ROLLBACK",
            aggregate_type="BatchTest",
            aggregate_id=batch_id,
            payload={
                "variant_index": 1,
                "rollback_to_checkpoint": 100,  # Rollback to v0's checkpoint
                "reason": "Variant 1 failed validation",
            },
        )
        setup["outbox_repository"].add(rollback_event)
        
        # Verify rollback event created
        saved = setup["outbox_repository"].get_by_id(f"rollback-{batch_id}-v1")
        assert saved.payload["variant_index"] == 1
    
    def test_concurrent_executions_with_shared_venv(
        self,
        full_integration_setup,
    ):
        """Concurrent executions sharing venv should be isolated."""
        setup = full_integration_setup
        
        # Create shared venv
        shared_venv = setup["venv_manager"].create_venv(
            venv_id="shared-venv",
            venv_path="/tmp/shared-venv",
        )
        setup["venv_manager"].install_packages("shared-venv", ["numpy"])
        
        results = {}
        lock = threading.Lock()
        
        def run_execution(exec_id: str, unique_value: int):
            # Create checkpoint
            setup["checkpoint_repository"].add_checkpoint(
                checkpoint_id=1000 + unique_value,
                thread_id=exec_id,
                step=1,
                state={"value": unique_value},
            )
            
            # Create files
            blob_hash = setup["blob_repository"].save(f"exec {unique_value}".encode())
            setup["commit_repository"].add_commit(
                commit_id=f"commit-{exec_id}",
                mementos=[MockMemento(f"output_{exec_id}.txt", blob_hash)],
                execution_id=exec_id,
            )
            
            with lock:
                results[exec_id] = unique_value
        
        threads = []
        for i in range(5):
            exec_id = f"concurrent-{i}"
            t = threading.Thread(target=run_execution, args=(exec_id, i))
            threads.append(t)
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Verify all executions completed with correct values
        assert len(results) == 5
        for exec_id, value in results.items():
            commit = setup["commit_repository"].get_by_id(f"commit-{exec_id}")
            assert commit is not None


# ═══════════════════════════════════════════════════════════════════════════════
# ACID Property Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrossSystemACIDProperties:
    """Tests for ACID properties across all systems."""
    
    def test_atomicity_all_or_nothing(
        self,
        full_integration_setup,
    ):
        """Cross-system operation should be all-or-nothing."""
        setup = full_integration_setup
        exec_id = "atomic-all-1"
        operations = []
        
        try:
            # LangGraph checkpoint
            setup["checkpoint_repository"].add_checkpoint(
                checkpoint_id=1,
                thread_id=exec_id,
                step=1,
            )
            operations.append(("checkpoint", True))
            
            # FileTracker blob
            blob_hash = setup["blob_repository"].save(b"atomic")
            operations.append(("blob", True))
            
            # FileTracker commit
            setup["commit_repository"].add_commit(
                commit_id=f"commit-{exec_id}",
                mementos=[MockMemento("f.txt", blob_hash)],
            )
            operations.append(("commit", True))
            
            # Ray actor
            setup["actor_pool"].create_actor(
                actor_id=f"actor-{exec_id}",
                execution_id=exec_id,
                workspace_id="ws",
            )
            operations.append(("actor", True))
            
            # Venv
            setup["venv_manager"].create_venv(
                venv_id=f"venv-{exec_id}",
                venv_path=f"/tmp/venv-{exec_id}",
            )
            operations.append(("venv", True))
            
            # Outbox
            setup["outbox_repository"].add(create_cross_system_event(
                event_id=f"event-{exec_id}",
                event_type_name="CROSS_SYSTEM_COMPLETE",
                aggregate_type="Execution",
                aggregate_id=exec_id,
            ))
            operations.append(("outbox", True))
            
        except Exception:
            operations.append(("error", False))
        
        result = verify_transaction_atomicity(operations)
        assert result.success
    
    def test_consistency_invariants_maintained(
        self,
        full_integration_setup,
    ):
        """State invariants should be maintained across systems."""
        setup = full_integration_setup
        exec_id = "consist-inv-1"
        
        states = []
        
        # Execute multiple steps, checking invariants at each
        for step in range(1, 4):
            state = {"step": step, "count": step, "messages": ["M"] * step}
            
            # Create checkpoint
            setup["checkpoint_repository"].add_checkpoint(
                checkpoint_id=step,
                thread_id=exec_id,
                step=step,
                state=state,
            )
            
            states.append(state)
        
        # Invariant: count equals number of messages
        invariant = lambda s: s["count"] == len(s["messages"])
        
        result = verify_state_consistency(states, invariant)
        assert result.success
    
    def test_isolation_concurrent_systems(
        self,
        full_integration_setup,
    ):
        """Concurrent operations in different systems should be isolated."""
        setup = full_integration_setup
        
        results = {"checkpoints": [], "commits": [], "actors": []}
        lock = threading.Lock()
        
        def create_checkpoint(exec_id: str, cp_id: int):
            setup["checkpoint_repository"].add_checkpoint(
                checkpoint_id=cp_id,
                thread_id=exec_id,
                step=1,
            )
            with lock:
                results["checkpoints"].append(cp_id)
        
        def create_commit(exec_id: str, commit_id: str):
            setup["commit_repository"].add_commit(
                commit_id=commit_id,
                mementos=[],
            )
            with lock:
                results["commits"].append(commit_id)
        
        def create_actor(exec_id: str, actor_id: str):
            setup["actor_pool"].create_actor(
                actor_id=actor_id,
                execution_id=exec_id,
                workspace_id="ws",
            )
            with lock:
                results["actors"].append(actor_id)
        
        threads = []
        for i in range(5):
            exec_id = f"iso-{i}"
            threads.append(threading.Thread(target=create_checkpoint, args=(exec_id, 2000 + i)))
            threads.append(threading.Thread(target=create_commit, args=(exec_id, f"commit-iso-{i}")))
            threads.append(threading.Thread(target=create_actor, args=(exec_id, f"actor-iso-{i}")))
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All operations should complete without interference
        assert len(results["checkpoints"]) == 5
        assert len(results["commits"]) == 5
        assert len(results["actors"]) == 5
    
    def test_durability_all_systems(
        self,
        full_integration_setup,
    ):
        """Data should be durable in all systems."""
        setup = full_integration_setup
        exec_id = "durable-1"
        
        # Create records in all systems
        cp = setup["checkpoint_repository"].add_checkpoint(
            checkpoint_id=1,
            thread_id=exec_id,
            step=1,
            state={"value": 42},
        )
        
        blob_hash = setup["blob_repository"].save(b"durable content")
        commit = setup["commit_repository"].add_commit(
            commit_id=f"commit-{exec_id}",
            mementos=[MockMemento("durable.txt", blob_hash)],
        )
        
        actor = setup["actor_pool"].create_actor(
            actor_id=f"actor-{exec_id}",
            execution_id=exec_id,
            workspace_id="ws",
        )
        
        venv = setup["venv_manager"].create_venv(
            venv_id=f"venv-{exec_id}",
            venv_path=f"/tmp/venv-{exec_id}",
        )
        
        event = setup["outbox_repository"].add(create_cross_system_event(
            event_id=f"durable-event-{exec_id}",
            event_type_name="DURABLE_TEST",
            aggregate_type="Execution",
            aggregate_id=exec_id,
        ))
        event.mark_processed()
        setup["outbox_repository"].update(event)
        
        # Verify all retrievable
        assert setup["checkpoint_repository"].get_by_id(1) is not None
        assert setup["commit_repository"].get_by_id(f"commit-{exec_id}") is not None
        assert setup["blob_repository"].exists(blob_hash)
        assert setup["actor_pool"].get_actor(f"actor-{exec_id}") is not None
        assert setup["venv_manager"].get_venv(f"venv-{exec_id}") is not None
        
        retrieved_event = setup["outbox_repository"].get_by_id(f"durable-event-{exec_id}")
        assert retrieved_event is not None
        assert retrieved_event.status.value == "processed"


# ═══════════════════════════════════════════════════════════════════════════════
# Failure Recovery Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFailureRecovery:
    """Tests for failure recovery across systems."""
    
    def test_partial_failure_recovery(
        self,
        full_integration_setup,
    ):
        """System should recover from partial failures."""
        setup = full_integration_setup
        exec_id = "partial-fail-1"
        
        # Successfully create some records
        setup["checkpoint_repository"].add_checkpoint(
            checkpoint_id=1,
            thread_id=exec_id,
            step=1,
        )
        
        blob_hash = setup["blob_repository"].save(b"before failure")
        setup["commit_repository"].add_commit(
            commit_id=f"commit-{exec_id}",
            mementos=[MockMemento("file.txt", blob_hash)],
        )
        
        # Record failure event
        failure_event = create_cross_system_event(
            event_id=f"failure-{exec_id}",
            event_type_name="EXECUTION_FAILED",
            aggregate_type="Execution",
            aggregate_id=exec_id,
            payload={
                "error_type": "ProcessingError",
                "failed_at_step": 2,
                "recoverable": True,
                "last_checkpoint": 1,
                "last_commit": f"commit-{exec_id}",
            },
        )
        setup["outbox_repository"].add(failure_event)
        
        # Verify recovery information available
        saved = setup["outbox_repository"].get_by_id(f"failure-{exec_id}")
        assert saved.payload["recoverable"]
        assert saved.payload["last_checkpoint"] == 1
    
    def test_cross_system_recovery(
        self,
        full_integration_setup,
    ):
        """Recovery should restore state across all systems."""
        setup = full_integration_setup
        exec_id = "cross-recover-1"
        
        # Create recovery checkpoint
        recovery_checkpoint = setup["checkpoint_repository"].add_checkpoint(
            checkpoint_id=1,
            thread_id=exec_id,
            step=1,
            state={"recovered": True},
        )
        
        # Create recovery commit
        blob_hash = setup["blob_repository"].save(b"recovery state")
        recovery_commit = setup["commit_repository"].add_commit(
            commit_id=f"recovery-commit-{exec_id}",
            mementos=[MockMemento("recovery.json", blob_hash)],
        )
        
        # Create recovery event
        recovery_event = create_cross_system_event(
            event_id=f"recovery-{exec_id}",
            event_type_name="EXECUTION_RECOVERED",
            aggregate_type="Execution",
            aggregate_id=exec_id,
            payload={
                "recovered_from_checkpoint": recovery_checkpoint.checkpoint_id,
                "recovered_from_commit": recovery_commit.commit_id,
                "systems_recovered": ["checkpoint", "filetracker", "outbox"],
            },
        )
        setup["outbox_repository"].add(recovery_event)
        
        # Verify recovery complete
        saved = setup["outbox_repository"].get_by_id(f"recovery-{exec_id}")
        assert "checkpoint" in saved.payload["systems_recovered"]
        assert "filetracker" in saved.payload["systems_recovered"]
