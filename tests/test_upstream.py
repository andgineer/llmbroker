"""The sync engine: the pairs-and-budget removal rule, key presence, the writers."""

import tomllib

import pytest

from llmbroker.broker.upstream import (
    catalog_alias_index,
    check_not_emptying,
    configs_from_data,
    fetch_preset_text,
    key_infos_from_data,
    merge_upstream,
    present_refs,
    refresh_alias_entries,
    render_merged_toml,
    sync_file,
    write_atomic,
)
from llmbroker.exceptions import SyncRefusedError
from llmbroker.models import KeyInfo, LLMConfig
from llmbroker.standalone.secrets import DictSecrets, Secrets


def _cfg(name, ref="K", *, model="m", url="https://x/v1", custom=False):
    return LLMConfig(
        name=name,
        base_url=url,
        model=model,
        api_key_ref=ref,
        custom=custom,
    )


def _merge(new, current, present=frozenset(), *, new_keys=None, current_keys=None):
    return merge_upstream(
        list(new),
        dict(new_keys or {}),
        list(current),
        dict(current_keys or {}),
        set(present),
        source="freetier",
    )


# ── The removal rule, one test per row of the table ──────────────────────────


def test_same_provider_replacement_needs_no_key_at_all():
    """The arrival carries the dropped entry's ref: same quota, nothing is lost."""
    merged, _keys, report = _merge([_cfg("groq-new", "GROQ")], [_cfg("groq-old", "GROQ")])
    assert [c.name for c in merged] == ["groq-new"]
    assert (report.removed, report.kept, report.added) == (
        ("groq-old",),
        (),
        ("groq-new",),
    )


def test_cross_provider_swap_with_the_arrivals_key_present_removes():
    merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ")],
        present={"GEMINI"},
    )
    assert [c.name for c in merged] == ["gemini"]
    assert report.removed == ("groq-old",)
    assert report.kept == ()


def test_cross_provider_swap_without_the_arrivals_key_keeps():
    merged, _keys, report = _merge([_cfg("gemini", "GEMINI")], [_cfg("groq-old", "GROQ")])
    assert [c.name for c in merged] == ["gemini", "groq-old"]
    assert report.kept == ("groq-old",)
    assert report.removed == ()


def test_provider_dropped_with_nothing_arriving_keeps():
    merged, _keys, report = _merge(
        [_cfg("a", "A")],
        [_cfg("a", "A"), _cfg("groq-old", "GROQ")],
        present={"A", "GROQ"},
    )
    assert [c.name for c in merged] == ["a", "groq-old"]
    assert report.kept == ("groq-old",)


def test_a_shrinking_lineup_with_every_key_present_removes_nothing():
    """No arrivals means no budget: five models down to three prunes nothing."""
    current = [_cfg(n, n.upper()) for n in ("a", "b", "c", "d", "e")]
    merged, _keys, report = _merge(
        current[:3],
        current,
        present={"A", "B", "C", "D", "E"},
    )
    assert [c.name for c in merged] == ["a", "b", "c", "d", "e"]
    assert report.kept == ("d", "e")
    assert report.removed == ()


def test_one_usable_arrival_pays_for_exactly_one_removal():
    """And it spends the budget on the entry that is inactive anyway."""
    merged, _keys, report = _merge(
        [_cfg("new", "NEW")],
        [_cfg("keyed", "KEYED"), _cfg("keyless", "ABSENT")],
        present={"NEW", "KEYED"},
    )
    assert report.removed == ("keyless",)
    assert report.kept == ("keyed",)
    assert [c.name for c in merged] == ["new", "keyed"]


def test_an_arrival_pays_only_once_even_when_two_entries_share_its_ref():
    merged, _keys, report = _merge(
        [_cfg("new", "GROQ")],
        [_cfg("old-a", "GROQ"), _cfg("old-b", "GROQ")],
        present={"GROQ"},
    )
    assert report.removed == ("old-a",)
    assert report.kept == ("old-b",)
    assert [c.name for c in merged] == ["new", "old-b"]


def test_with_no_keys_at_all_only_same_ref_pairs_are_removed():
    merged, _keys, report = _merge(
        [_cfg("groq-new", "GROQ"), _cfg("gemini", "GEMINI")],
        [_cfg("groq-old", "GROQ"), _cfg("openrouter-old", "OPENROUTER")],
        present=set(),
    )
    assert report.removed == ("groq-old",)
    assert report.kept == ("openrouter-old",)
    assert [c.name for c in merged] == ["groq-new", "gemini", "openrouter-old"]


async def test_have_keys_pays_for_a_removal_without_a_resolvable_key():
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


def test_kept_entries_survive_repeated_merges_without_duplicating():
    current = [_cfg("groq-old", "GROQ")]
    new = [_cfg("gemini", "GEMINI")]
    for _ in range(3):
        current, _keys, report = _merge(new, current)
        assert [c.name for c in current] == ["gemini", "groq-old"]
        assert report.kept == ("groq-old",)
    assert report.added == ()  # gemini arrived on the first pass only


def test_a_kept_entry_is_removed_once_a_replacement_becomes_usable():
    merged, _keys, _report = _merge([_cfg("gemini", "GEMINI")], [_cfg("groq-old", "GROQ")])
    merged, _keys, report = _merge(
        [_cfg("gemini", "GEMINI"), _cfg("cerebras", "CEREBRAS")],
        merged,
        present={"CEREBRAS"},
    )
    assert report.removed == ("groq-old",)
    assert [c.name for c in merged] == ["gemini", "cerebras"]


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
        new_keys={"GEMINI": KeyInfo(api_key_ref="GEMINI", help="gemini help", extra={})},
        current_keys={"GROQ": KeyInfo(api_key_ref="GROQ", help="groq help", extra={})},
    )
    assert keys["GROQ"].help == "groq help"
    assert {p.api_key_ref: p.help for p in report.pending_keys} == {
        "GEMINI": "gemini help",
        "GROQ": "groq help",
    }


def test_a_kept_entry_without_key_help_is_not_an_error():
    _merged, _keys, report = _merge([_cfg("gemini", "GEMINI")], [_cfg("groq-old", "GROQ")])
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


def test_the_rule_itself_never_produces_an_empty_lineup():
    """Why the guard above is a backstop and not a workflow: a removal needs an
    arrival, and an arrival is itself in the result — so nothing can empty it."""
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
