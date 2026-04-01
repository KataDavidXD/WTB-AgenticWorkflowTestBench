"""
LangGraph Node Replacer - Node variant management for LangGraph graphs.

Extends the NodeReplacer concept for LangGraph StateGraph manipulation,
enabling A/B testing and variant-based batch testing with LangGraph workflows.

Design Principles:
- SOLID: SRP (variant management), OCP (extensible via variant registry)
- Composition over inheritance: Works alongside existing NodeReplacer
- Immutable modifications: Creates new graphs, doesn't mutate originals

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                    LangGraphNodeReplacer                                 │
    │                                                                          │
    │  ┌───────────────────────────────────────────────────────────────────┐  │
    │  │ ILangGraphVariantRegistry                                          │  │
    │  │                                                                    │  │
    │  │  register_variant(node_id, variant_name, node_fn)                  │  │
    │  │  get_variants(node_id) -> List[LangGraphNodeVariant]               │  │
    │  │  create_variant_graph(base_graph, variant_set) -> StateGraph       │  │
    │  └───────────────────────────────────────────────────────────────────┘  │
    │                                                                          │
    │  Variant Application Flow:                                               │
    │  ═══════════════════════════                                            │
    │                                                                          │
    │  Original Graph          Variant Set           Modified Graph            │
    │  ┌────────────┐         ┌───────────┐         ┌────────────┐            │
    │  │ node_a     │ ──────► │ node_a:v2 │ ──────► │ node_a_v2  │            │
    │  │ node_b     │         │ node_b:v1 │         │ node_b_v1  │            │
    │  │ node_c     │         │           │         │ node_c     │            │
    │  └────────────┘         └───────────┘         └────────────┘            │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘

Usage:
    from wtb.application.services.langgraph_node_replacer import (
        LangGraphNodeReplacer,
        LangGraphNodeVariant,
    )
    
    # Create replacer
    replacer = LangGraphNodeReplacer()
    
    # Register variants
    replacer.register_variant(
        node_id="process_data",
        variant_name="optimized",
        node_fn=optimized_process_data,
        description="Optimized data processing with caching",
    )
    
    # Create variant graph
    variant_graph = replacer.create_variant_graph(
        base_graph=original_graph,
        variant_set={"process_data": "optimized"},
    )
"""

from typing import Dict, Any, List, Optional, Callable, Type, TypeVar
from dataclasses import dataclass, field
from datetime import datetime
from copy import deepcopy
import logging
import uuid

logger = logging.getLogger(__name__)

# Type variable for state
S = TypeVar('S', bound=Dict[str, Any])

# LangGraph availability check
LANGGRAPH_AVAILABLE = False
try:
    from langgraph.graph import StateGraph
    from langgraph.graph.state import CompiledStateGraph
    LANGGRAPH_AVAILABLE = True
except ImportError:
    StateGraph = None  # type: ignore
    CompiledStateGraph = None  # type: ignore


# ═══════════════════════════════════════════════════════════════════════════════
# Domain Models
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class LangGraphNodeVariant:
    """
    Represents a node variant for LangGraph graphs.
    
    A variant is an alternative implementation of a node that can be
    swapped in for A/B testing or batch testing.
    
    Attributes:
        id: Unique variant ID
        node_id: Original node ID this variant replaces
        variant_name: Human-readable variant name
        node_fn: The node function implementation
        description: Description of what this variant does differently
        metadata: Additional metadata (model params, configs, etc.)
        is_active: Whether this variant is available for use
        created_at: When this variant was registered
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    node_id: str = ""
    variant_name: str = ""
    node_fn: Optional[Callable] = None
    description: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary (excluding node_fn)."""
        return {
            "id": self.id,
            "node_id": self.node_id,
            "variant_name": self.variant_name,
            "description": self.description,
            "metadata": self.metadata,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat(),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any], node_fn: Callable = None) -> "LangGraphNodeVariant":
        """Deserialize from dictionary."""
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            node_id=data.get("node_id", ""),
            variant_name=data.get("variant_name", ""),
            node_fn=node_fn,
            description=data.get("description", ""),
            metadata=data.get("metadata", {}),
            is_active=data.get("is_active", True),
            created_at=datetime.fromisoformat(data["created_at"]) if "created_at" in data else datetime.now(),
        )


