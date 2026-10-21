import os
import uuid
from contextlib import ExitStack

import pytest


@pytest.fixture
def pg_namespace():
    dsn = os.environ.get("WTB_EVIDENCE_POSTGRES_DSN")
    if not dsn:
        if os.environ.get("WTB_REQUIRE_EVIDENCE_POSTGRES") == "1":
            pytest.fail("WTB_EVIDENCE_POSTGRES_DSN is required")
        pytest.skip(
            "WTB_EVIDENCE_POSTGRES_DSN is required for real PostgreSQL evidence tests"
        )
    import psycopg
    from psycopg import sql

    schema = "wtb_test_" + uuid.uuid4().hex
    try:
        yield dsn, schema
    finally:
        # This exact random namespace belongs to this fixture, never a user's
        # configured evidence schema or other shared database tables.
        with psycopg.connect(dsn) as db:
            db.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )


@pytest.fixture
def store_factory(pg_namespace):
    from wtb.infrastructure.evidence.postgres_store import PostgresEvidenceStore

    dsn, schema = pg_namespace
    with ExitStack() as resources:

        def create(**kwargs):
            return resources.enter_context(
                PostgresEvidenceStore(dsn, schema=schema, **kwargs)
            )

        yield create


@pytest.fixture
def store(store_factory):
    return store_factory()


@pytest.fixture
def collector(store):
    from tests.unit.evidence.test_event_store import identity
    from wtb.application.services.evidence_collector import EvidenceCollector

    store.register(identity(), expected_attempt=None)
    return EvidenceCollector(store)


@pytest.fixture
def sink(store):
    from tests.unit.evidence.test_publication_sink import setup_sink
    from wtb.infrastructure.evidence.postgres_store import PostgresPublicationSink

    return setup_sink(store=store, sink_type=PostgresPublicationSink)


@pytest.fixture
def reopen_sink(store_factory):
    from wtb.infrastructure.evidence.postgres_store import PostgresPublicationSink

    return lambda: PostgresPublicationSink(store_factory())


@pytest.fixture
def evidence_store_factory(store_factory):
    return store_factory
