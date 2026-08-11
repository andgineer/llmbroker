"""The merge: the mirror, the key report, the structural guard."""

import pytest

from llmbroker.broker.merge import check_not_emptying, merge_upstream
from llmbroker.exceptions import SyncRefusedError
from llmbroker.models import KeyInfo, ModelList, LLMConfig


def _cfg(name, ref="K", *, model="m", url="https://x/v1", from_preset=True):
    """Written by a sync unless stated otherwise; `from_preset=False` is an entry the
    installation put in its own registry."""
    return LLMConfig(
        name=name,
        base_url=url,
        model=model,
        api_key_ref=ref,
        from_preset=from_preset,
    )


def _merge(new, current, present=frozenset(), *, new_keys=None, current_keys=None):
    merged, report = merge_upstream(
        ModelList(configs=list(new), keys=dict(new_keys or {})),
        ModelList(configs=list(current), keys=dict(current_keys or {})),
        frozenset(present),
        source="freetier",
    )
    return merged.configs, merged.keys, report


# ── The mirror ───────────────────────────────────────────────────────────────


def test_an_entry_absent_from_the_arriving_list_is_removed_though_its_key_resolves():
    """The one case the removal rule existed to protect, now removed like any other:
    nothing weighs whether an absent entry might still work."""
    merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ")],
        present={"GEMINI", "GROQ"},
    )
    assert [c.name for c in merged] == ["gemini"]
    assert (report.removed, report.added) == (("groq-old",), ("gemini",))


def test_an_entry_the_installation_wrote_survives_that_same_sync():
    """The partition, asserted against the sync that removes its curated neighbour."""
    merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ"), _cfg("mine", "GROQ", from_preset=False)],
        present={"GEMINI", "GROQ"},
    )
    assert [c.name for c in merged] == ["gemini", "mine"]
    assert (report.removed, report.updated) == (("groq-old",), ())


def test_the_model_list_carrying_the_provider_replaces_the_entry():
    merged, _keys, report = _merge([_cfg("groq-new", "GROQ")], [_cfg("groq-old", "GROQ")])
    assert [c.name for c in merged] == ["groq-new"]
    assert (report.removed, report.added) == (("groq-old",), ("groq-new",))


def test_two_old_entries_on_the_carried_ref_both_go():
    merged, _keys, report = _merge(
        [_cfg("new", "GROQ")],
        [_cfg("old-a", "GROQ"), _cfg("old-b", "GROQ")],
        present={"GROQ"},
    )
    assert (report.removed, [c.name for c in merged]) == (("old-a", "old-b"), ["new"])


def test_a_keyless_provider_the_model_list_dropped_goes_too():
    merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ")],
        present={"GEMINI"},
    )
    assert ([c.name for c in merged], report.removed) == (["gemini"], ("groq-old",))


def test_the_same_merge_repeated_three_times_does_not_drift():
    current = [_cfg("groq-old", "GROQ")]
    new = [_cfg("gemini", "GEMINI")]
    for _ in range(3):
        current, _keys, report = _merge(new, current, present={"GEMINI", "GROQ"})
        assert [c.name for c in current] == ["gemini"]
    assert (report.added, report.removed) == ((), ())


# ── The unused-key advice ────────────────────────────────────────────────────


def test_a_ref_nothing_references_any_more_is_reported_as_unused():
    """A key that is here and has just lost its last user: the one case where
    revoking it at the provider is a real thing to do."""
    _merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ")],
        present={"GEMINI", "GROQ"},
    )
    assert (report.removed, report.orphan_refs) == (("groq-old",), ("GROQ",))


def test_an_own_entry_on_the_same_ref_keeps_it_out_of_the_orphan_advice():
    _merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ"), _cfg("groq-paid", "GROQ", from_preset=False)],
        present={"GEMINI", "GROQ"},
    )
    assert (report.removed, report.orphan_refs) == (("groq-old",), ())


def test_a_ref_with_no_key_behind_it_is_nothing_to_revoke():
    """The commonest removal of all — curation drops a provider you never had a key
    for. Advising a revocation there is noise in the one channel an admin reads."""
    _merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ")],
        present={"GEMINI"},
    )
    assert (report.removed, report.orphan_refs) == (("groq-old",), ())


def test_a_replaced_providers_ref_is_not_an_orphan():
    _merged, _keys, report = _merge([_cfg("groq-new", "GROQ")], [_cfg("groq-old", "GROQ")])
    assert (report.removed, report.orphan_refs) == (("groq-old",), ())


# ── The installation's own entries, keys, and the refusals ──────────────────


