# Optimizer

The `Optimizer` is an optional component that improves pool health over time: it computes
each cooldown from the provider's own signal on the actual response, tracks per-LLM
transport and quality health, derives per-operation demotions from rated quality, and
automatically retires a persistently unreliable LLM. There are only two lifecycle
phases, AVAILABLE and COOLING, always derived from `cooldown_until` vs. now — no
separate circuit-breaker state machine sits on top of them. Demotions and the curated
`deprecated` marker are a separate, softer axis layered on top (see "Tiered selection"
below) — they never become a `LifecyclePhase`.

Pass `optimize=True` (the default) or an explicit `Optimizer(...)` instance to
`AsyncBroker` / `Broker` to activate it.

---

## Cooldown duration: trust the provider

On a 429/503, the wait duration is computed fresh from *that response's own* signal —
never carried forward as persistent per-LLM state between events:

- If the provider sends `Retry-After` (seconds, or an HTTP-date), that number is used
  as-is on the first failure of a streak — the provider is the authority on its own
  quota and reset schedule.
- If **this same LLM** is already mid-failure-streak (no success since its last
  429/503), the response's own number is scaled by `backoff_factor ** consecutive_fails`,
  where `consecutive_fails` counts how many 429/503s have landed in a row since the
  last success. A success resets the streak to zero. This means a day-long quota wait
  followed by an unrelated 60-second rate limit never compounds into "wait two days" —
  each event is scaled from its own number, not a carried-forward one.
- If `Retry-After` is absent, a flat default (`60s`) is used as the base before the
  same streak-scaling is applied.
- The final wait is capped at `max_delay`.

A non-rate-limit failure (a generic HTTP error, a network error, or an HTTP 401/403)
uses the same formula with the flat default as its base, since there is no
`Retry-After` to read.

---

## Automatic retirement

There is no probe/offline cycle: after a cooldown ends, the slot simply re-enters the
normal queue rotation, and the next request routed to it *is* the health check.

Instead, retirement is driven by transport health: `should_retire` trips when an LLM's
`usable_rate` (see "Decayed aggregates" below) has at least `min_sample_count` samples
and sits below `removal_rate_floor` — a threshold distinct from, and stricter than, the
routing-only `usable_rate_floor` (which only deprioritizes a candidate, with margin to
keep it around as a last resort). When it trips, the LLM is dropped from the pool and an
alert is emitted. To restore it the operator must fix the underlying issue and re-add it
via `broker.add(cfg)`.

This is a harder, pool-membership verdict than a quality demotion (below): retirement
removes the LLM from the pool entirely, driven by transport failures (429/503/error),
not by rated answer quality. A model that answers `200 OK` with poor content is never
retired by this mechanism — that is exactly what quality demotion is for.

This check runs after every non-`OK` outcome, rate-limit or generic error alike. An
API key that is dead (HTTP 401 or 403) instead triggers immediate, unconditional
retirement — no amount of retrying fixes an invalid key, so it bypasses the quality
signal entirely.

A well-behaved daily-capped LLM (long, honored cooldowns, but successful whenever
actually tried) is never flagged for removal, however much cumulative time it spends
cooling: nothing is attempted while a slot is cooling, so an honored wait produces no
failed samples — only a call that is actually attempted and fails drags `usable_rate`
down.

Every non-`OK`, non-dead-key outcome fails over to the next available LLM instead of
raising to the caller of that one request — a generic HTTP error, a network error, and
now also a 401/403 all cool (or drop) the slot and let the router try the next LLM
within the same request. `AllLLMsFailedError` is reserved for the genuine "zero usable
models" case (see the registry/catalog docs for the keyless-pool behavior), never for
"this one LLM had a bad response".

---

## The learned profile: durable, cluster-shared, per operation

The catalog splits into a **static half** (curated: `name`, `base_url`, `model`,
`api_key_ref`, `metadata`, plus the two curator markers `origin` and `deprecated`) and a
**dynamic half** — the *learned profile*: what this deployment has learned about each
model, per kind of task. The optimizer owns the dynamic half; the seed path never writes
it. Both live in the same registry row, keyed by `(name, user_id)` — there is no
separate profile backend.

**Entry identity is immutable.** Within one catalog entry (`name`), the `model` it
points at never changes — all learned evidence is *about* that model. A new model
version is a new entry with a new name and a fresh, empty profile; it earns its place
through the normal trial period (fewer than `min_sample_count`/the quality
`min_count` passes the relevant floor unconditionally). This is what removes any need
for evidence-invalidation machinery.

Everything below is keyed by `(name, operation)` — a model's usefulness is genuinely
operation-shaped (weak on hard tasks, fine on simple ones). Calls made without an
`operation` fall into the `None` bucket. Pass `operation=` on `ask()`/`chat()` to
enable this:

```python
reply = await llms.ask("Summarize this contract clause", operation="summarize")
reply.record_quality(0.9)  # rated on the "summarize" bucket specifically
```

### Where the numbers live

