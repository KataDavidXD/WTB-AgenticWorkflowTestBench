"""
File Processing Test Suite.

Comprehensive tests for file version control integrated with WTB system.

Test Hierarchy:
├── unit/                          # Unit tests (isolated, no external deps)
│   ├── test_basic_operations.py   # Basic LangGraph file operations
│   └── test_execution_control.py  # Advanced: rollback, checkpoint, branch
│
└── integration/                    # Integration tests (cross-component)
    ├── test_file_event.py         # File + Event
    ├── test_file_audit.py         # File + LangGraph + Audit
    ├── test_file_event_audit.py   # File + Event + Audit
    ├── test_file_ray.py           # File + Event + Audit + Ray
    └── test_file_venv.py          # File + venv integration

Design Principles:
- SOLID: Tests follow Single Responsibility
- ACID: Transaction tests verify atomicity, consistency, isolation, durability
- Pattern: Consistent test structure across all test files

Run all tests:
    pytest tests/test_file_processing/ -v

Run unit tests only:
    pytest tests/test_file_processing/unit/ -v

Run integration tests only:
    pytest tests/test_file_processing/integration/ -v
"""
