"""
Outbox Transaction Consistency Test Suite.

Comprehensive integration tests for transaction consistency across:
- Ray (distributed batch execution)
- venv (virtual environment management)
- LangGraph (workflow orchestration)
- FileTracker (file system state management)

Test Categories:
- Pause/Resume: Transaction consistency during workflow pause and resume
- Update Operations: Node and workflow updates with transactional integrity
- Branching/Rollback: Branch creation and state rollback consistency
- Batch/Parallel: Batch testing and parallel execution scenarios
- Cross-System ACID: Full ACID compliance across all integrated systems

Architecture Reference:
- docs/Project_Init/WORKFLOW_TEST_BENCH_ARCHITECTURE.md
- docs/issues/WORKSPACE_ISOLATION_DESIGN.md
- docs/LangGraph/WTB_INTEGRATION.md

Run all tests:
    pytest tests/test_outbox_transaction_consistency/ -v

Run specific category:
    pytest tests/test_outbox_transaction_consistency/test_pause_resume.py -v
"""
