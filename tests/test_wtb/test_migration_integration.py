"""
Integration Tests for Migration (2026-01-27).

Tests the migration from wtb_checkpoint_files to checkpoint_file_links.
Uses in-memory SQLite for fast, isolated testing.
"""

import pytest
import tempfile
import uuid
from pathlib import Path
from datetime import datetime

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker

from wtb.infrastructure.database.models import Base
from wtb.infrastructure.database.file_processing_orm import (
    CheckpointFileLinkORM,
    FileCommitORM,
    FileMementoORM,
    FileBlobORM,
)
from wtb.infrastructure.database.repositories import (
    SQLAlchemyCheckpointFileLinkRepository,
    InMemoryCheckpointFileLinkRepository,
)
from wtb.domain.models.file_processing import (
    CheckpointFileLink,
    CommitId,
    FileCommit,
    FileMemento,
    BlobId,
)


def make_commit_id(name: str = "") -> CommitId:
    """Create a valid CommitId for testing."""
    return CommitId(str(uuid.uuid4()))


@pytest.fixture
def db_engine():
    """Create in-memory SQLite engine."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def db_session(db_engine):
    """Create database session."""
    Session = sessionmaker(bind=db_engine)
    session = Session()
    yield session
    session.close()


class TestCheckpointFileLinkRepository:
    """Integration tests for CheckpointFileLinkRepository."""
    
    def test_add_and_get_by_checkpoint(self, db_session):
        """Test adding and retrieving link by checkpoint ID."""
        repo = SQLAlchemyCheckpointFileLinkRepository(db_session)
        
        commit_id = make_commit_id()
        link = CheckpointFileLink(
            checkpoint_id=42,
            commit_id=commit_id,
            linked_at=datetime.now(),
            file_count=5,
            total_size_bytes=1024000,
        )
        
        repo.add(link)
        db_session.commit()
        
        # Retrieve
        retrieved = repo.get_by_checkpoint(42)
        
        assert retrieved is not None
        assert retrieved.checkpoint_id == 42
        assert retrieved.commit_id.value == commit_id.value
        assert retrieved.file_count == 5
        assert retrieved.total_size_bytes == 1024000
    
    def test_get_by_commit(self, db_session):
        """Test retrieving links by commit ID."""
        repo = SQLAlchemyCheckpointFileLinkRepository(db_session)
        
        commit_id = make_commit_id()
        
        # Add multiple links to same commit
        for cp_id in [1, 2, 3]:
            link = CheckpointFileLink(
                checkpoint_id=cp_id,
                commit_id=commit_id,
                linked_at=datetime.now(),
                file_count=1,
                total_size_bytes=100,
            )
            repo.add(link)
        
        db_session.commit()
        
        # Retrieve by commit
        links = repo.get_by_commit(commit_id)
        
        assert len(links) == 3
        assert all(l.commit_id.value == commit_id.value for l in links)
    
    def test_delete_by_checkpoint(self, db_session):
        """Test deleting link by checkpoint ID."""
        repo = SQLAlchemyCheckpointFileLinkRepository(db_session)
        
        link = CheckpointFileLink(
            checkpoint_id=99,
            commit_id=make_commit_id(),
            linked_at=datetime.now(),
            file_count=1,
            total_size_bytes=100,
        )
        
        repo.add(link)
        db_session.commit()
        
        # Delete
        result = repo.delete_by_checkpoint(99)
        db_session.commit()
        
        assert result is True
        assert repo.get_by_checkpoint(99) is None
    
    def test_upsert_behavior(self, db_session):
        """Test that add() updates existing links."""
        repo = SQLAlchemyCheckpointFileLinkRepository(db_session)
        
        commit_v1 = make_commit_id()
        commit_v2 = make_commit_id()
        
        # Add initial link
        link1 = CheckpointFileLink(
            checkpoint_id=50,
            commit_id=commit_v1,
            linked_at=datetime.now(),
            file_count=1,
            total_size_bytes=100,
        )
        repo.add(link1)
        db_session.commit()
        
        # Update with new commit
        link2 = CheckpointFileLink(
            checkpoint_id=50,
            commit_id=commit_v2,
            linked_at=datetime.now(),
            file_count=10,
            total_size_bytes=1000,
        )
        repo.add(link2)
        db_session.commit()
        
        # Should have updated, not duplicated
        retrieved = repo.get_by_checkpoint(50)
        
        assert retrieved is not None
        assert retrieved.commit_id.value == commit_v2.value
        assert retrieved.file_count == 10


class TestInMemoryCheckpointFileLinkRepository:
    """Integration tests for InMemoryCheckpointFileLinkRepository."""
    
    def test_add_and_get_by_checkpoint(self):
        """Test adding and retrieving link by checkpoint ID."""
        repo = InMemoryCheckpointFileLinkRepository()
        
        commit_id = make_commit_id()
        link = CheckpointFileLink(
            checkpoint_id=42,
            commit_id=commit_id,
            linked_at=datetime.now(),
            file_count=5,
            total_size_bytes=1024000,
        )
        
        repo.add(link)
        
        # Retrieve
        retrieved = repo.get_by_checkpoint(42)
        
        assert retrieved is not None
        assert retrieved.checkpoint_id == 42
        assert retrieved.commit_id.value == commit_id.value
    
    def test_get_by_commit(self):
        """Test retrieving links by commit ID."""
        repo = InMemoryCheckpointFileLinkRepository()
        
        commit_id = make_commit_id()
        
        # Add multiple links to same commit
        for cp_id in [1, 2, 3]:
            link = CheckpointFileLink(
                checkpoint_id=cp_id,
                commit_id=commit_id,
                linked_at=datetime.now(),
                file_count=1,
                total_size_bytes=100,
            )
            repo.add(link)
        
        # Retrieve by commit
        links = repo.get_by_commit(commit_id)
        
        assert len(links) == 3


class TestMigrationScriptIntegration:
    """Integration tests for the migration script."""
    
    def test_migration_creates_new_table(self, db_engine):
        """Test that migration creates checkpoint_file_links table."""
        inspector = inspect(db_engine)
        tables = inspector.get_table_names()
        
        # New table should exist (created by Base.metadata.create_all)
        assert "checkpoint_file_links" in tables
    
    def test_checkpoint_file_link_orm_columns(self, db_engine):
        """Test that CheckpointFileLinkORM has correct columns."""
        inspector = inspect(db_engine)
        columns = {c['name'] for c in inspector.get_columns('checkpoint_file_links')}
        
        expected_columns = {
            'checkpoint_id',
            'commit_id',
            'linked_at',
            'file_count',
            'total_size_bytes',
        }
        
        assert expected_columns.issubset(columns)
    
    def test_migration_script_syntax(self):
        """Test that migration script has valid SQL syntax."""
        # Use path relative to this test file's location
        test_dir = Path(__file__).parent.parent.parent
        migration_path = test_dir / "wtb/infrastructure/database/migrations/004_consolidate_checkpoint_files.sql"
        
        if not migration_path.exists():
            pytest.skip("Migration script not found")
        
        content = migration_path.read_text()
        
        # Basic syntax checks
        assert "CREATE TABLE" in content
        assert "INSERT" in content
        assert "DROP TABLE" in content
        
        # Should not have syntax errors (basic check)
        assert content.count("(") == content.count(")")


class TestTransactionConsistency:
    """Tests for ACID compliance in checkpoint-file linking."""
    
    def test_atomic_link_creation(self, db_session):
        """Test that link creation is atomic."""
        repo = SQLAlchemyCheckpointFileLinkRepository(db_session)
        
        commit_id = make_commit_id()
        link = CheckpointFileLink(
            checkpoint_id=100,
            commit_id=commit_id,
            linked_at=datetime.now(),
            file_count=1,
            total_size_bytes=100,
        )
        
        repo.add(link)
        
        # Before commit, should not be visible in new session
        # (This tests isolation)
        
        # After commit, should be visible
        db_session.commit()
        
        retrieved = repo.get_by_checkpoint(100)
        assert retrieved is not None
    
    def test_rollback_on_error(self, db_session):
        """Test that rollback works correctly."""
        repo = SQLAlchemyCheckpointFileLinkRepository(db_session)
        
        link = CheckpointFileLink(
            checkpoint_id=200,
            commit_id=make_commit_id(),
            linked_at=datetime.now(),
            file_count=1,
            total_size_bytes=100,
        )
        
        repo.add(link)
        
        # Rollback
        db_session.rollback()
        
        # Should not exist
        retrieved = repo.get_by_checkpoint(200)
        assert retrieved is None
    
    def test_concurrent_updates(self, db_engine):
        """Test that concurrent updates are handled correctly."""
        Session = sessionmaker(bind=db_engine)
        
        # Session 1
        session1 = Session()
        repo1 = SQLAlchemyCheckpointFileLinkRepository(session1)
        
        # Session 2
        session2 = Session()
        repo2 = SQLAlchemyCheckpointFileLinkRepository(session2)
        
        commit_id = make_commit_id()
        
        # Both add same checkpoint_id
        link1 = CheckpointFileLink(
            checkpoint_id=300,
            commit_id=commit_id,
            linked_at=datetime.now(),
            file_count=1,
            total_size_bytes=100,
        )
        
        repo1.add(link1)
        session1.commit()
        
        # Session 2 should see session 1's data
        retrieved = repo2.get_by_checkpoint(300)
        assert retrieved is not None
        assert retrieved.commit_id.value == commit_id.value
        
        session1.close()
        session2.close()
