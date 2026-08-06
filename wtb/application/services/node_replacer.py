"""
Node Replacer Implementation.

Manages node variants for A/B testing and hot-swapping.

ARCHITECTURE NOTE (2026-01-17):
    Services receive an ALREADY-OPENED UoW context from the factory.
    Services should NOT use `with self._uow:` as this closes the session.
    Instead, services access repositories directly and call commit() explicitly.
    This ensures ACID compliance for all write operations.
"""

from typing import TYPE_CHECKING, Optional

from wtb.domain.interfaces.node_replacer import INodeReplacer
from wtb.domain.interfaces.repositories import INodeVariantRepository
from wtb.domain.models import NodeVariant, TestWorkflow, WorkflowNode

if TYPE_CHECKING:
    from wtb.domain.interfaces.unit_of_work import IUnitOfWork


class NodeReplacer(INodeReplacer):
    """
    Manages node variants for A/B testing and hot-swapping.
    
    Features:
    - Register and manage node variants
    - Hot-swap nodes in workflows
    - Apply variant sets for batch testing
    - Track original nodes for restoration
    
    ACID Compliance:
    - Receives UoW reference for transaction management
    - Calls commit() at transaction boundaries for durability
    """
    
    def __init__(
        self, 
        variant_repository: INodeVariantRepository,
        unit_of_work: Optional["IUnitOfWork"] = None,
    ):
        """
        Initialize the node replacer.
        
        Args:
            variant_repository: Repository for persisting variants
            unit_of_work: Optional UoW for transaction management (ACID compliance)
        """
        self._variant_repo = variant_repository
        self._uow = unit_of_work
        
        # Cache original nodes for restoration
        # workflow_id -> {node_id -> original WorkflowNode}
        self._original_cache: dict[str, dict[str, WorkflowNode]] = {}
    
    def _commit(self) -> None:
        """Commit UoW transaction if available (ACID compliance)."""
        if self._uow is not None:
            self._uow.commit()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # IVariantRegistry Implementation
    # ═══════════════════════════════════════════════════════════════════════════
    
    def register(self, variant: NodeVariant) -> NodeVariant:
        """Register a new node variant with ACID transaction."""
        # Validate variant
        if not variant.original_node_id:
            raise ValueError("Variant must specify original_node_id")
        if not variant.variant_name:
            raise ValueError("Variant must have a name")
        
        # Check for duplicates
        existing = self.get_by_node(variant.workflow_id, variant.original_node_id)
        for v in existing:
            if v.variant_name == variant.variant_name:
                raise ValueError(
                    f"Variant '{variant.variant_name}' already exists for node {variant.original_node_id}"
                )
        
        result = self._variant_repo.add(variant)
        self._commit()  # ACID: Commit at transaction boundary
        return result
    
    def get(self, variant_id: str) -> NodeVariant | None:
        """Get a variant by ID."""
        return self._variant_repo.get(variant_id)
    
    def get_by_node(self, workflow_id: str, node_id: str) -> list[NodeVariant]:
        """Get all variants for a specific node."""
        return self._variant_repo.find_by_node(workflow_id, node_id)
    
    def list_for_workflow(self, workflow_id: str) -> list[NodeVariant]:
        """List all variants for a workflow."""
        return self._variant_repo.find_by_workflow(workflow_id)
    
    def deactivate(self, variant_id: str) -> bool:
        """Deactivate a variant with ACID transaction."""
        variant = self._variant_repo.get(variant_id)
        if not variant:
            return False
        
        variant.is_active = False
        self._variant_repo.update(variant)
        self._commit()  # ACID: Commit at transaction boundary
        return True
    
    # ═══════════════════════════════════════════════════════════════════════════
    # INodeSwapper Implementation
    # ═══════════════════════════════════════════════════════════════════════════
    
    def swap_node(
        self, 
        workflow: TestWorkflow,
        original_node_id: str,
        variant: NodeVariant
    ) -> TestWorkflow:
        """Swap a node with its variant, returning modified workflow."""
        # Validate
        if original_node_id not in workflow.nodes:
            raise ValueError(f"Node {original_node_id} not found in workflow")
        
        # Cache original if not already cached
        cache_key = workflow.id
        if cache_key not in self._original_cache:
            self._original_cache[cache_key] = {}
        
        if original_node_id not in self._original_cache[cache_key]:
            self._original_cache[cache_key][original_node_id] = workflow.nodes[original_node_id]
        
        # Create modified workflow (immutable pattern)
        modified = self._clone_workflow(workflow)
        
        # Create variant node with original ID (preserves edge connections)
        variant_node = WorkflowNode(
            id=original_node_id,  # Preserve original ID
            name=variant.variant_node.name,
            type=variant.variant_node.type,
            tool_name=variant.variant_node.tool_name,
            config=variant.variant_node.config,
        )
        
        modified.nodes[original_node_id] = variant_node
        
        return modified
    
    def restore_original(
        self,
        workflow: TestWorkflow,
        node_id: str
    ) -> TestWorkflow:
        """Restore original node, returning modified workflow."""
        cache_key = workflow.id
        
        if cache_key not in self._original_cache:
            raise ValueError(f"No cached originals for workflow {workflow.id}")
        
        if node_id not in self._original_cache[cache_key]:
            raise ValueError(f"Original node {node_id} not cached")
        
        # Create modified workflow
        modified = self._clone_workflow(workflow)
        modified.nodes[node_id] = self._original_cache[cache_key][node_id]
        
        return modified
    
    def apply_variant_set(
        self,
        workflow: TestWorkflow,
        variant_set: dict[str, str]  # node_id -> variant_id
    ) -> TestWorkflow:
        """Apply a set of variants to a workflow."""
        modified = self._clone_workflow(workflow)
        
        for node_id, variant_id in variant_set.items():
            variant = self.get(variant_id)
            if not variant:
                raise ValueError(f"Variant {variant_id} not found")
            if variant.original_node_id != node_id:
                raise ValueError(
                    f"Variant {variant_id} is for node {variant.original_node_id}, not {node_id}"
                )
            
            modified = self.swap_node(modified, node_id, variant)
        
        return modified
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Additional Utility Methods
    # ═══════════════════════════════════════════════════════════════════════════
    
    def create_variant_from_node(
        self,
        workflow: TestWorkflow,
        node_id: str,
        variant_name: str,
        modifications: dict[str, any],
        description: str = ""
    ) -> NodeVariant:
        """
        Create a variant by modifying an existing node.
        
        Args:
            workflow: The workflow containing the original node
            node_id: ID of the node to create a variant from
            variant_name: Name for the new variant
            modifications: Dict of modifications to apply
            description: Optional description
            
        Returns:
            Registered NodeVariant
        """
        if node_id not in workflow.nodes:
            raise ValueError(f"Node {node_id} not found")
        
        original = workflow.nodes[node_id]
        
        # Apply modifications to create variant node
        new_config = dict(original.config)
        new_name = original.name
        new_type = original.type
        new_tool_name = original.tool_name
        
        for key, value in modifications.items():
            if key == 'name':
                new_name = value
            elif key == 'type':
                new_type = value
            elif key == 'tool_name':
                new_tool_name = value
            else:
                new_config[key] = value
        
        variant_node = WorkflowNode(
            id=f"{node_id}_variant_{variant_name}",
            name=new_name,
            type=new_type,
            tool_name=new_tool_name,
            config=new_config,
        )
        
        variant = NodeVariant(
            workflow_id=workflow.id,
            original_node_id=node_id,
            variant_name=variant_name,
            variant_node=variant_node,
            description=description,
        )
        
        return self.register(variant)
    
    def clear_cache(self, workflow_id: str | None = None):
        """
        Clear the original node cache.
        
        Args:
            workflow_id: If specified, only clear cache for this workflow.
                        If None, clear all caches.
        """
        if workflow_id:
            self._original_cache.pop(workflow_id, None)
        else:
            self._original_cache.clear()
    
    def get_cached_originals(self, workflow_id: str) -> dict[str, WorkflowNode]:
        """Get cached original nodes for a workflow."""
        return dict(self._original_cache.get(workflow_id, {}))
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Private Methods
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _clone_workflow(self, workflow: TestWorkflow) -> TestWorkflow:
        """Create a deep copy of a workflow."""
        # Create new workflow with same metadata
        cloned = TestWorkflow(
            id=workflow.id,
            name=workflow.name,
            description=workflow.description,
            entry_point=workflow.entry_point,
            version=workflow.version,
            created_at=workflow.created_at,
            updated_at=workflow.updated_at,
            metadata=dict(workflow.metadata),
        )
        
        # Copy nodes
        for node_id, node in workflow.nodes.items():
            cloned.nodes[node_id] = node  # WorkflowNode is frozen/immutable
        
        # Copy edges
        cloned.edges = list(workflow.edges)  # WorkflowEdge is frozen/immutable
        
        return cloned

