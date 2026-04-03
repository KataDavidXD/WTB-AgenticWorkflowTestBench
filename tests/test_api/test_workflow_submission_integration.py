"""
Integration Tests for Workflow Submission System.

Tests:
1. WorkflowProject creation and configuration
2. Node variant registration and retrieval
3. Workflow variant management
4. WTBTestBench execution with variants
5. Batch test with variant matrix
6. Node version control and replacement
7. Parallel comparison of implementations

Design Principles:
- SOLID: Tests follow single responsibility
- ACID: Verifies transaction integrity
- DRY: Reusable fixtures and helpers

Author: Senior Architect
Date: 2026-01-15
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from typing import Dict, Any, List, Callable
import uuid

from wtb.sdk.workflow_project import (
    WorkflowProject,
    FileTrackingConfig,
    EnvironmentConfig,
    ExecutionConfig,
    EnvSpec,
    NodeResourceConfig,
    RayConfig,
    NodeVariant,
    WorkflowVariant,
)
from wtb.sdk.test_bench import WTBTestBench
from wtb.sdk import Execution, BatchTestResult, Checkpoint  # Domain models
from wtb.application.services.ray_batch_runner import VariantExecutionResult


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_graph():
    """Create a mock LangGraph."""
    graph = MagicMock()
    graph.invoke = MagicMock(return_value={"result": "success", "processed": True})
    return graph


@pytest.fixture
def mock_graph_factory(mock_graph):
    """Create a mock graph factory."""
    return lambda: mock_graph


@pytest.fixture
def sample_node_implementations() -> Dict[str, Callable]:
    """Create sample node implementations for testing."""
    
    def dense_retriever(state: Dict) -> Dict:
        return {
            "documents": [{"id": "doc1", "content": "Dense result", "score": 0.95}],
            "retrieval_method": "dense",
        }
    
    def bm25_retriever(state: Dict) -> Dict:
        return {
            "documents": [{"id": "doc2", "content": "BM25 result", "score": 0.80}],
            "retrieval_method": "bm25",
        }
    
    def hybrid_retriever(state: Dict) -> Dict:
        return {
            "documents": [
                {"id": "doc1", "content": "Dense result", "score": 0.90},
                {"id": "doc2", "content": "BM25 result", "score": 0.85},
            ],
            "retrieval_method": "hybrid",
        }
    
    def cross_encoder_reranker(state: Dict) -> Dict:
        docs = state.get("documents", [])
        return {"reranked_documents": sorted(docs, key=lambda x: x["score"], reverse=True)}
    
    def openai_generator(state: Dict) -> Dict:
        return {"answer": "Generated answer from OpenAI", "model": "gpt-4"}
    
    def local_generator(state: Dict) -> Dict:
        return {"answer": "Generated answer from local LLM", "model": "local"}
    
    return {
        "dense_retriever": dense_retriever,
        "bm25_retriever": bm25_retriever,
        "hybrid_retriever": hybrid_retriever,
        "cross_encoder_reranker": cross_encoder_reranker,
        "openai_generator": openai_generator,
        "local_generator": local_generator,
    }


@pytest.fixture
def basic_project(mock_graph_factory) -> WorkflowProject:
    """Create a basic WorkflowProject for testing."""
    return WorkflowProject(
        name="test_project",
        graph_factory=mock_graph_factory,
        description="Test workflow project",
    )


@pytest.fixture
def configured_project(mock_graph_factory, sample_node_implementations) -> WorkflowProject:
    """Create a fully configured WorkflowProject."""
    project = WorkflowProject(
        name="rag_pipeline",
        graph_factory=mock_graph_factory,
        description="RAG Pipeline with variants",
        file_tracking=FileTrackingConfig(
            enabled=True,
            tracked_paths=["./data/", "./outputs/"],
            commit_on="checkpoint",
        ),
        environment=EnvironmentConfig(
            granularity="node",
            default_env=EnvSpec(
                python_version="3.12",
                dependencies=["langchain>=0.1.0"],
            ),
            node_environments={
                "retriever": EnvSpec(dependencies=["faiss-cpu>=1.7"]),
                "generator": EnvSpec(dependencies=["openai>=1.0"]),
            },
        ),
        execution=ExecutionConfig(
            batch_executor="ray",
            node_resources={
                "retriever": NodeResourceConfig(num_cpus=2, memory="4GB"),
                "generator": NodeResourceConfig(num_cpus=1, memory="2GB"),
            },
        ),
    )
    
    # Register variants
    project.register_variant(
        node="retriever",
        name="dense",
        implementation=sample_node_implementations["dense_retriever"],
        description="Dense embedding retriever",
    )
    project.register_variant(
        node="retriever",
        name="bm25",
        implementation=sample_node_implementations["bm25_retriever"],
        description="BM25 sparse retriever",
    )
    project.register_variant(
        node="retriever",
        name="hybrid",
        implementation=sample_node_implementations["hybrid_retriever"],
        description="Hybrid retriever",
    )
    project.register_variant(
        node="generator",
        name="openai",
        implementation=sample_node_implementations["openai_generator"],
        description="OpenAI GPT-4",
    )
    project.register_variant(
        node="generator",
        name="local",
        implementation=sample_node_implementations["local_generator"],
        description="Local LLM",
    )
    
    return project


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: WorkflowProject Configuration
# ═══════════════════════════════════════════════════════════════════════════════

class TestWorkflowProjectConfiguration:
    """Test WorkflowProject configuration options."""
    
    def test_create_minimal_project(self, mock_graph_factory):
        """Test creating project with minimal configuration."""
        project = WorkflowProject(
            name="minimal",
            graph_factory=mock_graph_factory,
        )
        
        assert project.name == "minimal"
        assert project.id is not None
        assert project.version == 1
        assert project.description == ""
    
    def test_create_project_with_full_config(self, mock_graph_factory):
        """Test creating project with full configuration."""
        project = WorkflowProject(
            name="full_config",
            graph_factory=mock_graph_factory,
            description="Fully configured project",
            file_tracking=FileTrackingConfig(
                enabled=True,
                tracked_paths=["./data/"],
                auto_commit=True,
                commit_on="node_complete",
            ),
            environment=EnvironmentConfig(
                granularity="variant",
                use_current_env=False,
            ),
            execution=ExecutionConfig(
                batch_executor="ray",
                checkpoint_strategy="per_node",
            ),
        )
        
        assert project.file_tracking.enabled is True
        assert project.file_tracking.commit_on == "node_complete"
        assert project.environment.granularity == "variant"
        assert project.execution.batch_executor == "ray"
    
    def test_file_tracking_config_defaults(self):
        """Test FileTrackingConfig default values."""
        config = FileTrackingConfig()
        
        assert config.enabled is False
        assert config.tracked_paths == []
        assert "*.pyc" in config.ignore_patterns
        assert config.auto_commit is True
        assert config.commit_on == "checkpoint"
        assert config.snapshot_strategy == "incremental"
        assert config.max_snapshots == 100
    
    def test_environment_config_granularities(self):
        """Test environment configuration granularity options."""
        # Workflow level
        workflow_config = EnvironmentConfig(granularity="workflow")
        assert workflow_config.granularity == "workflow"
        
        # Node level
        node_config = EnvironmentConfig(
            granularity="node",
            node_environments={
                "node1": EnvSpec(dependencies=["pkg1"]),
            },
        )
        assert node_config.granularity == "node"
        assert "node1" in node_config.node_environments
        
        # Variant level
        variant_config = EnvironmentConfig(
            granularity="variant",
            variant_environments={
                "node1:variant_a": EnvSpec(dependencies=["pkg_a"]),
            },
        )
        assert variant_config.granularity == "variant"
    
    def test_execution_config_ray_options(self):
        """Test ExecutionConfig Ray configuration."""
        config = ExecutionConfig(
            batch_executor="ray",
            ray_config=RayConfig(
                address="ray://cluster:10001",
                max_concurrent_workflows=20,
                object_store_memory=4 * 1024**3,  # 4GB
                max_retries=5,
            ),
        )
        
        assert config.ray_config.address == "ray://cluster:10001"
        assert config.ray_config.max_concurrent_workflows == 20
        assert config.ray_config.object_store_memory == 4 * 1024**3
    
    def test_node_resource_config_ray_options(self):
        """Test NodeResourceConfig conversion to Ray options."""
        config = NodeResourceConfig(
            num_cpus=4,
            num_gpus=1,
            memory="8GB",
            custom_resources={"tpu": 1},
        )
        
        ray_options = config.to_ray_options()
        
        assert ray_options["num_cpus"] == 4
        assert ray_options["num_gpus"] == 1
        assert ray_options["memory"] == 8 * 1024**3
        assert ray_options["tpu"] == 1
    
    def test_memory_parsing(self):
        """Test memory string parsing."""
        config = NodeResourceConfig()
        
        assert config._parse_memory("1GB") == 1024**3
        assert config._parse_memory("512MB") == 512 * 1024**2
        assert config._parse_memory("2TB") == 2 * 1024**4
        assert config._parse_memory("100KB") == 100 * 1024
        assert config._parse_memory("invalid") is None


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: Node Variant Registration
# ═══════════════════════════════════════════════════════════════════════════════

class TestNodeVariantRegistration:
    """Test node-level variant registration."""
    
    def test_register_single_variant(self, basic_project, sample_node_implementations):
        """Test registering a single node variant."""
        basic_project.register_variant(
            node="retriever",
            name="dense",
            implementation=sample_node_implementations["dense_retriever"],
            description="Dense embedding retriever",
        )
        
        variants = basic_project.list_variants()
        assert "retriever" in variants
        assert "dense" in variants["retriever"]
    
    def test_register_multiple_variants_same_node(
        self,
        basic_project,
        sample_node_implementations,
    ):
        """Test registering multiple variants for the same node."""
        basic_project.register_variant(
            node="retriever",
            name="dense",
            implementation=sample_node_implementations["dense_retriever"],
        )
        basic_project.register_variant(
            node="retriever",
            name="bm25",
            implementation=sample_node_implementations["bm25_retriever"],
        )
        basic_project.register_variant(
            node="retriever",
            name="hybrid",
            implementation=sample_node_implementations["hybrid_retriever"],
        )
        
        variants = basic_project.list_variants(node="retriever")
        assert len(variants["retriever"]) == 3
        assert set(variants["retriever"]) == {"dense", "bm25", "hybrid"}
    
    def test_register_variant_with_environment(
        self,
        basic_project,
        sample_node_implementations,
    ):
        """Test registering variant with specific environment."""
        basic_project.environment.granularity = "variant"
        
        custom_env = EnvSpec(
            python_version="3.11",
            dependencies=["faiss-cpu==1.7.0"],
        )
        
        basic_project.register_variant(
            node="retriever",
            name="dense_v1",
            implementation=sample_node_implementations["dense_retriever"],
            environment=custom_env,
        )
        
        # Variant environment should be auto-registered
        assert "retriever:dense_v1" in basic_project.environment.variant_environments
    
    def test_register_variant_with_resources(
        self,
        basic_project,
        sample_node_implementations,
    ):
        """Test registering variant with resource allocation."""
        resources = NodeResourceConfig(
            num_cpus=4,
            num_gpus=1,
            memory="8GB",
        )
        
        basic_project.register_variant(
            node="retriever",
            name="gpu_variant",
            implementation=sample_node_implementations["dense_retriever"],
            resources=resources,
        )
        
        variant = basic_project.get_variant("retriever", "gpu_variant")
        assert variant.resources.num_gpus == 1
    
    def test_get_variant(self, configured_project):
        """Test retrieving a specific variant."""
        variant = configured_project.get_variant("retriever", "dense")
        
        assert variant is not None
        assert isinstance(variant, NodeVariant)
        assert variant.name == "dense"
        assert variant.node_id == "retriever"
        assert variant.description == "Dense embedding retriever"
    
    def test_get_nonexistent_variant(self, configured_project):
        """Test getting non-existent variant returns None."""
        variant = configured_project.get_variant("retriever", "nonexistent")
        assert variant is None
    
    def test_remove_variant(self, configured_project):
        """Test removing a registered variant."""
        assert configured_project.remove_variant("retriever", "dense") is True
        assert configured_project.get_variant("retriever", "dense") is None
        assert configured_project.remove_variant("retriever", "dense") is False
    
    def test_list_variants_filter_by_node(self, configured_project):
        """Test listing variants filtered by node."""
        retriever_variants = configured_project.list_variants(node="retriever")
        generator_variants = configured_project.list_variants(node="generator")
        
        assert "retriever" in retriever_variants
        assert "generator" not in retriever_variants
        assert "generator" in generator_variants


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: Workflow Variant Registration
# ═══════════════════════════════════════════════════════════════════════════════

class TestWorkflowVariantRegistration:
    """Test workflow-level variant registration."""
    
    def test_register_workflow_variant(self, basic_project, mock_graph_factory):
        """Test registering a workflow variant."""
        basic_project.register_workflow_variant(
            name="with_reranker",
            graph_factory=mock_graph_factory,
            description="RAG with reranking step",
        )
        
        variants = basic_project.list_workflow_variants()
        assert "with_reranker" in variants
    
    def test_register_multiple_workflow_variants(self, basic_project, mock_graph_factory):
        """Test registering multiple workflow variants."""
        basic_project.register_workflow_variant(
            name="simple",
            graph_factory=mock_graph_factory,
            description="Simple pipeline",
        )
        basic_project.register_workflow_variant(
            name="with_reranker",
            graph_factory=mock_graph_factory,
            description="With reranking",
        )
        basic_project.register_workflow_variant(
            name="with_query_expansion",
            graph_factory=mock_graph_factory,
            description="With query expansion",
        )
        
        variants = basic_project.list_workflow_variants()
        assert len(variants) == 3
    
    def test_get_workflow_variant(self, basic_project, mock_graph_factory):
        """Test retrieving a workflow variant."""
        basic_project.register_workflow_variant(
            name="advanced",
            graph_factory=mock_graph_factory,
            description="Advanced pipeline",
        )
        
        variant = basic_project.get_workflow_variant("advanced")
        assert variant is not None
        assert isinstance(variant, WorkflowVariant)
        assert variant.description == "Advanced pipeline"
    
    def test_remove_workflow_variant(self, basic_project, mock_graph_factory):
        """Test removing a workflow variant."""
        basic_project.register_workflow_variant(
            name="temp",
            graph_factory=mock_graph_factory,
        )
        
        assert basic_project.remove_workflow_variant("temp") is True
        assert basic_project.get_workflow_variant("temp") is None


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: WTBTestBench Execution
# ═══════════════════════════════════════════════════════════════════════════════

class TestWTBTestBenchExecution:
    """Test WTBTestBench execution capabilities."""
    
    def test_register_and_run_project(self, configured_project):
        """Test registering project and running execution."""
        wtb = WTBTestBench.create(mode="testing")
        wtb.register_project(configured_project)
        
        result = wtb.run(
            project="rag_pipeline",
            initial_state={"query": "What is LangGraph?"},
        )
        
        # run() returns domain Execution model
        assert isinstance(result, Execution)
        assert result.id is not None
    
    def test_execution_result_status(self, configured_project):
        """Test execution result status."""
        wtb = WTBTestBench.create(mode="testing")
        wtb.register_project(configured_project)
        
        result = wtb.run(
            project="rag_pipeline",
            initial_state={"query": "test"},
        )
        
        # Execution domain model uses ExecutionStatus enum
        from wtb.domain.models import ExecutionStatus
        assert result.status == ExecutionStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: Batch Testing
# ═══════════════════════════════════════════════════════════════════════════════

class TestBatchTesting:
    """Test batch test functionality."""
    
    def test_run_batch_test_basic(self, configured_project):
        """Test basic batch test execution."""
        wtb = WTBTestBench.create(mode="testing")
        wtb.register_project(configured_project)
        
        result = wtb.run_batch_test(
            project="rag_pipeline",
            variant_matrix=[
                {"retriever": "dense"},
                {"retriever": "bm25"},
            ],
            test_cases=[
                {"query": "test query 1"},
                {"query": "test query 2"},
            ],
        )
        
        # run_batch_test returns BatchTest domain model (aggregate)
        from wtb.domain.models import BatchTest, BatchTestStatus
        assert isinstance(result, BatchTest)
        assert result.status == BatchTestStatus.COMPLETED
    
    def test_batch_test_variant_results(self, configured_project):
        """Test batch test returns variant results."""
        wtb = WTBTestBench.create(mode="testing")
        wtb.register_project(configured_project)
        
        result = wtb.run_batch_test(
            project="rag_pipeline",
            variant_matrix=[
                {"retriever": "dense"},
                {"retriever": "bm25"},
            ],
            test_cases=[{"query": "test"}],
        )
        
        # BatchTest.results contains BatchTestResult value objects
        assert len(result.results) > 0
        for variant_result in result.results:
            assert isinstance(variant_result, BatchTestResult)
    
    def test_batch_test_best_variant_selection(self, configured_project):
        """Test batch test selects best variant."""
        wtb = WTBTestBench.create(mode="testing")
        wtb.register_project(configured_project)
        
        result = wtb.run_batch_test(
            project="rag_pipeline",
            variant_matrix=[
                {"retriever": "dense"},
                {"retriever": "bm25"},
            ],
            test_cases=[{"query": "test"}],
        )
        
        # BatchTest has best_combination_name
        assert result.best_combination_name is not None
    
    def test_batch_test_timing(self, configured_project):
        """Test batch test tracks timing."""
        wtb = WTBTestBench.create(mode="testing")
        wtb.register_project(configured_project)
        
        result = wtb.run_batch_test(
            project="rag_pipeline",
            variant_matrix=[{"retriever": "dense"}],
            test_cases=[{"query": "test"}],
        )
        
        assert result.started_at is not None
        assert result.completed_at is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: Project Serialization
# ═══════════════════════════════════════════════════════════════════════════════

class TestProjectSerialization:
    """Test WorkflowProject serialization."""
    
    def test_to_dict(self, configured_project):
        """Test project serialization to dict."""
        data = configured_project.to_dict()
        
        assert data["name"] == "rag_pipeline"
        assert data["id"] is not None
        assert data["version"] == 1
        assert "created_at" in data
        assert "file_tracking" in data
        assert "environment" in data
        assert "execution" in data
        assert "node_variants" in data
        assert "workflow_variants" in data
    
    def test_to_dict_includes_variants(self, configured_project):
        """Test serialization includes registered variants."""
        data = configured_project.to_dict()
        
        assert "retriever" in data["node_variants"]
        assert "dense" in data["node_variants"]["retriever"]
        assert "bm25" in data["node_variants"]["retriever"]


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: Error Handling
# ═══════════════════════════════════════════════════════════════════════════════

class TestErrorHandling:
    """Test error handling in workflow submission."""
    
    def test_register_duplicate_project(self, configured_project):
        """Test that registering duplicate project raises error."""
        wtb = WTBTestBench.create(mode="testing")
        wtb.register_project(configured_project)
        
        with pytest.raises(ValueError, match="already registered"):
            wtb.register_project(configured_project)
    
    def test_get_nonexistent_project(self):
        """Test getting non-existent project raises error."""
        wtb = WTBTestBench.create(mode="testing")
        
        with pytest.raises(KeyError, match="not found"):
            wtb.get_project("nonexistent")
    
    def test_run_nonexistent_project(self):
        """Test running non-existent project raises error."""
        wtb = WTBTestBench.create(mode="testing")
        
        with pytest.raises(KeyError):
            wtb.run(
                project="nonexistent",
                initial_state={"query": "test"},
            )
    
    def test_unknown_workflow_variant(self, basic_project):
        """Test using unknown workflow variant raises error."""
        with pytest.raises(ValueError, match="Unknown workflow variant"):
            basic_project.build_graph(workflow_variant="nonexistent")


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: Node Version Control
# ═══════════════════════════════════════════════════════════════════════════════

class TestNodeVersionControl:
    """Test node version control features."""
    
    def test_update_node_increments_version(self, basic_project, sample_node_implementations):
        """Test that update_node increments project version."""
        initial_version = basic_project.version
        
        basic_project.update_node(
            node="retriever",
            new_implementation=sample_node_implementations["dense_retriever"],
            reason="Improved performance",
        )
        
        assert basic_project.version == initial_version + 1
    
    def test_update_workflow_increments_version(self, basic_project, mock_graph_factory):
        """Test that update_workflow increments version."""
        initial_version = basic_project.version
        
        basic_project.update_workflow(
            graph_factory=mock_graph_factory,
            version="2.0",
            changelog="Added new node",
        )
        
        assert basic_project.version == initial_version + 1
    
    def test_variant_has_created_at(self, basic_project, sample_node_implementations):
        """Test that variants have created_at timestamp."""
        basic_project.register_variant(
            node="retriever",
            name="test",
            implementation=sample_node_implementations["dense_retriever"],
        )
        
        variant = basic_project.get_variant("retriever", "test")
        assert variant.created_at is not None
        assert isinstance(variant.created_at, datetime)


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: Execution Result Properties
# ═══════════════════════════════════════════════════════════════════════════════

class TestBatchTestResultProperties:
    """Test BatchTestResult domain dataclass properties."""
    
    def test_batch_test_result_creation(self):
        """Test creating BatchTestResult value object."""
        result = BatchTestResult(
            combination_name="variant-1",
            execution_id="exec-1",
            success=True,
            metrics={"accuracy": 0.95},
            overall_score=0.9,
            duration_ms=1500,
        )
        assert result.success is True
        assert result.combination_name == "variant-1"
    
    def test_batch_test_result_failed(self):
        """Test BatchTestResult with failure."""
        result = BatchTestResult(
            combination_name="variant-1",
            execution_id="exec-1",
            success=False,
            error_message="Node failed",
        )
        assert result.success is False
        assert result.error_message == "Node failed"
