"""
Input Validators for API Services.

Provides validation functions for API input parameters.
Follows SOLID principles - Single Responsibility for validation.

Created: 2026-01-28
Reference: CONSOLIDATED_ISSUES.md - ISSUE-API-003

Design:
- Pure functions, no side effects
- Raise ValueError with descriptive messages
- UUID validation for execution_id, checkpoint_id, etc.
"""

import re
import uuid
from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════════════════════
# UUID Validators
# ═══════════════════════════════════════════════════════════════════════════════


UUID_PATTERN = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE
)


def validate_uuid(value: str, field_name: str = "id", strict: bool = False) -> str:
    """
    Validate that a value is a valid ID string.
    
    Args:
        value: Value to validate
        field_name: Name of the field for error messages
        strict: If True, require UUID format. If False, accept any non-empty string.
        
    Returns:
        The validated ID string
        
    Raises:
        ValueError: If value is empty or invalid
        
    Note:
        By default (strict=False), accepts any non-empty string to support:
        - LangGraph checkpoint IDs (various formats)
        - Test fixtures with simple IDs like 'exec-123'
        - Custom ID schemes
    """
    if not value:
        raise ValueError(f"{field_name} is required")
    
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    
    # Accept both with and without dashes
    normalized = value.strip()
    
    if not normalized:
        raise ValueError(f"{field_name} cannot be empty")
    
    if strict:
        # Try to parse as UUID to validate
        try:
            parsed = uuid.UUID(normalized)
            return str(parsed)
        except (ValueError, AttributeError):
            raise ValueError(f"Invalid {field_name}: '{value}' is not a valid UUID format")
    
    # Non-strict mode: accept any non-empty string
    return normalized


def validate_execution_id(execution_id: str, strict: bool = False) -> str:
    """
    Validate execution ID.
    
    Args:
        execution_id: The execution ID to validate
        strict: If True, require UUID format
        
    Returns:
        Validated execution ID string
    """
    return validate_uuid(execution_id, "execution_id", strict=strict)


def validate_checkpoint_id(checkpoint_id: str, strict: bool = False) -> str:
    """
    Validate checkpoint ID.
    
    Note: LangGraph uses various checkpoint ID formats, so strict=False by default.
    """
    return validate_uuid(checkpoint_id, "checkpoint_id", strict=strict)


def validate_workflow_id(workflow_id: str, strict: bool = False) -> str:
    """Validate workflow ID."""
    return validate_uuid(workflow_id, "workflow_id", strict=strict)


def validate_batch_test_id(batch_test_id: str, strict: bool = False) -> str:
    """Validate batch test ID."""
    return validate_uuid(batch_test_id, "batch_test_id", strict=strict)


# ═══════════════════════════════════════════════════════════════════════════════
# String Validators
# ═══════════════════════════════════════════════════════════════════════════════


def validate_string(
    value: str,
    field_name: str,
    min_length: int = 0,
    max_length: int = 10000,
    required: bool = True,
) -> Optional[str]:
    """
    Validate a string value.
    
    Args:
        value: Value to validate
        field_name: Name of the field for error messages
        min_length: Minimum required length
        max_length: Maximum allowed length
        required: Whether the field is required
        
    Returns:
        The validated string or None if not required and empty
        
    Raises:
        ValueError: If validation fails
    """
    if value is None:
        if required:
            raise ValueError(f"{field_name} is required")
        return None
    
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    
    stripped = value.strip()
    
    if not stripped and required:
        raise ValueError(f"{field_name} cannot be empty")
    
    if len(stripped) < min_length:
        raise ValueError(f"{field_name} must be at least {min_length} characters")
    
    if len(stripped) > max_length:
        raise ValueError(f"{field_name} exceeds maximum length of {max_length} characters")
    
    return stripped if stripped else None


def validate_node_id(node_id: str, required: bool = True) -> Optional[str]:
    """Validate node ID."""
    return validate_string(node_id, "node_id", min_length=1, max_length=256, required=required)


def validate_reason(reason: str, required: bool = False) -> Optional[str]:
    """Validate reason string."""
    return validate_string(reason, "reason", max_length=1000, required=required)


# ═══════════════════════════════════════════════════════════════════════════════
# Pagination Validators
# ═══════════════════════════════════════════════════════════════════════════════


def validate_limit(limit: int, max_limit: int = 1000) -> int:
    """
    Validate pagination limit.
    
    Args:
        limit: Limit value to validate
        max_limit: Maximum allowed limit
        
    Returns:
        Validated limit value
        
    Raises:
        ValueError: If limit is invalid
    """
    if not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    
    if limit < 1:
        raise ValueError("limit must be at least 1")
    
    if limit > max_limit:
        raise ValueError(f"limit cannot exceed {max_limit}")
    
    return limit


def validate_offset(offset: int) -> int:
    """
    Validate pagination offset.
    
    Args:
        offset: Offset value to validate
        
    Returns:
        Validated offset value
        
    Raises:
        ValueError: If offset is invalid
    """
    if not isinstance(offset, int):
        raise ValueError("offset must be an integer")
    
    if offset < 0:
        raise ValueError("offset cannot be negative")
    
    return offset


