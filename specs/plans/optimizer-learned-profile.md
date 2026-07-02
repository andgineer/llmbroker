# Learned LLM profile — the dynamic, optimizer-owned half of the catalog

## Plan sequence — step 1 of 2

> **Prerequisites:** the typed dataclass ⇄ JSON-document boundary and the
> durable version-gated `ensure_schema` path this plan reuses for the new
> `profile` column are already implemented (see
> [`architecture.md`](../reference/architecture.md#columns-vs-json)) — **and**
> the keyless-not-routable pool change and the zero-routable alarm from the
> preset-onboarding effort are already implemented (the routable predicate this
> plan extends; see
> [`architecture.md`](../reference/architecture.md#key-acquisition-help) and
> [`optimizer.md`](../reference/optimizer.md)). **Blocks:** nothing.

The remaining two plans form one dependency chain; execute in this order (the
curated catalog knowledge, effort/value onboarding, the simplified two-phase
AVAILABLE/COOLING reliability model, and the keyless-not-routable pool change
are already implemented; see
[`architecture.md`](../reference/architecture.md) and
[`optimizer.md`](../reference/optimizer.md)):

1. **`optimizer-learned-profile.md`** *(this plan)* — the durable learned half
   (per-operation quality aggregates carried in the registry, derived quality
   demotions), cluster-shared aggregates via the state store, and
   `SeedPolicy.SYNC`; extends the existing routable predicate.
2. **`catalog-refresh.md`** — the manual re-curation runbook; consumes the
   already-fixed taxonomies plus the catalog-identity invariants this plan
   introduces (model bump = new entry name; preset removal = deprecation), and
   may run in parallel with this plan.

## Zero-admin goal

The design target for this plan is a **self-regulating pool**: after the one
genuinely human act (supplying keys), no decision in the lifecycle of a model —
demoting it, replacing it, delivering a better one — may require a deployment
admin. Concretely:

- no automatic verdict is ever waiting on a human to be reversed;
- quality verdicts are not "healed by time" (a weak model does not get smarter
  by resting) — the pool improves by **replacement, not rehabilitation**: bad
  members demote themselves, better members arrive through the curated preset
  via `SeedPolicy.SYNC`;
- the quality signal is **external and untrusted at the pool level**: ratings
  come from the caller's own code and may be miscalibrated, so no automatic
  quality verdict ever *excludes* a model — it only **demotes** it to a
  last-resort tier. A rater that scores everything low can reorder the pool,
  never empty it. The power to actually remove a model from routing is
  reserved for missing keys and for the human (`disable_llm`);
- self-regulation must be **loud**: a model the application's own ratings show
  to be practically useless is a standing, aggressively surfaced condition
  (see "A useless model must be loud"), not a silent reordering — zero-admin
  means no *required* human action, not no human-visible signal;
- the only sticky human verdict is an explicit manual bench — a deliberate
  opt-out of self-regulation for one model.

## Problem statement

llmbroker's concept is shifting: the LLM list is **curated by the maintainers**
and shipped as a preset; the end user mostly just supplies keys and does not
care which models are inside — they want it to work and not be nagged for new
keys. Under that concept the persisted data splits cleanly by *owner* and
*durability*, and today one quadrant has no home:

| Layer | Written by | Durability |
|---|---|---|
| **Curated catalog** (`llmbroker_registry` static columns + `metadata`) | preset roll / admin only | durable |
| **Ephemeral sync-state** (`llmbroker_state`) | the running broker, to coordinate instances | ephemeral — rebuilt from traffic, dropped on schema bump |
| **Raw call log** (`llmbroker_calls`) | the running broker | durable, but append-only |
| **Learned per-LLM knowledge** | the optimizer (+ admin overrides) | **durable — missing today** |

The missing quadrant is the **only user-side data that is genuinely valuable and
not cheaply recoverable**: how useful a given model has proved to be, per kind
of task. A blocked key recovers on its own; a model that stopped working sits
idle and harms nothing; but the accumulated judgement "this model is / isn't
worth routing to for this operation" is earned from real traffic and ratings
and must survive a **preset update**, an **ephemeral-state reset**, and a
**process restart** — and be shared by every instance of a cluster.

Concrete gaps:

1. **The quality signal is not durable and not shared.** `quality_score`
   ratings — in the overwhelming majority of deployments produced by the
   *calling code* evaluating each result, i.e. arriving at roughly call
   frequency — are written to the durable call log but never aggregated back:
   `Optimizer` state is per-process memory, lost on restart and invisible to
   other instances. A restarted or sibling process re-learns each model from
   zero; in a cluster each instance sees only its own slice of the evidence;
   and in the common short-lived deployment — a script that makes one call
   and exits — per-process memory means **nothing is ever learned at all**:
   no aggregate can reach a trust gate within a single run. The raw call log is the wrong place to *read* a per-LLM property
   from — in the simplest (JSONL) deployment that means scanning the whole
   file.
2. **Rated quality does not influence routing at all.** The existing selection
   policy gates and ranks on *transport* signals only (`usable_rate` = OK
   fraction, latency). A model that answers 200 OK but produces garbage for a
   given operation keeps receiving that operation's traffic indefinitely.
   There is no per-operation "not suitable for this task" demotion, and no
   pool-level "not suitable for anything" standing signal that survives a
   preset roll.
3. **A better preset model does not displace a worse sibling.** Transport-only
   ranking often *prefers* the older, smaller, faster model of the same
   provider, and both typically burn the same provider quota — the old model
   is not idle clutter, it actively converts shared quota into worse answers.
   Delivering "use the new one" is curator knowledge and needs a curated
   channel, not a smarter bandit.

This plan adds the missing durable, optimizer-owned layer — the **learned
profile** — the cluster-sharing path for its aggregates, the per-operation
demotions derived from them, and the `SeedPolicy.SYNC` reconcile behaviour that
lets the curated catalog flow to users without ever clobbering learned data.

## Concept: the catalog has two halves, one store

Logically the LLM catalog is one thing, but it has a **static half** and a
**dynamic half**. They must be *field-separated* so a catalog refresh can
overwrite one without touching the other — but they do **not** need separate
backends. Both live in the **registry**, keyed by the same `(name, user_id)`:

- **Static half — the curated catalog**: `name`, `base_url`, `model`,
  `api_key_ref`, and curated per-model config (`rate_limit`, …) in `metadata`.
  Owned by the preset / admin. The optimizer never writes here. Two curated
  markers are part of this half (carried as reserved `metadata` keys, written
  only by the seed path and `broker.add`):
  - **provenance** — whether the entry came from a preset or was added by the
    user (`origin: preset | user`);
  - **`deprecated`** — the curator withdrew the endorsement (the entry
    disappeared from the preset); see `SeedPolicy.SYNC` below.
- **Dynamic half — the learned profile**: what *this deployment* has learned
  about each model, per operation. Owned by the optimizer (with manual admin
  overrides). Stored in the registry's own `profile` field, which the seed
  **never** writes.

**Entry identity is immutable.** Within one catalog entry (`name`), the
`model` it points at never changes: all learned evidence is *about* that
model, and swapping the model under the name would silently re-attribute the
evidence. A new model version is a **new entry with a new name** and a fresh,
empty profile — it earns its place through the normal trial period (the
existing "fewer than `min_sample_count` samples passes the floor
unconditionally" rule). This single invariant is what removes any need for
evidence-invalidation machinery: the evidence can never refer to a model other
than the one it was collected on.

There is deliberately **no separate profile backend**. The learned profile is
another field of the registry, and the "never clobber learned data on
re-seed" guarantee is a narrow, testable rule — *the seed writes only static
fields* — not a whole parallel store.

## Where the numbers live: registry is the durable home, state store is the sync medium

Ratings arrive at call frequency, from every instance. Two existing stores
already split exactly along the durability/coordination axis, and the
aggregates use both, each for what it is for:

- **State store (`llmbroker_state` family) — cross-instance sharing.** The
  state store exists precisely to propagate information between instances
  cheaply and quickly (it already shares cooldowns via write-on-change plus a
  TTL-cached read — `_get_store_cache` / `apply_shared_cooling` in
  `broker/pool.py`). When a state store is configured, **all** decayed
  aggregates (quality, transport, latency — see below) are kept there as
  shared numeric summaries, updated with **server-side atomic decayed
  folds** (see Step 4) — never read-modify-write from the client, so
  concurrent instances cannot clobber each other. Instances read the merged
  summaries through the same TTL-cache pattern the cooldowns use. The state
  store remains disposable: dropping it loses at most the delta since the last
  durable snapshot.
- **Registry `profile` column — durability.** The merged aggregates (all
  kinds, see below) and the manual bench latch are snapshotted into the
  registry's `profile` field on a debounce and on `aclose`, and read back at
  provision to warm-start a fresh state store (or a fresh process when no
  state store is configured).
  Snapshot writes are plain last-writer-wins and need no versioning: every
  instance snapshots the *same merged view* read from the state store, so
  concurrent snapshots agree.
- **No state store configured** ⇒ summaries live in process memory and the
  registry snapshot is the only persistence — single-instance semantics,
  exactly like cooldowns today. Running multiple instances without a state
  store is already documented as unsupported for coordination
  (`protocols/state_store.py`); learned stats inherit that rule, and the plan
  documents it rather than inventing a second synchronization channel through
  the registry.

**There is one pipeline, not two.** All three aggregate kinds — quality
(rated `quality_score`), transport OK-rate (the replacement for today's
`_rolling` deque), and latency — ride the same flows: folded into the shared
state-store summaries when one is configured, snapshotted together into the
registry profile, warm-started together at provision. They differ only in
`value` semantics and decay constant, never in storage treatment; a node's
in-process memory is a write-behind buffer at most one TTL deep, not an
independent accumulator. Two usage patterns force the uniformity:

- a **short-lived script** (one call, exit) learns nothing per-process —
  only the `aclose` snapshot accumulates knowledge across runs, so the first
  pick of the next run is informed by all previous ones (no aggregate could
  otherwise ever reach `min_sample_count`);
- a **cluster node** must judge by the pool's merged evidence, not its own
  slice.

Staleness needs no special case: decay is per-event, so day-old transport
evidence yields to fresh reality within a handful of calls — and stale
evidence still beats none. The cost is negligible: four numbers per
`(operation, kind)`.

## Quality is per operation — measured and acted on

The caller names the kind of task (`operation`) on every call, and a model's
usefulness is genuinely operation-shaped: weak on hard tasks, fine on simple
ones. Everything in this plan is therefore keyed by `(name, operation)`
(calls without an operation fall into the `None` bucket):

- the quality aggregate is a per-`(name, operation)` summary;
- the **verdict is per-operation**: "this model is not suitable for this
  task" — a per-op **demotion** to the last-resort tier for that operation,
  derived from that operation's own aggregate;
- the **global verdict is derived, never stored**: a model is globally
  demoted ⟺ *every* operation with sufficient evidence is below the quality
  threshold and at least one such operation exists. Because it is computed
  from the shared aggregates rather than latched, it needs no propagation
  between instances (each instance derives the same verdict from the same
  numbers) and no un-demote mechanism (new evidence on any operation simply
  changes the derivation). Its two effects: operations the model has never
  been tried on are treated with distrust (below), and the standing
  "practically useless" signal fires (see "A useless model must be loud");
- a globally-demoted model with an operation it has **never been tried on**
  joins the last-resort tier for that operation too, but **ahead of**
  evidenced-bad operations: a new kind of task is new evidence territory and
  gets tried first when the pool has nothing better — at the lowest priority,
  since everything this model has been measured on was poor. If it turns out
  good at the new task, that operation's aggregate clears the derivation and
  the global verdict dissolves on its own, with no explicit un-demote call.

`docs/` currently does not mention `operation` at all — the concept has
dropped out of the user documentation entirely and is restored in Step 8.

## Verdicts: demote, don't exclude

The quality signal is the application's own opinion, and the pool must stay
functional even when that opinion is wrong. An automatic verdict derived from
it therefore never removes a model from routing — it moves it to the end of
the line. Exclusion requires either a missing key or an explicit human latch.

| Verdict | Owner | Stored? | Routing effect | Cleared by |
|---|---|---|---|---|
| `deprecated` | curator (preset) | static half | demoted — tier 1 | entry reappearing in the preset |
| quality demotion (per-op / global) | optimizer | derived from aggregates | demoted — tier 2 | new evidence (or `enable_llm` reset) |
| manual bench | human | profile (durable latch) | **excluded** | `enable_llm` only |

### Tiered selection

Slot selection for an operation partitions the AVAILABLE (non-cooling), keyed,
not-manually-benched candidates into tiers and serves from the **best
non-empty tier**; the existing selection policy (ε-exploration, transport
floor, latency/usable-rate ranking) applies *within* the chosen tier:

- **tier 0** — normal: not deprecated, operation not demoted;
- **tier 1** — `deprecated`: proven-fine models the curator has withdrawn;
  preferable to known-bad ones (they work, they are merely worse value);
- **tier 2** — quality-demoted for this operation; within the tier, untried
  operations of globally-demoted models rank ahead of evidenced-bad ones.

Consequences, by design:

- a demoted model is chosen **only when nothing better exists** for that
  operation (all tier-0/1 candidates cooling or absent) — "last resort"
  needs no fail-open exception machinery, the tiering *is* the behaviour;
- a rater that scores every model low demotes everything to tier 2 — and the
  pool keeps operating on transport ranking within that tier, exactly as it
  would without the quality signal at all. Miscalibration degrades the
  *ordering*, never the availability;
- Tier-1 ε-exploration never reaches a demoted tier (unlike the soft
  transport floor, which it deliberately bypasses): exploration keeps ranking
  honest among viable candidates, it does not re-inflict a known-bad model on
  users while better ones are available;
- serving a request from tier > 0 emits the existing under-provisioned-style
  debounced alert (visibility that the pool is degraded);
  `AllLLMsFailedError` remains reserved for "literally nothing available"
  (everything keyless, manually benched, or cooling).

### Auto (optimizer) — derived, per operation

- quality is judged from `quality_score` ratings via the decayed aggregate,
  per `(name, operation)` — the genuine usefulness signal — not from call
  `status` (which only reflects transport success);
- an operation is **demoted** when its Wilson-score **upper bound** (see the
  math section) sits below the quality threshold with sufficient evidence
  (`count` past the minimum); with insufficient evidence the bound is
  undefined and the rule simply does not fire — conservative by construction;
- **there is no time-based recovery and no probation traffic.** A model does
  not get smarter at the back of the queue, so no evidence-free path
  un-demotes it. Recovery paths are exactly: fresh evidence on a previously
  untried operation, last-resort traffic when the pool is degraded, a better
  replacement arriving via the preset, or a human `enable_llm`;
- a demoted operation's aggregate consequently near-freezes — which is
  consistent, because the verdict is *about* exactly that evidence and the
  model it was collected on (entry identity is immutable, see above).

### A useless model must be loud

A model whose measured quality is below the demotion boundary is a standing
problem someone will eventually want to act on (fix the rater, fix the
catalog, obtain a better key) — even though the pool no longer depends on
them acting. The signal is therefore aggressive and **standing**, not a
one-shot event:

- **on flip** (an operation becomes demoted, or the derived global verdict
  appears): an alert via the existing `Optimizer.add_alert` /
  `broker.alerts()` channel naming the model, the operation(s), the Wilson
  bound, and the `quality_floor − quality_margin` boundary, plus a
  `logger.warning`; the **global** verdict ("practically useless for
  everything it has been measured on") logs at `logger.error`;
- **while the condition holds**: the alert is re-emitted on a long debounce
  (default once per hour per model, an `Optimizer` field) — a standing
  condition must not depend on one fetch of `alerts()` that may never happen;
- **always visible in state**: `state()` / snapshots / `env` expose
  `deprecated` and the demoted operations per model, so any dashboard or
  support session shows *why* a model is not receiving traffic.

### Manual (admin)

`disable_llm(name, reason=…)` / `enable_llm(name)` on the broker set/clear the
one **stored** latch — the only verdict that actually excludes. A manual bench
covers every operation including future ones, is never overridden by the
optimizer, survives preset rolls, and only `enable_llm` clears it.
`enable_llm` also **resets the quality aggregates** for that model (all
operations) — a clean trial period, so the same stale evidence cannot
immediately re-derive a demotion. This is the single deliberate human override
in the system: an opt-out of self-regulation for one model.

## The learned profile lives in the registry

- **`LLMProfile`** (`src/llmbroker/models.py`, one typed dataclass ⇄ dict
  boundary, same pattern as `LLMState`): the per-operation summaries (all
  three kinds — quality, transport, latency) plus the manual-latch fields. Serialises to a single JSON document — reuse
  the columns-vs-JSON decision already implemented (see
  [`architecture.md`](../reference/architecture.md#columns-vs-json)): `name`,
  `user_id` are already the registry row's identity columns; the profile is
  one JSON body. Demotions are **not** stored — they are derived.
- **Registry protocol** (`src/llmbroker/protocols/registry.py`): add
  `read_profiles(user_id) -> dict[str, LLMProfile]` and
  `write_profile(name, profile, user_id) -> None`. No new protocol file, no
  new backend — two more methods on the registry every backend already
  implements.
- **DB registries** (`sqlite`, `postgres`, `mongodb`): one JSON column
  `profile` (`JSONB`/`TEXT`) on the existing `llmbroker_registry` row, next to
  `metadata`, via the **durable** version-gated `ensure_schema` path
  (additive `ALTER`; never `DROP` — the row is the valuable data).
- **File/TOML registry** (`src/llmbroker/standalone/registry.py`): config
  stays read-only from `llm.toml`/`.json`; the profile is persisted to a
  **sibling JSON file** (`<config_stem>.profile.json` by default,
  `profile_path=` override, `persist_profile=False` for the explicit
  zero-write mode where profiles are process-memory only). Keeping the
  profile in a separate sibling file is what makes a preset overwriting
  `llm.toml` physically unable to touch learned data.
- **Default is in-memory.** When a deployment configures neither profile
  persistence nor a state store, learned data lasts only for the process — an
  accepted trade-off for zero-config use, exactly like the state store.

## The math: decayed aggregates, effective sample size, decision band

### Shape: exponentially decayed weighted-proportion counter

A rate estimated from *all* history forever reacts too slowly to real
degradation; a stored sample window costs memory and scanning. Both aggregates
instead keep, per `(name, operation)`, **four numbers**:

- `weight ← weight · d + 1`
- `weighted_good ← weighted_good · d + value`
- `weight_sq ← weight_sq · d² + 1`
- `count ← count + 1` (plain, un-decayed integer)

where `value` is the event's own outcome — `1`/`0` for the ranking
aggregate's binary transport outcome, but the **raw `quality_score` itself**
(already a `[0, 1]` float) for the quality aggregate: a graded rating is fed
in as-is rather than forced through an arbitrary good/bad cutoff. Decay is
applied **per event, not per elapsed time** — the unit that matters for a
routing decision is "recent interactions", not calendar time. Update and
storage stay O(1): no list, no shifting, no history scan on load.

`weight` asymptotically approaches but never reaches `1 / (1 - d)`, so no
trust gate may ever compare against `weight` — trusting an aggregate is gated
on the separate, exactly-reachable `count`.

### Effective sample size: `(1+d)/(1-d)`, not `1/(1-d)`

The confidence math must not conflate **total weight** with **effective
sample size**. For exponential weights the variance-based (Kish) ESS is
`(Σw)² / Σw²`, which saturates at `(1+d)/(1-d)` — roughly **twice** the
weight ceiling `1/(1-d)`. Two consequences, both load-bearing:

- **Deriving `d` from a target `n`:** to make the aggregate carry the
  confidence of `n` independent samples, solve `(1+d)/(1-d) = n`, i.e.
  **`d = (n − 1) / (n + 1)`** — not `d = 1 − 1/n`.
- **Feeding the bound:** the Wilson computation uses the exact ESS at any
  fill level, `n_eff = weight² / weight_sq` (this is why `weight_sq` is
  tracked; after exactly one event `n_eff = 1`, saturating at `n`). The point
  estimate stays `weighted_good / weight`.

### Sizing the two windows — derived, not guessed

Target `n` comes from the standard sample-size-for-proportion formula
`n = z² · p(1-p) / d²` (`z` = confidence z-score, `d` = the gap to resolve,
`p` = the rate at the point of interest — worst case `p = 0.5`, otherwise
`p = quality_floor`):

- **Ranking (`usable_rate`)** — a wrong call costs one request's worth of a
  slightly worse pick; the next request self-corrects. Target 80% confidence
  (`z≈1.28`) to resolve a `0.2` gap around `usable_rate_floor=0.5`:
  `n ≈ 1.28² × 0.25 / 0.2² ≈ 10` → **`d_rank = 9/11 ≈ 0.818`** (weight
  ceiling ≈ 5.5, ESS → 10).
- **Quality demotion** — a wrong verdict pushes a working model behind every
  alternative until better evidence or a replacement arrives, and graded
  scores are noisier than a binary transport outcome, so it needs materially
  more confidence than a per-request ranking nudge. Target 95% (`z≈1.96`) to
  resolve a `0.15` gap at `p = quality_floor = 0.3` (`p(1-p) = 0.21`):
  `n ≈ 1.96² × 0.21 / 0.15² ≈ 36` → **`d_quality = 35/37 ≈ 0.946`** (weight
  ceiling ≈ 18.5, ESS → 36).

Both constants ship as `Optimizer` dataclass defaults; nothing for a
deployment to configure. The formulas are documented so the defaults are
reproducible if `quality_floor`/`quality_margin` ever change.

### The decision band is explicit: demotes ≤ floor − margin, never ≥ floor

Because ESS is **capped** at `n`, the Wilson interval has a permanent width
floor of roughly `z·√(p(1−p)/n) ≈ quality_margin` — it never tightens
further, however much traffic arrives. The rule "demote when the Wilson upper
bound < `quality_floor`" therefore means, by construction:

- true quality ≤ `quality_floor − quality_margin` (≈ 0.15): reliably demoted
  (at saturation, a point estimate of 0.15 puts the 95% upper bound at
  ≈ 0.30);
- true quality ≥ `quality_floor` (0.3): **never** demoted, no amount of
  evidence;
- the band in between: permanently undemotable — deliberately, this is the
  "poor but not hopeless" zone the soft ranking machinery (not tiering) is
  for.

`quality_floor` is thus the "definitely keep" boundary and
`quality_floor − quality_margin` the "definitely demote" boundary — the plan
and docs must present them this way, not as a single threshold.

### Wilson with fractional values is conservative, and that is fine

A `[0,1]`-valued score with mean `p` has variance at most `p(1−p)` (the
Bernoulli extreme is the worst case), so plugging fractional
`weighted_good` into the binomial Wilson formula can only **overstate**
uncertainty for graded ratings. The bound stays valid; it errs on the side of
not demoting.

### Smoothing recalibrated: the old Laplace prior is too heavy now

`(ok + 1)/(n + 2)` was tuned for a 50-sample window (prior weight ~4%). At
the ranking ceiling of 5.5 the same `+1/+2` prior weighs ~27% and visibly
distorts threshold crossings — with it, `removal_rate_floor = 0.15` becomes
borderline-unreachable. `usable_rate` therefore moves to the Jeffreys-style
half prior **`(weighted_ok + 0.5) / (weight + 1)`**. Consequences to encode in
tests (hand-recomputed, not carried over):

- from a saturated-good state, ~4 consecutive failures cross the 0.5 routing
  floor (was ~25 with the deque — the concrete "recent regression is no
  longer masked" fix);
- 10 consecutive failures from scratch give a smoothed rate ≈ 0.09 < 0.15, so
  retirement stays reachable at the `count` gate;
- the earlier assumption that `should_retire` / `OptimizerPolicy` tests carry
  over unchanged is **wrong** — thresholds shift with the new prior and decay;
  every expected value in those tests is recomputed.

## Re-seed: the catalog flows to users (SeedPolicy.SYNC)

Today the default `SeedPolicy.IF_EMPTY` seeds a user once and never delivers
later catalog updates. Add a new policy and make it the default:

- **`SeedPolicy.SYNC`** (new default), on every provision:
  - **add** entries new in the preset (marked `origin: preset`);
  - **update the operational static fields** of entries already present —
    `base_url`, `api_key_ref`, `metadata` (e.g. a revised `rate_limit`) —
    that is the curated half doing its job;
  - **never change `model` on an existing entry.** Entry identity is
    immutable (see the concept section): a preset row whose `model` differs
    from the stored entry of the same name is a curation error (it should
    have been a new name). `SYNC` refuses that one field-change and emits an
    alert naming both models — it neither silently re-attributes the learned
    evidence to a different model nor silently discards it;
  - **never delete.** An entry absent from the preset keeps its row and its
    learned profile;
  - **deprecate** `origin: preset` entries that are absent from the preset —
    the curator withdrew the endorsement. Deprecation is the mechanism that
    lets a strictly-better sibling actually displace the old model: the old
    entry stops burning the provider quota the two share (it is demoted to
    tier 1, used only when nothing better is available), while its profile
    and stats survive. It lifts automatically when the entry reappears in a
    later preset — fully reversible through the catalog, zero admin;
  - **never touch `origin: user` entries** — models the user added by hand
    are outside the preset's authority.
- `apply_seed` writes **only the static fields** — never the `profile`
  column / sibling file. That single rule is the "never clobber learned
  data" guarantee.
- `MIRROR` (hard-removes dropped entries) and `ADD` / `IF_EMPTY` remain
  available; only the default changes.

Why not quality-ranked routing instead of deprecation: "the new sibling is
strictly better" is knowledge the curator already holds with certainty, while
inferring it in-deployment would need hundreds of graded events per operation
and still lose to the older model on latency-first interactive ranking.
Deprecation delivers the conclusion in one sync.

## Relationship to the other plans

- The storage-shape foundation (see
  [`architecture.md`](../reference/architecture.md#columns-vs-json)): reuse
  its typed dataclass ⇄ JSON-document boundary and its version-gated
  `ensure_schema` pattern for the new `profile` column (durable `ALTER` path,
  contrasting the state store's disposable `DROP` path).
- The preset-onboarding foundation: this plan composes with the
  keyless-not-routable pool change and the zero-routable alarm, extending the
  routable predicate as described above. `phase` stays limited to
  `AVAILABLE`/`COOLING`; none of `deprecated`/`demoted` ever becomes a
  `LifecyclePhase` value.
- `catalog-refresh.md` consumes the identity invariants: model bump = new
  entry name; preset removal = deprecation, not data loss.

## Implementation

Ordered, self-contained steps. Run `invoke pre` and `python -m pytest` after
each; both green, no skips (testcontainers cover postgres/mongo, `fakeredis`
covers redis).

### Step 1 — `QualitySummary` and `LLMProfile`

File: `src/llmbroker/models.py`.

- Add `@dataclass class QualitySummary`: `weight: float = 0.0`,
  `weighted_good: float = 0.0`, `weight_sq: float = 0.0`, `count: int = 0` —
  the decayed counter described in the math section. Methods:
  - `update(value, decay)` applying the four-line fold;
  - `n_eff` property = `weight² / weight_sq` (`0.0` when empty);
  - `wilson_upper(self, z: float, *, min_count: int) -> float | None`: the
    Wilson-score upper bound of `weighted_good / weight` at confidence `z`
    computed with `n_eff`, or `None` when `count < min_count` (insufficient
    evidence to judge). `min_count` is a required parameter — callers pass
    their own trust threshold, keeping the aggregate free of Optimizer-level
    config.
- Add `@dataclass class LLMProfile` with: `stats: dict[str | None, dict[str,
  QualitySummary]]` (operation → kind → summary, the same `kind` vocabulary
  the state store uses: quality / transport / latency — the latency kind
  reuses the same shape with `value = latency_ms` and only the weighted mean
  read from it) and the manual-latch fields `benched: bool = False`,
  `benched_since: datetime | None = None`, `benched_reason: str | None =
  None`. No stored demotion fields — demotions are derived.
- `LLMProfile.to_dict()` / `from_dict()` written generically so future keys
  round-trip without change (mirror `LLMState`); doctest the round-trip incl.
  a tz-aware `benched_since`, a populated `quality` dict, and an unknown
  extra key preserved.

Tests (`tests/test_models.py`): round-trip incl. empty/populated `quality`;
unknown extra key preserved; `wilson_upper` against hand-computed bounds for
a few `(weight, weighted_good, weight_sq, count)` quadruples, including:
`n_eff == 1` after exactly one event; a low-`count` case returning `None`
with `weight` deliberately near its ceiling (the gate reads `count`, not
`weight`); a saturated case verifying `n_eff ≈ (1+d)/(1-d)`.

### Step 2 — registry reads/writes the learned profile

Files: `src/llmbroker/protocols/registry.py`, the three DB registries
(`sqlite`, `postgres`, `mongodb`), `src/llmbroker/standalone/registry.py`,
`sqlite/schema.py`, `postgres/schema.py`.

- Protocol: add `read_profiles(user_id) -> dict[str, LLMProfile]` and
  `write_profile(name, profile, user_id) -> None`. Plain last-writer-wins is
  correct here (see the storage section: snapshots are written from the
  merged shared view).
- SQL: one JSON column `profile` (`JSONB`/`TEXT`) on `llmbroker_registry`,
  via the durable version-gated `ensure_schema` path (additive
  `ALTER TABLE llmbroker_registry ADD COLUMN profile …` when the stored
  version marker is below the new `_SCHEMA_VERSION`; never `DROP`).
  `read_profiles` selects `(name, profile)`; `write_profile` `UPDATE`s
  **only** the `profile` column. The existing `sqlite`/`postgres` config
  `update()` is safe by construction — its explicit `SET` list simply never
  mentions `profile`.
- Mongo: store the `to_dict()` document under a `profile` key in the same
  record. **Mongo needs an explicit fix:** `mongodb/registry.py update()`
  today does a full-document `replace_one` built from scratch — once a
  `profile` key exists, that call silently deletes it on every config
  update. Switch `update()` to `update_one` with `$set` naming only the
  static fields.
- File/TOML registry: sibling JSON as described in the concept section
  (`profile_path=`, `persist_profile=False` no-op mode, atomic upsert of one
  model's entry).

Tests (`tests/test_registry_*`): each backend round-trips an `LLMProfile`
(quality summaries + a manual latch with tz-aware `since`); a
future-proofing extra key survives; config `update()` does **not** clobber a
stored `profile`; seed static writes leave `profile` intact (assert
directly). Migration test (sqlite/postgres): seed an old version marker with
no `profile` column, run `ensure_schema`, assert the column exists and
pre-existing rows are kept. File registry: default sibling path derived from
the config path; round-trip; `persist_profile=False` writes nothing and
reads `{}`.

### Step 3 — optimizer: decayed aggregates, per-op demotions, distrust ordering

File: `src/llmbroker/optimizer.py`.

**3a — replace `_rolling` with the decayed ranking aggregate:**

- Replace `_rolling: dict[(name, operation), deque[Call]]` with a
  `QualitySummary` per `(name, operation)` for transport outcomes (every
  call, `value ∈ {0, 1}`), plus a latency `QualitySummary` (`value =
  latency_ms`, OK calls only, only the weighted mean read) — both decayed
  with `d_rank = 9/11` (`n = 10`; see the math section), both persisted as
  their respective `kind` (see the storage section — short-lived processes
  accumulate them across runs via the profile snapshot).
- `usable_rate(name, operation)` returns the Jeffreys-smoothed point
  estimate `(weighted_ok + 0.5) / (weight + 1)`, or `None` when `count <
  min_sample_count` — same public signature; the trust gate is still a plain
  count.
- `mean_latency_ms(name, operation)` returns
  `latency_weighted_sum / latency_weight`, `None` when no OK call recorded —
  same public signature and semantics as today.
- `should_retire` and `OptimizerPolicy` keep calling these two methods; their
  numeric behaviour shifts with the new prior and decay, and their tests are
  **recomputed by hand**, not carried over (see the math section).
- `rolling_window` is removed from `Optimizer`; `min_sample_count` stays.

**3b — quality aggregate + derived demotions:**

- `OptimizerTelemetry.record_quality(call_id, score)` cannot resolve the
  `(name, operation)` a score belongs to from its arguments. Add a bounded
  `_call_index: dict[str, tuple[str, str | None]]` on `OptimizerTelemetry`,
  populated in `record()` (every `Call` carries `id`, `llm_name`,
  `operation`) and consulted — then popped — in `record_quality()`. Since
  ratings arrive from the calling code at up to call frequency, size the cap
  for the in-flight window (an `OrderedDict` evicting the oldest once full);
  a `call_id` that has aged out or belongs to a prior process is dropped
  with the same `logger.warning`-and-continue fallback
  `AsyncResult.record_quality` already uses.
- Feed the resolved `(name, operation, score)` into that pair's quality
  `QualitySummary` with `d_quality = 35/37` — the raw `[0,1]` score, never
  collapsed to a flag — in addition to today's `score == 0 →
  mark_quality_fail`.
- Config fields: `quality_floor: float = 0.3`, `quality_margin: float =
  0.15`, `quality_confidence: float = 0.95`, with the math-section
  derivations: `quality_effective_n = 36` (also the `min_count` passed to
  `wilson_upper`, rounded up), `d_quality = (36−1)/(36+1)`; plus
  `demotion_realert_interval` (default 1 hour) for the standing signal.
- `evaluate_demotions(name) -> dict[str | None, bool]` (or an equivalent
  small result object): per-operation demotions — demoted ⟺
  `wilson_upper(z(quality_confidence), min_count=36)` is defined and `<
  quality_floor`. Globally demoted is **derived**: all defined ops demoted ∧
  at least one defined. Not consulted when a manual latch is set (manual
  excludes outright).
- `load_summaries(...)` / `to_profile(name)`: warm-start assignment from
  persisted aggregates and serialisation back — direct field assignment, no
  recomputation from raw calls.

Tests (`tests/test_optimizer.py`):
- Ranking: `usable_rate`/`mean_latency_ms` match hand-computed decayed
  values after a known event sequence; a saturated-good model crosses the
  0.5 floor after ~4 consecutive failures (regression: recent regression no
  longer masked) and a stale bad streak is forgiven after a comparable good
  streak; retirement reachable from scratch at the `count` gate;
  `usable_rate` becomes non-`None` after `min_sample_count` events (the gate
  tracks `count`, not the asymptotic `weight`).
- Quality: aggregate matches hand-computed
  `weight`/`weighted_good`/`weight_sq` after a known rating sequence with
  `count` un-decayed; all evidenced ops bad → globally demoted; bad on one
  op, good on another → only that op demoted; every op below `min_count` →
  nothing demoted; a point estimate of `quality_floor − quality_margin` at
  saturation trips the bound while `quality_floor` does not (the
  decision-band test); manual latch suppresses `evaluate_demotions`
  entirely; summaries restored via `load_summaries` reproduce the same
  bounds; a demoted op with no further events keeps a frozen aggregate (no
  time-based drift).
- Derived recovery: a globally-demoted model rated well on a previously
  unseen operation loses the global verdict with no explicit un-demote call.
- `record_quality`: a rating for a recently recorded `call_id` updates the
  right `(name, operation)`; unknown/evicted `call_id` warns and is dropped;
  `_call_index` never exceeds its cap under sustained traffic.

### Step 4 — state store: shared summaries with atomic decayed folds

Files: `src/llmbroker/protocols/state_store.py`, the four state stores
(`sqlite`, `redis`, `postgres`, `mongodb`), plus the broker-side in-memory
fallback.

- Extend `StateStoreProtocol` with summary operations:
  - `apply_summary_delta(name, operation, kind, decay_pow, add_weight,
    add_good, add_weight_sq, add_count, user_id)` — one **server-side atomic
    fold**: `weight ← weight · decay_pow + add_weight`, and likewise for the
    other fields (`weight_sq` uses `decay_pow²`; `count` is a plain add).
    `kind` distinguishes the ranking and quality aggregates (latency rides
    the same mechanism as a third kind);
  - `read_summaries(user_id) -> dict[(name, operation, kind),
    QualitySummary]`;
  - `seed_summary(name, operation, kind, summary, user_id)` — insert-if-absent
    (idempotent across racing instances), used to warm-start a fresh state
    store from the registry snapshot.
- The delta form makes client batching exact: an instance folds `k` local
  events between flushes and applies them in one call with
  `decay_pow = d^k`, `add_weight = Σ d^(k−i)`, `add_good = Σ d^(k−i)·vᵢ` —
  algebraically identical to applying the `k` events one by one. Interleaved
  batches from different instances commute only approximately (event order
  across instances is arbitrary), which is acceptable: the events are
  exchangeable and the aggregate is order-insensitive in expectation.
- Atomicity per backend: SQL backends use single-`UPDATE` arithmetic
  (`SET weight = weight * :dp + :aw, …`); Mongo uses an
  aggregation-pipeline update; Redis a Lua script over a hash. A dedicated
  numeric structure (`llmbroker_summaries` table / hash / document per
  `(name, user_id)`) — **not** JSON-blob read-modify-write, which is exactly
  the clobber this step exists to avoid. Disposable schema path (`DROP` on
  version bump), like the rest of the state store.
- Flush cadence: accumulate locally, flush pending deltas on the existing
  store-cache TTL heartbeat and on `aclose`; read the merged summaries
  through the same TTL cache (`_get_store_cache` pattern). No state store ⇒
  the in-memory summaries are authoritative (single-instance semantics).

Tests (`tests/test_state_store_*`): the atomic fold matches a hand-computed
sequential application per backend; two concurrent appliers (asyncio tasks)
never lose events (final `count` is the sum); batched delta ≡ event-by-event
application; `seed_summary` is idempotent; schema bump drops and recreates
the summaries structure.

### Step 5 — pool: tiered selection

Files: `src/llmbroker/broker/pool.py`, `src/llmbroker/broker/state.py`.

- Queue-level withdrawal stays reserved for the op-independent hard
  exclusions: keyless (existing) and the manual latch — `set_benched(name)` /
  `clear_benched(name)` withdraw/readmit the slot. `_reenqueue_config` and
  `cool_down`'s expiry callback must check the latch — a model can be
  mid-cooldown when it gets manually benched.
- `deprecated` and per-op demotions do **not** withdraw slots: they are
  markers consulted at acquire time (`set_deprecated(name)` /
  `clear_deprecated(name)`; the demotion map is refreshed from the optimizer,
  see Step 6), because tier membership depends on the operation and must not
  hide the slot from other operations.
- Acquire partitions the available candidates into tiers (0 = normal, 1 =
  deprecated, 2 = quality-demoted for this operation, with untried
  operations of globally-demoted models ordered ahead of evidenced-bad ones)
  and passes the best non-empty tier to the selection policy; ε-exploration
  therefore never crosses a tier boundary.
- Serving from tier > 0 triggers the existing debounced
  under-provisioned-style alert; `AllLLMsFailedError` only when no tier has
  an available candidate.
- `state()` / snapshots expose `deprecated` and the demoted operations so
  `env` and admin views show why a model is idle.

Tests (`tests/test_pool.py`): a manually-benched keyed config is in
`configs` but never acquired, even when it is the only one; a deprecated
config is acquired only when no tier-0 candidate is available; an op-demoted
config is skipped for that operation while alternatives exist, chosen when
none do, and acquired normally for other operations; with every model
demoted the pool still serves (transport ranking within tier 2); the
degraded-tier alert fires debounced.

### Step 6 — wire the profile through the broker

Files: `src/llmbroker/broker/broker.py`, `src/llmbroker/broker/catalog.py`,
`src/llmbroker/sync.py`.

- No new broker constructor argument: profiles go through the **registry**
  the broker already holds; shared summaries through the **state store** it
  already holds.
- Provision: after `catalog.provision()`, read `registry.read_profiles`,
  `seed_summary` the state store from them (insert-if-absent), apply manual
  latches and `deprecated` markers to the pool, and load the merged
  summaries into the optimizer.
- Live path: rated events update the local summaries immediately and enqueue
  deltas for the flush cadence (Step 4). On the same TTL heartbeat, refresh
  the merged summaries, re-derive demotions, and push the demotion map to
  the pool — this is how a verdict decided by evidence on instance A reaches
  instance B within one TTL, with no separate verdict-propagation channel.
- Durable snapshot: serialise the merged summaries of **all kinds** (quality,
  transport, latency) + manual latch into `LLMProfile` and
  `registry.write_profile` on a debounce and on `aclose` (manual-latch
  changes persist immediately). The `aclose` snapshot is what makes learned
  knowledge accumulate across short-lived processes — a script that makes
  one call and exits still contributes its events to the next run.
- The loud signal (see "A useless model must be loud"): on a demotion flip,
  `add_alert` + `logger.warning` with model, operation(s), Wilson bound and
  the `quality_floor − quality_margin` boundary; on a global flip,
  `logger.error`; while any demotion holds, re-emit the alert every
  `demotion_realert_interval` (per model, monotonic-clock debounce like the
  existing floor alert).
- Manual API: `AsyncBroker.disable_llm(name, *, reason=None)` /
  `enable_llm(name)` — set/clear the manual latch via `registry.write_profile`
  and the pool; `enable_llm` also resets the model's quality aggregates
  (registry snapshot and state-store summaries). Proxy both on the sync
  `Broker`.

Tests (`tests/test_broker_bench.py`, `tests/test_sync.py`): a persisted
manual latch and a deprecated marker are applied at provision; a live
derived demotion moves the op's traffic away and, via a second broker
instance sharing the same state store, reaches that instance within one TTL
refresh; the flip alert and the standing re-alert both fire (re-alert
respects the interval); `disable_llm`/`enable_llm` round-trip through
registry and pool, and `enable_llm` demonstrably resets the aggregates;
snapshot lands in the registry on `aclose`; **short-lived accumulation**: N
sequential broker lifecycles (open → one call → `aclose`) against the same
registry accumulate `count` across runs until `usable_rate` becomes
non-`None`, and the first selection of a fresh run is driven by the persisted
latency/rate of previous runs (no state store configured); sync proxies work.

### Step 7 — `SeedPolicy.SYNC` default

Files: `src/llmbroker/models.py`, `src/llmbroker/broker/catalog.py`,
`src/llmbroker/broker/broker.py`.

- Add `SeedPolicy.SYNC = "sync"` implementing the semantics from the SYNC
  section: add-new (with `origin: preset`), update operational fields only,
  refuse `model` changes with an alert, deprecate absent preset-origin
  entries, lift deprecation on reappearance, never delete, never touch
  user-origin entries.
- `broker.add(cfg)` marks `origin: user`.
- Change the default `seed_policy` on `AsyncBroker` (and preset wiring) from
  `IF_EMPTY` to `SYNC`.
- Confirm `apply_seed` never writes `profile`.

Tests (`tests/test_catalog_seed.py`): `SYNC` adds a preset-new model,
updates a changed `rate_limit`, refuses a changed `model` under an existing
name (entry intact + alert), deprecates a dropped preset-origin model
(profile intact, acquired only when no tier-0 candidate exists), lifts
deprecation on reappearance, and leaves a user-added model alone; a manual
latch survives a `SYNC` re-seed; the default policy is `SYNC`.

### Step 8 — docs

Files: `README`, `docs/src/en/*` + `docs/src/ru/*`,
`specs/reference/architecture.md`, `specs/reference/optimizer.md`.

- **Restore `operation` to the user docs — it is currently absent from
  `docs/src/` entirely**: what it is, passing it on `ask`/`chat`, that all
  quality accounting and demotions are per-operation, and
  `background_operations` ranking.
- Document the two-halves catalog, entry-identity immutability ("model bump
  = new entry name"), profile persistence modes, the verdict table (demote
  vs exclude, owners, tiers), the decision band (`quality_floor` vs
  `quality_floor − quality_margin`), the standing "practically useless"
  signal and where it surfaces (`alerts()`, logs, `state()`/`env`), the
  state store's role in cluster-sharing the aggregates (and that
  multi-instance learned stats require a state store), and `SYNC` as the
  default.

## Non-goals

- **A separate profile backend.** The learned profile is a field of the
  registry row; the shared summaries are a structure of the existing state
  store — no fifth store type.
- **Automatic exclusion on the quality signal.** Ratings are external and
  possibly miscalibrated; they demote, never remove. Exclusion belongs to
  missing keys and to the human latch only.
- **Time-based demotion recovery / probation traffic.** A model does not
  improve by resting; verdicts change only with evidence (an untried
  operation, last-resort traffic) or a human `enable_llm`. Pool health
  improves by replacement through the preset, not rehabilitation.
- **Cross-instance sync without a state store.** The registry is the durable
  home, not a coordination channel; multi-instance deployments need a state
  store (already the documented rule for cooldowns).
- **Moving `cooldown`/`phase` into the profile.** Ephemeral sync-state stays
  in the state store; only durable learned knowledge lives in the profile.
- **A telemetry/crowdsourcing pipeline.** The quality aggregate is this
  deployment's own ratings materialised for cheap reads and restart
  survival — not shared or uploaded (see
  [`freetier-providers.md`](../reference/freetier-providers.md)).
- **Quality-ranked routing beyond tier demotion.** "The new sibling is
  strictly better" is delivered by curated deprecation, not learned
  in-deployment.
- **Removing dropped models on re-seed by default.** `SYNC` deprecates them
  (keeping the learned profile); `MIRROR` remains for callers who explicitly
  want pruning.
