"""Death evidence: which entries this installation's own journal condemns.

The criterion is deliberately hard to satisfy — a sync that removes a working
model is the one failure this whole rule exists to prevent.
"""

from datetime import UTC, datetime, timedelta

import pytest

from llmbroker.broker.keys import KeyEvidence
from llmbroker.broker.merge import dead_entries, retirement_candidates
from llmbroker.models import Call, CallStatus, LLMConfig, Retirement
from llmbroker.standalone.store import FileStore

_BASE = datetime(2030, 1, 1, tzinfo=UTC)


def _seen(*refs: str) -> KeyEvidence:
    """A probe that found every named ref, at a site where absence is evidence."""
    return KeyEvidence(present=frozenset(refs), visible=True)


def _cfg(name, ref="K", *, custom=False):
    return LLMConfig(
        name=name,
        base_url="https://x/v1",
        model="m",
        api_key_ref=ref,
        custom=custom,
        synced=not custom,
    )


def _call(llm_name, status, http_status=None, call_id="c", ts=_BASE):
    return Call(
        id=call_id,
        llm_name=llm_name,
        operation=None,
        trace_id=None,
        status=status,
        kind="call",
        ts=ts,
        http_status=http_status,
    )


async def _store(tmp_path, *calls: Call) -> FileStore:
    store = FileStore(tmp_path / "store")
    for call in calls:
        await store.record(call)
    return store


# ── The criterion ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("code", [401, 403, 404])
async def test_a_permanent_failure_with_no_success_is_death(tmp_path, code):
    store = await _store(tmp_path, _call("groq-old", CallStatus.ERROR, code))
    assert await dead_entries(["groq-old"], store) == {
        "groq-old": Retirement(name="groq-old", http_status=code, since=_BASE),
    }


async def test_the_evidence_reaches_back_to_the_oldest_failure_in_the_window(tmp_path):
    """The report says "401 since <date>", so the date must be where the run of
    failures starts, not the newest row that happens to be on top of the tail."""
    store = await _store(
        tmp_path,
        _call("groq-old", CallStatus.ERROR, 401, "a", ts=_BASE),
        _call("groq-old", CallStatus.ERROR, 401, "b", ts=_BASE + timedelta(days=9)),
    )
    assert (await dead_entries(["groq-old"], store))["groq-old"].since == _BASE


async def test_the_status_is_the_one_the_provider_answers_now(tmp_path):
    """`since` reaches back to the start of the run, but the code an admin is asked
    to go and check must be the current one, not whatever it was months ago."""
    store = await _store(
        tmp_path,
        _call("groq-old", CallStatus.ERROR, 404, "a", ts=_BASE),
        _call("groq-old", CallStatus.ERROR, 429, "b", ts=_BASE + timedelta(days=5)),
        _call("groq-old", CallStatus.ERROR, 401, "c", ts=_BASE + timedelta(days=9)),
    )
    assert await dead_entries(["groq-old"], store) == {
        "groq-old": Retirement(name="groq-old", http_status=401, since=_BASE),
    }


async def test_a_success_anywhere_in_the_tail_keeps_it_alive(tmp_path):
    store = await _store(
        tmp_path,
        _call("groq-old", CallStatus.ERROR, 401, "a"),
        _call("groq-old", CallStatus.OK, 200, "b"),
    )
    assert await dead_entries(["groq-old"], store) == {}


async def test_a_bad_week_proves_nothing(tmp_path):
    """429 and 5xx are what the cooldown machinery exists for, not evidence of death."""
    store = await _store(
        tmp_path,
        _call("groq-old", CallStatus.RATE_LIMITED, 429, "a"),
        _call("groq-old", CallStatus.UNAVAILABLE, 503, "b"),
    )
    assert await dead_entries(["groq-old"], store) == {}


async def test_an_empty_journal_condemns_nobody(tmp_path):
    assert await dead_entries(["groq-old"], await _store(tmp_path)) == {}


async def test_a_non_queryable_store_condemns_nobody():
    class _WriteOnly:
        async def record(self, call):
            pass

        async def record_quality(self, *a, **k):
            pass

    assert await dead_entries(["groq-old"], _WriteOnly()) == {}


async def test_no_store_at_all_condemns_nobody():
    assert await dead_entries(["groq-old"], None) == {}


async def test_only_the_named_candidates_are_judged(tmp_path):
    store = await _store(tmp_path, _call("other", CallStatus.ERROR, 401))
    assert await dead_entries(["groq-old"], store) == {}


async def test_an_empty_candidate_set_never_queries_the_store():
    """The whole cost story: on an ordinary sync there are no candidates, and the
    journal is not read at all."""

    class _Explodes:
        async def record(self, call):
            pass

        async def record_quality(self, *a, **k):
            pass

        async def calls(self, **_kwargs):
            raise AssertionError("the journal must not be read without a candidate")

    assert await dead_entries([], _Explodes()) == {}


# ── Who is a candidate ───────────────────────────────────────────────────────


def test_only_entries_the_merge_would_otherwise_keep_are_candidates():
    current = [
        _cfg("groq-old", "GROQ"),  # provider gone, key here — the one candidate
        _cfg("gemini", "GEMINI"),  # still in the lineup
        _cfg("cerebras-old", "CEREBRAS"),  # provider gone, no key: removed anyway
        _cfg("mine", "GROQ", custom=True),  # custom entries are never pruned
    ]
    new = [_cfg("gemini", "GEMINI")]
    assert retirement_candidates(new, current, _seen("GROQ", "GEMINI")) == ["groq-old"]


def test_an_entry_whose_provider_the_lineup_still_carries_is_not_a_candidate():
    """Its models replace it with no key lookup, so no evidence is needed."""
    new = [_cfg("groq-new", "GROQ")]
    current = [_cfg("groq-old", "GROQ")]
    assert retirement_candidates(new, current, _seen("GROQ")) == []


def test_an_ordinary_sync_has_no_candidates():
    lineup = [_cfg("a", "A"), _cfg("b", "B")]
    assert retirement_candidates(lineup, lineup, _seen("A", "B")) == []


def test_where_a_missing_key_proves_nothing_every_dropped_entry_is_a_candidate():
    """Per-user keys resolve nothing at the merge site, so keying candidacy on
    `present` would leave a scoped installation unable to retire anything, ever —
    and "nobody could call it" is the only evidence it can ever produce."""
    current = [_cfg("groq-old", "GROQ"), _cfg("cerebras-old", "CEREBRAS")]
    new = [_cfg("gemini", "GEMINI")]
    blind = KeyEvidence(present=frozenset(), visible=False)
    assert retirement_candidates(new, current, blind) == ["groq-old", "cerebras-old"]
    assert retirement_candidates(new, current, KeyEvidence(visible=True)) == []