# ═══════════════════════════════════════════════════════════════════════════════
# State Validators
# ═══════════════════════════════════════════════════════════════════════════════


def validate_state_changes(changes: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate state changes dictionary.
    
    Args:
        changes: Dictionary of state changes
        
    Returns:
        Validated changes dictionary
        
    Raises:
        ValueError: If changes are invalid
    """
    if not changes:
        raise ValueError("changes cannot be empty")
    
    if not isinstance(changes, dict):
        raise ValueError("changes must be a dictionary")
    
    # Validate key names
    for key in changes.keys():
        if not isinstance(key, str):
            raise ValueError(f"State key must be a string, got {type(key).__name__}")
        if not key.strip():
            raise ValueError("State keys cannot be empty")
        if len(key) > 256:
            raise ValueError(f"State key '{key[:50]}...' exceeds maximum length")
    
    return changes


def validate_status(status: str, allowed_statuses: List[str]) -> str:
    """
    Validate status string against allowed values.
    
    Args:
        status: Status to validate
        allowed_statuses: List of allowed status values
        
    Returns:
        Validated status string
        
    Raises:
        ValueError: If status is invalid
    """
    if not status:
        raise ValueError("status is required")
    
    if status not in allowed_statuses:
        raise ValueError(
            f"Invalid status '{status}'. Allowed: {', '.join(allowed_statuses)}"
        )
    
    return status


# ═══════════════════════════════════════════════════════════════════════════════
# Composite Validators
# ═══════════════════════════════════════════════════════════════════════════════


class ValidationError(ValueError):
    """
    Custom validation error with field information.
    
    Attributes:
        field: The field that failed validation
        message: Error message
        value: The invalid value (optional)
    """
    
    def __init__(self, message: str, field: str = None, value: Any = None):
        super().__init__(message)
        self.field = field
        self.message = message
        self.value = value


def validate_execution_request(
    execution_id: str,
    checkpoint_id: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Validate common execution request parameters.
    
    Args:
        execution_id: Execution ID
        checkpoint_id: Optional checkpoint ID
        reason: Optional reason string
        
    Returns:
        Dictionary with validated parameters
        
    Raises:
        ValidationError: If any parameter is invalid
    """
    try:
        result = {
            "execution_id": validate_execution_id(execution_id),
        }
        
        if checkpoint_id is not None:
            result["checkpoint_id"] = validate_checkpoint_id(checkpoint_id)
        
        if reason is not None:
            result["reason"] = validate_reason(reason)
        
        return result
    
    except ValueError as e:
        raise ValidationError(str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# Idempotency Key Validation
# ═══════════════════════════════════════════════════════════════════════════════


def validate_idempotency_key(key: Optional[str]) -> Optional[str]:
    """
    Validate and normalize idempotency key.
    
    DESIGN DECISION (ISSUE-OB-002):
    Idempotency keys MUST be client-provided for safe retries.
    The server does NOT auto-generate keys because:
    - Auto-generated keys defeat the purpose of idempotency
    - Each call would get a new key, making deduplication impossible
    - Client retries need the SAME key to detect duplicates
    
    Usage Pattern:
        # Client generates unique key for this request
        response = await api.pause_execution(
            execution_id="...",
            idempotency_key="client-request-12345"  # Client provides
        )
        
        # On network failure, client retries with SAME key
        response = await api.pause_execution(
            execution_id="...",
            idempotency_key="client-request-12345"  # Same key = deduplicated
        )
    
    Args:
        key: Idempotency key to validate (client-provided, optional)
        
    Returns:
        Normalized idempotency key or None (no deduplication if None)
        
    Raises:
        ValueError: If key format is invalid
    """
    if key is None:
        return None
    
    if not isinstance(key, str):
        raise ValueError("idempotency_key must be a string")
    
    key = key.strip()
    
    if not key:
        return None
    
    if len(key) > 256:
        raise ValueError("idempotency_key exceeds maximum length of 256 characters")
    
    # Allow UUID format or custom format
    if UUID_PATTERN.match(key):
        return key.lower()
    
    # For custom formats, ensure alphanumeric with limited special chars
    if not re.match(r'^[a-zA-Z0-9_\-:.]+$', key):
        raise ValueError(
            "idempotency_key must contain only alphanumeric characters, "
            "underscores, hyphens, colons, and periods"
        )
    
    return key


__all__ = [
    # UUID validators
    "validate_uuid",
    "validate_execution_id",
    "validate_checkpoint_id",
    "validate_workflow_id",
    "validate_batch_test_id",
    # String validators
    "validate_string",
    "validate_node_id",
    "validate_reason",
    # Pagination validators
    "validate_limit",
    "validate_offset",
    # State validators
    "validate_state_changes",
    "validate_status",
    # Composite validators
    "ValidationError",
    "validate_execution_request",
    # Idempotency (client-provided only)
    "validate_idempotency_key",
]
