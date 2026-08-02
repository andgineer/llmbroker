# Pool priority: a curated weight that survives storage, blended with learned quality and live availability

**Depends on `preset-autorefresh.md` and ships after it.** Two of its results decide this plan's
shape. Its identity gate defines when a fetched lineup "changes nothing" — this plan adds a
persisted field to `LLMConfig`, so the comparison must account for it, and doing that once, after
the gate exists, is one edit instead of a rebase. And once a refresh runs unattended on a daily
clock, a curated weight is something an installation adopts without an admin reading it, which
raises the bar on §6's rubric and on the validation in §7. `pool-lifecycle.md` (#1) rewrites the
merge rule in `broker/upstream.py` that decides what reaches the registry at all; both land first.

Line anchors below are current-main numbers; #1 and #2 move them.

## The problem

**Curated priority does not survive a database registry.** `pool.py:32` calls `order` "curated
priority: registry/preset position, lower is better", and `catalog.py:89` fills it with
`enumerate(pooled)` — the position in whatever list the registry returned. For a file registry that
is the preset's own order. For every database registry it is not: `backends/spec.py:27` gives the
registry table `key=("name",)`, and each driver's `fetch` sorts by the table key —
`sqlite/driver.py:194`, `postgres/driver.py:141`, `mongodb/driver.py:97`. **Rows come back in
alphabetical order by entry name.**

Proven by running it — mirroring `presets/freetier.toml` into a sqlite registry, then mirroring the
same list reversed:

```
preset file order:              what the registry hands the pool, both times:
0 groq-gpt-oss-120b             0 google-gemini-3.5-flash-lite
1 openrouter-nemotron-3-ultra   1 groq-gpt-oss-120b
2 openrouter-laguna-s-2.1       2 openrouter-laguna-s-2.1
3 google-gemini-lite            3 openrouter-nemotron-3-ultra
```

Not a sync defect: `upstream.py:309` builds `merged = [*new_managed, *kept, *custom]`, exactly the
intended ranking — preset entries in preset order, retained entries next, user entries last. The
storage layer discards it, because no column and no metadata key carries a position. Renaming an
entry silently reorders the pool; a curator reordering preset rows changes nothing.

**The spec asserts the opposite.** `optimizer.md`, Selection: *"among slots with the same demotion
verdict, curated priority wins (registry/preset position — lower is better)"*. True for a file
registry only.

**Among healthy models nothing distinguishes them.** The sort key at `pool.py:238` is
`(over_budget, is_demoted, order)`. `is_demoted` is binary and needs ≥10 explicit host ratings;
`wilson_bound` exists but its docstring marks it diagnostic and it is not consulted. So a model
that just clawed out of a failure streak and one that has never failed are indistinguishable, and
the router prefers whichever name sorts earlier. On a free-tier pool this has a price: once a daily
quota is spent every attempt 429s, and `max_delay` caps the cooldown at one hour, so a model whose
quota resets in ten hours is tried and rejected roughly ten more times that day.

## Design summary

1. **`weight` on an entry is a curated prior on quality, on the same 0..1 scale as a host rating.**
   One scale for the prior and the evidence is what makes them blendable without invented
   conversion factors.
2. **It lives in `metadata`,** the JSON column every registry backend already carries. No schema
   migration, and priority stops depending on row order — `order` degrades to a pure tiebreaker,
   which is all a positional index was ever reliable for.
3. **Absent weight is `0.0`,** so entries the preset does not carry — `kept` survivors, user
   `[[custom]]` rows — sink below every curated one without needing a rule of their own.
4. **Evidence displaces the prior by Bayesian shrinkage,** not by a threshold:
   `(n·mean + m·weight) / (n + m)`. At `n = 0` the value is exactly the weight, so a strong new
   model starts where the curator put it instead of at the bottom where it could never earn its
   way up. The prior never expires; it is outweighed.
5. **Availability multiplies the blend** as a third, independent axis — expected quality times the
   chance the call lands.
6. **The three axes stay separate on purpose,** and availability specifically must never feed the
   quality window. Demotion has no time-based recovery: an outage long enough to push ten zeros
   into the window would demote a model permanently, and a demoted model gets no traffic, so no new
   ratings, so no way back. Availability is time-varying and self-healing; quality is sticky.
   Merging them converts a transient outage into an irreversible one.
7. **Cooldown keeps exclusive ownership of hard exclusion.** Availability only reorders models that
   are already callable right now; `availability_floor` stops the rank from ever amounting to
   exclusion.

## 1. The `weight` field — `models.py`

Add to `LLMConfig`, after `alias`:

```python
weight: float = 0.0
```

