"""
Unit Tests for LangGraphNodeReplacer.

Tests node variant management and graph modification capabilities
without requiring LangGraph installation.
"""

import pytest
from typing import Dict, Any
from datetime import datetime


# These tests don't require LangGraph - they test the pure Python logic
# Only skip if the module itself fails to import
_IMPORT_FAILED = False

# Import module
try:
    from wtb.application.services.langgraph_node_replacer import (
        LangGraphNodeReplacer,
        LangGraphNodeVariant,
        GraphStructure,
        GraphNodeInfo,
        GraphEdgeInfo,
        VariantSet,
        LANGGRAPH_AVAILABLE,
    )
except ImportError:
    _IMPORT_FAILED = True
    LANGGRAPH_AVAILABLE = False
    LangGraphNodeReplacer = None
    LangGraphNodeVariant = None
    GraphStructure = None
    GraphNodeInfo = None
    GraphEdgeInfo = None
    VariantSet = None

# Skip all tests if import failed
pytestmark = pytest.mark.skipif(
    _IMPORT_FAILED,
    reason="LangGraphNodeReplacer module failed to import"
)


class TestLangGraphNodeVariant:
    """Tests for LangGraphNodeVariant dataclass."""
    
    def test_variant_creation(self):
        """Test creating a node variant."""
        def sample_node(state):
            return {"result": "sample"}
        
        variant = LangGraphNodeVariant(
            node_id="process",
            variant_name="optimized",
            node_fn=sample_node,
            description="Optimized processing",
            metadata={"version": "2.0"},
        )
        
        assert variant.node_id == "process"
        assert variant.variant_name == "optimized"
        assert variant.node_fn is sample_node
        assert variant.description == "Optimized processing"
        assert variant.metadata == {"version": "2.0"}
        assert variant.is_active is True
        assert isinstance(variant.created_at, datetime)
    
    def test_variant_serialization(self):
        """Test serializing variant to dict (excluding function)."""
        variant = LangGraphNodeVariant(
            node_id="agent",
            variant_name="gpt4",
            description="GPT-4 based agent",
        )
        
        data = variant.to_dict()
        
        assert data["node_id"] == "agent"
        assert data["variant_name"] == "gpt4"
        assert data["description"] == "GPT-4 based agent"
        assert data["is_active"] is True
        assert "node_fn" not in data  # Functions not serialized
    
    def test_variant_deserialization(self):
        """Test deserializing variant from dict."""
        def restored_fn(state):
            return state
        
        data = {
            "id": "test-id",
            "node_id": "processor",
            "variant_name": "v2",
            "description": "Version 2",
            "metadata": {"key": "value"},
            "is_active": True,
            "created_at": datetime.now().isoformat(),
        }
        
        variant = LangGraphNodeVariant.from_dict(data, node_fn=restored_fn)
        
        assert variant.id == "test-id"
        assert variant.node_id == "processor"
        assert variant.variant_name == "v2"
        assert variant.node_fn is restored_fn


class TestVariantSet:
    """Tests for VariantSet dataclass."""
    
    def test_variant_set_creation(self):
        """Test creating a variant set."""
        vs = VariantSet(
            name="combo_a",
            variants={
                "agent": "gpt4",
                "processor": "optimized",
            },
            metadata={"test_run": 1},
        )
        
        assert vs.name == "combo_a"
        assert vs.variants["agent"] == "gpt4"
        assert vs.variants["processor"] == "optimized"
        assert vs.metadata["test_run"] == 1
    
    def test_variant_set_serialization(self):
        """Test serializing variant set."""
        vs = VariantSet(
            name="test_combo",
            variants={"node1": "v1", "node2": "v2"},
        )
        
        data = vs.to_dict()
        
        assert data["name"] == "test_combo"
        assert data["variants"] == {"node1": "v1", "node2": "v2"}


