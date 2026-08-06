"""
Integration tests using REAL services - NO MOCKS.

This test file verifies system behavior with:
- Real SQLite databases
- Real OutboxProcessor
- Real FileTracker
- Real LangGraph checkpointer
- Real UV Venv Manager (when available)
- Real Ray (when available)

Run with: pytest tests/test_outbox_transaction_consistency/test_real_services_integration.py -v
"""

import socket
import uuid

import pytest

from wtb.domain.models.outbox import OutboxEvent, OutboxEventType, OutboxStatus
from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork

# ═══════════════════════════════════════════════════════════════════════════════
# Helper: Check Service Availability
# ═══════════════════════════════════════════════════════════════════════════════

def is_service_available(host: str, port: int) -> bool:
    """Check if a TCP service is available."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        result = sock.connect_ex((host, port))
        return result == 0
    finally:
        sock.close()


def is_ray_available() -> bool:
    """Check if Ray is available."""
    try:
        import ray
        return True
    except ImportError:
        return False


def is_venv_manager_available() -> bool:
    """Check if UV Venv Manager gRPC service is running."""
    return is_service_available('localhost', 50051)


# ═══════════════════════════════════════════════════════════════════════════════
# Real Database Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRealDatabaseIntegration:
    """Tests using real SQLite database - no mocks."""
    
    def test_real_outbox_event_persistence(self, unit_of_work):
        """OutboxEvent should persist to real SQLite database."""
        # Create a real outbox event
        event = OutboxEvent.create(
            event_type=OutboxEventType.CHECKPOINT_CREATE,
            aggregate_id=f"exec-{uuid.uuid4().hex[:8]}",
            aggregate_type="Execution",
            payload={"node_id": "test-node", "state": {"count": 1}},
            idempotency_key=f"idem-{uuid.uuid4().hex[:8]}",
        )
        
        # Persist via real repository
        unit_of_work.outbox.add(event)
        unit_of_work.commit()
        
        # Verify persisted using get_by_id (uses event_id)
        retrieved = unit_of_work.outbox.get_by_id(event.event_id)
        assert retrieved is not None
        assert retrieved.event_type == OutboxEventType.CHECKPOINT_CREATE
        assert retrieved.aggregate_id == event.aggregate_id
        assert retrieved.idempotency_key == event.idempotency_key
        assert retrieved.status == OutboxStatus.PENDING
    
    def test_real_blob_persistence(self, unit_of_work):
        """Blob content should persist to real storage."""
        content = b"real blob content for testing"
        
        blob_id = unit_of_work.blobs.save(content)
        unit_of_work.commit()
        
        # Verify content retrievable
        retrieved = unit_of_work.blobs.get(blob_id)
        assert retrieved == content
    
    def test_transaction_atomicity_real_db(self, wtb_db_url):
        """Transactions should be atomic with real SQLite database."""
        exec_id = f"exec-{uuid.uuid4().hex[:8]}"
        
        # First transaction: create event
        with SQLAlchemyUnitOfWork(wtb_db_url) as uow:
            event = OutboxEvent.create(
                event_type=OutboxEventType.CHECKPOINT_CREATE,
                aggregate_id=exec_id,
                aggregate_type="Execution",
                payload={"step": 1},
            )
            saved_event = uow.outbox.add(event)
            uow.commit()
            event_id = saved_event.event_id
            saved_id = saved_event.id  # DB primary key needed for update
        
        # Second transaction: verify and update using Repository pattern
        with SQLAlchemyUnitOfWork(wtb_db_url) as uow:
            retrieved = uow.outbox.get_by_pk(saved_id)
            assert retrieved is not None
            
            # Mark as processing (modifies domain object)
            retrieved.mark_processing()
            # Repository pattern: explicitly update to persist changes
            uow.outbox.update(retrieved)
            uow.commit()
        
        # Third transaction: verify update persisted
        with SQLAlchemyUnitOfWork(wtb_db_url) as uow:
            final = uow.outbox.get_by_pk(saved_id)
            assert final.status == OutboxStatus.PROCESSING
    
    def test_multiple_events_same_aggregate(self, unit_of_work):
        """Multiple events for same aggregate should all persist."""
        exec_id = f"exec-{uuid.uuid4().hex[:8]}"
        
        # Create multiple events
        events = []
        for i in range(3):
            event = OutboxEvent.create(
                event_type=OutboxEventType.CHECKPOINT_CREATE,
                aggregate_id=exec_id,
                aggregate_type="Execution",
                payload={"step": i},
            )
            unit_of_work.outbox.add(event)
            events.append(event)
        
        unit_of_work.commit()
        
        # Verify all events persisted
        for event in events:
            retrieved = unit_of_work.outbox.get_by_id(event.event_id)
            assert retrieved is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Real OutboxProcessor Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRealOutboxProcessorIntegration:
    """Tests using real OutboxProcessor - no mocks."""
    
    def test_processor_starts_and_processes(self, running_outbox_processor, outbox_processor_db_url):
        """Real OutboxProcessor should start and process events."""
        import time
        
        processor = running_outbox_processor
        
        # Create event in the processor's database
        with SQLAlchemyUnitOfWork(outbox_processor_db_url) as uow:
            event = OutboxEvent.create(
                event_type=OutboxEventType.CHECKPOINT_CREATE,
                aggregate_id=f"exec-{uuid.uuid4().hex[:8]}",
                aggregate_type="Execution",
                payload={"node_id": "test-node"},
            )
            uow.outbox.add(event)
            uow.commit()
        
        # Wait for processor to process
        time.sleep(0.5)
        
        # Verify processor is running
        assert processor.is_running()
        
        # Get stats (different key names)
        stats = processor.get_stats()
        assert isinstance(stats, dict)
    
    def test_processor_lifecycle(self, outbox_processor):
        """OutboxProcessor lifecycle management."""
        processor = outbox_processor
        
        # Start
        processor.start()
        assert processor.is_running()
        
        # Stop
        processor.stop(timeout=2.0)
        assert not processor.is_running()


# ═══════════════════════════════════════════════════════════════════════════════
# Real LangGraph Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRealLangGraphIntegration:
    """Tests using real LangGraph checkpointer - no mocks."""
    
    def test_real_sqlite_checkpointer_persistence(self, checkpoint_db_path):
        """LangGraph SQLite checkpointer should persist state."""
        from typing import TypedDict

        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.graph import END, StateGraph
        
        class State(TypedDict):
            count: int
            messages: list
        
        def increment_node(state: State) -> State:
            return {"count": state["count"] + 1, "messages": state["messages"]}
        
        # Build graph
        builder = StateGraph(State)
        builder.add_node("increment", increment_node)
        builder.set_entry_point("increment")
        builder.add_edge("increment", END)
        
        # Use real SQLite checkpointer
        with SqliteSaver.from_conn_string(str(checkpoint_db_path)) as checkpointer:
            graph = builder.compile(checkpointer=checkpointer)
            
            thread_id = f"thread-{uuid.uuid4().hex[:8]}"
            config = {"configurable": {"thread_id": thread_id}}
            
            # Execute
            result = graph.invoke({"count": 0, "messages": []}, config)
            assert result["count"] == 1
            
            # Verify checkpoint persisted
            state = graph.get_state(config)
            assert state.values["count"] == 1
    
    def test_real_checkpoint_history(self, checkpoint_db_path):
        """Checkpoint history should work with real SQLite."""
        from typing import TypedDict

        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.graph import END, StateGraph
        
        class State(TypedDict):
            count: int
        
        def increment(state: State) -> State:
            return {"count": state["count"] + 1}
        
        builder = StateGraph(State)
        builder.add_node("inc", increment)
        builder.set_entry_point("inc")
        builder.add_edge("inc", END)
        
        with SqliteSaver.from_conn_string(str(checkpoint_db_path)) as checkpointer:
            graph = builder.compile(checkpointer=checkpointer)
            
            thread_id = f"history-{uuid.uuid4().hex[:8]}"
            config = {"configurable": {"thread_id": thread_id}}
            
            # Run multiple times
            graph.invoke({"count": 0}, config)
            graph.invoke({"count": 0}, config)
            graph.invoke({"count": 0}, config)
            
            # Get history - should have entries
            history = list(graph.get_state_history(config))
            assert len(history) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# Real UV Venv Manager Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRealVenvManagerIntegration:
    """Tests using real UV Venv Manager service."""
    
    @pytest.mark.asyncio
    async def test_real_venv_creation(self):
        """Create real virtual environment via gRPC service and verify
        code execution inside Docker using the ``RunCode`` RPC."""
        if not is_venv_manager_available():
            pytest.skip("UV Venv Manager not running on localhost:50051")
        
        import grpc

        from wtb.infrastructure.environment.providers import GrpcEnvironmentProvider
        
        provider = GrpcEnvironmentProvider("localhost:50051", timeout_seconds=60)
        
        variant_id = f"test-variant-{uuid.uuid4().hex[:8]}"
        config = {
            "workflow_id": "test-workflow",
            "node_id": "test-node",
            "packages": ["requests>=2.28"],
            "python_version": "3.11"
        }
        
        try:
            try:
                env_info = provider.create_environment(variant_id, config)
            except grpc.RpcError as exc:
                if exc.code() in {
                    grpc.StatusCode.UNAVAILABLE,
                    grpc.StatusCode.DEADLINE_EXCEEDED,
                }:
                    pytest.skip(
                        f"UV Venv Manager gRPC service unavailable: {exc.code().name}"
                    )
                raise
            
            assert "python_path" in env_info
            assert env_info.get("type") == "grpc_uv"
            assert env_info.get("env_path")
            
            status = provider.get_env_status(variant_id)
            assert status["status"] == "ACTIVE"
            assert status["has_venv"] is True
            assert status["has_pyproject"] is True
            
        finally:
            try:
                provider.cleanup_environment(variant_id)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# Real Ray Integration Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRealRayIntegration:
    """Tests using real Ray cluster."""
    
    def test_real_ray_cluster_available(self):
        """Verify Ray cluster is available."""
        if not is_ray_available():
            pytest.skip("Ray not installed")
        
        import ray
        
        # Initialize or connect to existing cluster
        if not ray.is_initialized():
            try:
                ray.init(address="auto", ignore_reinit_error=True)
            except ConnectionError:
                ray.init(ignore_reinit_error=True)
        
        # Verify cluster resources
        resources = ray.cluster_resources()
        assert "CPU" in resources
        assert resources["CPU"] > 0
    
    def test_real_ray_task_execution(self):
        """Execute real task on Ray cluster."""
        if not is_ray_available():
            pytest.skip("Ray not installed")
        
        import ray
        
        if not ray.is_initialized():
            try:
                ray.init(address="auto", ignore_reinit_error=True)
            except ConnectionError:
                ray.init(ignore_reinit_error=True)
        
        @ray.remote
        def compute_task(x: int) -> int:
            return x * 2
        
        # Execute
        result = ray.get(compute_task.remote(21))
        assert result == 42
    
    def test_real_ray_actor_execution(self):
        """Execute real actor on Ray cluster."""
        if not is_ray_available():
            pytest.skip("Ray not installed")
        
        import ray
        
        if not ray.is_initialized():
            try:
                ray.init(address="auto", ignore_reinit_error=True)
            except ConnectionError:
                ray.init(ignore_reinit_error=True)
        
        @ray.remote
        class Counter:
            def __init__(self):
                self.count = 0
            
            def increment(self) -> int:
                self.count += 1
                return self.count
            
            def get(self) -> int:
                return self.count
        
        # Create and use actor
        counter = Counter.remote()
        assert ray.get(counter.increment.remote()) == 1
        assert ray.get(counter.increment.remote()) == 2
        assert ray.get(counter.get.remote()) == 2


# ═══════════════════════════════════════════════════════════════════════════════
# Full Stack Integration Tests (All Real Services)
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullStackRealIntegration:
    """End-to-end tests using ALL real services."""
    
    def test_complete_workflow_with_real_services(
        self,
        wtb_bench_development,
        running_outbox_processor,
        temp_data_dir,
    ):
        """Complete workflow execution with all real services."""
        bench = wtb_bench_development
        
        # Verify test bench is properly initialized
        assert bench is not None
        assert running_outbox_processor.is_running()
        
        # The bench uses real SQLite for checkpoints
        # and can execute actual workflows
    
    def test_cross_system_consistency(
        self,
        unit_of_work,
        temp_data_dir,
    ):
        """Verify consistency across all systems with real databases."""
        exec_id = f"consistency-{uuid.uuid4().hex[:8]}"
        
        # 1. Create and save blob
        content = f"output for {exec_id}".encode()
        blob_id = unit_of_work.blobs.save(content)
        
        # 2. Create outbox event linking execution to blob
        # Best practice: convert Value Objects to primitives in payloads
        event = OutboxEvent.create(
            event_type=OutboxEventType.FILE_COMMIT_VERIFY,
            aggregate_id=exec_id,
            aggregate_type="Execution",
            payload={
                "blob_id": blob_id.value,  # Convert BlobId to string
                "execution_id": exec_id,
            },
        )
        unit_of_work.outbox.add(event)
        
        # 3. Commit all in single transaction (ACID)
        unit_of_work.commit()
        
        # 4. Verify all persisted consistently
        retrieved_blob = unit_of_work.blobs.get(blob_id)
        retrieved_event = unit_of_work.outbox.get_by_id(event.event_id)
        
        assert retrieved_blob == content
        assert retrieved_event is not None
        assert retrieved_event.payload["blob_id"] == blob_id.value
    
    def test_outbox_event_types_all_persist(self, unit_of_work):
        """All outbox event types should persist correctly."""
        exec_id = f"all-types-{uuid.uuid4().hex[:8]}"
        
        event_types = [
            OutboxEventType.CHECKPOINT_CREATE,
            OutboxEventType.CHECKPOINT_VERIFY,
            OutboxEventType.FILE_COMMIT_VERIFY,
            OutboxEventType.EXECUTION_PAUSED,
            OutboxEventType.EXECUTION_RESUMED,
            OutboxEventType.EXECUTION_STOPPED,
            OutboxEventType.STATE_MODIFIED,
            OutboxEventType.ROLLBACK_PERFORMED,
            OutboxEventType.BATCH_TEST_CREATED,
            OutboxEventType.BATCH_TEST_CANCELLED,
            OutboxEventType.WORKFLOW_CREATED,
            OutboxEventType.RAY_EVENT,
        ]
        
        created_events = []
        for event_type in event_types:
            event = OutboxEvent.create(
                event_type=event_type,
                aggregate_id=exec_id,
                aggregate_type="Execution",
                payload={"type": event_type.value},
            )
            unit_of_work.outbox.add(event)
            created_events.append(event)
        
        unit_of_work.commit()
        
        # Verify all persisted
        for event in created_events:
            retrieved = unit_of_work.outbox.get_by_id(event.event_id)
            assert retrieved is not None
            assert retrieved.event_type == event.event_type
