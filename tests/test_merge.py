"""The merge: the removal rule, key evidence, the structural guard."""

from datetime import UTC, datetime

import pytest

from llmbroker.broker.keys import KeyEvidence
from llmbroker.broker.merge import check_not_emptying, merge_upstream
from llmbroker.exceptions import SyncRefusedError
from llmbroker.models import KeyInfo, Lineup, LLMConfig, Retirement

_EVIDENCE_TS = datetime(2030, 7, 2, tzinfo=UTC)


def _cfg(name, ref="K", *, model="m", url="https://x/v1", custom=False):
    return LLMConfig(
        name=name,
        base_url=url,
        model=model,
        api_key_ref=ref,
        custom=custom,
    )


def _merge(  # noqa: PLR0913
    new,
    current,
    present=frozenset(),
    *,
    new_keys=None,
    current_keys=None,
    dead=frozenset(),
    keys_visible=True,
    keys_scoped=False,
):
    merged, report = merge_upstream(
        Lineup(configs=list(new), keys=dict(new_keys or {})),
        Lineup(configs=list(current), keys=dict(current_keys or {})),
        KeyEvidence(
            present=frozenset(present),
            visible=keys_visible,
            scoped=keys_scoped,
        ),
        source="freetier",
        dead={n: Retirement(name=n, http_status=401, since=_EVIDENCE_TS) for n in dead},
    )
    return merged.configs, merged.keys, report


def _retired(report):
    return tuple(item.name for item in report.retired)


# ── The removal rule, one test per row of the table ──────────────────────────


def test_the_lineup_carrying_the_provider_replaces_the_entry_with_no_key_involved():
    """Same ref means same quota and same failure domain: nothing is lost."""
    merged, _keys, report = _merge([_cfg("groq-new", "GROQ")], [_cfg("groq-old", "GROQ")])
    assert [c.name for c in merged] == ["groq-new"]
    assert (report.removed, report.kept, report.added) == (
        ("groq-old",),
        (),
        ("groq-new",),
    )


def test_two_old_entries_on_the_carried_ref_both_go():
    """The unit is the provider, not the entry — there is nothing to count."""
    merged, _keys, report = _merge(
        [_cfg("new", "GROQ")],
        [_cfg("old-a", "GROQ"), _cfg("old-b", "GROQ")],
        present={"GROQ"},
    )
    assert report.removed == ("old-a", "old-b")
    assert (report.kept, [c.name for c in merged]) == ((), ["new"])


def test_a_provider_the_lineup_dropped_goes_when_no_key_exists_for_it():
    merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ")],
        present={"GEMINI"},
    )
    assert [c.name for c in merged] == ["gemini"]
    assert (report.removed, report.kept, _retired(report)) == (("groq-old",), (), ())


def test_a_provider_the_lineup_dropped_stays_while_its_key_is_here():
    merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ")],
        present={"GEMINI", "GROQ"},
    )
    assert [c.name for c in merged] == ["gemini", "groq-old"]
    assert (report.kept, report.kept_refs, report.removed) == (("groq-old",), ("GROQ",), ())


def test_a_kept_entry_the_journal_proved_dead_is_retired():
    _merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ")],
        present={"GEMINI", "GROQ"},
        dead={"groq-old"},
    )
    assert (report.removed, _retired(report), report.kept) == (("groq-old",), ("groq-old",), ())


def test_with_keys_invisible_the_entry_stays_whatever_present_says():
    """A merge site that cannot see the keys must never read absence as evidence."""
    _merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ")],
        present={"GEMINI"},
        keys_visible=False,
    )
    assert (report.kept, report.removed) == (("groq-old",), ())


def test_a_custom_entry_on_the_same_ref_keeps_it_out_of_the_orphan_advice():
    """The paid direct model on the retired provider: the key is still in use."""
    _merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ"), _cfg("groq-paid", "GROQ", custom=True)],
        present={"GEMINI", "GROQ"},
        dead={"groq-old"},
    )
    assert report.removed == ("groq-old",)
    assert report.orphan_refs == ()


def test_a_ref_nothing_references_any_more_is_reported_as_unused():
    """A key that is here and has just lost its last user: the one case where
    revoking it at the provider is a real thing to do."""
    _merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ")],
        present={"GEMINI", "GROQ"},
        dead={"groq-old"},
    )
    assert (_retired(report), report.orphan_refs) == (("groq-old",), ("GROQ",))


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


# ── The invariant the rule exists for ────────────────────────────────────────


def test_a_sync_never_takes_away_a_model_this_installation_can_call():
    """Invariant 11: an entry with a key goes only when the same provider replaces it,
    or when the journal says it does not work."""
    current = [_cfg(n, n.upper()) for n in ("a", "b", "c", "d", "e")]
    present = {"A", "B", "C", "D", "E"}
    _merged, _keys, report = _merge(current[:3], current, present=present)
    assert (report.kept, report.removed) == (("d", "e"), ())

    _merged, _keys, report = _merge(current[:3], current, present=present, dead={"d"})
    assert (report.removed, _retired(report), report.kept) == (("d",), ("d",), ("e",))


@pytest.mark.parametrize(
    ("present", "new_names"),
    [
        (set(), ("gemini",)),
        ({"GROQ"}, ("gemini",)),
        ({"GEMINI"}, ("gemini",)),
        ({"GROQ", "GEMINI"}, ()),
    ],
)
def test_the_callable_count_never_decreases(present, new_names):
    new = [_cfg(n, "GEMINI") for n in new_names]
    _merged, _keys, report = _merge(new, [_cfg("groq-old", "GROQ")], present=present)
    assert report.active_after >= report.active_before


# ── Kept entries are recomputed, never accumulated ───────────────────────────


