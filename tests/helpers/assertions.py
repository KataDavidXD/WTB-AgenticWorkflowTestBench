"""
Enhanced assertions with contextual error messages.

Provides domain-specific assertions that give better error context
than generic pytest assertions.
"""

from typing import Any, Optional, List, Set
from wtb.domain.models import ExecutionStatus


class AssertionError(Exception):
    """Enhanced assertion error with context."""
    pass


def assert_execution_completed(
    execution: Any,
    expected_path: Optional[List[str]] = None,
    expected_variables: Optional[dict] = None,
) -> None:
    """
    Assert that execution completed successfully with optional state checks.
    
    Args:
        execution: Execution object to check
        expected_path: Optional list of nodes that should be in execution path
        expected_variables: Optional dict of variables to verify
        
    Raises:
        AssertionError: With detailed context if assertion fails
    """
    if execution.status != ExecutionStatus.COMPLETED:
        raise AssertionError(
            f"Expected execution to complete, but status is {execution.status.value}.\n"
            f"  Execution ID: {execution.id}\n"
            f"  Current Node: {execution.state.current_node_id}\n"
            f"  Error: {execution.error_message or 'None'}\n"
            f"  Error Node: {execution.error_node_id or 'None'}\n"
            f"  Path: {execution.state.execution_path}"
        )
    
    if expected_path:
        missing = set(expected_path) - set(execution.state.execution_path)
        if missing:
            raise AssertionError(
                f"Execution completed but path is incomplete.\n"
                f"  Missing nodes: {missing}\n"
                f"  Actual path: {execution.state.execution_path}"
            )
    
    if expected_variables:
        for key, expected_value in expected_variables.items():
            actual_value = execution.state.workflow_variables.get(key)
            if actual_value != expected_value:
                raise AssertionError(
                    f"Execution completed but variable '{key}' mismatch.\n"
                    f"  Expected: {expected_value}\n"
                    f"  Actual: {actual_value}"
                )


def assert_execution_failed(
    execution: Any,
    expected_error_contains: Optional[str] = None,
    expected_error_node: Optional[str] = None,
) -> None:
    """
    Assert that execution failed with optional error checks.
    
    Args:
        execution: Execution object to check
        expected_error_contains: Optional substring that should be in error message
        expected_error_node: Optional node ID where error should have occurred
        
    Raises:
        AssertionError: With detailed context if assertion fails
    """
    if execution.status != ExecutionStatus.FAILED:
        raise AssertionError(
            f"Expected execution to fail, but status is {execution.status.value}.\n"
            f"  Execution ID: {execution.id}\n"
            f"  Path: {execution.state.execution_path}"
        )
    
    if expected_error_contains and execution.error_message:
        if expected_error_contains not in execution.error_message:
            raise AssertionError(
                f"Error message does not contain expected substring.\n"
                f"  Expected to contain: '{expected_error_contains}'\n"
                f"  Actual message: '{execution.error_message}'"
            )
    
    if expected_error_node and execution.error_node_id != expected_error_node:
        raise AssertionError(
            f"Error occurred at unexpected node.\n"
            f"  Expected: {expected_error_node}\n"
            f"  Actual: {execution.error_node_id}"
        )


def assert_execution_paused(
    execution: Any,
    expected_node: Optional[str] = None,
    expected_breakpoint_hit: bool = False,
) -> None:
    """
    Assert that execution is paused with optional checks.
    
    Args:
        execution: Execution object to check
        expected_node: Optional node ID where execution should be paused
        expected_breakpoint_hit: If True, verify breakpoint was removed (one-shot)
        
    Raises:
        AssertionError: With detailed context if assertion fails
    """
    if execution.status != ExecutionStatus.PAUSED:
        raise AssertionError(
            f"Expected execution to be paused, but status is {execution.status.value}.\n"
            f"  Execution ID: {execution.id}\n"
            f"  Current Node: {execution.state.current_node_id}"
        )
    
    if expected_node and execution.state.current_node_id != expected_node:
        raise AssertionError(
            f"Execution paused at unexpected node.\n"
            f"  Expected: {expected_node}\n"
            f"  Actual: {execution.state.current_node_id}"
        )
    
    if expected_breakpoint_hit and expected_node:
        if expected_node in execution.breakpoints:
            raise AssertionError(
                f"Breakpoint was not removed after triggering (one-shot violation).\n"
                f"  Node: {expected_node}\n"
                f"  Remaining breakpoints: {execution.breakpoints}"
            )


def assert_checkpoints_exist(
    checkpoints: List[Any],
    min_count: int = 1,
    expected_nodes: Optional[Set[str]] = None,
) -> None:
    """
    Assert checkpoints exist with optional content checks.
    
    Args:
        checkpoints: List of checkpoint objects
        min_count: Minimum number of checkpoints expected
        expected_nodes: Optional set of nodes that should have checkpoints
        
    Raises:
        AssertionError: With detailed context if assertion fails
    """
    if len(checkpoints) < min_count:
        raise AssertionError(
            f"Insufficient checkpoints.\n"
            f"  Expected at least: {min_count}\n"
            f"  Actual count: {len(checkpoints)}"
        )
    
    if expected_nodes:
        checkpoint_nodes = {cp.node_id for cp in checkpoints if hasattr(cp, 'node_id')}
        missing = expected_nodes - checkpoint_nodes
        if missing:
            raise AssertionError(
                f"Missing checkpoints for nodes.\n"
                f"  Missing: {missing}\n"
                f"  Found checkpoints for: {checkpoint_nodes}"
            )
