# Pool priority: a curated weight that survives storage, shrunk toward host quality ratings

**The preset auto-refresh is shipped, and two of its results decide this plan's shape.** Its
identity gate defines when a fetched lineup "changes nothing": the registry target compares entries
keyed by name, so a persisted weight on `LLMConfig` joins that comparison by itself and this plan
owes it a test rather than a mechanism. And a refresh now runs unattended on a daily clock, so a
curated weight is something an installation adopts without an admin reading it — which raises the
bar on §5's rubric and on the validation in §6.

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
5. **Availability stays out of this entirely.** Cooldown already owns it: the wait scales as
   `backoff_factor ** consecutive_fails` and resets on success, so a degrading model is excluded
   for longer and a recovering one returns at once. `decisions.md` already rejected ranking on a
   usable rate as bandit machinery that duplicates exponential cooldown, and that judgement holds.
6. **Nothing but a host rating may enter the quality window.** Demotion has no time-based recovery,
   so anything auto-generated — a failure count, an outage, a synthetic score — would demote a
   model permanently: demoted means no traffic, no traffic means no new ratings, no new ratings
   means no way back. This is the invariant a later change is most likely to break.

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
backend by itself. This section is nonetheless a required *test* (§6): the round trip is the exact
defect this plan exists to fix, and it must have a regression test on a real backend, not a
reasoned assurance.

**The sync's identity gate** must treat a changed weight as a change. It already does on both
targets — the file branch compares the rendered TOML, which §2's `_entry_dict` edit feeds, and the
registry branch compares entries by name, which picks up any new `LLMConfig` field. §6 tests it
rather than assuming it.

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
"is this definitely bad"; the blend answers "which is better". Record this split in the spec (§7).

## 4. The selection key — `broker/pool.py`

Add beside `_is_demoted` (line 129), matching its `optimizer is None` handling:

```python
def _priority(self, slot: _Slot, operation: str | None) -> float:
    """The curated weight, shrunk toward host ratings as they accumulate.
    Falls back to the raw weight with no optimizer."""
    if self._optimizer is None:
        return slot.config.weight
    return self._optimizer.quality_score(slot.config.name, operation, slot.config.weight)
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

## 5. The shipped preset and the runbook

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

## 6. Tests

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

One case guards the invariant of design summary 6, because it is the one a later change breaks:
a run of failed calls (`RATE_LIMITED`, `UNAVAILABLE`, `ERROR`) on a model leaves its quality window
untouched and its priority unchanged — failures cool a model, they never rate it.

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

## 7. Specs (same batch as the behavior)

`specs/reference/optimizer.md`:

- **Selection** — rewrite. Curated position is not the priority carrier; the carrier is the entry's
  weight, shrunk toward observed host ratings, with position surviving only as the tiebreaker.
- **New section, the two axes** — cooldown (provider-driven, self-healing, hard exclusion) and
  quality (host-driven, sticky, demotion). State the invariant that keeps them apart: *nothing but a
  host rating enters the quality window, because demotion has no time-based recovery and an
  auto-generated score would make a transient failure permanent.* This is the load-bearing rule of
  the whole design and the one a future change is most likely to break — availability ranking was
  proposed and rejected on it, alongside the entry `decisions.md` already carries.
- **Quality demotion** — one line recording that the Wilson bound answers "definitely bad" while the
  blended mean answers "which is better", and why the bound must not be used for ranking.

`specs/reference/architecture.md` — the registry stores no ordering; an entry's standing in the pool
is data on the entry, not its row position. This is what a future backend author must not assume
away.

`specs/reference/freetier-providers.md` — the weight rubric and what the shipped weights rest on.

## 8. Docs (`docs/src/en/` + `docs/src/ru/`)

`usage.md` (or wherever the config file is documented): `weight` on an entry, the 0..1 scale, the
default of 0.0 and its consequence, and one sentence that a rating recorded through
`record_quality()` is what moves an entry off its weight. Both languages in the same batch.

## Work order

Each batch ends green on `invoke pre` + `python -m pytest` (`. ./activate.sh` first).

1. §1 + §2, the field and its round trip, with the `test_store_backends.py` regression and the
   parser tests. Valuable and reviewable on its own: it closes the lost-priority defect even before
   anything consults the value.
2. §3 + §4, the blend and the sort key, with tests; `optimizer.md`'s rewritten Selection section and
   the two-axis invariant in the same batch, so the spec is never left asserting the position rule
   this replaces.
3. §5 preset weights and runbook rubric; §7 remaining specs; §8 docs en + ru.

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

---

## Handover

### Done

All three batches of the work order, §1 through §8.

- **§1 `models.py`** — `weight: float = 0.0` on `LLMConfig` (after `alias`), the docstring line,
  `to_metadata`/`from_metadata` with the plan's doctest, `check_weight` beside `check_score`, and
  `_weight_from_metadata`, which clamps a stored value and logs the clamp at WARNING naming the
  entry (`llmbroker.registry` logger — `models.py` had none before).
- **§2 round trip** — `config_from_entry` parses and validates `weight` for `[[llms]]` and
  `[[custom]]` alike; `_entry_dict` emits it so a file-target sync stops stripping it. `ports.py`
  needed no change, as predicted.
- **§3 + §4** — `Optimizer.prior_strength = 10.0`, `Optimizer.quality_score()` (with the fading
  prior described below), `LLMPool._priority()`, and `-self._priority(s, operation)` in the
  acquisition key between the demotion verdict and `s.order`.
- **§5** — every `presets/freetier.toml` row carries the plan's weight, rows left where they were, a
  header comment saying row order carries no priority; the refresh runbook gained the weight axis in
  §2, the mandatory-weight rule, and the §6 range check.
- **§7** — `optimizer.md` Selection rewritten, including "the weight decides where a model starts,
  never where it stays", "Why the blend, and not the Wilson bound", and a new
  "The two axes, and the invariant that keeps them apart"; `architecture.md` records that the
  registry stores no ordering; `freetier-providers.md` gained a "Weight axis" section with the rubric
  and the basis of each shipped value.
- **§8** — `usage.md` en + ru: a "Which model is tried first" section under the config-file text, and
  a clause in the quality section tying `record_quality()` to displacing the weight.

### Done differently from the plan

- **§3's formula gained a fading prior, so that §6's expectations hold.** As written — a constant
  `prior_strength` — the formula could not deliver two of §6's rows, because `n` is bounded by
  `quality_window`: the weight kept a permanent `10/40` share, so a fully-rated entry could never
  move more than 0.75 against its curated start, and a catastrophic window still scored above a
  weightless entry. That contradicts the point of the field: ratings must be able to reorder the
  pool against the curated order, not merely nudge it. The prior's strength now fades with the
  evidence — `prior_strength · (1 − n / quality_window)` — so an empty window leaves the weight
  untouched, a full one leaves the observed mean alone, and every rating in between moves the score
  monotonically toward the mean. `prior_strength = 10.0` keeps its stated meaning and its
  justification: the weight loses its majority at 7.5 ratings, still "about where a demotion verdict
  becomes expressible". Both §6 rows now pass as the plan wrote them, and the plan's §3 text is the
  one thing in it this implementation does not follow literally.
- **The DB round-trip regression lives in `tests/test_registry.py`, not `test_store_backends.py`.**
  The latter is parametrized on the *store* (journal) fixture and has no registry handle;
  `test_registry.py` already carries the `mutable_registry` fixture over sqlite/postgres/mongodb and
  the sibling `parallel`/`alias` round-trip tests. Same coverage, correct file.
- **§6's "a full window of 0.0 falls below a weightless entry" holds through the key, not through the
  priority term alone.** Both entries measure 0.0 — the bottom of the scale — and the demotion
  verdict one term above is what separates proven-bad from never-tried; without it the earlier
  `order` would still hand the traffic to the fallen entry. The test asserts the acquisition order
  *and* the mechanism, so a later change to either cannot pass it silently.
- **The failure-invariant test drives the real journal path** (`_LearningHook.record` on failed calls
  + `maybe_rebuild(force=True)` over a sqlite store) rather than poking the optimizer, since the
  rebuild is where an auto-generated score would realistically leak in.
- **`_weight_from_entry` reaches `check_weight` through a `try`/`except ValueError`** that re-raises
  naming the entry, and types are rejected in the same place. A bare `isinstance` guard raising
  `ValueError` trips ruff's TRY004 (it wants `TypeError`), and a `TypeError` would break the
  registry's "a config error is a `ValueError`" contract every other parse error in that file keeps.
- **One phrase in `architecture.md`** ("curated order stands" under the budget-miss term) became "the
  curated ranking stands" — with priority in the key, the sentence was no longer accurate.

### Deliberately left out

- The paid catalog and `add-model` gain nothing, per §5's explicit non-goal.
- `snapshot()` does not expose the weight or the blended priority. Nothing asked for it, and it would
  be a new public read-model surface.

### Decisions taken during implementation

- The clamp path logs on a bad *type* as well as an out-of-range number, and treats a missing key
  silently — `metadata.get("weight") is None` is the ordinary case for every unweighted entry.
- `bool` is rejected in both paths (it is an `int` subclass): clamped to `0.0` with a warning when
  stored, refused outright in a file.

### Gate

`. ./activate.sh` first; run after each batch and at the end:

- `invoke pre` — all checks passed, pyrefly 0 errors.
- `python -m pytest` — **1109 passed**, zero skips, zero errors (Docker up; postgres/mongodb
  testcontainers ran).
- The plan's manual verification ran against the shipped preset mirrored into sqlite in reverse
  order: rows come back alphabetical, every weight intact (0.75 / 0.55 / 0.70 / 0.72).
- Version not bumped.

### Unrelated fix carried in this branch

`tests/test_upstream.py::test_write_atomic_keeps_the_targets_permissions` failed on Windows CI
(`438 != 420`): Windows honors only the read-only bit, so `chmod(0o644)` lands as `0o666`. The test
now asserts that the mode the OS actually *granted* survives the write, which is the property
`write_atomic` promises on every platform.
