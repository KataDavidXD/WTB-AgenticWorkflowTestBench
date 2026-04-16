"""Tests for InMemoryUnitOfWork repository completeness (C6 fix)."""

from wtb.infrastructure.database.inmemory_unit_of_work import InMemoryUnitOfWork


class TestInMemoryUoWRepositories:
    def test_has_blobs_repository(self):
        uow = InMemoryUnitOfWork()
        assert hasattr(uow, "blobs")
        assert uow.blobs is not None

    def test_has_file_commits_repository(self):
        uow = InMemoryUnitOfWork()
        assert hasattr(uow, "file_commits")
        assert uow.file_commits is not None

    def test_has_checkpoint_file_links(self):
        uow = InMemoryUnitOfWork()
        assert hasattr(uow, "checkpoint_file_links")
        assert uow.checkpoint_file_links is not None

    def test_has_executions(self):
        uow = InMemoryUnitOfWork()
        assert hasattr(uow, "executions")
        assert uow.executions is not None

    def test_has_workflows(self):
        uow = InMemoryUnitOfWork()
        assert hasattr(uow, "workflows")
        assert uow.workflows is not None

    def test_has_outbox(self):
        uow = InMemoryUnitOfWork()
        assert hasattr(uow, "outbox")
        assert uow.outbox is not None

    def test_commit_and_rollback(self):
        uow = InMemoryUnitOfWork()
        uow.commit()
        uow.rollback()

    def test_reset_clears_state(self):
        uow = InMemoryUnitOfWork()
        uow.reset()
        assert hasattr(uow, "blobs")
        assert hasattr(uow, "file_commits")
