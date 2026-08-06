"""Tests for InMemoryUnitOfWork repository completeness (C6 fix)."""

from threading import Event, Thread

from wtb.domain.models.file_processing import BlobId
from wtb.domain.models.workflow import TestWorkflow as WorkflowModel
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

    def test_blob_repository_round_trip_uses_blob_id_value_contract(self):
        uow = InMemoryUnitOfWork()
        content = b"content-addressed payload"

        blob_id = uow.blobs.save(content)

        assert isinstance(blob_id, BlobId)
        assert blob_id == BlobId.from_content(content)
        assert uow.blobs.get(blob_id) == content
        assert uow.blobs.exists(blob_id) is True
        assert uow.blobs.delete(blob_id) is True
        assert uow.blobs.exists(blob_id) is False
        assert uow.blobs.get(blob_id) is None

    def test_unpaired_exit_is_a_safe_noop(self):
        uow = InMemoryUnitOfWork()

        uow.__exit__(None, None, None)

        assert uow._transaction_depth == 0
        assert uow._in_transaction is False

        with uow:
            assert uow._transaction_depth == 1

        assert uow._transaction_depth == 0

    def test_normal_exit_discards_uncommitted_changes(self):
        uow = InMemoryUnitOfWork()

        with uow:
            uow.workflows.add(WorkflowModel(id="uncommitted"))

        assert uow.workflows.list() == []

    def test_normal_exit_keeps_commit_and_discards_later_changes(self):
        uow = InMemoryUnitOfWork()

        with uow:
            uow.workflows.add(WorkflowModel(id="committed"))
            uow.commit()
            uow.workflows.add(WorkflowModel(id="after-commit"))

        assert [workflow.id for workflow in uow.workflows.list()] == ["committed"]


    def test_reset_waits_for_active_transaction_and_resets_metadata(self):
        uow = InMemoryUnitOfWork()
        transaction_entered = Event()
        allow_transaction_exit = Event()
        reset_completed = Event()
        thread_errors = []

        def transaction_worker():
            try:
                with uow:
                    uow.workflows.add(WorkflowModel(id="uncommitted"))
                    transaction_entered.set()
                    assert allow_transaction_exit.wait(timeout=2.0)
            except BaseException as error:
                thread_errors.append(error)

        def reset_worker():
            try:
                assert transaction_entered.wait(timeout=2.0)
                uow.reset()
                reset_completed.set()
            except BaseException as error:
                thread_errors.append(error)

        transaction = Thread(target=transaction_worker)
        reset = Thread(target=reset_worker)
        transaction.start()
        reset.start()
        assert transaction_entered.wait(timeout=2.0)

        reset_completed_while_transaction_active = reset_completed.wait(timeout=0.1)
        allow_transaction_exit.set()
        transaction.join(timeout=2.0)
        reset.join(timeout=2.0)

        assert reset_completed_while_transaction_active is False
        assert not transaction.is_alive()
        assert not reset.is_alive()
        assert thread_errors == []
        assert uow._transaction_depth == 0
        assert uow._in_transaction is False
        assert uow.workflows.list() == []
    def test_shared_transactions_do_not_capture_uncommitted_rows_from_other_threads(self):
        uow = InMemoryUnitOfWork()
        allow_competing_transaction = Event()
        competing_transaction_entered = Event()
        first_transaction_committed = Event()
        thread_errors = []

        def committed_worker():
            try:
                with uow:
                    uow.workflows.add(WorkflowModel(id="committed"))
                    allow_competing_transaction.set()
                    competing_transaction_entered.wait(timeout=0.5)
                    uow.commit()
                    first_transaction_committed.set()
            except BaseException as error:
                thread_errors.append(error)

        def rolled_back_worker():
            try:
                assert allow_competing_transaction.wait(timeout=2.0)
                try:
                    with uow:
                        uow.workflows.add(WorkflowModel(id="rolled-back"))
                        competing_transaction_entered.set()
                        assert first_transaction_committed.wait(timeout=2.0)
                        raise RuntimeError("rollback competing transaction")
                except RuntimeError:
                    pass
            except BaseException as error:
                thread_errors.append(error)

        first = Thread(target=committed_worker)
        second = Thread(target=rolled_back_worker)
        first.start()
        second.start()
        first.join(timeout=3.0)
        second.join(timeout=3.0)

        assert not first.is_alive()
        assert not second.is_alive()
        assert thread_errors == []
        assert [workflow.id for workflow in uow.workflows.list()] == ["committed"]
