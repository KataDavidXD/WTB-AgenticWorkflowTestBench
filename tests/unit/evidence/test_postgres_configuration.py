import pytest

from wtb.infrastructure.evidence.postgres_store import PostgresEvidenceStore


@pytest.mark.parametrize(
    "schema",
    ["public", "pg_test", "information_schema", "x;DROP", "Mixed", "x" * 64, ""],
)
def test_invalid_namespace_is_rejected_before_connect(schema):
    with pytest.raises(ValueError, match="schema"):
        PostgresEvidenceStore("unused", schema=schema)


@pytest.mark.parametrize("timeout", [0, -1, True, 1.5])
def test_invalid_timeout_is_rejected_before_connect(timeout):
    with pytest.raises(ValueError, match="Timeout"):
        PostgresEvidenceStore("unused", lock_timeout_ms=timeout)


@pytest.mark.parametrize("minimum,maximum", [(0, 1), (2, 1), (True, 1), (1, 1.5)])
def test_invalid_pool_sizes_fail_before_connect(minimum, maximum):
    with pytest.raises(ValueError, match="Pool sizes"):
        PostgresEvidenceStore("unused", pool_min_size=minimum, pool_max_size=maximum)


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_invalid_pool_timeout_fails_before_connect(timeout):
    with pytest.raises(ValueError, match="Pool timeout"):
        PostgresEvidenceStore("unused", pool_timeout=timeout)
