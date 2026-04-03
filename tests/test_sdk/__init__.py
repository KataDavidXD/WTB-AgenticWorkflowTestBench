"""
SDK Integration Tests Package.

Comprehensive tests for WTB SDK integration with:
- Ray distributed computing
- LangGraph workflow orchestration  
- Virtual environment management (uv_venv_manager)
- File system tracking

Test Categories:
1. Unit tests: Mocked dependencies
2. Integration tests: Real components working together
3. ACID compliance tests: Transaction guarantees

Run all SDK tests:
    pytest tests/test_sdk/ -v

Run specific test categories:
    pytest tests/test_sdk/ -k "langgraph" -v
    pytest tests/test_sdk/ -k "ray" -v
    pytest tests/test_sdk/ -k "venv" -v
    pytest tests/test_sdk/ -k "file_tracking" -v
"""
