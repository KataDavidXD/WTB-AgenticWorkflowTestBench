"""
Pytest fixtures for WTB tests.
"""

import pytest
from typing import Optional, List, Dict, Any

from wtb.domain.models import (
    Execution,
    TestWorkflow,
    WorkflowNode,
    WorkflowEdge,
    NodeVariant,
)
from wtb.domain.interfaces.repositories import (
    IExecutionRepository,
    IWorkflowRepository,
    INodeVariantRepository,
)
from wtb.infrastructure.adapters.inmemory_state_adapter import InMemoryStateAdapter


# ═══════════════════════════════════════════════════════════════════════════════
# Shared Mock Repositories
# ═══════════════════════════════════════════════════════════════════════════════

class MockExecutionRepository(IExecutionRepository):
    """Shared mock execution repository."""
    
    def __init__(self):
        self._storage: Dict[str, Execution] = {}
    
    def get(self, id: str) -> Optional[Execution]:
        return self._storage.get(id)
    
    def list(self, limit: int = 100, offset: int = 0) -> List[Execution]:
        return list(self._storage.values())[offset:offset + limit]
    
    def add(self, entity: Execution) -> Execution:
        self._storage[entity.id] = entity
        return entity
    
    def update(self, entity: Execution) -> Execution:
        self._storage[entity.id] = entity
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._storage:
            del self._storage[id]
            return True
        return False
    
    def exists(self, id: str) -> bool:
        return id in self._storage
    
    def find_by_workflow(self, workflow_id: str, status: Optional[str] = None) -> List[Execution]:
        results = [e for e in self._storage.values() if e.workflow_id == workflow_id]
        if status:
            results = [e for e in results if e.status.value == status]
        return results
    
    def find_by_status(self, status: str) -> List[Execution]:
        return [e for e in self._storage.values() if e.status.value == status]
    
    def count_by_status(self) -> Dict[str, int]:
        counts = {}
        for e in self._storage.values():
            counts[e.status.value] = counts.get(e.status.value, 0) + 1
        return counts
    
    def find_running(self) -> List[Execution]:
        from wtb.domain.models import ExecutionStatus
        return [e for e in self._storage.values() if e.status == ExecutionStatus.RUNNING]
    
    def clear(self):
        self._storage.clear()


class MockWorkflowRepository(IWorkflowRepository):
    """Shared mock workflow repository."""
    
    def __init__(self):
        self._storage: Dict[str, TestWorkflow] = {}
    
    def get(self, id: str) -> Optional[TestWorkflow]:
        return self._storage.get(id)
    
    def list(self, limit: int = 100, offset: int = 0) -> List[TestWorkflow]:
        return list(self._storage.values())[offset:offset + limit]
    
    def add(self, entity: TestWorkflow) -> TestWorkflow:
        self._storage[entity.id] = entity
        return entity
    
    def update(self, entity: TestWorkflow) -> TestWorkflow:
        self._storage[entity.id] = entity
        return entity
    
    def delete(self, id: str) -> bool:
        if id in self._storage:
            del self._storage[id]
            return True
        return False
    
    def exists(self, id: str) -> bool:
        return id in self._storage
    
    def find_by_name(self, name: str) -> Optional[TestWorkflow]:
        for w in self._storage.values():
            if w.name == name:
                return w
        return None
    
    def find_by_version(self, name: str, version: str) -> Optional[TestWorkflow]:
        for w in self._storage.values():
            if w.name == name and w.version == version:
                return w
        return None
    
    def clear(self):
        self._storage.clear()


class MockNodeVariantRepository(INodeVariantRepository):
    """Shared mock node variant repository."""
    
    def __init__(self):
        self._storage: Dict[str, NodeVariant] = {}
    
    def get(self, id: str) -> Optional[NodeVariant]:
        return self._storage.get(id)
    
    def list(self, limit: int = 100, offset: int = 0) -> List[NodeVariant]:
        return list(self._storage.values())[offset:offset + limit]
    
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
    
    def clear(self):
        self._storage.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# Pytest Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def execution_repository():
    """Provide a fresh mock execution repository."""
    return MockExecutionRepository()


@pytest.fixture
def workflow_repository():
    """Provide a fresh mock workflow repository."""
    return MockWorkflowRepository()


@pytest.fixture
def variant_repository():
    """Provide a fresh mock variant repository."""
    return MockNodeVariantRepository()


@pytest.fixture
def state_adapter():
    """Provide a fresh in-memory state adapter."""
    return InMemoryStateAdapter()


@pytest.fixture
def simple_workflow():
    """Create a simple start -> action -> end workflow."""
    workflow = TestWorkflow(name="Simple Workflow")
    
    start = WorkflowNode(id="start", name="Start", type="start")
    action = WorkflowNode(id="action", name="Action", type="action")
    end = WorkflowNode(id="end", name="End", type="end")
    
    workflow.add_node(start)
    workflow.add_node(action)
    workflow.add_node(end)
    
    workflow.add_edge(WorkflowEdge(source_id="start", target_id="action"))
    workflow.add_edge(WorkflowEdge(source_id="action", target_id="end"))
    
    return workflow


@pytest.fixture
def decision_workflow():
    """Create a workflow with a decision node."""
    workflow = TestWorkflow(name="Decision Workflow")
    
    start = WorkflowNode(id="start", name="Start", type="start")
    decision = WorkflowNode(
        id="decision",
        name="Check Value",
        type="decision",
        config={"condition": "x > 5"}
    )
    branch_true = WorkflowNode(id="branch_true", name="True Branch", type="action")
    branch_false = WorkflowNode(id="branch_false", name="False Branch", type="action")
    end = WorkflowNode(id="end", name="End", type="end")
    
    workflow.add_node(start)
    workflow.add_node(decision)
    workflow.add_node(branch_true)
    workflow.add_node(branch_false)
    workflow.add_node(end)
    
    workflow.add_edge(WorkflowEdge(source_id="start", target_id="decision"))
    workflow.add_edge(WorkflowEdge(source_id="decision", target_id="branch_true", condition="decision"))
    workflow.add_edge(WorkflowEdge(source_id="decision", target_id="branch_false", condition="not decision"))
    workflow.add_edge(WorkflowEdge(source_id="branch_true", target_id="end"))
    workflow.add_edge(WorkflowEdge(source_id="branch_false", target_id="end"))
    
    return workflow