@dataclass
class VariantSet:
    """
    A set of variants to apply to a graph.
    
    Maps node_id -> variant_name for batch testing.
    """
    name: str
    variants: Dict[str, str] = field(default_factory=dict)  # node_id -> variant_name
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "variants": self.variants,
            "metadata": self.metadata,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Graph Builder Helper
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class GraphNodeInfo:
    """Information about a node in a LangGraph graph."""
    node_id: str
    node_fn: Callable
    node_type: str  # "function", "subgraph", "tool"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEdgeInfo:
    """Information about an edge in a LangGraph graph."""
    source: str
    target: str
    condition: Optional[Callable] = None
    is_conditional: bool = False


class GraphStructure:
    """
    Captures the structure of a LangGraph StateGraph for modification.
    
    Since LangGraph doesn't expose graph internals easily, this class
    provides a way to capture and recreate graph structure.
    """
    
    def __init__(
        self,
        state_schema: Type,
        nodes: Dict[str, GraphNodeInfo] = None,
        edges: List[GraphEdgeInfo] = None,
        entry_point: str = "",
    ):
        self.state_schema = state_schema
        self.nodes = nodes or {}
        self.edges = edges or []
        self.entry_point = entry_point
    
    def add_node(self, node_id: str, node_fn: Callable, **metadata) -> None:
        """Add a node to the structure."""
        self.nodes[node_id] = GraphNodeInfo(
            node_id=node_id,
            node_fn=node_fn,
            node_type="function",
            metadata=metadata,
        )
    
    def add_edge(self, source: str, target: str) -> None:
        """Add an edge to the structure."""
        self.edges.append(GraphEdgeInfo(source=source, target=target))
    
    def add_conditional_edge(
        self,
        source: str,
        condition: Callable,
        targets: Dict[str, str]
    ) -> None:
        """Add conditional edges from source."""
        for condition_result, target in targets.items():
            self.edges.append(GraphEdgeInfo(
                source=source,
                target=target,
                condition=condition,
                is_conditional=True,
            ))
    
    def set_entry_point(self, node_id: str) -> None:
        """Set the entry point node."""
        self.entry_point = node_id
    
    def build(self) -> "StateGraph":
        """Build a StateGraph from this structure."""
        if not LANGGRAPH_AVAILABLE:
            raise ImportError("LangGraph not available")
        
        graph = StateGraph(self.state_schema)
        
        # Add nodes
        for node_id, node_info in self.nodes.items():
            graph.add_node(node_id, node_info.node_fn)
        
        # Add edges
        for edge in self.edges:
            if edge.is_conditional:
                raise NotImplementedError(
                    f"Conditional edge routing for source '{edge.source}' is not yet implemented. "
                    "Use simple edges or implement LangGraph add_conditional_edges."
                )
            graph.add_edge(edge.source, edge.target)
        
        # Set entry point
        if self.entry_point:
            graph.set_entry_point(self.entry_point)
        
        return graph


# ═══════════════════════════════════════════════════════════════════════════════
# LangGraph Node Replacer
# ═══════════════════════════════════════════════════════════════════════════════