Extend the class docstring with one line: the weight is a curated prior on the quality rating this
entry is expected to earn, not a routing hard-order.

`to_metadata()` — append, following the existing only-non-defaults rule (`0.0` is falsy, so a
weightless entry still serializes to `{}` and the current doctests hold unchanged):

```python
if self.weight:
    metadata["weight"] = self.weight
```

Add a doctest alongside the existing ones:

```
>>> LLMConfig(name="g", base_url="u", model="m", api_key_ref="K", weight=0.7).to_metadata()
{'weight': 0.7}
```

`from_metadata()` — read it back, rejecting `bool` explicitly (it is an `int` subclass) and
clamping rather than raising:

```python
raw_weight = metadata.get("weight")
weight = (
    min(1.0, max(0.0, float(raw_weight)))
    if isinstance(raw_weight, (int, float)) and not isinstance(raw_weight, bool)
    else 0.0
)
```

**Clamp here, raise at the file parser (§2).** A malformed row in a shared database must not take a
running broker down at pool build; a malformed preset row is a curation error and should fail loudly
while a human is looking. Log the clamp at WARNING naming the entry.

Add `check_weight(weight: float) -> None` next to `check_score` (`models.py:298`), same shape,
rejecting outside `[0.0, 1.0]`.

## 2. Parsing and round-tripping the weight

**`standalone/registry.py`, `config_from_entry` (line 14).** Read `weight` from the entry dict,
call `check_weight`, pass it to `LLMConfig`. Applies to `[[llms]]` and `[[custom]]` alike — the
function serves both, and a user is entitled to weight their own pooled entry.

**`broker/upstream.py`, `_entry_dict` (line 364).** Must emit the weight, or a file-target sync
silently strips it from every `kept` and `[[custom]]` entry it rewrites:

```python
if cfg.weight:
    entry["weight"] = cfg.weight
```

**`backends/ports.py` — no change.** `mirror()` already writes `cfg.to_metadata()` and `load()`
already reconstructs through `from_metadata`, so §1 carries the weight through every database
backend by itself. This section is nonetheless a required *test* (§7): the round trip is the exact
defect this plan exists to fix, and it must have a regression test on a real backend, not a
reasoned assurance.

**`preset-autorefresh.md` §2's identity gate** must treat a changed weight as a change. Confirm
against that plan's implementation, which lands first; if it compares rendered TOML, §2's
`_entry_dict` edit covers it for free, and the test in §7 says so either way.

## 3. Blending prior with evidence — `optimizer.py`

New config field on `Optimizer`, beside the existing quality knobs (lines 41-46):

```python
prior_strength: float = 10.0  # pseudo-ratings the curated weight is worth
```

The default deliberately equals `quality_min_count`'s magnitude: the prior loses its majority at
about the same point the existing demotion verdict becomes expressible at all.

New method:

```python
def quality_score(self, llm_name: str, operation: str | None, weight: float) -> float:
    """The curated weight shrunk toward the observed window as ratings accumulate.

    >>> Optimizer().quality_score("m", None, 0.8)  # no ratings yet
    0.8
    """
    window = self._scores.get((llm_name, operation))
    if not window:
        return weight
    n = len(window)
    mean = sum(window) / n
    return (n * mean + self.prior_strength * weight) / (n + self.prior_strength)
```

**Mean, not `wilson_upper`.** The Wilson upper bound is deliberately optimistic at small `n`, which
is right for its job — refusing to demote on thin evidence — and wrong for ranking: at equal true
quality it sits *lower* for the model with more samples, and the model with more samples is the
leader, so the leader would demote itself, yield, recover, and oscillate. Wilson keeps answering
"is this definitely bad"; the blend answers "which is better". Record this split in the spec (§8).

## 4. Availability — `optimizer.py` + `broker/learning.py`

Three more knobs on `Optimizer`:

```python
availability_window: int = 20     # recent outcomes kept per model
availability_min_count: int = 5   # below this, no penalty is applied
availability_floor: float = 0.1   # rank never amounts to exclusion
```

State mirroring `_scores` exactly, so there is no second pattern to learn:

```python
_outcomes: dict[str, deque] = field(default_factory=dict, init=False, repr=False)
```

```python
def record_outcome(self, llm_name: str, ok: bool) -> None:
    """Fold one call outcome into the model's rolling availability window."""

def availability(self, llm_name: str) -> float:
    """Share of recent calls that succeeded; 1.0 until availability_min_count
    outcomes exist, floored at availability_floor."""

def load_availability(self, outcomes: dict[str, list[bool]]) -> None:
    """Replace every window wholesale — the journal-rebuild counterpart of load_scores."""
```

