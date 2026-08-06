"""
ProjectService - Application Service for Project Management.

This service handles project registration and management with proper
transaction boundaries. The SDK delegates to this service instead of
directly manipulating UoW.

Refactored (v1.7):
- Added WorkflowConversionService for converting SDK projects to domain workflows
- Moved _project_to_workflow logic from SDK to Application layer (layer separation)

Architecture:
    SDK (WTBTestBench)
        └── ProjectService (Application Layer)
        └── WorkflowConversionService (Application Layer)
                └── IUnitOfWork (Infrastructure Layer)

Design Principles:
    - Single Responsibility: Only handles project CRUD with transactions
    - Dependency Inversion: Depends on IUnitOfWork interface
    - Transaction Boundary: Each operation commits atomically
    - Layer Separation: SDK does not contain domain conversion logic

ARCHITECTURE NOTE (2026-01-17):
    Services receive an ALREADY-OPENED UoW context from the factory.
    Services should NOT use `with self._uow:` as this closes the session.
    Instead, services access repositories directly and call commit() explicitly.
    This ensures the session stays open for the controller's entire lifecycle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from wtb.domain.interfaces import IUnitOfWork
    from wtb.domain.models import TestWorkflow

logger = logging.getLogger(__name__)


class ProjectService:
    """
    Application Service for project management.
    
    Encapsulates UoW transaction management for project operations.
    The SDK should delegate to this service instead of using UoW directly.
    
    IMPORTANT: The UoW context is managed by the factory, not by this service.
    This service calls commit() but does NOT enter/exit the UoW context.
    
    Usage:
        service = ProjectService(uow)  # UoW already opened by factory
        service.register_workflow(workflow)  # Commits atomically
        workflow = service.get_workflow_by_name("my_workflow")
    """
    
    def __init__(self, uow: "IUnitOfWork"):
        """
        Initialize ProjectService.
        
        Args:
            uow: Unit of Work for transaction management (already opened)
        """
        self._uow = uow
    
    def register_workflow(self, workflow: "TestWorkflow") -> None:
        """
        Register a workflow with ACID transaction.
        
        Args:
            workflow: TestWorkflow domain entity to register
            
        Raises:
            ValueError: If workflow with same name already exists
        """
        existing = self._uow.workflows.find_by_name(workflow.name)
        if existing:
            raise ValueError(f"Project '{workflow.name}' already registered")
        
        self._uow.workflows.add(workflow)
        self._uow.commit()  # ACID: Commit at transaction boundary
        
        logger.debug(f"Registered workflow: {workflow.name}")
    
    def get_workflow_by_name(self, name: str) -> "TestWorkflow" | None:
        """
        Get a workflow by name.
        
        Args:
            name: Workflow name
            
        Returns:
            TestWorkflow if found, None otherwise
        """
        return self._uow.workflows.find_by_name(name)
    
    def get_workflow_by_id(self, workflow_id: str) -> "TestWorkflow" | None:
        """
        Get a workflow by ID.
        
        Args:
            workflow_id: Workflow ID
            
        Returns:
            TestWorkflow if found, None otherwise
        """
        return self._uow.workflows.get(workflow_id)
    
    def unregister_workflow(self, name: str) -> bool:
        """
        Unregister a workflow with ACID transaction.
        
        Deletes all FK-dependent children in leaf-first order to satisfy
        constraints, then deletes the workflow itself.
        
        Deletion order (leaves first):
          evaluation_results -> executions -> node_variants -> batch_tests -> workflow
        
        Args:
            name: Workflow name to unregister
            
        Returns:
            True if unregistered, False if not found
        """
        workflow = self._uow.workflows.find_by_name(name)
        if not workflow:
            return False
        
        wf_id = workflow.id

        for exec_ in self._uow.executions.find_by_workflow(wf_id):
            if hasattr(self._uow, "evaluation_results"):
                for er in self._uow.evaluation_results.find_by_execution(exec_.id):
                    self._uow.evaluation_results.delete(er.id)
            self._uow.executions.delete(exec_.id)

        if hasattr(self._uow, "variants"):
            for v in self._uow.variants.find_by_workflow(wf_id):
                self._uow.variants.delete(v.id)

        if hasattr(self._uow, "batch_tests"):
            for bt in self._uow.batch_tests.find_by_workflow(wf_id):
                self._uow.batch_tests.delete(bt.id)

        self._uow.workflows.delete(wf_id)
        self._uow.commit()
        
        logger.debug(f"Unregistered workflow: {name}")
        return True
    
    def list_workflows(self) -> list["TestWorkflow"]:
        """
        List all registered workflows.
        
        Returns:
            List of TestWorkflow entities
        """
        return self._uow.workflows.list_all()
    
    def workflow_exists(self, name: str) -> bool:
        """
        Check if a workflow exists by name.
        
        Args:
            name: Workflow name
            
        Returns:
            True if exists, False otherwise
        """
        return self._uow.workflows.find_by_name(name) is not None


class VariantService:
    """
    Application Service for variant management.
    
    Encapsulates UoW transaction management for variant operations.
    
    IMPORTANT: The UoW context is managed by the factory, not by this service.
    This service calls commit() but does NOT enter/exit the UoW context.
    """
    
    def __init__(self, uow: "IUnitOfWork", variant_registry: "IVariantRegistry"):
        """
        Initialize VariantService.
        
        Args:
            uow: Unit of Work for transaction management (already opened)
            variant_registry: Domain service for variant registration
        """
        self._uow = uow
        self._registry = variant_registry
    
    def register_variant(
        self,
        workflow_name: str,
        node_id: str,
        variant_name: str,
        implementation,
        description: str = "",
    ) -> None:
        """
        Register a node variant with ACID transaction.
        
        Args:
            workflow_name: Name of the workflow
            node_id: ID of the node to create variant for
            variant_name: Name of the variant
            implementation: Variant implementation callable
            description: Optional description
            
        Raises:
            ValueError: If workflow not found
        """
        from wtb.domain.models import NodeVariant, WorkflowNode
        
        workflow = self._uow.workflows.find_by_name(workflow_name)
        if not workflow:
            raise ValueError(f"Project '{workflow_name}' not found")
        
        # Create a variant node that wraps the implementation
        variant_node = WorkflowNode(
            id=f"{node_id}_{variant_name}",
            name=variant_name,
            type="action",
            config={"implementation": implementation},
        )
        
        variant = NodeVariant(
            workflow_id=workflow.id,
            original_node_id=node_id,
            variant_name=variant_name,
            variant_node=variant_node,
            description=description,
        )
        self._registry.register(variant)
        self._uow.commit()  # ACID: Commit at transaction boundary
        
        logger.debug(f"Registered variant: {variant_name} for node {node_id}")


# Type hint for avoiding circular imports
if TYPE_CHECKING:
    from wtb.domain.interfaces import IVariantRegistry


# ═══════════════════════════════════════════════════════════════════════════════
# WorkflowConversionService (v1.7)
# ═══════════════════════════════════════════════════════════════════════════════


class WorkflowConversionService:
    """
    Application Service for converting SDK WorkflowProject to domain TestWorkflow.
    
    v1.7: Moved from SDK layer to Application layer for proper layer separation.
    
    SOLID Compliance:
    - SRP: Only responsible for workflow conversion
    - OCP: Can extend to support new graph formats
    - DIP: Depends on domain models, not SDK
    
    Usage:
        converter = WorkflowConversionService()
        workflow = converter.convert_from_project(project)
    """
    
    def convert_from_project(self, project: Any) -> "TestWorkflow":
        """
        Convert an SDK WorkflowProject to domain TestWorkflow.
        
        Args:
            project: SDK WorkflowProject with name, id, description, and build_graph()
            
        Returns:
            TestWorkflow domain entity
        """
        from wtb.domain.models import TestWorkflow, WorkflowEdge, WorkflowNode
        
        workflow = TestWorkflow(
            id=getattr(project, 'id', str(id(project))),
            name=getattr(project, 'name', 'unknown'),
            description=getattr(project, 'description', ''),
        )
        
        try:
            graph = project.build_graph()
            nodes_info = self._extract_graph_nodes(graph)
            edges_info = self._extract_graph_edges(graph)
            entry_point = self._determine_entry_point(nodes_info, edges_info, graph)
            
            # Add regular nodes
            for node_id, node_info in nodes_info.items():
                node_type = "start" if node_id == entry_point else node_info.get("type", "action")
                workflow.add_node(WorkflowNode(
                    id=node_id,
                    name=node_info.get("name", node_id),
                    type=node_type,
                ))
            
            # Track terminal nodes
            terminal_nodes = set()
            internal_nodes = {"__start__", "__end__", "END"}
            
            for edge in edges_info:
                if edge["target"] in ("__end__", "END") and edge["source"] in nodes_info:
                    terminal_nodes.add(edge["source"])
                elif edge["source"] not in internal_nodes and edge["target"] not in internal_nodes:
                    if edge["source"] in nodes_info and edge["target"] in nodes_info:
                        workflow.add_edge(WorkflowEdge(
                            source_id=edge["source"],
                            target_id=edge["target"],
                        ))
            
            # Add virtual "end" node
            if terminal_nodes:
                workflow.add_node(WorkflowNode(id="__end__", name="End", type="end"))
                for term_node in terminal_nodes:
                    workflow.add_edge(WorkflowEdge(source_id=term_node, target_id="__end__"))
            
            if entry_point:
                workflow.entry_point = entry_point
                
        except Exception as e:
            logger.warning(f"Failed to extract graph structure: {e}")
            workflow.add_node(WorkflowNode(id="start", name="Start", type="start"))
            workflow.entry_point = "start"
        
        return workflow
    
    def _extract_graph_nodes(self, graph: Any) -> dict[str, dict[str, Any]]:
        """Extract node information from LangGraph graph."""
        nodes: dict[str, dict[str, Any]] = {}
        node_graph = graph
        
        if hasattr(graph, 'get_graph'):
            try:
                node_graph = graph.get_graph()
            except Exception:
                pass
        
        if hasattr(node_graph, 'nodes'):
            node_list = (
                list(node_graph.nodes)
                if not isinstance(node_graph.nodes, dict)
                else list(node_graph.nodes.keys())
            )
            for node_id in node_list:
                if node_id not in ("__start__", "__end__"):
                    nodes[node_id] = {"name": node_id, "type": "action"}
        
        return nodes or {"start": {"name": "Start", "type": "start"}}
    
    def _extract_graph_edges(self, graph: Any) -> list[dict[str, Any]]:
        """Extract edge information from LangGraph graph."""
        edges: list[dict[str, Any]] = []
        edge_graph = graph
        
        if hasattr(graph, 'get_graph') and (not hasattr(graph, 'edges') or graph.edges is None):
            try:
                edge_graph = graph.get_graph()
            except Exception:
                pass
        
        if hasattr(edge_graph, 'edges') and edge_graph.edges is not None:
            edge_data = edge_graph.edges
            if isinstance(edge_data, dict):
                for (src, tgt), data in edge_data.items():
                    edges.append({"source": src, "target": tgt})
            else:
                for edge in edge_data:
                    if hasattr(edge, 'source') and hasattr(edge, 'target'):
                        edges.append({"source": edge.source, "target": edge.target})
                    elif isinstance(edge, tuple) and len(edge) >= 2:
                        edges.append({"source": edge[0], "target": edge[1]})
        
        return edges or [{"source": "start", "target": "action"}]
    
    def _determine_entry_point(
        self,
        nodes_info: dict[str, dict[str, Any]],
        edges_info: list[dict[str, Any]],
        graph: Any,
    ) -> str | None:
        """Determine the entry point node from graph."""
        # Check edges for __start__ -> first node
        for edge in edges_info:
            if edge["source"] == "__start__" and edge["target"] in nodes_info:
                return edge["target"]
        
        # Check graph attributes
        for attr in ('_entry_point', 'entry_point'):
            if hasattr(graph, attr):
                ep = getattr(graph, attr)
                if isinstance(ep, str) and ep in nodes_info:
                    return ep
        
        # Default to "start" or first node
        if nodes_info:
            return "start" if "start" in nodes_info else next(iter(nodes_info.keys()))
        return None