class LangGraphNodeReplacer:
    """
    Manages node variants for LangGraph graphs.
    
    Key Features:
    - Register node variants with alternative implementations
    - Create variant graphs for A/B testing
    - Generate variant combinations for batch testing
    - Track original nodes for restoration
    
    SOLID Compliance:
    - SRP: Only manages node variants, doesn't execute
    - OCP: New variant types via extension
    - LSP: Works with any StateGraph
    - ISP: Focused interface for variant management
    - DIP: Doesn't depend on specific graph implementations
    
    Usage:
        replacer = LangGraphNodeReplacer()
        
        # Register variant
        replacer.register_variant(
            node_id="agent",
            variant_name="gpt4",
            node_fn=gpt4_agent_node,
        )
        
        # Create variant graph
        variant_graph = replacer.create_variant_graph(
            base_graph_structure=structure,
            variant_set={"agent": "gpt4"},
        )
    """
    
    def __init__(self):
        """Initialize the node replacer."""
        # Variant registry: node_id -> {variant_name -> LangGraphNodeVariant}
        self._variants: Dict[str, Dict[str, LangGraphNodeVariant]] = {}
        
        # Original node functions cache: node_id -> Callable
        self._original_nodes: Dict[str, Callable] = {}
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Variant Registration
    # ═══════════════════════════════════════════════════════════════════════════
    
    def register_variant(
        self,
        node_id: str,
        variant_name: str,
        node_fn: Callable,
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LangGraphNodeVariant:
        """
        Register a node variant.
        
        Args:
            node_id: Original node ID this variant replaces
            variant_name: Human-readable variant name
            node_fn: The node function implementation
            description: What this variant does differently
            metadata: Additional metadata (model params, etc.)
            
        Returns:
            Registered LangGraphNodeVariant
            
        Raises:
            ValueError: If variant with same name already exists
        """
        if node_id not in self._variants:
            self._variants[node_id] = {}
        
        if variant_name in self._variants[node_id]:
            raise ValueError(
                f"Variant '{variant_name}' already exists for node '{node_id}'"
            )
        
        variant = LangGraphNodeVariant(
            node_id=node_id,
            variant_name=variant_name,
            node_fn=node_fn,
            description=description,
            metadata=metadata or {},
        )
        
        self._variants[node_id][variant_name] = variant
        
        logger.info(f"Registered variant '{variant_name}' for node '{node_id}'")
        return variant
    
    def register_original(self, node_id: str, node_fn: Callable) -> None:
        """
        Register the original node function for a node.
        
        This is used to track the baseline implementation.
        
        Args:
            node_id: Node ID
            node_fn: Original node function
        """
        self._original_nodes[node_id] = node_fn
        
        # Also register as "original" variant
        if node_id not in self._variants:
            self._variants[node_id] = {}
        
        self._variants[node_id]["original"] = LangGraphNodeVariant(
            node_id=node_id,
            variant_name="original",
            node_fn=node_fn,
            description="Original implementation",
        )
    
    def get_variant(
        self,
        node_id: str,
        variant_name: str
    ) -> Optional[LangGraphNodeVariant]:
        """Get a specific variant."""
        return self._variants.get(node_id, {}).get(variant_name)
    
    def get_variants(self, node_id: str) -> List[LangGraphNodeVariant]:
        """Get all variants for a node."""
        return list(self._variants.get(node_id, {}).values())
    
    def get_all_variants(self) -> Dict[str, List[LangGraphNodeVariant]]:
        """Get all registered variants."""
        return {
            node_id: list(variants.values())
            for node_id, variants in self._variants.items()
        }
    
    def deactivate_variant(self, node_id: str, variant_name: str) -> bool:
        """Deactivate a variant."""
        variant = self.get_variant(node_id, variant_name)
        if variant:
            variant.is_active = False
            return True
        return False
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Graph Modification
    # ═══════════════════════════════════════════════════════════════════════════
    
    def create_variant_graph(
        self,
        base_structure: GraphStructure,
        variant_set: Dict[str, str],  # node_id -> variant_name
    ) -> "StateGraph":
        """
        Create a new graph with variants applied.
        
        Args:
            base_structure: GraphStructure capturing the base graph
            variant_set: Map of node_id -> variant_name to apply
            
        Returns:
            New StateGraph with variants applied
            
        Raises:
            ValueError: If variant not found
        """
        if not LANGGRAPH_AVAILABLE:
            raise ImportError("LangGraph not available")
        
        # Create new structure with variants applied
        modified_structure = GraphStructure(
            state_schema=base_structure.state_schema,
            entry_point=base_structure.entry_point,
        )
        
        # Copy edges
        modified_structure.edges = list(base_structure.edges)
        
        # Copy nodes, substituting variants where specified
        for node_id, node_info in base_structure.nodes.items():
            if node_id in variant_set:
                variant_name = variant_set[node_id]
                variant = self.get_variant(node_id, variant_name)
                
                if not variant:
                    raise ValueError(
                        f"Variant '{variant_name}' not found for node '{node_id}'"
                    )
                
                if not variant.is_active:
                    raise ValueError(
                        f"Variant '{variant_name}' for node '{node_id}' is deactivated"
                    )
                
                # Use variant function
                modified_structure.add_node(
                    node_id=node_id,
                    node_fn=variant.node_fn,
                    variant_name=variant_name,
                    **node_info.metadata,
                )
            else:
                # Use original
                modified_structure.add_node(
                    node_id=node_id,
                    node_fn=node_info.node_fn,
                    **node_info.metadata,
                )
        
        return modified_structure.build()
    
    def create_variant_structure(
        self,
        base_structure: GraphStructure,
        variant_set: Dict[str, str],
    ) -> GraphStructure:
        """
        Create a modified GraphStructure with variants applied.
        
        This is useful when you need to further modify the graph
        before building it.
        
        Args:
            base_structure: Base graph structure
            variant_set: Variants to apply
            
        Returns:
            Modified GraphStructure
        """
        modified = GraphStructure(
            state_schema=base_structure.state_schema,
            entry_point=base_structure.entry_point,
        )
        modified.edges = list(base_structure.edges)
        
        for node_id, node_info in base_structure.nodes.items():
            if node_id in variant_set:
                variant_name = variant_set[node_id]
                variant = self.get_variant(node_id, variant_name)
                
                if variant and variant.is_active:
                    modified.add_node(
                        node_id=node_id,
                        node_fn=variant.node_fn,
                        variant_name=variant_name,
                    )
                    continue
            
            modified.add_node(
                node_id=node_id,
                node_fn=node_info.node_fn,
            )
        
        return modified
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Batch Testing Support
    # ═══════════════════════════════════════════════════════════════════════════
    
    def generate_variant_combinations(
        self,
        nodes: Optional[List[str]] = None
    ) -> List[VariantSet]:
        """
        Generate all variant combinations for batch testing.
        
        If nodes is specified, only those nodes are varied.
        Otherwise, all nodes with variants are included.
        
        Args:
            nodes: Optional list of node IDs to vary
            
        Returns:
            List of VariantSet objects representing all combinations
        """
        from itertools import product
        
        # Determine which nodes to vary
        if nodes:
            target_nodes = {n: self._variants.get(n, {}) for n in nodes}
        else:
            target_nodes = self._variants
        
        # Get variant names for each node
        node_variants = {}
        for node_id, variants in target_nodes.items():
            active_variants = [v.variant_name for v in variants.values() if v.is_active]
            if active_variants:
                node_variants[node_id] = active_variants
        
        if not node_variants:
            return []
        
        # Generate all combinations
        combinations = []
        node_ids = list(node_variants.keys())
        variant_lists = [node_variants[n] for n in node_ids]
        
        for combo in product(*variant_lists):
            variant_set = dict(zip(node_ids, combo))
            name = "_".join(f"{n}:{v}" for n, v in variant_set.items())
            
            combinations.append(VariantSet(
                name=name,
                variants=variant_set,
            ))
        
        logger.info(f"Generated {len(combinations)} variant combinations")
        return combinations
    
    def generate_pairwise_combinations(
        self,
        nodes: Optional[List[str]] = None
    ) -> List[VariantSet]:
        """
        Generate pairwise variant combinations (reduced set).
        
        Uses pairwise testing to reduce combinations while maintaining
        good coverage.
        
        Args:
            nodes: Optional list of node IDs to vary
            
        Returns:
            List of VariantSet objects
        """
        # For now, use simple pairwise approach
        # In production, use proper pairwise testing algorithm
        
        all_combos = self.generate_variant_combinations(nodes)
        
        if len(all_combos) <= 10:
            return all_combos
        
        # Simple reduction: sample every Nth combination
        step = max(1, len(all_combos) // 10)
        return all_combos[::step]
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Utility Methods
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_original_node(self, node_id: str) -> Optional[Callable]:
        """Get the original node function."""
        return self._original_nodes.get(node_id)
    
    def clear_variants(self, node_id: Optional[str] = None) -> None:
        """
        Clear registered variants.
        
        Args:
            node_id: If specified, only clear variants for this node
        """
        if node_id:
            self._variants.pop(node_id, None)
        else:
            self._variants.clear()
    
    def export_variant_registry(self) -> Dict[str, Any]:
        """Export variant registry as serializable dict."""
        return {
            node_id: {
                variant_name: variant.to_dict()
                for variant_name, variant in variants.items()
            }
            for node_id, variants in self._variants.items()
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Convenience Functions
# ═══════════════════════════════════════════════════════════════════════════════


def capture_graph_structure(
    graph_builder_fn: Callable[[GraphStructure], None],
    state_schema: Type,
) -> GraphStructure:
    """
    Capture graph structure using a builder function.
    
    This provides a way to define graph structure once and recreate
    it with modifications.
    
    Args:
        graph_builder_fn: Function that builds the graph structure
        state_schema: State schema type
        
    Returns:
        GraphStructure that can be modified and built
        
    Example:
        def build_my_graph(structure: GraphStructure):
            structure.add_node("agent", agent_fn)
            structure.add_node("tool", tool_fn)
            structure.add_edge("agent", "tool")
            structure.set_entry_point("agent")
        
        structure = capture_graph_structure(build_my_graph, MyState)
    """
    structure = GraphStructure(state_schema=state_schema)
    graph_builder_fn(structure)
    return structure


def is_available() -> bool:
    """Check if LangGraph is available."""
    return LANGGRAPH_AVAILABLE