@pytest.mark.skipif(
    not LangGraphNodeReplacer,
    reason="LangGraphNodeReplacer not available"
)
class TestLangGraphNodeReplacer:
    """Tests for LangGraphNodeReplacer variant management."""
    
    def test_register_variant(self):
        """Test registering a node variant."""
        replacer = LangGraphNodeReplacer()
        
        def my_node(state):
            return {"processed": True}
        
        variant = replacer.register_variant(
            node_id="processor",
            variant_name="v1",
            node_fn=my_node,
            description="Version 1 processor",
        )
        
        assert variant.node_id == "processor"
        assert variant.variant_name == "v1"
        
        # Verify it's retrievable
        retrieved = replacer.get_variant("processor", "v1")
        assert retrieved is not None
        assert retrieved.node_fn is my_node
    
    def test_register_duplicate_variant_raises(self):
        """Test that registering duplicate variant raises error."""
        replacer = LangGraphNodeReplacer()
        
        def node_v1(state):
            return state
        
        replacer.register_variant("node", "v1", node_v1)
        
        with pytest.raises(ValueError, match="already exists"):
            replacer.register_variant("node", "v1", node_v1)
    
    def test_get_variants_for_node(self):
        """Test getting all variants for a node."""
        replacer = LangGraphNodeReplacer()
        
        def node_v1(state):
            return state
        
        def node_v2(state):
            return {"enhanced": True}
        
        replacer.register_variant("agent", "v1", node_v1)
        replacer.register_variant("agent", "v2", node_v2)
        replacer.register_variant("other", "v1", node_v1)
        
        variants = replacer.get_variants("agent")
        
        assert len(variants) == 2
        names = {v.variant_name for v in variants}
        assert names == {"v1", "v2"}
    
    def test_deactivate_variant(self):
        """Test deactivating a variant."""
        replacer = LangGraphNodeReplacer()
        
        def my_node(state):
            return state
        
        replacer.register_variant("node", "v1", my_node)
        
        # Initially active
        variant = replacer.get_variant("node", "v1")
        assert variant.is_active is True
        
        # Deactivate
        result = replacer.deactivate_variant("node", "v1")
        assert result is True
        
        # Verify deactivated
        variant = replacer.get_variant("node", "v1")
        assert variant.is_active is False
    
    def test_register_original(self):
        """Test registering original node function."""
        replacer = LangGraphNodeReplacer()
        
        def original_node(state):
            return {"original": True}
        
        replacer.register_original("processor", original_node)
        
        # Should be retrievable as "original" variant
        variant = replacer.get_variant("processor", "original")
        assert variant is not None
        assert variant.variant_name == "original"
        
        # Should also be in original cache
        orig = replacer.get_original_node("processor")
        assert orig is original_node
    
    def test_generate_variant_combinations(self):
        """Test generating all variant combinations."""
        replacer = LangGraphNodeReplacer()
        
        # Register variants for two nodes
        def fn():
            pass
        
        replacer.register_variant("node1", "a", fn)
        replacer.register_variant("node1", "b", fn)
        replacer.register_variant("node2", "x", fn)
        replacer.register_variant("node2", "y", fn)
        
        combinations = replacer.generate_variant_combinations()
        
        # Should have 2 * 2 = 4 combinations
        assert len(combinations) == 4
        
        # Verify all combinations
        combo_sets = [frozenset(c.variants.items()) for c in combinations]
        expected = [
            frozenset([("node1", "a"), ("node2", "x")]),
            frozenset([("node1", "a"), ("node2", "y")]),
            frozenset([("node1", "b"), ("node2", "x")]),
            frozenset([("node1", "b"), ("node2", "y")]),
        ]
        for exp in expected:
            assert exp in combo_sets
    
    def test_generate_combinations_with_filter(self):
        """Test generating combinations for specific nodes only."""
        replacer = LangGraphNodeReplacer()
        
        def fn():
            pass
        
        replacer.register_variant("node1", "a", fn)
        replacer.register_variant("node1", "b", fn)
        replacer.register_variant("node2", "x", fn)
        replacer.register_variant("node2", "y", fn)
        replacer.register_variant("node3", "p", fn)
        
        # Only vary node1 and node2
        combinations = replacer.generate_variant_combinations(nodes=["node1", "node2"])
        
        assert len(combinations) == 4
        
        # node3 should not appear in any combination
        for combo in combinations:
            assert "node3" not in combo.variants
    
    def test_clear_variants(self):
        """Test clearing registered variants."""
        replacer = LangGraphNodeReplacer()
        
        def fn():
            pass
        
        replacer.register_variant("node1", "v1", fn)
        replacer.register_variant("node2", "v1", fn)
        
        # Clear specific node
        replacer.clear_variants("node1")
        assert len(replacer.get_variants("node1")) == 0
        assert len(replacer.get_variants("node2")) == 1
        
        # Clear all
        replacer.clear_variants()
        assert len(replacer.get_all_variants()) == 0
    
    def test_export_variant_registry(self):
        """Test exporting variant registry."""
        replacer = LangGraphNodeReplacer()
        
        def fn():
            pass
        
        replacer.register_variant("agent", "gpt4", fn, description="GPT-4 agent")
        replacer.register_variant("agent", "claude", fn, description="Claude agent")
        
        export = replacer.export_variant_registry()
        
        assert "agent" in export
        assert "gpt4" in export["agent"]
        assert "claude" in export["agent"]
        assert export["agent"]["gpt4"]["description"] == "GPT-4 agent"


