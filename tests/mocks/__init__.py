"""
Centralized Mock Infrastructure for WTB Tests.

This module provides standardized mock implementations that:
1. Implement real interfaces (SOLID - LSP compliance)
2. Work with both mock and real implementations (DRY)
3. Are shared across all test modules (Single Source of Truth)

Structure:
- repositories.py: Mock repository implementations (IOutboxRepository, etc.)
- domain_objects.py: Mock domain objects (compatible with real types)
- services.py: Mock services (VenvManager, ActorPool, etc.)
- factories.py: Factory functions for creating test fixtures

Usage:
    from tests.mocks import MockOutboxRepository, create_test_outbox_event
    
Design Philosophy:
- Mocks MUST accept real domain types (OutboxEvent, not MockOutboxEvent)
- Mocks SHOULD implement the same interface as real implementations
- Tests SHOULD pass with both mock and real implementations

Updated: 2026-01-28
"""

from tests.mocks.repositories import (
    MockOutboxRepository,
    MockCheckpointRepository,
    MockCommitRepository,
    MockBlobRepository,
    MockExecutionRepository,
    MockWorkflowRepository,
    MockNodeVariantRepository,
    MockMemento,
    MockCommit,
    MockCheckpoint,
)

from tests.mocks.domain_objects import (
    # Factory functions that create REAL domain objects for testing
    create_test_outbox_event,
    create_test_checkpoint,
    create_test_commit,
    create_test_memento,
    create_test_execution,
    create_test_workflow,
    # Test data generators
    generate_test_commits,
    generate_test_files,
    generate_batch_test_variants,
)

from tests.mocks.services import (
    MockActorPool,
    MockVenvManager,
    MockRayEventBridge,
    MockActor,
    MockVenv,
)

__all__ = [
    # Repositories
    "MockOutboxRepository",
    "MockCheckpointRepository",
    "MockCommitRepository",
    "MockBlobRepository",
    "MockExecutionRepository",
    "MockWorkflowRepository",
    "MockNodeVariantRepository",
    # Mock dataclasses
    "MockMemento",
    "MockCommit",
    "MockCheckpoint",
    # Domain object factories
    "create_test_outbox_event",
    "create_test_checkpoint",
    "create_test_commit",
    "create_test_memento",
    "create_test_execution",
    "create_test_workflow",
    # Test data generators
    "generate_test_commits",
    "generate_test_files",
    "generate_batch_test_variants",
    # Services
    "MockActorPool",
    "MockVenvManager",
    "MockRayEventBridge",
    "MockActor",
    "MockVenv",
]
