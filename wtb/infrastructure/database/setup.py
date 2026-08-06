"""
Database Setup and Initialization.

Uses SQLAlchemy to initialize database schemas following the architecture pattern:
- Config: Paths and URLs (config.py)
- Models: SQLAlchemy ORM (models.py) 
- This module: Schema creation using SQLAlchemy
- Repositories: Data access (repositories/)
- UoW: Transaction management (unit_of_work.py)

Usage:
    from wtb.infrastructure.database.setup import setup_all_databases
    
    setup_all_databases()
"""

import sys

from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from .config import get_database_config, redirect_agentgit_database
from .engine_cache import get_engine
from .models import Base


def setup_wtb_database(echo: bool = False) -> bool:
    """
    Initialize WTB database using SQLAlchemy ORM.
    
    Creates all tables defined in models.py.
    
    Args:
        echo: Whether to log SQL statements
        
    Returns:
        True if successful
    """
    config = get_database_config()
    
    engine = get_engine(config.wtb_db_url, echo)
    
    # Create all tables from ORM models
    Base.metadata.create_all(engine)
    
    # Verify tables were created
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    
    expected_tables = [
        "wtb_workflows", "wtb_executions", "wtb_node_variants",
        "wtb_batch_tests", "wtb_evaluation_results", 
        "wtb_node_boundaries", "wtb_checkpoint_files"
    ]
    
    created_count = sum(1 for t in expected_tables if t in tables)
    
    print(f"[OK] WTB database initialized: {config.wtb_db_path}")
    print(f"     Tables: {created_count}/{len(expected_tables)}")
    
    return True


def setup_agentgit_database(echo: bool = False) -> bool:
    """
    Initialize AgentGit database schema.
    
    AgentGit uses its own models, so we create tables using raw SQL
    that matches their schema. This is the anti-corruption layer approach.
    
    Args:
        echo: Whether to log SQL statements
        
    Returns:
        True if successful
    """
    config = get_database_config()
    
    engine = get_engine(config.agentgit_db_url, echo)
    
    # AgentGit schema (matches their internal structure)
    with engine.connect() as conn:
        # Users table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                created_at TEXT,
                last_login TEXT,
                data TEXT,
                api_key TEXT,
                session_limit INTEGER DEFAULT 5
            )
        """))
        
        # External sessions
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS external_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                session_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                is_active INTEGER DEFAULT 1,
                data TEXT,
                metadata TEXT,
                branch_count INTEGER DEFAULT 0,
                total_checkpoints INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """))
        
        # Internal sessions
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS internal_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_session_id INTEGER NOT NULL,
                langgraph_session_id TEXT UNIQUE NOT NULL,
                state_data TEXT,
                conversation_history TEXT,
                created_at TEXT NOT NULL,
                is_current INTEGER DEFAULT 0,
                checkpoint_count INTEGER DEFAULT 0,
                parent_session_id INTEGER,
                branch_point_checkpoint_id INTEGER,
                tool_invocation_count INTEGER DEFAULT 0,
                metadata TEXT,
                FOREIGN KEY (external_session_id) REFERENCES external_sessions(id) ON DELETE CASCADE,
                FOREIGN KEY (parent_session_id) REFERENCES internal_sessions(id) ON DELETE SET NULL
            )
        """))
        
        # Checkpoints
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                internal_session_id INTEGER NOT NULL,
                checkpoint_name TEXT,
                checkpoint_data TEXT NOT NULL,
                is_auto INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                user_id INTEGER,
                FOREIGN KEY (internal_session_id) REFERENCES internal_sessions(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            )
        """))
        
        # Indexes
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_external_sessions_user ON external_sessions(user_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_internal_sessions_external ON internal_sessions(external_session_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_internal_sessions_langgraph ON internal_sessions(langgraph_session_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_checkpoints_session ON checkpoints(internal_session_id)"))
        
        conn.commit()
    
    # Verify
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    expected = ["users", "external_sessions", "internal_sessions", "checkpoints"]
    created_count = sum(1 for t in expected if t in tables)
    
    print(f"[OK] AgentGit database initialized: {config.agentgit_db_path}")
    print(f"     Tables: {created_count}/{len(expected)}")
    
    return True


def setup_all_databases(echo: bool = False) -> dict:
    """
    Setup all databases for the WTB system.
    
    1. Redirects AgentGit to local data/ directory
    2. Initializes AgentGit schema
    3. Initializes WTB schema using SQLAlchemy ORM
    
    Args:
        echo: Whether to log SQL statements
        
    Returns:
        Dict with database config and status
    """
    config = get_database_config()
    
    print("\n" + "="*60)
    print("DATABASE SETUP")
    print("="*60)
    print(f"Data directory: {config.data_dir}")
    print("-"*60)
    
    # Redirect AgentGit
    redirected = redirect_agentgit_database()
    if redirected:
        print("[OK] AgentGit database path redirected")
    else:
        print("[WARN] AgentGit not installed, skipping redirect")
    
    # Initialize databases
    setup_agentgit_database(echo)
    setup_wtb_database(echo)
    
    print("-"*60)
    print("Database files:")
    print(f"  AgentGit: {config.agentgit_db_path}")
    print(f"  WTB:      {config.wtb_db_path}")
    print("="*60 + "\n")
    
    return {
        "config": config,
        "agentgit_redirected": redirected,
        "status": "success"
    }


def get_wtb_session():
    """
    Get a new SQLAlchemy session for WTB database.
    
    For simple cases. For transactions, use SQLAlchemyUnitOfWork.
    
    Returns:
        SQLAlchemy Session
    """
    config = get_database_config()
    engine = get_engine(config.wtb_db_url, False)
    Session = sessionmaker(bind=engine)
    return Session()


if __name__ == "__main__":
    # Configure stdout for Windows
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    
    setup_all_databases(echo=False)
    
    from .config import print_database_locations
    print_database_locations()

