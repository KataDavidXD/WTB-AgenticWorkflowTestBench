"""
Unit tests for NodeReplacer.

Tests node variant management and hot-swapping functionality.
"""

import pytest
from typing import Optional, List, Dict, Any

from wtb.domain.models import (
    NodeVariant,
    TestWorkflow,
    WorkflowNode,
    WorkflowEdge,
)
from wtb.domain.interfaces.repositories import INodeVariantRepository
from wtb.application.services.node_replacer import NodeReplacer


# ═══════════════════════════════════════════════════════════════════════════════
# Mock Repository
# ═══════════════════════════════════════════════════════════════════════════════

class MockNodeVariantRepository(INodeVariantRepository):
    """In-memory node variant repository for testing."""
    
    def __init__(self):
        self._storage: Dict[str, NodeVariant] = {}
    
    def get(self, id: str) -> Optional[NodeVariant]:
        return self._storage.get(id)
    
    def list(self, limit: int = 100, offset: int = 0) -> List[NodeVariant]:
        items = list(self._storage.values())
        return items[offset:offset + limit]
    
    def add(self, entity: NodeVariant) -> NodeVariant:
        self._storage[entity.id] = entity
        return entity
    
    def update(self, entity: NodeVariant) -> NodeVariant:
        self._storage[entity.id] = entity
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._storage:
            del self._storage[id]
            return True
        return False
    
    def exists(self, id: str) -> bool:
        return id in self._storage
    
    def find_by_workflow(self, workflow_id: str) -> List[NodeVariant]:
        return [v for v in self._storage.values() if v.workflow_id == workflow_id]
    
    def find_by_node(self, workflow_id: str, node_id: str) -> List[NodeVariant]:
        return [
            v for v in self._storage.values()
            if v.workflow_id == workflow_id and v.original_node_id == node_id
        ]
    
    def find_active(self, workflow_id: str) -> List[NodeVariant]:
        return [
            v for v in self._storage.values()
            if v.workflow_id == workflow_id and v.is_active
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════

def create_test_workflow() -> TestWorkflow:
    """Create a test workflow with multiple nodes."""
    workflow = TestWorkflow(name="Test Workflow")
    
    start = WorkflowNode(id="start", name="Start", type="start")
    process = WorkflowNode(
        id="process",
        name="Process Data",
        type="action",
        tool_name="process_tool",
        config={"mode": "slow", "option": "A"}
    )
    end = WorkflowNode(id="end", name="End", type="end")
    
    workflow.add_node(start)
    workflow.add_node(process)
    workflow.add_node(end)
    
    workflow.add_edge(WorkflowEdge(source_id="start", target_id="process"))
    workflow.add_edge(WorkflowEdge(source_id="process", target_id="end"))
    
    return workflow


def create_variant(
    workflow_id: str,
    original_node_id: str,
    variant_name: str,
    **config
) -> NodeVariant:
    """Create a node variant for testing."""
    variant_node = WorkflowNode(
        id=f"{original_node_id}_variant_{variant_name}",
        name=f"{variant_name} Version",
        type="action",
        tool_name="variant_tool",
        config=config
    )
    
    return NodeVariant(
        workflow_id=workflow_id,
        original_node_id=original_node_id,
        variant_name=variant_name,
        variant_node=variant_node,
        description=f"Variant: {variant_name}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# NodeReplacer Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestNodeReplacer:
    """Tests for NodeReplacer."""
    
    @pytest.fixture
    def replacer(self):
        """Create replacer with mock repository."""
        repo = MockNodeVariantRepository()
        return NodeReplacer(variant_repository=repo), repo
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Variant Registry Tests
    # ═══════════════════════════════════════════════════════════════════════════
    
    def test_register_variant(self, replacer):
        """Test registering a new variant."""
        rep, repo = replacer
        
        workflow = create_test_workflow()
        variant = create_variant(workflow.id, "process", "fast", mode="fast")
        
        registered = rep.register(variant)
        
        assert registered.id == variant.id
        assert repo.exists(variant.id)
    
    def test_register_variant_without_node_id_fails(self, replacer):
        """Test that variant without original_node_id fails."""
        rep, _ = replacer
        
        variant = NodeVariant(
            workflow_id="wf1",
            original_node_id="",  # Missing
            variant_name="test",
            variant_node=WorkflowNode(id="v", name="V", type="action")
        )
        
        with pytest.raises(ValueError, match="original_node_id"):
            rep.register(variant)
    
    def test_register_variant_without_name_fails(self, replacer):
        """Test that variant without name fails."""
        rep, _ = replacer
        
        variant = NodeVariant(
            workflow_id="wf1",
            original_node_id="node1",
            variant_name="",  # Missing
            variant_node=WorkflowNode(id="v", name="V", type="action")
        )
        
        with pytest.raises(ValueError, match="name"):
            rep.register(variant)
    
    def test_register_duplicate_variant_fails(self, replacer):
        """Test that duplicate variant names fail."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        variant1 = create_variant(workflow.id, "process", "fast")
        variant2 = create_variant(workflow.id, "process", "fast")  # Same name
        
        rep.register(variant1)
        
        with pytest.raises(ValueError, match="already exists"):
            rep.register(variant2)
    
    def test_get_variant(self, replacer):
        """Test getting a variant by ID."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        variant = create_variant(workflow.id, "process", "fast")
        rep.register(variant)
        
        retrieved = rep.get(variant.id)
        
        assert retrieved is not None
        assert retrieved.variant_name == "fast"
    
    def test_get_by_node(self, replacer):
        """Test getting variants for a specific node."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        variant1 = create_variant(workflow.id, "process", "fast")
        variant2 = create_variant(workflow.id, "process", "slow")
        variant3 = create_variant(workflow.id, "start", "alt_start")
        
        rep.register(variant1)
        rep.register(variant2)
        rep.register(variant3)
        
        process_variants = rep.get_by_node(workflow.id, "process")
        
        assert len(process_variants) == 2
    
    def test_list_for_workflow(self, replacer):
        """Test listing all variants for a workflow."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        variant1 = create_variant(workflow.id, "process", "fast")
        variant2 = create_variant(workflow.id, "start", "alt")
        variant3 = create_variant("other_workflow", "node", "v")
        
        rep.register(variant1)
        rep.register(variant2)
        rep.register(variant3)
        
        variants = rep.list_for_workflow(workflow.id)
        
        assert len(variants) == 2
    
    def test_deactivate_variant(self, replacer):
        """Test deactivating a variant."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        variant = create_variant(workflow.id, "process", "fast")
        rep.register(variant)
        
        assert variant.is_active
        
        result = rep.deactivate(variant.id)
        
        assert result is True
        
        updated = rep.get(variant.id)
        assert not updated.is_active
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Node Swapping Tests
    # ═══════════════════════════════════════════════════════════════════════════
    
    def test_swap_node(self, replacer):
        """Test swapping a node with a variant."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        variant = create_variant(workflow.id, "process", "fast", mode="fast")
        
        original_node = workflow.nodes["process"]
        
        modified = rep.swap_node(workflow, "process", variant)
        
        # Original workflow unchanged
        assert workflow.nodes["process"] == original_node
        
        # Modified workflow has variant
        assert modified.nodes["process"].id == "process"  # ID preserved
        assert modified.nodes["process"].config.get("mode") == "fast"
    
    def test_swap_preserves_edges(self, replacer):
        """Test that swapping preserves edge connections."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        variant = create_variant(workflow.id, "process", "fast")
        
        modified = rep.swap_node(workflow, "process", variant)
        
        # Check edges still point to the node
        outgoing = modified.get_outgoing_edges("process")
        incoming = modified.get_incoming_edges("process")
        
        assert len(outgoing) == 1
        assert len(incoming) == 1
        assert outgoing[0].target_id == "end"
    
    def test_swap_nonexistent_node_fails(self, replacer):
        """Test that swapping nonexistent node fails."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        variant = create_variant(workflow.id, "missing", "v")
        
        with pytest.raises(ValueError, match="not found"):
            rep.swap_node(workflow, "missing", variant)
    
    def test_restore_original(self, replacer):
        """Test restoring original node after swap."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        variant = create_variant(workflow.id, "process", "fast")
        
        original_config = dict(workflow.nodes["process"].config)
        
        modified = rep.swap_node(workflow, "process", variant)
        restored = rep.restore_original(modified, "process")
        
        assert restored.nodes["process"].config == original_config
    
    def test_restore_without_cache_fails(self, replacer):
        """Test that restoring without cached original fails."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        
        with pytest.raises(ValueError, match="No cached originals"):
            rep.restore_original(workflow, "process")
    
    def test_apply_variant_set(self, replacer):
        """Test applying multiple variants at once."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        
        variant1 = create_variant(workflow.id, "process", "fast", mode="fast")
        variant2 = create_variant(workflow.id, "start", "alt", alt=True)
        
        rep.register(variant1)
        rep.register(variant2)
        
        modified = rep.apply_variant_set(workflow, {
            "process": variant1.id,
            "start": variant2.id,
        })
        
        assert modified.nodes["process"].config.get("mode") == "fast"
        assert modified.nodes["start"].config.get("alt") is True
    
    def test_apply_variant_set_invalid_variant(self, replacer):
        """Test that applying invalid variant ID fails."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        
        with pytest.raises(ValueError, match="not found"):
            rep.apply_variant_set(workflow, {"process": "invalid_id"})
    
    def test_apply_variant_set_wrong_node(self, replacer):
        """Test that applying variant to wrong node fails."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        variant = create_variant(workflow.id, "process", "fast")
        rep.register(variant)
        
        with pytest.raises(ValueError, match="is for node"):
            rep.apply_variant_set(workflow, {"start": variant.id})
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Utility Method Tests
    # ═══════════════════════════════════════════════════════════════════════════
    
    def test_create_variant_from_node(self, replacer):
        """Test creating a variant from existing node with modifications."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        
        variant = rep.create_variant_from_node(
            workflow,
            "process",
            "optimized",
            modifications={"mode": "turbo", "extra": True}
        )
        
        assert variant.variant_name == "optimized"
        assert variant.variant_node.config["mode"] == "turbo"
        assert variant.variant_node.config["extra"] is True
        # Original config values are preserved
        assert variant.variant_node.config.get("option") == "A"
    
    def test_create_variant_from_nonexistent_node_fails(self, replacer):
        """Test creating variant from nonexistent node fails."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        
        with pytest.raises(ValueError, match="not found"):
            rep.create_variant_from_node(workflow, "missing", "v", {})
    
    def test_clear_cache(self, replacer):
        """Test clearing the original node cache."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        variant = create_variant(workflow.id, "process", "fast")
        
        rep.swap_node(workflow, "process", variant)
        
        assert len(rep.get_cached_originals(workflow.id)) == 1
        
        rep.clear_cache(workflow.id)
        
        assert len(rep.get_cached_originals(workflow.id)) == 0
    
    def test_clear_all_cache(self, replacer):
        """Test clearing all caches."""
        rep, _ = replacer
        
        workflow1 = create_test_workflow()
        workflow2 = TestWorkflow(name="WF2")
        workflow2.add_node(WorkflowNode(id="n", name="N", type="action"))
        
        variant1 = create_variant(workflow1.id, "process", "v1")
        variant2 = NodeVariant(
            workflow_id=workflow2.id,
            original_node_id="n",
            variant_name="v2",
            variant_node=WorkflowNode(id="x", name="X", type="action")
        )
        
        rep.swap_node(workflow1, "process", variant1)
        rep.swap_node(workflow2, "n", variant2)
        
        rep.clear_cache()  # Clear all
        
        assert len(rep.get_cached_originals(workflow1.id)) == 0
        assert len(rep.get_cached_originals(workflow2.id)) == 0
    
    def test_get_cached_originals(self, replacer):
        """Test getting cached original nodes."""
        rep, _ = replacer
        
        workflow = create_test_workflow()
        variant = create_variant(workflow.id, "process", "fast")
        
        original = workflow.nodes["process"]
        
        rep.swap_node(workflow, "process", variant)
        
        cached = rep.get_cached_originals(workflow.id)
        
        assert "process" in cached
        assert cached["process"] == original

