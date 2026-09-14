"""Tests for the DTOs: LLMConfig's tool parameters, KeyInfo, SyncReport."""

import logging

import pytest

import llmbroker
from llmbroker.models import RESERVED_BODY_KEYS, KeyInfo, LLMConfig, PendingKey, SyncReport


def _core(**kw) -> dict:
    return {"name": "g", "base_url": "u", "model": "m", "api_key_ref": "K", **kw}


def _restored(cfg: LLMConfig, metadata: dict | None) -> LLMConfig:
    return LLMConfig.from_metadata(
        name=cfg.name,
        base_url=cfg.base_url,
        model=cfg.model,
        api_key_ref=cfg.api_key_ref,
        metadata=metadata,
    )


# --- LLMConfig.tool_params ------------------------------------------------


def test_tool_params_survive_the_metadata_round_trip():
    cfg = LLMConfig(**_core(tool_params={"reasoning_effort": "none", "nested": {"a": [1]}}))
    metadata = cfg.to_metadata()
    assert metadata == {"tool_params": {"reasoning_effort": "none", "nested": {"a": [1]}}}
    assert _restored(cfg, metadata) == cfg


def test_a_config_without_tool_params_stores_none():
    cfg = LLMConfig(**_core())
    assert cfg.tool_params == {}
    assert cfg.to_metadata() == {}
    assert _restored(cfg, None) == cfg


def test_tool_params_keep_the_config_hashable():
    cfg = LLMConfig(**_core(tool_params={"reasoning_effort": "none"}))
    assert hash(cfg) == hash(LLMConfig(**_core()))
    assert cfg != LLMConfig(**_core())


def test_a_config_owns_its_tool_params():
    needs = {"reasoning_effort": "none"}
    cfg = LLMConfig(**_core(tool_params=needs))
    needs["reasoning_effort"] = "high"
    assert cfg.tool_params == {"reasoning_effort": "none"}


@pytest.mark.parametrize("key", sorted(RESERVED_BODY_KEYS))
def test_a_reserved_key_in_tool_params_is_refused_at_construction(key):
    with pytest.raises(ValueError, match=f"{key!r} is built by llmbroker"):
        LLMConfig(**_core(tool_params={key: "x"}))


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("none", {}),
        (["reasoning_effort"], {}),
        ({"model": "other", "reasoning_effort": "none"}, {"reasoning_effort": "none"}),
    ],
)
def test_malformed_stored_tool_params_are_dropped_not_raised(stored, expected, caplog):
    """A malformed row in a shared database must not stop a broker building its pool."""
    with caplog.at_level(logging.WARNING, logger="llmbroker.registry"):
        cfg = LLMConfig.from_metadata(**_core(), metadata={"tool_params": stored})
    assert cfg.tool_params == expected
    assert any("tool_params" in r.message for r in caplog.records)


def test_key_info_full():
    info = KeyInfo(
        api_key_ref="GROQ_API_KEY",
        help="Create a free account.",
        extra={"effort": "signup", "value": "good"},
    )
    assert info.api_key_ref == "GROQ_API_KEY"
    assert info.help == "Create a free account."
    assert info.extra == {"effort": "signup", "value": "good"}


def test_key_info_no_extra():
    info = KeyInfo(api_key_ref="K", help="", extra={})
    assert info.extra == {}


# --- SyncReport ---------------------------------------------------------


def test_sync_report_defaults_to_a_no_op():
    report = SyncReport(source="freetier", applied=True)
    assert report.added == report.updated == report.removed == ()
    assert report.pending_keys == ()


def test_report_types_and_refusal_are_top_level():
    assert llmbroker.SyncReport is SyncReport
    assert llmbroker.PendingKey is PendingKey
    exc = llmbroker.SyncRefusedError("nope", report=SyncReport(source="s", applied=False))
    assert isinstance(exc, llmbroker.LLMBrokerError)
    assert exc.report.source == "s"
    with pytest.raises(llmbroker.SyncRefusedError):
        raise exc
