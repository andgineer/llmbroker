"""Unit tests for LLMPool: slot invariants and key-resolution handling."""

from llmbroker.broker.pool import LLMPool
from llmbroker.models import LLMConfig


def _cfg(name="p1") -> LLMConfig:
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref="K")


def test_add_new_enqueues_one_slot():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "key")
    assert pool._queue.qsize() == 1
    assert "p1" in pool


def test_add_existing_does_not_enqueue_extra_slot():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "key")
    pool.add(_cfg(), "key2")  # same name — update, no extra queue slot
    assert pool._queue.qsize() == 1


def test_add_none_key_preserves_existing_key():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "original")
    pool.add(_cfg(), None)  # None means "leave key intact"
    assert pool.resolved_key("p1") == "original"


def test_add_nonnone_key_overwrites_existing_key():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "old")
    pool.add(_cfg(), "new")
    assert pool.resolved_key("p1") == "new"


def test_add_with_none_key_for_new_entry_leaves_no_key():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), None)
    assert not pool.has_key("p1")


def test_drop_removes_config_and_key():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "key")
    pool.drop("p1")
    assert "p1" not in pool
    assert not pool.has_key("p1")


def test_drop_nonexistent_does_not_raise():
    pool = LLMPool(state_store=None, user_id=None)
    pool.drop("ghost")  # must be silent


def test_len_tracks_membership():
    pool = LLMPool(state_store=None, user_id=None)
    assert len(pool) == 0
    pool.add(_cfg("a"), "k")
    pool.add(_cfg("b"), "k")
    assert len(pool) == 2
    pool.drop("a")
    assert len(pool) == 1
