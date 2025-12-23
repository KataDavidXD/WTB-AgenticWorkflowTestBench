"""
AgentGit State Adapter Implementation.

Anti-corruption layer between WTB and AgentGit.
Uses AgentGit's raw SQLite repositories directly.
Uses WTB's SQLAlchemy UoW for WTB-owned tables (node_boundaries, checkpoint_files).

Design Philosophy:
==================
"WTB uses dual database patterns: AgentGit repositories for checkpoint state;
SQLAlchemy UoW for WTB domain data. Never mix - adapters bridge the boundary."

"Single Checkpoint Type + Node Boundary Pointers: All checkpoints atomic at
tool/message level. Node boundaries are POINTERS (entry_cp_id, exit_cp_id),
NOT separate checkpoints."
"""

from typing import Optional, Dict, Any, List
from datetime import datetime
from copy import deepcopy
import uuid

# AgentGit imports (external package, unchanged)
from agentgit.checkpoints.checkpoint import Checkpoint
from agentgit.sessions.internal_session import InternalSession
from agentgit.sessions.internal_session_mdp import InternalSession_mdp
from agentgit.sessions.external_session import ExternalSession
from agentgit.database.repositories.checkpoint_repository import CheckpointRepository
from agentgit.database.repositories.internal_session_repository import InternalSessionRepository
from agentgit.database.repositories.external_session_repository import ExternalSessionRepository
from agentgit.managers.tool_manager import ToolManager

# WTB imports
from wtb.domain.interfaces.state_adapter import (
    IStateAdapter,
    CheckpointTrigger,
    CheckpointInfo,
    NodeBoundaryInfo,
)
from wtb.domain.models import ExecutionState, NodeBoundary, CheckpointFile
from wtb.infrastructure.database.unit_of_work import SQLAlchemyUnitOfWork
from wtb.infrastructure.database.config import get_database_config


