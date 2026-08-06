"""
Workspace Infrastructure - Isolated execution environment management.

Provides workspace creation, activation, cleanup, and orphan detection
for parallel variant execution in WTB.

Components:
- WorkspaceManager: Central service for workspace lifecycle
- create_file_link: Hard link with cross-partition fallback

Related Documents:
- docs/issues/WORKSPACE_ISOLATION_DESIGN.md
"""

from .manager import (
    WorkspaceCleanupError,
    WorkspaceCreationError,
    WorkspaceManager,
    WorkspaceManagerError,
    WorkspaceNotFoundError,
    create_file_link,
)

__all__ = [
    "WorkspaceManager",
    "WorkspaceManagerError",
    "WorkspaceNotFoundError",
    "WorkspaceCreationError",
    "WorkspaceCleanupError",
    "create_file_link",
]