def test_the_same_merge_repeated_three_times_does_not_drift():
    current = [_cfg("groq-old", "GROQ")]
    new = [_cfg("gemini", "GEMINI")]
    for _ in range(3):
        current, _keys, report = _merge(new, current, present={"GEMINI", "GROQ"})
        assert [c.name for c in current] == ["gemini", "groq-old"]
        assert report.kept == ("groq-old",)
    assert report.added == ()  # gemini arrived on the first pass only


def test_the_next_sync_removes_a_kept_entry_once_the_journal_condemns_it():
    """Convergence, fed the previous merge's own result: the rule that this replaces
    shipped green because its test handed the second merge a fresh arrival instead."""
    new = [_cfg("gemini", "GEMINI")]
    merged, _keys, report = _merge(new, [_cfg("groq-old", "GROQ")], present={"GEMINI", "GROQ"})
    assert report.kept == ("groq-old",)

    merged, _keys, report = _merge(new, merged, present={"GEMINI", "GROQ"}, dead={"groq-old"})
    assert (report.removed, _retired(report)) == (("groq-old",), ("groq-old",))
    assert [c.name for c in merged] == ["gemini"]

    merged, _keys, report = _merge(new, merged, present={"GEMINI", "GROQ"})
    assert (report.removed, report.kept, report.added) == ((), (), ())


# ── Custom entries, keys, and the refusals ───────────────────────────────────


def test_custom_entries_and_their_keys_are_carried_over():
    merged, keys, report = _merge(
        [_cfg("groq-new", "GROQ")],
        [_cfg("groq-old", "GROQ"), _cfg("mine", "MY_KEY", custom=True)],
        current_keys={"MY_KEY": KeyInfo(api_key_ref="MY_KEY", help="my help", extra={})},
    )
    assert [c.name for c in merged] == ["groq-new", "mine"]
    assert keys["MY_KEY"].help == "my help"
    assert "mine" not in report.removed + report.kept + report.added


def test_an_arriving_lineups_own_custom_entry_never_replaces_the_stored_one():
    """A curated lineup states the pool only — the fetch refuses `[[custom]]` — so
    nothing arriving can move a model the host declared, and the report says so."""
    merged, _keys, report = _merge(
        [_cfg("mine", "MY_KEY", model="v2", url="https://new/v1", custom=True)],
        [_cfg("mine", "MY_KEY", model="v1", custom=True)],
    )
    assert [(c.name, c.model) for c in merged] == [("mine", "v1")]
    assert report.updated == ()


def test_key_help_for_a_kept_entry_is_carried_over():
    _merged, keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ")],
        keys_visible=False,
        new_keys={"GEMINI": KeyInfo(api_key_ref="GEMINI", help="gemini help", extra={})},
        current_keys={"GROQ": KeyInfo(api_key_ref="GROQ", help="groq help", extra={})},
    )
    assert keys["GROQ"].help == "groq help"
    assert {p.api_key_ref: p.help for p in report.pending_keys} == {
        "GEMINI": "gemini help",
        "GROQ": "groq help",
    }


def test_a_kept_entry_without_key_help_is_not_an_error():
    _merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ")],
        keys_visible=False,
    )
    assert [(p.api_key_ref, p.help) for p in report.pending_keys] == [
        ("GEMINI", ""),
        ("GROQ", ""),
    ]


def test_a_name_clash_between_managed_and_custom_is_refused():
    with pytest.raises(ValueError, match="two entries named 'clash'"):
        _merge([_cfg("clash", "A")], [_cfg("clash", "B", custom=True)])


def test_a_model_change_under_an_existing_name_is_refused():
    with pytest.raises(ValueError, match="refusing to change model"):
        _merge([_cfg("p1", model="model-b")], [_cfg("p1", model="model-a")])


def test_report_fields_on_a_no_op_run():
    current = [_cfg("a", "A"), _cfg("b", "B")]
    merged, _keys, report = _merge(current, current, present={"A", "B"})
    assert [c.name for c in merged] == ["a", "b"]
    assert (report.added, report.updated, report.removed, report.kept) == ((), (), (), ())
    assert (report.active_before, report.active_after) == (2, 2)


def test_a_no_op_run_still_names_the_keys_it_is_waiting_for():
    current = [_cfg("a", "A"), _cfg("b", "B")]
    _merged, _keys, report = _merge(current, current, present={"A"})
    assert (report.added, report.removed, report.kept) == ((), (), ())
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
    _merged, _keys, report = _merge([], current, present={"A"})
    with pytest.raises(SyncRefusedError) as excinfo:
        check_not_emptying([], current, report)
    assert excinfo.value.report.applied is False


def test_an_empty_result_over_an_empty_registry_is_onboarding():
    _merged, _keys, report = _merge([], [])
    check_not_emptying([], [], report)  # does not raise


def test_an_empty_lineup_over_a_keyless_registry_reaches_the_guard():
    """The guard is on the normal path now: nothing arrives, no key exists for
    anything already there, so every entry is removable and the result is empty."""
    current = [_cfg("a", "A"), _cfg("b", "B")]
    merged, _keys, report = _merge([], current, present={"C"})
    assert (merged, report.removed) == ([], ("a", "b"))
    with pytest.raises(SyncRefusedError):
        check_not_emptying(merged, current, report)


def test_an_empty_lineup_over_a_keyed_registry_keeps_everything():
    current = [_cfg("a", "A"), _cfg("b", "B")]
    merged, _keys, report = _merge([], current, present={"A", "B"})
    assert [c.name for c in merged] == ["a", "b"]
    assert report.kept == ("a", "b")
    check_not_emptying(merged, current, report)  # does not raise
