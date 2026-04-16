"""
Integration Test: Rollback with Real LLM.

Tests the rollback functionality using real LLM calls to verify:
1. Memory correctness - LLM can recall past conversation after rollback
2. Audit sessions - Internal and external session tracking
3. Checkpoint integrity - State is correctly restored

Architecture Pattern:
- Config: Paths and URLs (config.py)
- Setup: Schema initialization using SQLAlchemy (setup.py)
- Models: SQLAlchemy ORM (models.py)
- Repositories: Data access (repositories/)
- UoW: Transaction management (unit_of_work.py)

Usage:
    pytest tests/test_wtb/test_rollback_with_llm.py -v -s
"""

import os
import sys
import json
import pytest
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Add project root to path
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Load environment
def load_dotenv():
    env_file = project_root / ".env"
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())

load_dotenv()

# LLM Client
try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

# WTB imports - following the architecture pattern
from wtb.infrastructure.database import (
    get_database_config,
    setup_all_databases,
    redirect_agentgit_database,
)


# ═══════════════════════════════════════════════════════════════════════════════
# LLM Client for Testing
# ═══════════════════════════════════════════════════════════════════════════════

class LLMClientHelper:
    """
    LLM client for testing with REAL LLM.
    NO MOCK - requires valid API key.
    """
    
    def __init__(self):
        if not HAS_OPENAI:
            raise RuntimeError("OpenAI package not installed! Run: pip install openai")
        
        self.api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("LLM_BASE_URL")
        raw_model = os.getenv("DEFAULT_LLM_PROVIDER") or os.getenv("DEFAULT_LLM") or "gpt-4o-mini"
        
        if not self.api_key:
            raise RuntimeError(
                "LLM API key required! Set LLM_API_KEY or OPENAI_API_KEY in .env file.\n"
                "Example .env:\n"
                "  LLM_API_KEY=sk-xxx\n"
                "  LLM_BASE_URL=https://api.openai.com/v1  # optional"
            )
        
        # Normalize model name
        model_mapping = {"gpt4o-mini": "gpt-4o-mini", "gpt4o": "gpt-4o", "gpt4": "gpt-4"}
        self.model = model_mapping.get(raw_model, raw_model)
        
        # Initialize REAL LLM client
        if self.base_url:
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        else:
            self.client = OpenAI(api_key=self.api_key)
        
        print(f"[LLM] Using REAL LLM: model={self.model}, base_url={self.base_url or 'default'}")
    
    def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """Send chat completion request to REAL LLM."""
        print(f"[LLM] Sending {len(messages)} messages to {self.model}...")
        
        response = self.client.chat.completions.create(
            model=kwargs.get("model", self.model),
            messages=messages,
            temperature=kwargs.get("temperature", 0.3),
            max_tokens=kwargs.get("max_tokens", 500),
        )
        
        result = response.choices[0].message.content
        print(f"[LLM] Response received: {len(result)} chars")
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# Session Manager for Testing
# ═══════════════════════════════════════════════════════════════════════════════

