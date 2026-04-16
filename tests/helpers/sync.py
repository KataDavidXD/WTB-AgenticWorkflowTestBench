"""
Synchronization helpers for async/concurrent tests.

Replaces brittle time.sleep() calls with proper polling mechanisms.
Thread-safe and process-safe implementations.
"""

import time
from typing import Callable, TypeVar, Optional, Any

T = TypeVar("T")


class TimeoutError(Exception):
    """Raised when a condition is not met within the timeout period."""
    pass


def wait_for_condition(
    condition: Callable[[], bool],
    timeout: float = 5.0,
    poll_interval: float = 0.05,
    description: str = "condition",
) -> bool:
    """
    Poll until condition returns True or timeout expires.
    
    Args:
        condition: Callable that returns True when condition is met
        timeout: Maximum time to wait in seconds
        poll_interval: Time between polls in seconds
        description: Human-readable description for error messages
        
    Returns:
        True if condition was met
        
    Raises:
        TimeoutError: If timeout expires before condition is met
        
    Example:
        wait_for_condition(
            lambda: execution.status == ExecutionStatus.COMPLETED,
            timeout=10.0,
            description="execution completion"
        )
    """
    start_time = time.monotonic()
    last_exception = None
    
    while time.monotonic() - start_time < timeout:
        try:
            if condition():
                return True
        except Exception as e:
            last_exception = e
        time.sleep(poll_interval)
    
    elapsed = time.monotonic() - start_time
    error_msg = f"Timeout waiting for {description} after {elapsed:.2f}s"
    if last_exception:
        error_msg += f" (last error: {last_exception})"
    raise TimeoutError(error_msg)


def wait_for_status(
    get_status: Callable[[], Any],
    expected_status: Any,
    timeout: float = 5.0,
    poll_interval: float = 0.05,
) -> Any:
    """
    Poll until status matches expected value.
    
    Args:
        get_status: Callable that returns current status
        expected_status: Expected status value (can be single value or list)
        timeout: Maximum time to wait
        poll_interval: Time between polls
        
    Returns:
        The final status value
        
    Raises:
        TimeoutError: If timeout expires
        
    Example:
        final_status = wait_for_status(
            lambda: execution.status,
            [ExecutionStatus.COMPLETED, ExecutionStatus.FAILED],
            timeout=10.0
        )
    """
    expected_set = (
        set(expected_status) if isinstance(expected_status, (list, tuple, set))
        else {expected_status}
    )
    
    start_time = time.monotonic()
    current_status = None
    
    while time.monotonic() - start_time < timeout:
        current_status = get_status()
        if current_status in expected_set:
            return current_status
        time.sleep(poll_interval)
    
    elapsed = time.monotonic() - start_time
    raise TimeoutError(
        f"Status {current_status} did not reach {expected_set} after {elapsed:.2f}s"
    )


def poll_until(
    func: Callable[[], T],
    predicate: Callable[[T], bool],
    timeout: float = 5.0,
    poll_interval: float = 0.05,
    description: str = "result",
) -> T:
    """
    Poll function until predicate returns True on result.
    
    Args:
        func: Function to call repeatedly
        predicate: Function that takes result and returns True when done
        timeout: Maximum time to wait
        poll_interval: Time between polls
        description: Description for error message
        
    Returns:
        The final result that satisfied the predicate
        
    Raises:
        TimeoutError: If timeout expires
        
    Example:
        result = poll_until(
            lambda: api.get_job_status(job_id),
            lambda r: r.state in ["completed", "failed"],
            timeout=30.0,
            description="job completion"
        )
    """
    start_time = time.monotonic()
    last_result = None
    last_exception = None
    
    while time.monotonic() - start_time < timeout:
        try:
            last_result = func()
            if predicate(last_result):
                return last_result
        except Exception as e:
            last_exception = e
        time.sleep(poll_interval)
    
    elapsed = time.monotonic() - start_time
    error_msg = f"Timeout waiting for {description} after {elapsed:.2f}s"
    if last_result is not None:
        error_msg += f" (last result: {last_result})"
    if last_exception:
        error_msg += f" (last error: {last_exception})"
    raise TimeoutError(error_msg)