def test_own_entries_and_their_keys_are_carried_over():
    merged, keys, report = _merge(
        [_cfg("groq-new", "GROQ")],
        [_cfg("groq-old", "GROQ"), _cfg("mine", "MY_KEY", from_preset=False)],
        current_keys={"MY_KEY": KeyInfo(api_key_ref="MY_KEY", help="my help", extra={})},
    )
    assert [c.name for c in merged] == ["groq-new", "mine"]
    assert keys["MY_KEY"].help == "my help"
    assert "mine" not in report.removed + report.added


def test_an_arriving_entry_never_replaces_one_the_installation_wrote_itself():
    """An arriving list is entirely a sync's, so nothing in it can move an entry the
    installation put in its own registry, and the report says so."""
    merged, _keys, report = _merge(
        [_cfg("mine", "MY_KEY", model="v2", url="https://new/v1", from_preset=False)],
        [_cfg("mine", "MY_KEY", model="v1", from_preset=False)],
    )
    assert [(c.name, c.model) for c in merged] == [("mine", "v1")]
    assert report.updated == ()


def test_key_help_travels_with_the_pending_key():
    _merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [],
        new_keys={"GEMINI": KeyInfo(api_key_ref="GEMINI", help="gemini help", extra={})},
    )
    assert [(p.api_key_ref, p.help) for p in report.pending_keys] == [("GEMINI", "gemini help")]


def test_a_pending_key_without_help_is_not_an_error():
    _merged, _keys, report = _merge([_cfg("gemini", "GEMINI")], [])
    assert [(p.api_key_ref, p.help) for p in report.pending_keys] == [("GEMINI", "")]


def test_a_name_clash_between_a_synced_and_an_own_entry_is_refused():
    with pytest.raises(ValueError, match="two entries named 'clash'"):
        _merge([_cfg("clash", "A")], [_cfg("clash", "B", from_preset=False)])


def test_a_model_change_under_an_existing_name_is_refused():
    with pytest.raises(ValueError, match="refusing to change model"):
        _merge([_cfg("p1", model="model-b")], [_cfg("p1", model="model-a")])


def test_report_fields_on_a_no_op_run():
    current = [_cfg("a", "A"), _cfg("b", "B")]
    merged, _keys, report = _merge(current, current, present={"A", "B"})
    assert [c.name for c in merged] == ["a", "b"]
    assert (report.added, report.updated, report.removed) == ((), (), ())
    assert (report.active_before, report.active_after) == (2, 2)


def test_a_no_op_run_still_names_the_keys_it_is_waiting_for():
    current = [_cfg("a", "A"), _cfg("b", "B")]
    _merged, _keys, report = _merge(current, current, present={"A"})
    assert (report.added, report.removed) == ((), ())
    assert [p.api_key_ref for p in report.pending_keys] == ["B"]


def test_an_updated_entry_is_reported_as_updated_not_added():
    _merged, _keys, report = _merge(
        [_cfg("a", "A", url="https://new/v1")],
        [_cfg("a", "A", url="https://old/v1")],
    )
    assert (report.updated, report.added, report.removed) == (("a",), (), ())


# ── The structural guard ─────────────────────────────────────────────────────


def test_an_empty_result_over_a_working_registry_is_refused():
    current = [_cfg("a", "A")]
    merged, _keys, report = _merge([], current, present={"A"})
    with pytest.raises(SyncRefusedError) as excinfo:
        check_not_emptying(merged, current, report)
    assert excinfo.value.report.applied is False


def test_an_empty_result_over_an_empty_registry_is_onboarding():
    merged, _keys, report = _merge([], [])
    check_not_emptying(merged, [], report)  # does not raise


def test_an_empty_arriving_list_empties_the_curated_half_and_reaches_the_guard():
    """The guard is on the normal path: nothing arrives, so every curated entry is
    removed however well-keyed it is, and the result is empty."""
    current = [_cfg("a", "A"), _cfg("b", "B")]
    merged, _keys, report = _merge([], current, present={"A", "B"})
    assert (merged, report.removed) == ([], ("a", "b"))
    with pytest.raises(SyncRefusedError):
        check_not_emptying(merged, current, report)


def test_an_own_entry_keeps_an_empty_arriving_list_off_the_guard():
    current = [_cfg("a", "A"), _cfg("mine", "M", from_preset=False)]
    merged, _keys, report = _merge([], current, present={"A", "M"})
    assert ([c.name for c in merged], report.removed) == (["mine"], ("a",))
    check_not_emptying(merged, current, report)  # does not raise