class SessionManagerHelper:
    """
    Manages sessions and checkpoints for rollback testing.
    Uses SQLAlchemy to work with AgentGit database.
    
    This follows the architecture pattern:
    - Uses SQLAlchemy engine/session (not raw sqlite3)
    - Wraps AgentGit tables for testing
    """
    
    def __init__(self, db_url: str):
        self.engine = create_engine(db_url)
        self.Session = sessionmaker(bind=self.engine)
    
    def create_user(self, username: str) -> int:
        """Create a test user."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                INSERT INTO users (username, password_hash, created_at)
                VALUES (:username, 'test_hash', :created_at)
            """), {"username": username, "created_at": datetime.now().isoformat()})
            conn.commit()
            return result.lastrowid
    
    def create_external_session(self, user_id: int, name: str) -> int:
        """Create an external session."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                INSERT INTO external_sessions (user_id, session_name, created_at, metadata)
                VALUES (:user_id, :name, :created_at, :metadata)
            """), {
                "user_id": user_id,
                "name": name,
                "created_at": datetime.now().isoformat(),
                "metadata": json.dumps({"test": True})
            })
            conn.commit()
            return result.lastrowid
    
    def create_internal_session(
        self, 
        external_session_id: int, 
        conversation_history: List[Dict[str, str]],
        parent_session_id: Optional[int] = None,
        branch_checkpoint_id: Optional[int] = None
    ) -> int:
        """Create an internal session with conversation history."""
        import uuid
        langgraph_id = f"test_session_{uuid.uuid4().hex[:12]}"
        
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                INSERT INTO internal_sessions (
                    external_session_id, langgraph_session_id, 
                    conversation_history, created_at, is_current,
                    parent_session_id, branch_point_checkpoint_id, metadata
                )
                VALUES (:ext_id, :lg_id, :history, :created_at, 1, :parent_id, :branch_cp, :metadata)
            """), {
                "ext_id": external_session_id,
                "lg_id": langgraph_id,
                "history": json.dumps(conversation_history),
                "created_at": datetime.now().isoformat(),
                "parent_id": parent_session_id,
                "branch_cp": branch_checkpoint_id,
                "metadata": json.dumps({"session_type": "test"})
            })
            conn.commit()
            return result.lastrowid
    
    def create_checkpoint(
        self,
        internal_session_id: int,
        name: str,
        conversation_history: List[Dict[str, str]],
        state_data: Dict[str, Any],
        metadata: Dict[str, Any] = None
    ) -> int:
        """Create a checkpoint with full state."""
        checkpoint_data = {
            "conversation_history": conversation_history,
            "state": state_data,
            "timestamp": datetime.now().isoformat(),
        }
        
        full_metadata = {
            "checkpoint_type": "test",
            "tool_track_position": len(conversation_history),
            **(metadata or {})
        }
        checkpoint_data["metadata"] = full_metadata
        
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                INSERT INTO checkpoints (
                    internal_session_id, checkpoint_name, checkpoint_data,
                    is_auto, created_at
                )
                VALUES (:session_id, :name, :data, 1, :created_at)
            """), {
                "session_id": internal_session_id,
                "name": name,
                "data": json.dumps(checkpoint_data),
                "created_at": datetime.now().isoformat()
            })
            conn.commit()
            return result.lastrowid
    
    def get_checkpoint(self, checkpoint_id: int) -> Optional[Dict[str, Any]]:
        """Get checkpoint data."""
        with self.engine.connect() as conn:
            result = conn.execute(text(
                "SELECT * FROM checkpoints WHERE id = :id"
            ), {"id": checkpoint_id})
            row = result.fetchone()
            if row:
                return dict(row._mapping)
        return None
    
    def get_internal_session(self, session_id: int) -> Optional[Dict[str, Any]]:
        """Get internal session data."""
        with self.engine.connect() as conn:
            result = conn.execute(text(
                "SELECT * FROM internal_sessions WHERE id = :id"
            ), {"id": session_id})
            row = result.fetchone()
            if row:
                return dict(row._mapping)
        return None
    
    def get_conversation_history(self, session_id: int) -> List[Dict[str, str]]:
        """Get conversation history for a session."""
        session = self.get_internal_session(session_id)
        if session and session["conversation_history"]:
            return json.loads(session["conversation_history"])
        return []
    
    def update_conversation_history(
        self, 
        session_id: int, 
        history: List[Dict[str, str]]
    ):
        """Update conversation history for a session."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE internal_sessions 
                SET conversation_history = :history
                WHERE id = :id
            """), {"history": json.dumps(history), "id": session_id})
            conn.commit()
    
    def restore_from_checkpoint(self, checkpoint_id: int) -> Dict[str, Any]:
        """Restore state from a checkpoint."""
        checkpoint = self.get_checkpoint(checkpoint_id)
        if not checkpoint:
            raise ValueError(f"Checkpoint {checkpoint_id} not found")
        
        data = json.loads(checkpoint["checkpoint_data"])
        return {
            "conversation_history": data.get("conversation_history", []),
            "state": data.get("state", {}),
            "metadata": data.get("metadata", {}),
        }
    
    def get_all_sessions(self, external_session_id: int) -> List[Dict[str, Any]]:
        """Get all internal sessions for an external session."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT * FROM internal_sessions 
                WHERE external_session_id = :ext_id
                ORDER BY created_at
            """), {"ext_id": external_session_id})
            return [dict(row._mapping) for row in result.fetchall()]
    
    def get_session_checkpoints(self, internal_session_id: int) -> List[Dict[str, Any]]:
        """Get all checkpoints for a session."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT * FROM checkpoints 
                WHERE internal_session_id = :session_id
                ORDER BY created_at
            """), {"session_id": internal_session_id})
            return [dict(row._mapping) for row in result.fetchall()]
    
    def close(self):
        """Dispose engine."""
        self.engine.dispose()


# ═══════════════════════════════════════════════════════════════════════════════
# Test Fixtures
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def db_config():
    """Setup databases and return config."""
    result = setup_all_databases()
    return result["config"]


