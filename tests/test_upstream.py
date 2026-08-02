"""The sync engine: the removal rule, key presence, the fetch, the writers."""

import http.client
import stat
import tomllib
import urllib.error
from datetime import UTC, datetime

import pytest

from llmbroker.broker import upstream
from llmbroker.broker.upstream import (
    catalog_alias_index,
    check_not_emptying,
    configs_from_data,
    fetch_preset_text,
    key_infos_from_data,
    keys_are_visible,
    merge_upstream,
    present_refs,
    refresh_alias_entries,
    render_merged_toml,
    sync_file,
    write_atomic,
)
from llmbroker.exceptions import SyncRefusedError
from llmbroker.models import KeyInfo, LLMConfig, Retirement
from llmbroker.standalone.secrets import DictSecrets, Secrets

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
    return merge_upstream(
        list(new),
        dict(new_keys or {}),
        list(current),
        dict(current_keys or {}),
        set(present),
        source="freetier",
        dead={n: Retirement(name=n, http_status=401, since=_EVIDENCE_TS) for n in dead},
        keys_visible=keys_visible,
        keys_scoped=keys_scoped,
    )


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


def test_a_probe_that_resolved_nothing_at_all_proves_nothing():
    """The empty-probe clause: registry in llms.toml, secrets in Vault — absence here
    says the keys live somewhere this merge site cannot reach."""
    assert keys_are_visible(set(), scope=None, have_keys=False) is False
    _merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ")],
        present=set(),
        keys_visible=keys_are_visible(set(), scope=None, have_keys=False),
    )
    assert report.kept == ("groq-old",)


def test_a_scoped_installation_needs_a_declaration_before_absence_counts():
    assert keys_are_visible({"GEMINI"}, scope="alice", have_keys=False) is False
    assert keys_are_visible({"GEMINI"}, scope="alice", have_keys=["GEMINI"]) is True
    assert keys_are_visible({"GEMINI"}, scope=None, have_keys=False) is True


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


async def test_have_keys_declares_a_ref_the_backend_cannot_resolve():
    present = await present_refs(
        ["GEMINI", "GROQ"],
        DictSecrets({}),
        scope=None,
        have_keys=["GEMINI"],
    )
    _merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ")],
        present=present,
    )
    assert report.removed == ("groq-old",)


# ── The invariant the rule exists for ────────────────────────────────────────


def test_a_sync_never_takes_away_a_model_this_installation_can_call():
    """Invariant 4: an entry with a key goes only when the same provider replaces it,
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


def test_a_custom_entry_in_the_new_lineup_wins_over_the_stored_one():
    """Syncing a vendored file into a DB must carry the file's own [[custom]] edits."""
    merged, _keys, _report = _merge(
        [_cfg("mine", "MY_KEY", model="v2", custom=True)],
        [_cfg("mine", "MY_KEY", model="v1", custom=True)],
    )
    assert [(c.name, c.model) for c in merged] == [("mine", "v2")]


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
    assert str(report).endswith("no changes")


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


# ── present_refs ─────────────────────────────────────────────────────────────


async def test_present_refs_prefers_a_scope_prefixed_ref():
    present = await present_refs(
        ["K"],
        DictSecrets({"alice/K": "own"}),
        scope="alice",
        have_keys=False,
    )
    assert present == {"K"}


async def test_present_refs_reads_an_empty_value_as_absent(monkeypatch):
    monkeypatch.setenv("BLANK", "")
    monkeypatch.setenv("FILLED", "v")
    present = await present_refs(["BLANK", "FILLED"], Secrets(), scope=None, have_keys=False)
    assert present == {"FILLED"}


async def test_present_refs_with_have_keys_true_declares_everything():
    present = await present_refs(["A", "B"], DictSecrets({}), scope=None, have_keys=True)
    assert present == {"A", "B"}


async def test_present_refs_ignores_an_empty_ref():
    present = await present_refs(["", "A"], DictSecrets({"A": "v"}), scope=None, have_keys=True)
    assert present == {"A"}


async def test_have_keys_as_a_bare_string_declares_that_one_ref():
    """A str is a Sequence[str]; taken apart into characters it declares nothing."""
    present = await present_refs(
        ["GEMINI_API_KEY", "GROQ"],
        DictSecrets({}),
        scope=None,
        have_keys="GEMINI_API_KEY",
    )
    assert present == {"GEMINI_API_KEY"}


# ── Writers ──────────────────────────────────────────────────────────────────