"No evidence, no penalty" is the same stance `quality_min_count` already takes, and it is what stops
a single unlucky failure from burying a freshly added model.

**Live feed — `learning.py:118`, `_drive(call)`.** It already branches on every call's status to
drive the streak counter; add the outcome fold in the same branches: `CallStatus.OK` → `ok=True`,
`RATE_LIMITED` / `UNAVAILABLE` / `ERROR` → `ok=False`. Own outcomes must apply immediately, before
any rebuild, exactly as own ratings already do.

**Rebuild — `learning.py:173`, `_apply_scores_and_metrics`.** It already receives the newest-first
tail. `stats_from_calls` (`broker/stats.py`) keeps `by_status` per model, which is precisely the raw
material; `metrics_from_calls` then throws it away. Call `stats_from_calls(rows)` once, derive both
the metrics cache and the per-model outcome lists from that single pass, and hand the latter to
`load_availability`.

**Known limit, stated not fixed:** the rebuild tail (`quality_rebuild_limit`, default 300) is shared
across all models, so a chatty model crowds out a quiet one's history. The spec already accepts this
for quality windows and names the limit as the knob. For availability it bites differently — a
rarely-routed model may have no rows at all — which is why the min-count guard returns a clean 1.0
rather than a penalty. A time-windowed variant is possible later (`Call.ts` is journaled) and is
explicitly **not** in this plan.

## 5. The selection key — `broker/pool.py`

Add beside `_is_demoted` (line 129), matching its `optimizer is None` handling:

```python
def _priority(self, slot: _Slot, operation: str | None) -> float:
    """Expected quality × chance the call lands. Falls back to the curated weight
    with no optimizer."""
    if self._optimizer is None:
        return slot.config.weight
    blended = self._optimizer.quality_score(slot.config.name, operation, slot.config.weight)
    return blended * self._optimizer.availability(slot.config.name)
```

The acquisition key (line 238) gains one term:

```python
key=lambda s: (
    self._over_budget(s, remaining, now),
    self._is_demoted(s.config.name, operation),
    -self._priority(s, operation),
    s.order,
),
```

Negated because acquisition is a `min`. `order` stays last: with equal weights and no evidence the
choice must still be deterministic. Nothing above `_priority` moves — budget and demotion keep
precedence, and the `avail` filter (line 229) keeps excluding cooling, capped and disabled slots
before any of this runs.

## 6. The shipped preset and the runbook

`presets/freetier.toml` — weight every `[[llms]]` row. The scale is a curator's prior on the rating
a host would give an answer, informed by benchmarks but not equal to any of them:

| weight | meaning |
|---|---|
| 0.8–1.0 | frontier-class |
| 0.6–0.8 | strong general-purpose |
| 0.4–0.6 | usable, clearly behind the leaders |
| < 0.4 | niche or weak |

Proposed values, from the evidence recorded in `freetier-providers.md`:

| entry | weight | basis |
|---|---|---|
| `google-gemini-3.5-flash-lite` | 0.75 | AA intelligence index 50 |
| `openrouter-nemotron-3-ultra` | 0.72 | AA 47.7, highest US open-weight |
| `openrouter-laguna-s-2.1` | 0.70 | SWE-Bench Multilingual 78.5%, general answers sound |
| `groq-gpt-oss-120b` | 0.55 | AA 33.3 |

**Row order stops carrying priority** — leave the rows where they are. This closes the reordering
question the refresh raised: reordering was only ever going to work on file registries.

`presets/freetier-refresh-prompt.md` — a weight is mandatory on every `[[llms]]` row; add the rubric
above, and add to §6 the check that every row has one in `[0, 1]`. A weightless preset row lands at
the bottom of the pool, which is a silent curation failure and must be caught at review.

**Explicit non-goal:** the paid catalog gains no weight column and `add-model` learns no `--weight`.
Catalog entries land `pooled=False` (direct-only), so they are not in the selection order at all; a
user who deliberately pools one can write `weight` into their own file by hand.

## 7. Tests

`tests/test_pool_priority.py` (new) — the blend and the key.

| scenario | expected |
|---|---|
| no ratings, no outcomes | `_priority` equals the entry's weight exactly |
| weightless entry vs weighted, both fresh | weighted acquired first |
| two weightless entries | acquisition falls to `order`, deterministic across runs |
| window full of 1.0 on a low-weight entry | it overtakes a high-weight entry with no ratings |
| window full of 0.0 on a high-weight entry | it falls below a weightless one, but stays acquirable |
| ratings arriving one at a time | priority moves monotonically from weight toward the mean |
| ratings on `operation="a"` | priority for `operation=None` and `"b"` unchanged |
| demoted high-weight vs healthy weightless | demoted acquired last — demotion outranks priority |
| over-budget high-priority slot | budget still outranks priority |
| `optimizer=None` pool | priority is the raw weight, nothing raises |