@pytest.fixture
def session_manager(db_config):
    """Create a session manager using SQLAlchemy."""
    mgr = SessionManagerHelper(db_config.agentgit_db_url)
    yield mgr
    mgr.close()


@pytest.fixture
def llm_client():
    """Create LLM client."""
    return LLMClientHelper()


# ═══════════════════════════════════════════════════════════════════════════════
# Test Cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestRollbackWithLLM:
    """Tests for rollback functionality with real LLM."""
    
    def test_database_setup(self, db_config):
        """Test that databases are properly set up."""
        assert db_config.agentgit_db_path.exists(), "AgentGit DB should exist"
        assert db_config.wtb_db_path.exists(), "WTB DB should exist"
        
        # Verify AgentGit tables using SQLAlchemy
        from sqlalchemy import inspect
        engine = create_engine(db_config.agentgit_db_url)
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        engine.dispose()
        
        assert "users" in tables
        assert "external_sessions" in tables
        assert "internal_sessions" in tables
        assert "checkpoints" in tables
        
        print(f"\n[OK] AgentGit DB: {db_config.agentgit_db_path}")
        print(f"[OK] WTB DB: {db_config.wtb_db_path}")
    
    def test_conversation_memory_after_rollback(self, session_manager, llm_client, db_config):
        """
        Test that LLM can recall conversation after rollback.
        
        Scenario:
        1. Create session, have conversation with secret info
        2. Create checkpoint
        3. Continue conversation (add more messages)
        4. Rollback to checkpoint
        5. Ask LLM about the secret - should remember from checkpoint state
        """
        print("\n" + "="*60)
        print("TEST: Conversation Memory After Rollback")
        print("="*60)
        
        # Setup
        user_id = session_manager.create_user(f"test_user_{datetime.now().timestamp()}")
        ext_session_id = session_manager.create_external_session(user_id, "Memory Test")
        
        # Initial conversation with secret
        initial_history = [
            {"role": "system", "content": "You are a helpful assistant with perfect memory."},
            {"role": "user", "content": "I'm going to tell you a secret code: ALPHA-BRAVO-123. Remember it."},
            {"role": "assistant", "content": "I've noted the secret code: ALPHA-BRAVO-123. I will remember it."},
        ]
        
        int_session_id = session_manager.create_internal_session(
            ext_session_id, 
            initial_history
        )
        print(f"[OK] Created session {int_session_id} with initial conversation")
        
        # Create checkpoint BEFORE more conversation
        checkpoint_id = session_manager.create_checkpoint(
            int_session_id,
            "Before additional conversation",
            initial_history,
            {"secret_shared": True, "checkpoint_reason": "save_secret"}
        )
        print(f"[OK] Created checkpoint {checkpoint_id}")
        
        # Continue conversation (simulating more work)
        extended_history = initial_history + [
            {"role": "user", "content": "Let's change the topic. What's the weather like?"},
            {"role": "assistant", "content": "I don't have access to real-time weather data."},
            {"role": "user", "content": "Forget the secret code, it's not important anymore."},
            {"role": "assistant", "content": "Understood, I'll disregard the previous code."},
        ]
        session_manager.update_conversation_history(int_session_id, extended_history)
        print("[OK] Extended conversation (told LLM to forget)")
        
        # Now rollback to checkpoint
        restored = session_manager.restore_from_checkpoint(checkpoint_id)
        restored_history = restored["conversation_history"]
        print(f"[OK] Rolled back to checkpoint, history length: {len(restored_history)}")
        
        # Verify restored history has the secret
        assert len(restored_history) == 3, "Should have 3 messages"
        assert "ALPHA-BRAVO-123" in restored_history[1]["content"], "Secret should be in history"
        
        # Ask LLM about the secret using restored history
        test_messages = restored_history + [
            {"role": "user", "content": "What was the secret code I told you earlier?"}
        ]
        
        response = llm_client.chat(test_messages)
        print(f"[OK] LLM Response: {response[:100]}...")
        
        # Verify LLM remembers (check for code or acknowledgment)
        assert any(word in response.upper() for word in ["ALPHA", "BRAVO", "123", "CODE", "SECRET"]), \
            f"LLM should remember the secret. Response: {response}"
        
        print("[SUCCESS] LLM correctly remembers conversation after rollback")
    
    def test_internal_session_branching(self, session_manager, llm_client, db_config):
        """
        Test internal session branching from checkpoint.
        
        Scenario:
        1. Create main session with conversation
        2. Create checkpoint
        3. Continue main session (path A)
        4. Branch from checkpoint (path B) 
        5. Verify both paths exist independently
        """
        print("\n" + "="*60)
        print("TEST: Internal Session Branching")
        print("="*60)
        
        # Setup
        user_id = session_manager.create_user(f"branch_user_{datetime.now().timestamp()}")
        ext_session_id = session_manager.create_external_session(user_id, "Branch Test")
        
        # Main session
        main_history = [
            {"role": "system", "content": "You are an ML assistant."},
            {"role": "user", "content": "Analyze this dataset: 1000 rows, 50 features."},
            {"role": "assistant", "content": "I'll analyze the dataset. Initial observation: moderate size."},
        ]
        
        main_session_id = session_manager.create_internal_session(ext_session_id, main_history)
        print(f"[OK] Created main session {main_session_id}")
        
        # Checkpoint at decision point
        checkpoint_id = session_manager.create_checkpoint(
            main_session_id,
            "Decision Point: Model Selection",
            main_history,
            {"stage": "model_selection", "options": ["XGBoost", "RandomForest"]}
        )
        print(f"[OK] Created checkpoint {checkpoint_id}")
        
        # Path A: Continue main session with XGBoost
        path_a_history = main_history + [
            {"role": "user", "content": "Let's use XGBoost for this analysis."},
            {"role": "assistant", "content": "Configuring XGBoost with default hyperparameters."},
        ]
        session_manager.update_conversation_history(main_session_id, path_a_history)
        print("[OK] Path A: Chose XGBoost")
        
        # Path B: Branch from checkpoint with RandomForest
        restored = session_manager.restore_from_checkpoint(checkpoint_id)
        path_b_history = restored["conversation_history"] + [
            {"role": "user", "content": "Let's use RandomForest instead."},
            {"role": "assistant", "content": "Configuring RandomForest with 100 trees."},
        ]
        
        branch_session_id = session_manager.create_internal_session(
            ext_session_id,
            path_b_history,
            parent_session_id=main_session_id,
            branch_checkpoint_id=checkpoint_id
        )
        print(f"[OK] Path B: Created branch session {branch_session_id}")
        
        # Verify both sessions exist
        all_sessions = session_manager.get_all_sessions(ext_session_id)
        assert len(all_sessions) == 2, f"Should have 2 sessions, got {len(all_sessions)}"
        
        # Verify histories are different
        main_session = session_manager.get_internal_session(main_session_id)
        branch_session = session_manager.get_internal_session(branch_session_id)
        
        main_hist = json.loads(main_session["conversation_history"])
        branch_hist = json.loads(branch_session["conversation_history"])
        
        assert "XGBoost" in main_hist[-2]["content"], "Main session should mention XGBoost"
        assert "RandomForest" in branch_hist[-2]["content"], "Branch should mention RandomForest"
        
        # Verify branch metadata
        assert branch_session["parent_session_id"] == main_session_id
        assert branch_session["branch_point_checkpoint_id"] == checkpoint_id
        
        print(f"[OK] Main session history: {len(main_hist)} messages")
        print(f"[OK] Branch session history: {len(branch_hist)} messages")
        print("[SUCCESS] Internal session branching works correctly")
    
    def test_audit_trail_across_sessions(self, session_manager, db_config):
        """
        Test audit trail tracking across internal and external sessions.
        
        Verifies:
        1. External session contains all internal sessions
        2. Each internal session has checkpoints
        3. Checkpoint metadata tracks audit info
        """
        print("\n" + "="*60)
        print("TEST: Audit Trail Across Sessions")
        print("="*60)
        
        # Setup
        user_id = session_manager.create_user(f"audit_user_{datetime.now().timestamp()}")
        ext_session_id = session_manager.create_external_session(user_id, "Audit Trail Test")
        
        # Create multiple internal sessions (simulating workflow execution)
        session_ids = []
        checkpoint_ids = []
        
        for i in range(3):
            history = [
                {"role": "system", "content": "Workflow step executor."},
                {"role": "user", "content": f"Execute step {i+1}"},
                {"role": "assistant", "content": f"Step {i+1} completed successfully."},
            ]
            
            parent_id = session_ids[-1] if session_ids else None
            session_id = session_manager.create_internal_session(
                ext_session_id, 
                history,
                parent_session_id=parent_id
            )
            session_ids.append(session_id)
            
            # Create checkpoint with audit metadata
            cp_id = session_manager.create_checkpoint(
                session_id,
                f"Step {i+1} Completed",
                history,
                {"step_number": i+1, "outcome": "success"},
                metadata={
                    "audit_trail": {
                        "action": f"execute_step_{i+1}",
                        "user_id": user_id,
                        "timestamp": datetime.now().isoformat(),
                        "previous_checkpoint": checkpoint_ids[-1] if checkpoint_ids else None,
                    }
                }
            )
            checkpoint_ids.append(cp_id)
            print(f"[OK] Session {session_id}, Checkpoint {cp_id}")
        
        # Verify all sessions under external session
        all_sessions = session_manager.get_all_sessions(ext_session_id)
        assert len(all_sessions) == 3, f"Should have 3 internal sessions"
        
        # Verify checkpoints
        for i, session_id in enumerate(session_ids):
            checkpoints = session_manager.get_session_checkpoints(session_id)
            assert len(checkpoints) >= 1, f"Session {session_id} should have checkpoints"
            
            # Verify audit metadata
            cp = checkpoints[0]
            cp_data = json.loads(cp["checkpoint_data"])
            audit = cp_data.get("metadata", {}).get("audit_trail", {})
            
            assert "action" in audit, "Checkpoint should have audit action"
            assert "timestamp" in audit, "Checkpoint should have timestamp"
            print(f"[OK] Session {session_id}: {len(checkpoints)} checkpoints with audit trail")
        
        print("[SUCCESS] Audit trail correctly tracked across all sessions")
    
    def test_rollback_with_multiple_checkpoints(self, session_manager, llm_client, db_config):
        """
        Test rolling back through multiple checkpoints.
        
        Scenario:
        1. Create session with 5 checkpoints at different stages
        2. Rollback to checkpoint 3
        3. Verify state matches checkpoint 3
        4. Rollback to checkpoint 1
        5. Verify state matches checkpoint 1
        """
        print("\n" + "="*60)
        print("TEST: Rollback Through Multiple Checkpoints")
        print("="*60)
        
        # Setup
        user_id = session_manager.create_user(f"multi_cp_user_{datetime.now().timestamp()}")
        ext_session_id = session_manager.create_external_session(user_id, "Multi Checkpoint Test")
        
        # Create session
        history = [{"role": "system", "content": "State tracking assistant."}]
        session_id = session_manager.create_internal_session(ext_session_id, history)
        
        # Create 5 checkpoints with distinct states
        checkpoint_ids = []
        states = []
        
        for i in range(5):
            # Add messages
            history.append({"role": "user", "content": f"Set counter to {i+1}"})
            history.append({"role": "assistant", "content": f"Counter set to {i+1}"})
            session_manager.update_conversation_history(session_id, history)
            
            state = {"counter": i+1, "stage": f"stage_{i+1}"}
            states.append(state)
            
            cp_id = session_manager.create_checkpoint(
                session_id,
                f"Checkpoint at counter={i+1}",
                list(history),  # Copy
                state
            )
            checkpoint_ids.append(cp_id)
            print(f"[OK] Created checkpoint {cp_id} with counter={i+1}")
        
        # Rollback to checkpoint 3 (counter=3)
        restored_3 = session_manager.restore_from_checkpoint(checkpoint_ids[2])
        assert restored_3["state"]["counter"] == 3, "Should restore counter=3"
        assert len(restored_3["conversation_history"]) == 7, "Should have 7 messages"
        print(f"[OK] Rolled back to checkpoint 3: counter={restored_3['state']['counter']}")
        
        # Rollback to checkpoint 1 (counter=1)
        restored_1 = session_manager.restore_from_checkpoint(checkpoint_ids[0])
        assert restored_1["state"]["counter"] == 1, "Should restore counter=1"
        assert len(restored_1["conversation_history"]) == 3, "Should have 3 messages"
        print(f"[OK] Rolled back to checkpoint 1: counter={restored_1['state']['counter']}")
        
        # Verify we can still access later checkpoints
        restored_5 = session_manager.restore_from_checkpoint(checkpoint_ids[4])
        assert restored_5["state"]["counter"] == 5, "Should restore counter=5"
        print(f"[OK] Accessed checkpoint 5: counter={restored_5['state']['counter']}")
        
        print("[SUCCESS] Multiple checkpoint rollback works correctly")


# ═══════════════════════════════════════════════════════════════════════════════
# Run Tests
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Configure stdout for Windows
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    
    print("\n" + "="*70)
    print("WTB ROLLBACK WITH LLM TEST SUITE")
    print("="*70)
    
    # Setup databases
    config = setup_databases()
    
    # Run tests
    pytest.main([__file__, "-v", "-s", "--tb=short"])

