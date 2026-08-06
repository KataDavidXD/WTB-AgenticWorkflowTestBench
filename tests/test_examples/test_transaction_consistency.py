"""
Tests for transaction consistency example fixes.

Validates:
1. Scenario B line-item filtering uses li["data"].get("order_id") correctly
2. SimulatedDataStore transactional atomicity (begin/commit/rollback)

Run with:
    pytest tests/test_examples/test_transaction_consistency.py -v
"""

import copy
from typing import Any

import pytest

# ═══════════════════════════════════════════════════════════════════════════════
# SimulatedDataStore – lightweight in-memory store with transaction support
# ═══════════════════════════════════════════════════════════════════════════════


class SimulatedDataStore:
    """
    In-memory data store that mirrors the pattern used in the
    transaction_consistency example scenarios.

    Supports begin_transaction / commit_transaction / rollback_transaction
    to verify ACID-like behaviour in example tests.
    """

    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._snapshot: dict[str, list[dict[str, Any]]] | None = None
        self._in_transaction = False

    def insert(self, table: str, record: dict[str, Any]) -> None:
        self._tables.setdefault(table, []).append(record)

    def query(self, table: str) -> list[dict[str, Any]]:
        return list(self._tables.get(table, []))

    def begin_transaction(self) -> None:
        if self._in_transaction:
            raise RuntimeError("Nested transactions are not supported")
        self._snapshot = copy.deepcopy(self._tables)
        self._in_transaction = True

    def commit_transaction(self) -> None:
        if not self._in_transaction:
            raise RuntimeError("No active transaction to commit")
        self._snapshot = None
        self._in_transaction = False

    def rollback_transaction(self) -> None:
        if not self._in_transaction:
            raise RuntimeError("No active transaction to rollback")
        self._tables = self._snapshot  # type: ignore[assignment]
        self._snapshot = None
        self._in_transaction = False

    @property
    def in_transaction(self) -> bool:
        return self._in_transaction


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario B line-item filtering helpers
# ═══════════════════════════════════════════════════════════════════════════════


def make_line_item(
    table: str,
    record_id: str,
    order_id: str,
    product: str,
) -> dict[str, Any]:
    """Build a line item in the canonical format used by scenarios."""
    return {
        "table": table,
        "id": record_id,
        "data": {"order_id": order_id, "product": product},
    }


def filter_line_items_correct(
    items: list[dict[str, Any]],
    target_order_id: str,
) -> list[dict[str, Any]]:
    """Correct filtering – uses li['data'].get('order_id')."""
    return [li for li in items if li["data"].get("order_id") == target_order_id]


def filter_line_items_buggy(
    items: list[dict[str, Any]],
    target_order_id: str,
) -> list[dict[str, Any]]:
    """Buggy filtering – accesses 'order_id' at the top level (wrong)."""
    return [li for li in items if li.get("order_id") == target_order_id]


# ═══════════════════════════════════════════════════════════════════════════════
# Test: Scenario B filtering correctness
# ═══════════════════════════════════════════════════════════════════════════════


class TestScenarioBFiltering:
    """Verify Scenario B line-item filtering uses the data field."""

    @pytest.fixture
    def line_items(self) -> list[dict[str, Any]]:
        return [
            make_line_item("orders", "1", "o1", "Widget"),
            make_line_item("orders", "2", "o1", "Gadget"),
            make_line_item("orders", "3", "o2", "Sprocket"),
            make_line_item("orders", "4", "o3", "Doohickey"),
        ]

    def test_verify_uses_data_field(self, line_items: list[dict[str, Any]]) -> None:
        """Correct filter drills into li['data'] to find order_id."""
        result = filter_line_items_correct(line_items, "o1")
        assert len(result) == 2
        assert all(r["data"]["order_id"] == "o1" for r in result)

    def test_buggy_filter_returns_nothing(self, line_items: list[dict[str, Any]]) -> None:
        """
        The old (buggy) approach checks li.get('order_id') at the root
        level where 'order_id' doesn't exist, so nothing is returned.
        """
        result = filter_line_items_buggy(line_items, "o1")
        assert len(result) == 0

    def test_correct_filter_single_match(self, line_items: list[dict[str, Any]]) -> None:
        result = filter_line_items_correct(line_items, "o2")
        assert len(result) == 1
        assert result[0]["id"] == "3"
        assert result[0]["data"]["product"] == "Sprocket"

    def test_correct_filter_no_match(self, line_items: list[dict[str, Any]]) -> None:
        result = filter_line_items_correct(line_items, "nonexistent")
        assert len(result) == 0

    def test_correct_filter_all_match(self) -> None:
        items = [make_line_item("t", str(i), "same", f"p{i}") for i in range(5)]
        result = filter_line_items_correct(items, "same")
        assert len(result) == 5

    def test_data_field_structure(self, line_items: list[dict[str, Any]]) -> None:
        """Every line item has the expected nested structure."""
        for li in line_items:
            assert "table" in li
            assert "id" in li
            assert "data" in li
            assert isinstance(li["data"], dict)
            assert "order_id" in li["data"]
            assert "product" in li["data"]