class TestGraphStructure:
    """Tests for GraphStructure (without LangGraph build)."""
    
    def test_add_node(self):
        """Test adding nodes to graph structure."""
        structure = GraphStructure(state_schema=dict)
        
        def my_node(state):
            return state
        
        structure.add_node("process", my_node, custom_key="value")
        
        assert "process" in structure.nodes
        assert structure.nodes["process"].node_fn is my_node
        assert structure.nodes["process"].metadata["custom_key"] == "value"
    
    def test_add_edge(self):
        """Test adding edges to graph structure."""
        structure = GraphStructure(state_schema=dict)
        
        structure.add_edge("start", "process")
        structure.add_edge("process", "end")
        
        assert len(structure.edges) == 2
        assert structure.edges[0].source == "start"
        assert structure.edges[0].target == "process"
    
    def test_set_entry_point(self):
        """Test setting entry point."""
        structure = GraphStructure(state_schema=dict)
        
        structure.set_entry_point("start")
        
        assert structure.entry_point == "start"


class TestGraphNodeInfo:
    """Tests for GraphNodeInfo dataclass."""
    
    def test_node_info_creation(self):
        """Test creating node info."""
        def sample_fn(state):
            return state
        
        info = GraphNodeInfo(
            node_id="processor",
            node_fn=sample_fn,
            node_type="function",
            metadata={"version": 1},
        )
        
        assert info.node_id == "processor"
        assert info.node_fn is sample_fn
        assert info.node_type == "function"
        assert info.metadata["version"] == 1


class TestGraphEdgeInfo:
    """Tests for GraphEdgeInfo dataclass."""
    
    def test_edge_info_creation(self):
        """Test creating edge info."""
        edge = GraphEdgeInfo(
            source="start",
            target="process",
        )
        
        assert edge.source == "start"
        assert edge.target == "process"
        assert edge.condition is None
        assert edge.is_conditional is False
    
    def test_conditional_edge_info(self):
        """Test creating conditional edge info."""
        def my_condition(state):
            return "yes" if state.get("valid") else "no"
        
        edge = GraphEdgeInfo(
            source="decision",
            target="yes_branch",
            condition=my_condition,
            is_conditional=True,
        )
        
        assert edge.source == "decision"
        assert edge.target == "yes_branch"
        assert edge.condition is my_condition
        assert edge.is_conditional is True
