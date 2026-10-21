"""Application-level lifecycle errors."""

from __future__ import annotations

from typing import Any


class PostCommitCleanupError(RuntimeError):
    """The transaction committed, but an owned resource failed to close.

    ``committed_outcome`` lets orchestration preserve the durable result while
    surfacing cleanup as an operator alert instead of a retryable execution
    failure.
    """

    def __init__(
        self,
        error: BaseException,
        *,
        committed_outcome: Any = None,
    ) -> None:
        super().__init__(f"Post-commit cleanup failed: {error}")
        self.cleanup_error = error
        self.committed_outcome = committed_outcome