class AgentGitStateAdapter(IStateAdapter):
    """
    Production implementation of IStateAdapter using AgentGit.
    
    Architecture:
    - Uses AgentGit's CheckpointRepository/InternalSessionRepository directly
    - Uses WTB's SQLAlchemyUnitOfWork for node_boundaries and checkpoint_files
    - Tool rollback via AgentGit's ToolManager
    
    Usage:
        adapter = AgentGitStateAdapter(
            agentgit_db_path="data/agentgit.db",
            wtb_db_url="sqlite:///data/wtb.db"
        )
        
        session_id = adapter.initialize_session(execution_id, initial_state)
        checkpoint_id = adapter.save_checkpoint(state, node_id, trigger)
        restored = adapter.rollback(checkpoint_id)
    """
    
    def __init__(
        self,
        agentgit_db_path: Optional[str] = None,
        wtb_db_url: Optional[str] = None,
        external_session_id: Optional[int] = None,
        tools: Optional[List] = None,
    ):
        """
        Initialize the adapter.
        
        Args:
            agentgit_db_path: Path to agentgit.db (uses config default if None)
            wtb_db_url: SQLAlchemy URL for wtb.db (uses config default if None)
            external_session_id: Optional external session ID (creates if None)
            tools: Optional list of tools for ToolManager
        """
        # Get default paths from config if not provided
        config = get_database_config()
        self._agentgit_db_path = agentgit_db_path or str(config.agentgit_db_path)
        self._wtb_db_url = wtb_db_url or config.wtb_db_url
        
        # Initialize AgentGit repositories (raw SQLite)
        self._checkpoint_repo = CheckpointRepository(self._agentgit_db_path)
        self._session_repo = InternalSessionRepository(self._agentgit_db_path)
        self._external_session_repo = ExternalSessionRepository(self._agentgit_db_path)
        
        # Initialize tool manager
        self._tool_manager = ToolManager(tools=tools or [])
        
        # Session tracking
        self._external_session_id = external_session_id
        self._current_session: Optional[InternalSession_mdp] = None
        self._current_execution_id: Optional[str] = None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Session Management
    # ═══════════════════════════════════════════════════════════════════════════
    
    def initialize_session(
        self, 
        execution_id: str,
        initial_state: ExecutionState
    ) -> Optional[int]:
        """Initialize a new AgentGit session for this execution."""
        # Ensure external session exists
        if self._external_session_id is None:
            self._external_session_id = self._ensure_external_session()
        
        # Create InternalSession_mdp
        langgraph_id = f"wtb_{execution_id}_{uuid.uuid4().hex[:8]}"
        
        session = InternalSession_mdp(
            id=None,
            external_session_id=self._external_session_id,
            langgraph_session_id=langgraph_id,
            session_state=initial_state.to_dict(),
            conversation_history=[],
            created_at=datetime.now(),
            is_current=True,
            checkpoint_count=0,
            parent_session_id=None,
            branch_point_checkpoint_id=None,
            tool_invocation_count=0,
            metadata={
                "wtb_execution_id": execution_id,
                "session_type": "wtb_workflow",
                "created_at": datetime.now().isoformat(),
            },
            # MDP extensions
            current_node_id=initial_state.current_node_id,
            workflow_variables=dict(initial_state.workflow_variables),
            execution_path=list(initial_state.execution_path),
        )
        
        # Persist via AgentGit repository
        saved_session = self._session_repo.create(session)
        
        self._current_session = saved_session
        self._current_execution_id = execution_id
        self._tool_manager.clear_track()
        
        return saved_session.id
    
    def _ensure_external_session(self) -> int:
        """Ensure an external session exists for WTB, create if needed."""
        # Check for existing WTB external session
        sessions = self._external_session_repo.get_by_user(user_id=1)
        for session in sessions:
            if session.session_name == "WTB_Session" and session.is_active:
                return session.id
        
        # Create new external session
        external = ExternalSession(
            id=None,
            user_id=1,
            session_name="WTB_Session",
            created_at=datetime.now(),
            is_active=True,
            metadata={"type": "wtb_workflow_container"}
        )
        saved = self._external_session_repo.create(external)
        return saved.id
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Checkpoint Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    def save_checkpoint(
        self,
        state: ExecutionState,
        node_id: str,
        trigger: CheckpointTrigger,
        tool_name: Optional[str] = None,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> int:
        """Save a checkpoint using AgentGit."""
        if not self._current_session:
            raise RuntimeError("No active session. Call initialize_session first.")
        
        # Update session state
        self._current_session.current_node_id = node_id
        self._current_session.workflow_variables = dict(state.workflow_variables)
        self._current_session.execution_path = list(state.execution_path)
        self._current_session.session_state = state.to_dict()
        
        # Update session in database
        self._session_repo.update(self._current_session)
        
        # Build checkpoint metadata
        tool_position = self._tool_manager.get_tool_track_position()
        cp_metadata = metadata or {}
        cp_metadata.update({
            "trigger_type": trigger.value,
            "node_id": node_id,
            "tool_name": tool_name,
            "tool_track_position": tool_position,
            "wtb_execution_id": self._current_execution_id,
            "mdp_state": {
                "current_node_id": state.current_node_id,
                "workflow_variables": state.workflow_variables,
                "execution_path": state.execution_path,
            }
        })
        
        # Create checkpoint
        checkpoint = Checkpoint(
            id=None,
            internal_session_id=self._current_session.id,
            checkpoint_name=name,
            session_state=state.to_dict(),
            is_auto=(trigger != CheckpointTrigger.USER_REQUEST),
            created_at=datetime.now(),
            user_id=None,
            metadata=cp_metadata,
            tool_invocations=[inv.to_dict() for inv in self._tool_manager.get_tool_track()],
        )
        
        saved_cp = self._checkpoint_repo.create(checkpoint)
        
        # Update session checkpoint count
        self._current_session.checkpoint_count = (self._current_session.checkpoint_count or 0) + 1
        self._session_repo.update(self._current_session)
        
        return saved_cp.id
    
    def load_checkpoint(self, checkpoint_id: int) -> ExecutionState:
        """Load checkpoint and return ExecutionState."""
        checkpoint = self._checkpoint_repo.get_by_id(checkpoint_id)
        if not checkpoint:
            raise ValueError(f"Checkpoint {checkpoint_id} not found")
        
        return self._checkpoint_to_execution_state(checkpoint)
    
    def _checkpoint_to_execution_state(self, checkpoint: Checkpoint) -> ExecutionState:
        """Convert AgentGit Checkpoint to WTB ExecutionState."""
        # Try to get MDP state from metadata first
        mdp_state = checkpoint.metadata.get("mdp_state", {}) if checkpoint.metadata else {}
        
        # Fall back to session_state if mdp_state not available
        session_state = checkpoint.session_state or {}
        
        return ExecutionState(
            current_node_id=mdp_state.get("current_node_id") or session_state.get("current_node_id"),
            workflow_variables=mdp_state.get("workflow_variables", session_state.get("workflow_variables", {})),
            execution_path=mdp_state.get("execution_path", session_state.get("execution_path", [])),
            node_results=session_state.get("node_results", {}),
        )
    
    def link_file_commit(
        self, 
        checkpoint_id: int, 
        file_commit_id: str,
        file_count: int = 0,
        total_size_bytes: int = 0
    ) -> bool:
        """Link FileTracker commit to checkpoint (stored in WTB database)."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            file_link = CheckpointFile(
                checkpoint_id=checkpoint_id,
                file_commit_id=file_commit_id,
                file_count=file_count,
                total_size_bytes=total_size_bytes,
            )
            uow.checkpoint_files.add(file_link)
            uow.commit()
            return True
    
    def get_file_commit(self, checkpoint_id: int) -> Optional[str]:
        """Get linked file commit ID from WTB database."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            file_link = uow.checkpoint_files.find_by_checkpoint(checkpoint_id)
            return file_link.file_commit_id if file_link else None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Node Boundary Operations (WTB Database)
    # ═══════════════════════════════════════════════════════════════════════════
    
    def mark_node_started(self, node_id: str, entry_checkpoint_id: int) -> int:
        """Mark node as started (stored in WTB database)."""
        if not self._current_session:
            raise RuntimeError("No active session")
        
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            boundary = NodeBoundary(
                execution_id=self._current_execution_id or "",
                internal_session_id=self._current_session.id,
                node_id=node_id,
                entry_checkpoint_id=entry_checkpoint_id,
                node_status="started",
            )
            saved = uow.node_boundaries.add(boundary)
            uow.commit()
            return saved.id
    
    def mark_node_completed(
        self, 
        node_id: str, 
        exit_checkpoint_id: int,
        tool_count: int = 0,
        checkpoint_count: int = 0
    ) -> bool:
        """Mark node as completed (update in WTB database)."""
        if not self._current_session:
            return False
        
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            boundary = uow.node_boundaries.find_by_node(
                self._current_session.id, node_id
            )
            if boundary:
                boundary.exit_checkpoint_id = exit_checkpoint_id
                boundary.node_status = "completed"
                boundary.completed_at = datetime.now()
                boundary.tool_count = tool_count
                boundary.checkpoint_count = checkpoint_count
                uow.node_boundaries.update(boundary)
                uow.commit()
                return True
            return False
    
    def mark_node_failed(self, node_id: str, error_message: str) -> bool:
        """Mark node as failed (update in WTB database)."""
        if not self._current_session:
            return False
        
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            boundary = uow.node_boundaries.find_by_node(
                self._current_session.id, node_id
            )
            if boundary:
                boundary.node_status = "failed"
                boundary.error_message = error_message
                boundary.completed_at = datetime.now()
                uow.node_boundaries.update(boundary)
                uow.commit()
                return True
            return False
    
    def get_node_boundaries(self, session_id: int) -> List[NodeBoundaryInfo]:
        """Get all node boundaries from WTB database."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            boundaries = uow.node_boundaries.find_by_session(session_id)
            return [self._boundary_to_info(b) for b in boundaries]
    
    def get_node_boundary(self, session_id: int, node_id: str) -> Optional[NodeBoundaryInfo]:
        """Get specific node boundary from WTB database."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            boundary = uow.node_boundaries.find_by_node(session_id, node_id)
            return self._boundary_to_info(boundary) if boundary else None
    
    def _boundary_to_info(self, boundary: NodeBoundary) -> NodeBoundaryInfo:
        """Convert NodeBoundary domain model to NodeBoundaryInfo."""
        started_at = boundary.started_at
        if isinstance(started_at, datetime):
            started_at = started_at.isoformat()
        
        completed_at = boundary.completed_at
        if isinstance(completed_at, datetime):
            completed_at = completed_at.isoformat()
        
        return NodeBoundaryInfo(
            id=boundary.id or 0,
            node_id=boundary.node_id,
            entry_checkpoint_id=boundary.entry_checkpoint_id or 0,
            exit_checkpoint_id=boundary.exit_checkpoint_id or 0,
            node_status=boundary.node_status,
            tool_count=boundary.tool_count,
            checkpoint_count=boundary.checkpoint_count,
            started_at=started_at or "",
            completed_at=completed_at,
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Rollback & Branching
    # ═══════════════════════════════════════════════════════════════════════════
    
    def rollback(self, to_checkpoint_id: int) -> ExecutionState:
        """
        Unified rollback to a checkpoint.
        
        Steps:
        1. Load checkpoint from AgentGit
        2. Reverse tools after checkpoint's tool_track_position
        3. Restore files if linked
        4. Return ExecutionState from checkpoint
        """
        checkpoint = self._checkpoint_repo.get_by_id(to_checkpoint_id)
        if not checkpoint:
            raise ValueError(f"Checkpoint {to_checkpoint_id} not found")
        
        # Get tool track position from checkpoint metadata
        target_position = 0
        if checkpoint.metadata:
            target_position = checkpoint.metadata.get("tool_track_position", 0)
        
        # Rollback tools from current position to target
        current_position = self._tool_manager.get_tool_track_position()
        if current_position > target_position:
            self._tool_manager.rollback_to_position(target_position)
        
        # Restore files if linked
        file_commit_id = self.get_file_commit(to_checkpoint_id)
        if file_commit_id:
            # TODO: Integrate with FileTracker when available
            # self._file_tracker.restore_commit(file_commit_id)
            pass
        
        # Restore tool track from checkpoint
        if checkpoint.tool_invocations:
            self._tool_manager.restore_track_from_list(checkpoint.tool_invocations)
        
        return self._checkpoint_to_execution_state(checkpoint)
    
    def create_branch(self, from_checkpoint_id: int) -> int:
        """Create a new branch from a checkpoint."""
        checkpoint = self._checkpoint_repo.get_by_id(from_checkpoint_id)
        if not checkpoint:
            raise ValueError(f"Checkpoint {from_checkpoint_id} not found")
        
        if not self._current_session:
            raise RuntimeError("No active session")
        
        # Create branched session
        branch_id = f"wtb_branch_{uuid.uuid4().hex[:8]}"
        
        branched = InternalSession_mdp(
            id=None,
            external_session_id=self._external_session_id,
            langgraph_session_id=branch_id,
            session_state=checkpoint.session_state,
            conversation_history=[],
            created_at=datetime.now(),
            is_current=True,
            checkpoint_count=0,
            parent_session_id=self._current_session.id,
            branch_point_checkpoint_id=from_checkpoint_id,
            tool_invocation_count=0,
            metadata={
                "wtb_execution_id": self._current_execution_id,
                "session_type": "wtb_branch",
                "branched_from_checkpoint": from_checkpoint_id,
                "created_at": datetime.now().isoformat(),
            },
        )
        
        # Extract MDP state from checkpoint
        if checkpoint.metadata and "mdp_state" in checkpoint.metadata:
            mdp = checkpoint.metadata["mdp_state"]
            branched.current_node_id = mdp.get("current_node_id")
            branched.workflow_variables = mdp.get("workflow_variables", {})
            branched.execution_path = mdp.get("execution_path", [])
        
        saved = self._session_repo.create(branched)
        self._current_session = saved
        
        # Restore tool track from checkpoint
        if checkpoint.tool_invocations:
            self._tool_manager.restore_track_from_list(checkpoint.tool_invocations)
        
        return saved.id
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Query Operations
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_checkpoints(
        self, 
        session_id: int,
        node_id: Optional[str] = None
    ) -> List[CheckpointInfo]:
        """Get checkpoints from AgentGit."""
        checkpoints = self._checkpoint_repo.get_by_internal_session(session_id)
        
        result = []
        for cp in checkpoints:
            cp_node_id = None
            if cp.metadata:
                cp_node_id = cp.metadata.get("node_id")
            
            if node_id and cp_node_id != node_id:
                continue
            
            result.append(self._checkpoint_to_info(cp))
        
        return sorted(result, key=lambda c: c.tool_track_position)
    
    def get_node_rollback_targets(self, session_id: int) -> List[CheckpointInfo]:
        """Get exit checkpoints of completed nodes."""
        with SQLAlchemyUnitOfWork(self._wtb_db_url) as uow:
            boundaries = uow.node_boundaries.find_completed(session_id)
            
            targets = []
            for b in boundaries:
                if b.exit_checkpoint_id:
                    cp = self._checkpoint_repo.get_by_id(b.exit_checkpoint_id)
                    if cp:
                        info = self._checkpoint_to_info(cp)
                        # Override name to show node info
                        info = CheckpointInfo(
                            id=info.id,
                            name=f"Node: {b.node_id}",
                            node_id=b.node_id,
                            tool_track_position=info.tool_track_position,
                            trigger_type=info.trigger_type,
                            created_at=info.created_at,
                            is_auto=info.is_auto,
                            has_file_commit=info.has_file_commit,
                        )
                        targets.append(info)
            return targets
    
    def get_checkpoints_in_node(self, session_id: int, node_id: str) -> List[CheckpointInfo]:
        """Get all checkpoints within a node."""
        return self.get_checkpoints(session_id, node_id)
    
    def _checkpoint_to_info(self, checkpoint: Checkpoint) -> CheckpointInfo:
        """Convert AgentGit Checkpoint to CheckpointInfo."""
        trigger_str = "auto"
        node_id = None
        tool_track_pos = 0
        
        if checkpoint.metadata:
            trigger_str = checkpoint.metadata.get("trigger_type", "auto")
            node_id = checkpoint.metadata.get("node_id")
            tool_track_pos = checkpoint.metadata.get("tool_track_position", 0)
        
        try:
            trigger = CheckpointTrigger(trigger_str)
        except ValueError:
            trigger = CheckpointTrigger.AUTO
        
        created_at = checkpoint.created_at
        if isinstance(created_at, datetime):
            created_at = created_at.isoformat()
        
        return CheckpointInfo(
            id=checkpoint.id,
            name=checkpoint.checkpoint_name,
            node_id=node_id,
            tool_track_position=tool_track_pos,
            trigger_type=trigger,
            created_at=created_at or "",
            is_auto=checkpoint.is_auto,
            has_file_commit=self.get_file_commit(checkpoint.id) is not None,
        )
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Session Management
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_current_session_id(self) -> Optional[int]:
        """Get current session ID."""
        return self._current_session.id if self._current_session else None
    
    def set_current_session(self, session_id: int) -> bool:
        """Set current session."""
        session = self._session_repo.get_by_id(session_id)
        if session:
            # Convert to MDP session if needed
            if isinstance(session, InternalSession_mdp):
                self._current_session = session
            else:
                # Wrap in MDP session
                self._current_session = InternalSession_mdp(
                    id=session.id,
                    external_session_id=session.external_session_id,
                    langgraph_session_id=session.langgraph_session_id,
                    session_state=session.session_state,
                    conversation_history=session.conversation_history,
                    created_at=session.created_at,
                    is_current=session.is_current,
                    checkpoint_count=session.checkpoint_count,
                    parent_session_id=session.parent_session_id,
                    branch_point_checkpoint_id=session.branch_point_checkpoint_id,
                )
            return True
        return False
    
    def cleanup(self, session_id: int, keep_latest: int = 5) -> int:
        """Cleanup old auto-checkpoints."""
        checkpoints = self._checkpoint_repo.get_by_internal_session(session_id)
        
        # Filter auto-checkpoints
        auto_cps = [cp for cp in checkpoints if cp.is_auto]
        
        # Sort by creation time (newest first)
        auto_cps.sort(key=lambda c: c.created_at or datetime.min, reverse=True)
        
        # Delete old ones
        to_delete = auto_cps[keep_latest:]
        deleted = 0
        for cp in to_delete:
            self._checkpoint_repo.delete(cp.id)
            deleted += 1
        
        return deleted
    
    # ═══════════════════════════════════════════════════════════════════════════
    # Utility Methods
    # ═══════════════════════════════════════════════════════════════════════════
    
    def get_tool_manager(self) -> ToolManager:
        """Get the ToolManager for tool registration."""
        return self._tool_manager
    
    def get_external_session_id(self) -> Optional[int]:
        """Get the external session ID."""
        return self._external_session_id

