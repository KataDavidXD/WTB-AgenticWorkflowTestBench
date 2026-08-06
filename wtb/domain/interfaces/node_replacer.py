"""
Node Replacer Interfaces.

Defines contracts for managing node variants and hot-swapping.
Split into IVariantRegistry and INodeSwapper following ISP.
"""

from abc import ABC, abstractmethod

from ..models.workflow import NodeVariant, TestWorkflow


class IVariantRegistry(ABC):
    """
    Interface for managing node variants.
    
    Responsibilities:
    - Register new variants
    - Query variants by ID, node, or workflow
    - Activate/deactivate variants
    """
    
    @abstractmethod
    def register(self, variant: NodeVariant) -> NodeVariant:
        """
        Register a new node variant.
        
        Args:
            variant: The variant to register
            
        Returns:
            Registered variant with ID assigned
            
        Raises:
            ValueError: If variant is invalid or duplicate
        """
        pass
    
    @abstractmethod
    def get(self, variant_id: str) -> NodeVariant | None:
        """
        Get a variant by ID.
        
        Args:
            variant_id: ID of the variant
            
        Returns:
            NodeVariant if found, None otherwise
        """
        pass
    
    @abstractmethod
    def get_by_node(self, workflow_id: str, node_id: str) -> list[NodeVariant]:
        """
        Get all variants for a specific node.
        
        Args:
            workflow_id: ID of the workflow
            node_id: ID of the original node
            
        Returns:
            List of variants for the node (may be empty)
        """
        pass
    
    @abstractmethod
    def list_for_workflow(self, workflow_id: str) -> list[NodeVariant]:
        """
        List all variants for a workflow.
        
        Args:
            workflow_id: ID of the workflow
            
        Returns:
            List of all variants for the workflow
        """
        pass
    
    @abstractmethod
    def deactivate(self, variant_id: str) -> bool:
        """
        Deactivate a variant.
        
        Deactivated variants won't be used in new tests but
        remain for historical reference.
        
        Args:
            variant_id: ID of the variant to deactivate
            
        Returns:
            True if deactivated, False if not found
        """
        pass


class INodeSwapper(ABC):
    """
    Interface for swapping nodes at runtime.
    
    Responsibilities:
    - Swap individual nodes with variants
    - Restore original nodes
    - Apply variant sets (multiple swaps)
    
    All operations return modified workflow copies (immutable pattern).
    """
    
    @abstractmethod
    def swap_node(
        self, 
        workflow: TestWorkflow,
        original_node_id: str,
        variant: NodeVariant
    ) -> TestWorkflow:
        """
        Swap a node with its variant.
        
        Returns a modified copy of the workflow.
        The original workflow is not modified.
        
        Args:
            workflow: The workflow to modify
            original_node_id: ID of the node to replace
            variant: The variant to swap in
            
        Returns:
            New TestWorkflow with node swapped
            
        Raises:
            ValueError: If node not found or variant mismatch
        """
        pass
    
    @abstractmethod
    def restore_original(
        self,
        workflow: TestWorkflow,
        node_id: str
    ) -> TestWorkflow:
        """
        Restore original node, returning modified workflow.
        
        Args:
            workflow: The workflow to modify
            node_id: ID of the node to restore
            
        Returns:
            New TestWorkflow with original node restored
            
        Raises:
            ValueError: If original not cached
        """
        pass
    
    @abstractmethod
    def apply_variant_set(
        self,
        workflow: TestWorkflow,
        variant_set: dict[str, str]  # node_id -> variant_id
    ) -> TestWorkflow:
        """
        Apply a set of variants to a workflow.
        
        Used for batch testing with multiple variant combinations.
        
        Args:
            workflow: The workflow to modify
            variant_set: Mapping of node IDs to variant IDs
            
        Returns:
            New TestWorkflow with all variants applied
            
        Raises:
            ValueError: If any variant not found or mismatch
        """
        pass


class INodeReplacer(IVariantRegistry, INodeSwapper):
    """
    Combined interface for node replacement functionality.
    
    Combines IVariantRegistry and INodeSwapper for convenience.
    Implementations should handle variant caching for restoration.
    """
    pass