Availability, same file:

| scenario | expected |
|---|---|
| fewer than `availability_min_count` outcomes | `availability` is 1.0, no penalty |
| all recent outcomes OK | 1.0 |
| half failing | ≈0.5, and the slot sorts below an equally-weighted healthy one |
| every outcome failing | `availability_floor`, never 0 — the slot is still acquirable when alone |
| failures aging out under fresh successes | priority returns to the weight as `n` of ratings stays 0 |
| a `RATE_LIMITED` call | recorded as not-OK, and the existing cooldown behavior is unchanged |
| rebuild from a journal tail | `load_availability` reproduces what the live folds produced |

`tests/test_registry.py` — parsing:

| scenario | expected |
|---|---|
| `[[llms]]` row with `weight = 0.7` | parsed onto the config |
| `[[custom]]` row with a weight | parsed the same way |
| row without a weight | `0.0` |
| `weight = 1.5` / `-0.1` / `"high"` | `ValueError` naming the entry |

`tests/test_store_backends.py` — **the regression test this plan exists for.** On every backend the
suite already covers (sqlite, and postgres/mongodb via their testcontainers): mirror a lineup whose
entries carry distinct weights, load it back, assert every weight survives — and assert it survives
when the mirror is performed in an order different from the load order, since alphabetical
reordering is exactly what discarded the position.

`tests/test_upstream.py` — a file-target sync of a lineup with weighted `kept` and `[[custom]]`
entries rewrites their weights into the file rather than stripping them; a preset whose only change
is a weight is *not* treated as unchanged by the identity gate.

No `pytest.skip`, no `importorskip` — testcontainers cover the database backends.

## 8. Specs (same batch as the behavior)

`specs/reference/optimizer.md`:

- **Selection** — rewrite. Curated position is not the priority carrier; the carrier is the entry's
  weight, shrunk toward observed ratings and scaled by recent availability, with position surviving
  only as the tiebreaker.
- **New section, the three axes** — cooldown (provider-driven, self-healing, hard exclusion),
  availability (journal-driven, self-healing, soft rank), quality (host-driven, sticky, demotion).
  State the invariant that keeps them apart: *availability never enters the quality window, because
  demotion has no time-based recovery and would make an outage permanent.* This is the load-bearing
  rule of the whole design and the one a future change is most likely to break.
- **Quality demotion** — one line recording that the Wilson bound answers "definitely bad" while the
  blended mean answers "which is better", and why the bound must not be used for ranking.

`specs/reference/architecture.md` — the registry stores no ordering; an entry's standing in the pool
is data on the entry, not its row position. This is what a future backend author must not assume
away.

`specs/reference/freetier-providers.md` — the weight rubric and what the shipped weights rest on.

## 9. Docs (`docs/src/en/` + `docs/src/ru/`)

`usage.md` (or wherever the config file is documented): `weight` on an entry, the 0..1 scale, the
default of 0.0 and its consequence, and one sentence that a rating recorded through
`record_quality()` is what moves an entry off its weight. Both languages in the same batch.

## Work order

Each batch ends green on `invoke pre` + `python -m pytest` (`. ./activate.sh` first).

1. §1 + §2, the field and its round trip, with the `test_store_backends.py` regression and the
   parser tests. Valuable and reviewable on its own: it closes the lost-priority defect even before
   anything consults the value.
2. §3 + §5, the blend and the sort key, with tests; `optimizer.md`'s rewritten Selection section in
   the same batch, so the spec is never left asserting the position rule this replaces.
3. §4, availability, with tests; the three-axis spec section — including the invariant — in the
   same batch.
4. §6 preset weights and runbook rubric; §8 remaining specs; §9 docs en + ru.

Version bump: none (the maintainer does it by hand).

## Verification

```bash
. ./activate.sh
invoke pre
python -m pytest
```

Then, on a database registry, confirm by hand what the alphabetical `fetch` used to hide:

```bash
python - <<'EOF'
import asyncio
from llmbroker.sqlite import Registry
from llmbroker.standalone.registry import Registry as FileRegistry

async def main():
    cfgs = await FileRegistry("presets/freetier.toml").load()
    reg = Registry("/tmp/priority-check.sqlite")
    await reg.mirror(list(reversed(cfgs)))          # deliberately wrong order
    for c in await reg.load():                       # comes back alphabetical
        print(f"{c.name:32s} weight={c.weight}")     # weights must be intact
    await reg.aclose()

asyncio.run(main())
EOF
```