- **State store — cross-instance sharing.** When a `StateStoreProtocol` backend is
  configured, every rated event/call outcome is folded into shared decayed summaries
  there via a server-side atomic operation (never client read-modify-write, so
  concurrent instances cannot clobber each other). Every instance periodically
  (roughly every 2 seconds of activity) re-reads the merged view and re-derives
  demotions from it — this is how a verdict decided by evidence on instance A reaches
  instance B, with no separate verdict-propagation channel.
- **Registry `profile` field — durability.** The merged aggregates (and the manual
  bench latch) are snapshotted into the registry row on a debounce and on `aclose()`,
  and read back at provision to warm-start a fresh state store (or a fresh process
  when no state store is configured).
- **No state store configured ⇒** summaries live in process memory and the registry
  snapshot on `aclose()` is the only persistence — single-instance semantics, exactly
  like cooldowns today. This is also what makes a **short-lived script** (one call,
  exit) learn something: the `aclose()` snapshot accumulates knowledge across runs, so
  the first pick of the next run is informed by every previous one.

### Profile persistence per registry backend

| Registry | Where the profile lives |
|---|---|
| `llmbroker.sqlite.Registry`, `llmbroker.postgres.Registry`, `llmbroker.mongodb.Registry` | one JSON column/field (`profile`) on the same row as the static config |
| File/TOML `Registry(path)` | a sibling JSON file next to the config, with a configurable path; an explicit zero-write mode is available (profiles live only in process memory) |

The sibling-file design for the file registry is deliberate: a preset overwriting
`llm.toml` is physically unable to touch the learned data next to it.

---

## Decayed aggregates

Both the transport-ranking aggregate (replacing the old rolling window) and the quality
aggregate keep, per `(name, operation)`, a small set of decayed statistics: an overall
weight, a weighted count of successes, a weight-squared term (for confidence bounds),
and a plain, un-decayed sample count. Decay is applied **per event**, not per elapsed
time — the unit that matters for a routing decision is "recent interactions", not
calendar time. The sample count is the only number ever compared against a trust
threshold — the decayed weight asymptotically approaches but never reaches a ceiling
and must never be used as a gate.

- **Ranking aggregate** — every call folds in a transport-OK/not-OK outcome; OK calls
  with a latency also fold into a parallel latency aggregate. Tuned for an
  80%-confidence 10-sample-equivalent window: quick to react, since a wrong ranking
  pick only costs one request's worth of a slightly worse choice. The usable-rate point
  estimate is Jeffreys-smoothed (undefined below the minimum sample count); mean
  latency is undefined with no OK call recorded yet.
- **Quality aggregate** — each recorded quality score folds in the raw `[0, 1]` value
  as-is (never collapsed to a pass/fail flag). Tuned for 95%-confidence, 36-sample-
  equivalent evidence, since a wrong quality verdict pushes a working model behind
  every alternative and graded scores are noisier than a binary transport outcome.

---

## Alerts

Alerts are retrievable via `AsyncBroker.alerts()` and accumulate until fetched, then
clear. Conditions that emit alerts:

- **Auth failure** — a call returns HTTP 401 or 403. The LLM is immediately and
  permanently dropped; the alert names the `api_key_ref` to fix.
- **Retirement** — `usable_rate` drops below `removal_rate_floor` with enough samples.
  The LLM is permanently dropped and an alert is emitted.
- **Pool under-provisioned** — `NoLLMAvailableError` is raised and all LLMs in the
  pool are simultaneously non-AVAILABLE (COOLING). This alert is debounced per broker
  instance: at most one emission per 60 seconds.
- **Serving from a degraded tier** — a request was served from a `deprecated` or
  quality-demoted candidate because nothing better was available. Debounced per
  `(name, operation)`, once per 60 seconds.
- **Quality demotion (flip + standing)** — an operation becomes demoted, or a model's
  derived global verdict appears (see "A useless model must be loud" below).
- **Seed `SYNC` refusing a model-identity change** — a preset row's `model` differs
  from the stored entry of the same name; see "Re-seeding" below.

---

## Verdicts: demote, don't exclude

The quality signal is the calling application's own opinion — in the overwhelming
majority of deployments it arrives from the caller grading its own results, and it may
be miscalibrated. An automatic verdict derived from it therefore never removes a model
from routing — it moves it to the end of the line. Exclusion is reserved for a missing
key and for an explicit human latch.

| Verdict | Owner | Stored? | Routing effect | Cleared by |
|---|---|---|---|---|
| `deprecated` | curator (preset, via `SeedPolicy.SYNC`) | static half | demoted — tier 1 | entry reappearing in the preset |
| quality demotion (per-op / global) | optimizer | derived from aggregates, never stored | demoted — tier 2/3 | new evidence (or `enable_llm` reset) |
| manual bench | human (`disable_llm`) | profile (durable latch) | **excluded** | `enable_llm` only |

### Tiered selection

Slot acquisition for an operation partitions the AVAILABLE (non-cooling), keyed,
not-manually-benched candidates into tiers and serves from the **best non-empty
tier**; the usual selection machinery (ε-exploration, quality floor, latency/usable-rate
ranking — described below under "Ranking within a tier") applies *within* the chosen
tier only:

- **tier 0** — normal: not deprecated, operation not demoted;
- **tier 1** — `deprecated`: proven-fine models the curator has withdrawn (preferable to
  known-bad ones — they work, they are merely worse value);
- **tier 2** — a globally-demoted model's *untried* operation: new evidence territory,
  tried before evidenced-bad operations when the pool has nothing better;
- **tier 3** — quality-demoted for this specific operation: evidenced bad.

A model is **globally demoted** ⟺ every operation with sufficient evidence is below the
quality floor and at least one such operation exists — derived fresh from the shared
aggregates each refresh, never stored, so no separate un-demote call is ever needed:
new evidence on any operation simply changes the derivation.

Consequences, by design:

- a demoted/deprecated model is chosen **only when nothing better exists** for that
  operation — tiering *is* the "last resort" behavior, no extra fail-open logic;
- a rater that scores every model low demotes everything to tier 2/3 — and the pool
  keeps operating on transport ranking within that tier, exactly as it would without
  the quality signal at all. Miscalibration degrades the *ordering*, never the
  availability;
- there is **no time-based demotion recovery and no probation traffic** — a model does
  not get smarter at the back of the queue. Recovery paths are exactly: fresh evidence
  on a previously untried operation, last-resort traffic when the pool is degraded, a
  better replacement arriving via the preset (deprecation), or a human `enable_llm`.

### The decision band

Because the trust gate (`count`) is capped by the same fixed window the confidence math
targets, the demotion rule "Wilson-score upper bound < `quality_floor`" resolves to a
band, not a single threshold:

- true quality at or below `quality_floor − quality_margin` (defaults: 0.3 − 0.15 =
  0.15): reliably demoted;
- true quality at or above `quality_floor` (0.3): **never** demoted, no amount of
  evidence;
- the band in between is permanently undemotable by design — that "poor but not
  hopeless" zone is what the soft ranking (usable-rate ordering), not tiering, is for.

### A useless model must be loud

A model whose measured quality sits below the demotion boundary is a standing problem
someone will eventually want to act on (fix the rater, fix the catalog, obtain a better
key) — even though the pool no longer depends on them acting:

- **on flip** (an operation becomes demoted, or the global verdict appears): an alert
  naming the model, operation, Wilson bound, and the `quality_floor − quality_margin`
  boundary, plus a `logger.warning`; the global verdict additionally logs at
  `logger.error`;
- **while the condition holds**: re-emitted on a long debounce (`demotion_realert_interval`,
  default one hour) — a standing condition does not depend on one fetch of `alerts()`
  that may never happen.

### Manual bench

```python
await llms.disable_llm("groq-llama", reason="hallucinating on our eval set")
...
await llms.enable_llm("groq-llama")
```

`disable_llm(name, reason=...)` / `enable_llm(name)` set/clear the one **stored** latch
that actually excludes — the only verdict that covers every operation including future
ones, is never overridden by the optimizer, and survives preset rolls. `enable_llm` also
**resets the quality aggregates** for that model (optimizer, state store, and registry
snapshot) — a clean trial period, so the same stale evidence cannot immediately
re-derive a demotion. This is the one deliberate human override in the system: an
opt-out of self-regulation for a single model.

---

## Ranking within a tier

Within whichever tier `acquire()` selects, ranking works exactly as before — pool
availability gating (only AVAILABLE-phase LLMs are candidates), then:

**Exploration reserve.**
A random fraction (`exploration_fraction`, default 10%) of selections are routed
uniformly at random across the tier's candidates, bypassing floor gating and ranking.
This is ε-greedy: without occasional exploration, data never accumulates on LLMs that
have been ranked out, making the ranking permanent regardless of whether the LLM has
recovered. Exploration never crosses a tier boundary — it keeps ranking honest among
viable candidates, it does not re-inflict a known-bad model on users while better ones
are available.

**Quality floor.**
Candidates with a `usable_rate` below `usable_rate_floor` (default 0.5) are excluded.
An LLM with fewer than `min_sample_count` samples always passes unconditionally — new
LLMs must be tried before they can be judged. If all candidates in the tier fail the
floor, the floor is dropped for this selection and an alert is emitted.

**Objective ranking.**
The surviving candidates are ranked by a two-element tuple. The objective depends on
whether the operation is listed in `background_operations`:

- **Background operation** — quality matters most: rank by `(-usable_rate, latency)`.
- **Interactive operation** (default) — latency matters most: rank by `(latency, -usable_rate)`.

An LLM with no OK calls receives `latency = ∞`, ranking last in interactive mode.
An LLM with fewer than `min_sample_count` samples receives a neutral prior rate of
0.5 in the ranking key.

### TPM awareness

A `max_tpm`-based ranking axis was considered and rejected: free-tier LLMs rarely
publish exact TPM limits, so the field would almost always be absent. Sustained
rate-limiting is already handled empirically by the cooldown formula and
`usable_rate`. TPM awareness can be revisited if a concrete use-case with known
limits emerges.