# ═══════════════════════════════════════════════════════════════════════════════
# Test: SimulatedDataStore transactional atomicity
# ═══════════════════════════════════════════════════════════════════════════════


class TestSimulatedDataStoreAtomicity:
    """Verify begin/commit/rollback semantics of SimulatedDataStore."""

    @pytest.fixture
    def store(self) -> SimulatedDataStore:
        return SimulatedDataStore()

    def test_insert_and_query(self, store: SimulatedDataStore) -> None:
        store.insert("users", {"id": "u1", "name": "Alice"})
        rows = store.query("users")
        assert len(rows) == 1
        assert rows[0]["name"] == "Alice"

    def test_rollback_restores_state(self, store: SimulatedDataStore) -> None:
        store.insert("orders", {"id": "o1", "total": 100})
        assert len(store.query("orders")) == 1

        store.begin_transaction()
        store.insert("orders", {"id": "o2", "total": 200})
        store.insert("orders", {"id": "o3", "total": 300})
        assert len(store.query("orders")) == 3

        store.rollback_transaction()
        assert len(store.query("orders")) == 1
        assert store.query("orders")[0]["id"] == "o1"

    def test_commit_persists_state(self, store: SimulatedDataStore) -> None:
        store.begin_transaction()
        store.insert("products", {"id": "p1", "name": "Widget"})
        store.insert("products", {"id": "p2", "name": "Gadget"})
        store.commit_transaction()

        assert len(store.query("products")) == 2

    def test_rollback_empty_table(self, store: SimulatedDataStore) -> None:
        """Rollback on a previously empty table should return to empty."""
        store.begin_transaction()
        store.insert("temp", {"id": "t1"})
        store.rollback_transaction()

        assert store.query("temp") == []

    def test_nested_transaction_raises(self, store: SimulatedDataStore) -> None:
        store.begin_transaction()
        with pytest.raises(RuntimeError, match="Nested transactions"):
            store.begin_transaction()
        store.rollback_transaction()

    def test_commit_without_begin_raises(self, store: SimulatedDataStore) -> None:
        with pytest.raises(RuntimeError, match="No active transaction"):
            store.commit_transaction()

    def test_rollback_without_begin_raises(self, store: SimulatedDataStore) -> None:
        with pytest.raises(RuntimeError, match="No active transaction"):
            store.rollback_transaction()

    def test_in_transaction_flag(self, store: SimulatedDataStore) -> None:
        assert not store.in_transaction
        store.begin_transaction()
        assert store.in_transaction
        store.commit_transaction()
        assert not store.in_transaction

    def test_multiple_transactions(self, store: SimulatedDataStore) -> None:
        """Sequential transactions should work independently."""
        store.begin_transaction()
        store.insert("t", {"id": "1"})
        store.commit_transaction()

        store.begin_transaction()
        store.insert("t", {"id": "2"})
        store.rollback_transaction()

        rows = store.query("t")
        assert len(rows) == 1
        assert rows[0]["id"] == "1"

    def test_rollback_does_not_affect_other_tables(
        self, store: SimulatedDataStore
    ) -> None:
        """
        Pre-existing data in another table inserted before the transaction
        should survive rollback.
        """
        store.insert("stable", {"id": "s1"})

        store.begin_transaction()
        store.insert("volatile", {"id": "v1"})
        store.rollback_transaction()

        assert len(store.query("stable")) == 1
        assert store.query("volatile") == []

    def test_commit_snapshot_cleanup(self, store: SimulatedDataStore) -> None:
        """After commit, the snapshot should be discarded."""
        store.begin_transaction()
        store.insert("t", {"id": "1"})
        store.commit_transaction()
        assert store._snapshot is None
