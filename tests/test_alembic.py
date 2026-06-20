"""Tests for the Alembic autogenerate filter."""

from llmbroker.alembic import include_object


def test_llmbroker_table_excluded():
    assert include_object(None, "llmbroker_calls", "table", False, None) is False


def test_llmbroker_registry_excluded():
    assert include_object(None, "llmbroker_registry", "table", False, None) is False


def test_llmbroker_prefix_any_name_excluded():
    assert include_object(None, "llmbroker_secrets", "index", False, None) is False


def test_non_llmbroker_table_included():
    assert include_object(None, "expenses", "table", False, None) is True


def test_none_name_included():
    assert include_object(None, None, "table", False, None) is True


def test_empty_name_included():
    assert include_object(None, "", "table", False, None) is True
