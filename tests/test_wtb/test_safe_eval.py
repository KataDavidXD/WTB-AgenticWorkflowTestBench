"""Tests for safe_eval_condition (C8 fix: replaced eval())."""

import pytest

from wtb.application.services.execution_controller import safe_eval_condition


class TestSafeEvalCondition:
    def test_simple_true(self):
        assert safe_eval_condition("True", {}) is True

    def test_simple_false(self):
        assert safe_eval_condition("False", {}) is False

    def test_comparison(self):
        assert safe_eval_condition("x > 5", {"x": 10}) is True
        assert safe_eval_condition("x > 5", {"x": 3}) is False

    def test_equality(self):
        assert safe_eval_condition("status == 'active'", {"status": "active"}) is True

    def test_boolean_and(self):
        assert safe_eval_condition("x > 0 and y > 0", {"x": 1, "y": 1}) is True
        assert safe_eval_condition("x > 0 and y > 0", {"x": 1, "y": -1}) is False

    def test_boolean_or(self):
        assert safe_eval_condition("x > 0 or y > 0", {"x": -1, "y": 1}) is True

    def test_not(self):
        assert safe_eval_condition("not done", {"done": False}) is True

    def test_in_operator(self):
        assert safe_eval_condition("'a' in items", {"items": ["a", "b"]}) is True

    def test_undefined_variable_raises(self):
        with pytest.raises(NameError):
            safe_eval_condition("unknown > 5", {})

    def test_blocks_function_calls(self):
        """Verify that function calls like __import__ are blocked."""
        with pytest.raises(ValueError):
            safe_eval_condition("__import__('os')", {})

    def test_blocks_attribute_call(self):
        """Verify method calls are blocked (no Call node support)."""
        with pytest.raises(ValueError):
            safe_eval_condition("''.join(['a'])", {})

    def test_constant_string(self):
        assert safe_eval_condition("'hello'", {}) is True

    def test_constant_zero_is_false(self):
        assert safe_eval_condition("0", {}) is False

    def test_complex_comparison(self):
        assert safe_eval_condition("1 < x < 10", {"x": 5}) is True
        assert safe_eval_condition("1 < x < 10", {"x": 15}) is False
