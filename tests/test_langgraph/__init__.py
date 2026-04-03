"""
LangGraph Test Suite.

Comprehensive tests for LangGraph integration with WTB system.

Test Categories:
================
1. Unit Tests (tests/test_langgraph/unit/)
   - Basic operations: Graph creation, compilation, execution
   - Checkpoint operations: Save, load, history
   - State management: State mutations, reducers

2. Integration Tests (tests/test_langgraph/integration/)
   - Execution control: Rollback, branch, pause, single-node execution
   - Event integration: Event bus, streaming events
   - Audit integration: Audit trail, compliance
   - File processing: Transaction consistency, checkpoint-file links
   - Ray integration: Distributed execution
   - Venv integration: Environment management

SOLID Compliance:
=================
- Single Responsibility: Each test class focuses on one aspect
- Open/Closed: Tests use fixtures for extension
- Liskov Substitution: Mock implementations follow interfaces
- Interface Segregation: Tests verify specific interfaces
- Dependency Inversion: Tests inject dependencies via fixtures

ACID Compliance:
================
- Atomicity: Transaction tests verify all-or-nothing
- Consistency: State invariant tests
- Isolation: Thread safety and execution isolation tests
- Durability: Persistence tests with checkpoint stores
"""