_PRESET_ENDING_IN_A_KEYS_TABLE = (
    '[[llms]]\nname = "p"\nbase_url = "https://x/v1"\nmodel = "m"\napi_key_ref = "P"\n'
    '\n[keys.P]\nhelp = "p help"\n'
)


def test_short_entries_stay_top_level_after_a_trailing_keys_table():
    """tomli_w renders a short array of tables inline, which after a [keys.*] table
    parses as a member of it — every entry must carry its own explicit header."""
    text = render_merged_toml(
        _PRESET_ENDING_IN_A_KEYS_TABLE,
        [_cfg("l", "K", url="http://h/v1")],
        [{"name": "c", "base_url": "http://h/v1", "model": "m", "api_key_ref": "K"}],
        {"K": {"help": "k"}},
    )
    data = tomllib.loads(text)
    assert [e["name"] for e in data["llms"]] == ["p", "l"]
    assert [e["name"] for e in data["custom"]] == ["c"]
    assert set(data["keys"]) == {"P", "K"}
    assert "custom" not in data["keys"]["P"]


def test_the_new_text_is_written_verbatim_with_its_comments():
    text = render_merged_toml("# a comment\n" + _PRESET_ENDING_IN_A_KEYS_TABLE, [], [], {})
    assert text.startswith("# a comment\n")


def test_kept_entries_are_written_under_a_generated_header():
    text = render_merged_toml(_PRESET_ENDING_IN_A_KEYS_TABLE, [_cfg("kept-one", "K")], [], {})
    assert "# Kept from your previous lineup" in text
    assert [e["name"] for e in tomllib.loads(text)["llms"]] == ["p", "kept-one"]


def test_write_atomic_leaves_no_temp_files(tmp_path):
    target = tmp_path / "llms.toml"
    write_atomic(target, "a = 1\n")
    assert target.read_text() == "a = 1\n"
    assert [p.name for p in tmp_path.iterdir()] == ["llms.toml"]


