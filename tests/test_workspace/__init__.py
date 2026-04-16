"""
Workspace Isolation Integration Tests.

This package contains comprehensive integration tests for workspace isolation
across all major WTB subsystems:

- Ray (distributed execution)
- LangGraph (checkpointing, time-travel)
- Venv (virtual environment isolation)
- FileSystem (FileTracker integration)

Test Categories:
================
1. test_real_ray_workspace.py - Real Ray cluster workspace tests
2. test_langgraph_workspace.py - LangGraph checkpointing with workspace
3. test_venv_workspace.py - Virtual environment isolation tests
4. test_filesystem_workspace.py - FileTracker workspace integration
5. test_full_integration.py - Complete system integration tests

Design Reference:
=================
docs/issues/WORKSPACE_ISOLATION_DESIGN.md

Test Matrix:
============
| Feature           | Ray | LangGraph | Venv | FileSystem | Combined |
|-------------------|-----|-----------|------|------------|----------|
| Pause/Resume      | ✓   | ✓         | -    | ✓          | ✓        |
| Update Node       | ✓   | ✓         | ✓    | ✓          | ✓        |
| Update Workflow   | ✓   | ✓         | ✓    | ✓          | ✓        |
| Branching         | ✓   | ✓         | ✓    | ✓          | ✓        |
| Rolling Back      | ✓   | ✓         | ✓    | ✓          | ✓        |
| Batch Testing     | ✓   | ✓         | ✓    | ✓          | ✓        |
"""