def test_write_atomic_keeps_the_targets_permissions(tmp_path):
    """The temp file is created 0600 and os.replace carries its mode over — a runtime
    sync must not lock the serving process out of the config it just rewrote."""
    target = tmp_path / "llms.toml"
    target.write_text("a = 1\n")
    target.chmod(0o644)
    write_atomic(target, "a = 2\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_write_atomic_creates_a_missing_target_without_reading_a_mode(tmp_path):
    target = tmp_path / "fresh.toml"
    write_atomic(target, "a = 1\n")
    assert target.read_text() == "a = 1\n"


# ── The alias-following refresh ──────────────────────────────────────────────

_CATALOG = {
    "provider": [
        {
            "id": "anthropic",
            "base_url": "https://api.anthropic.com/v2",
            "api_key_ref": "ANTHROPIC_API_KEY",
            "key_help": "console.anthropic.com",
            "models": [{"alias": "opus", "model": "claude-opus-5"}],
        },
    ],
}


def test_alias_entries_follow_the_catalog():
    entries = [{"alias": "opus", "name": "anthropic-claude-opus-4-8", "model": "claude-opus-4-8"}]
    refresh = refresh_alias_entries(entries, catalog_alias_index(_CATALOG))
    assert entries[0]["model"] == "claude-opus-5"
    assert entries[0]["name"] == "anthropic-claude-opus-5"
    assert refresh.key_help == {"ANTHROPIC_API_KEY": "console.anthropic.com"}
    assert "opus: claude-opus-4-8 -> claude-opus-5" in refresh.notices


def test_an_unknown_alias_warns_and_leaves_the_entry_alone():
    entries = [{"alias": "ghost", "name": "x-y", "model": "y"}]
    refresh = refresh_alias_entries(entries, catalog_alias_index(_CATALOG))
    assert entries[0]["model"] == "y"
    assert refresh.warnings == ("alias 'ghost' is not in the paid catalog — entry left untouched",)


def test_a_duplicate_catalog_alias_is_an_invalid_catalog():
    catalog = {
        "provider": [
            *_CATALOG["provider"],
            {
                "id": "dup",
                "base_url": "https://dup/v1",
                "api_key_ref": "D",
                "models": [{"alias": "opus", "model": "dup-1"}],
            },
        ],
    }
    with pytest.raises(ValueError, match="alias 'opus' is used twice"):
        catalog_alias_index(catalog)


# ── sync_file: the file target end to end ────────────────────────────────────

_NEW = (
    '[[llms]]\nname = "gemini"\nbase_url = "https://g/v1"\nmodel = "m"\n'
    'api_key_ref = "GEMINI_API_KEY"\n\n[keys.GEMINI_API_KEY]\nhelp = "gemini help"\n'
)


def _write_current(tmp_path, body):
    target = tmp_path / "llms.toml"
    target.write_text(body)
    return target


async def test_sync_file_keeps_a_dropped_entry_without_a_usable_replacement(tmp_path):
    target = _write_current(
        tmp_path,
        '[[llms]]\nname="groq-old"\nbase_url="https://groq/v1"\nmodel="m"'
        '\napi_key_ref="GROQ_API_KEY"\n[keys.GROQ_API_KEY]\nhelp = "groq help"\n',
    )
    outcome = await sync_file(_NEW, target, source="freetier", secrets=DictSecrets({}))
    data = tomllib.loads(target.read_text())
    assert [e["name"] for e in data["llms"]] == ["gemini", "groq-old"]
    assert data["keys"]["GROQ_API_KEY"]["help"] == "groq help"
    assert outcome.report.kept == ("groq-old",)


async def test_sync_file_removes_it_once_the_arrivals_key_is_there(tmp_path):
    target = _write_current(
        tmp_path,
        '[[llms]]\nname="groq-old"\nbase_url="https://groq/v1"\nmodel="m"'
        '\napi_key_ref="GROQ_API_KEY"\n',
    )
    outcome = await sync_file(
        _NEW,
        target,
        source="freetier",
        secrets=DictSecrets({"GEMINI_API_KEY": "sk-x"}),
    )
    assert [e["name"] for e in tomllib.loads(target.read_text())["llms"]] == ["gemini"]
    assert outcome.report.removed == ("groq-old",)


async def test_sync_file_reads_keys_from_the_sibling_env_file(tmp_path):
    """The CLI resolves exactly what the application will — env plus the file's own .env."""
    target = _write_current(
        tmp_path,
        '[[llms]]\nname="groq-old"\nbase_url="https://groq/v1"\nmodel="m"'
        '\napi_key_ref="GROQ_API_KEY"\n',
    )
    (tmp_path / ".env").write_text("GEMINI_API_KEY=sk-from-file\n")
    outcome = await sync_file(
        _NEW,
        target,
        source="freetier",
        secrets=Secrets(tmp_path / ".env"),
    )
    assert outcome.report.removed == ("groq-old",)


async def test_sync_file_leaves_the_target_untouched_on_a_clash(tmp_path):
    target = _write_current(
        tmp_path,
        '[[custom]]\nname="gemini"\nbase_url="https://mine/v1"\nmodel="m"\napi_key_ref="MY_KEY"\n',
    )
    original = target.read_text()
    with pytest.raises(ValueError, match="two entries named 'gemini'"):
        await sync_file(_NEW, target, source="freetier", secrets=DictSecrets({}))
    assert target.read_text() == original


async def test_sync_file_with_an_empty_lineup_loses_nothing(tmp_path):
    target = _write_current(
        tmp_path,
        '[[llms]]\nname="groq-old"\nbase_url="https://groq/v1"\nmodel="m"\napi_key_ref="G"\n',
    )
    outcome = await sync_file("", target, source="freetier", secrets=DictSecrets({"G": "sk"}))
    assert [e["name"] for e in tomllib.loads(target.read_text())["llms"]] == ["groq-old"]
    assert outcome.report.kept == ("groq-old",)


async def test_sync_file_creates_a_missing_target(tmp_path):
    target = tmp_path / "new.toml"
    outcome = await sync_file(_NEW, target, source="freetier", secrets=DictSecrets({}))
    assert [e["name"] for e in tomllib.loads(target.read_text())["llms"]] == ["gemini"]
    assert outcome.report.added == ("gemini",)


async def test_sync_file_refuses_to_write_a_file_that_does_not_match_the_merge(tmp_path):
    """The last guard before the one write that can destroy a live config: a source
    carrying its own [[custom]] renders it twice, and the file stops parsing."""
    target = _write_current(
        tmp_path,
        '[[custom]]\nname="mine"\nbase_url="https://mine/v1"\nmodel="m"\napi_key_ref="MY_KEY"\n',
    )
    original = target.read_text()
    source = (
        _NEW + '\n[[custom]]\nname="mine"\nbase_url="https://mine/v1"\nmodel="m"'
        '\napi_key_ref="MY_KEY"\n'
    )
    with pytest.raises(ValueError, match="nothing written"):
        await sync_file(source, target, source="lineup.toml", secrets=DictSecrets({}))
    assert target.read_text() == original


async def test_sync_file_rejects_a_non_toml_target(tmp_path):
    with pytest.raises(ValueError, match=".toml"):
        await sync_file(_NEW, tmp_path / "llms.json", source="freetier", secrets=DictSecrets({}))


async def test_sync_file_fetches_the_catalog_only_for_alias_entries(tmp_path):
    calls: list[int] = []

    def fetch():
        calls.append(1)
        return ""

    target = _write_current(
        tmp_path,
        '[[custom]]\nname="mine"\nbase_url="https://mine/v1"\nmodel="m"\napi_key_ref="MY_KEY"\n',
    )
    await sync_file(
        _NEW,
        target,
        source="freetier",
        secrets=DictSecrets({}),
        fetch_catalog=fetch,
    )
    assert calls == []


async def test_sync_file_refreshes_an_alias_entry_from_the_catalog(tmp_path):
    target = _write_current(
        tmp_path,
        '[[custom]]\nalias="opus"\nname="anthropic-claude-opus-4-8"\nmodel="claude-opus-4-8"'
        '\nbase_url="https://api.anthropic.com/v1"\napi_key_ref="ANTHROPIC_API_KEY"\npool=false\n',
    )
    outcome = await sync_file(
        _NEW,
        target,
        source="freetier",
        secrets=DictSecrets({}),
        fetch_catalog=lambda: tomli_dumps_catalog(),
    )
    (entry,) = tomllib.loads(target.read_text())["custom"]
    assert (entry["model"], entry["name"]) == ("claude-opus-5", "anthropic-claude-opus-5")
    assert entry["pool"] is False
    assert "opus: claude-opus-4-8 -> claude-opus-5" in outcome.notices
    keys = tomllib.loads(target.read_text())["keys"]
    assert keys["ANTHROPIC_API_KEY"]["help"] == "console.anthropic.com"


def tomli_dumps_catalog() -> str:
    return (
        '[[provider]]\nid="anthropic"\nbase_url="https://api.anthropic.com/v2"\n'
        'api_key_ref="ANTHROPIC_API_KEY"\nkey_help="console.anthropic.com"\n'
        '  [[provider.models]]\n  alias="opus"\n  model="claude-opus-5"\n'
    )


# ── Parsing helpers ──────────────────────────────────────────────────────────


def test_configs_and_keys_are_read_in_file_order():
    data = tomllib.loads(
        '[[llms]]\nname="a"\nbase_url="u"\nmodel="m"\napi_key_ref="A"\n'
        '[[custom]]\nname="c"\nbase_url="u"\nmodel="m"\napi_key_ref="C"\n'
        '[keys.A]\nhelp="a help"\n',
    )
    configs = configs_from_data(data)
    assert [(c.name, c.custom) for c in configs] == [("a", False), ("c", True)]
    assert key_infos_from_data(data)["A"].help == "a help"


def test_fetch_preset_text_refuses_an_invalid_name():
    with pytest.raises(ValueError, match="invalid preset name"):
        fetch_preset_text("../etc/passwd")


# ── Fetch failures are all ValueError, connect phase or not ──────────────────


class _Response:
    """A urlopen context manager whose body dies mid-read."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        raise self._exc


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("timed out"),
        ConnectionResetError("peer went away"),
        http.client.IncompleteRead(b"partial"),
    ],
)
def test_a_body_that_dies_mid_read_is_a_value_error(monkeypatch, exc):
    """The docstring promises every failure is a ValueError; `sync=` and the CLI
    both catch on that promise, and only the connect phase used to keep it."""
    monkeypatch.setattr(upstream.urllib.request, "urlopen", lambda *a, **k: _Response(exc))
    with pytest.raises(ValueError, match="failed reading"):
        fetch_preset_text("freetier")


def test_an_undecodable_body_is_a_value_error(monkeypatch):
    class _Bytes(_Response):
        def read(self):
            return b"\xff\xfe not utf-8"

    monkeypatch.setattr(upstream.urllib.request, "urlopen", lambda *a, **k: _Bytes(None))
    with pytest.raises(ValueError, match="not valid UTF-8"):
        fetch_preset_text("freetier")


def test_a_url_error_still_reports_its_reason(monkeypatch):
    def boom(*_a, **_k):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(upstream.urllib.request, "urlopen", boom)
    with pytest.raises(ValueError, match="name resolution failed"):
        fetch_preset_text("freetier")
