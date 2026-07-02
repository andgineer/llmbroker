# Curated catalog & key-effort onboarding

## Plan sequence — step 1 of 3

> **Prerequisites:** the storage-shape foundation (columns-vs-JSON; `RateLimit`,
> `LLMConfig.rate_limit`, the generic `extra`-preserving `LLMState` (de)serialization,
> and the version-gated `ensure_schema` migration path) is already implemented — see
> [`architecture.md`](../reference/architecture.md#columns-vs-json).
> **Blocks:** `optimizer-learned-profile.md` (step 2), which extends the
> keyless-not-routable pool change and the zero-routable alarm introduced here,
> and `catalog-refresh.md` (step 3), which consumes the `effort`/`value`/
> `rate_limit` taxonomies finalized here.

The remaining three plans form one dependency chain; execute in this order:

1. **`preset-onboarding-effort.md`** *(this plan)* — curated catalog knowledge,
   effort/value onboarding, a simplified two-phase (AVAILABLE/COOLING) reliability
   model that honors the provider's own `Retry-After`, and the keyless-not-routable
   pool change.
2. **`optimizer-learned-profile.md`** — the durable learned half (learned profile
   carried in the registry, bench verdict) and `SeedPolicy.SYNC`; extends the
   routable predicate from this plan.
3. **`catalog-refresh.md`** — the manual re-curation runbook; consumes the
   taxonomies fixed here and may run in parallel with (2).

## Problem statement

llmbroker's value is pooling many free, rate-limited, mediocre LLM endpoints and
routing across them, so their combined availability adds up to dependable,
"good-enough" quality without paying for a premium model. That value only
materializes once the user has keys for *several* providers.

llmbroker's job is to ship **both the tool and curated knowledge about the
LLMs** — not a bare list. The current flow does the opposite: a preset lists
models as `name/base_url/model/api_key_ref` and nothing else. Everything we
actually know about these providers (availability windows, retry behavior, how
hard a key is to get, whether the models are any good) is absent, so every user
rediscovers it from a cold start: the `Optimizer` begins at a flat
`initial_delay = 60` and re-learns each provider's rate-limit spacing by trial.
Handing out a "we know nothing about these LLMs" list and making each user
re-derive availability/retry parameters individually contradicts the package's
reason to exist.

Two friction points compound this:

- **Getting keys is the user's main pain.** The current preset gives no signal
  about which keys are easy to get versus which require real effort (e.g. a key
  buried in the AWS/GCP cloud console behind billing and free-tier setup is
  far more daunting than an OAuth sign-in), and no signal about which keys are
  *worth* the effort.
- **Working without some keys is the normal mode, not a failure.** We do not
  expect any user to obtain every key; the pool is designed to assemble itself
  from whatever keys are present. Yet the code currently treats an unresolved
  key as alarming: `catalog` logs a per-key warning that "calls will fail", and
  the router raises `AllLLMsFailedError` for an unkeyed config. Missing keys for
  *some* models must read as expected; only **zero usable models** is a real
  error.

A secondary smell: `presets/smart-freetier.toml` is an almost-exact duplicate of
`presets/freetier.toml` (one extra model, different header comment). It carries
no clear value and should be resolved into a single catalog.

## Goals

- Collapse to a single curated catalog; the user decides how many keys to get,
  and the pool fills in from whatever is present (close to zero-config).
- Ship curated per-provider knowledge so users **start fast and work well out of
  the box**, instead of every optimizer re-learning each provider from zero.
- Make key acquisition legible on two axes the user actually weighs:
  - **effort** — how hard the key is to obtain;
  - **value** — how good the models it unlocks are (quantity is irrelevant; a
    provider may expose ten models yet only one is genuinely usable).
- Make partial-key operation read as the **normal mode** throughout messaging.
- Serve both usage modes from one source of metadata:
  - simple mode: TOML config + one-line usage; feedback via `env` output;
  - app mode: own DB + own app giving live feedback (e.g. "you have N active
    models; this easy key unlocks M more").

## Research phase (front-loaded — determines the rest)

We are not equipped to run a telemetry pipeline (see non-goals), so the curated
knowledge is gathered **manually from open sources** before the schema is fixed.
The taxonomies and field shapes below are *outputs* of this phase, not
assumptions. The plan deliberately does not pre-invent them.

**Status: complete.** The curated provider knowledge — sources, exact numbers,
and the `effort`/`value`/`rate_limit` taxonomies — is recorded as reference in
[`../reference/freetier-providers.md`](../reference/freetier-providers.md). The
Design section below carries the resulting design decisions.

Research tasks:

1. **Find public sources** that track free-tier behavior of LLM providers —
   community projects, status pages, provider docs that publish rate limits and
   reliability. Capture what is available and stable enough to bake into a
   catalog.
2. **Rate-limit shape.** Determine the real limit dimensions per provider. A
   single number is likely insufficient: free tiers commonly impose multiple
   windows (e.g. requests-per-minute *and* requests-per-day), sometimes also
   token windows. The `rate_limit` field shape (a small sub-table of named
   windows) is fixed from these findings.
3. **Effort taxonomy.** Derive the effort scale from the *actual* friction users
   hit when obtaining keys — OAuth sign-in vs. new-account signup vs. phone/card
   verification vs. **keys/billing/free-tier buried in a complex cloud console
   (AWS, Google Cloud)** vs. waitlist/approval. The number and names of buckets
   are an output here, not a guess; the ordering doubles as the easiest-first
   sort key.
4. **Value signal.** For each provider, establish whether it exposes at least
   one genuinely useful, sufficiently capable model, and how good it is — the
   payoff the user gets for the effort. This also settles whether the
   smart-only `openrouter-nemotron-120b` is worth carrying at all.
5. **Does a long external cooldown need special-casing?** Research confirmed a
   daily (or otherwise long) cap that takes a provider offline for an extended
   period — distinct from short rate-limit spacing — is real for all three
   curated providers. That finding is handled now, not deferred to a v2 — but
   the answer turned out to be "honor the provider's own number as the cooldown
   duration, whatever its scale" rather than adding a new FSM state; see
   Reliability model below for why a distinct state was tried and rejected.

## Design

### Catalog = curated provider knowledge

Two kinds of curated knowledge, split by what they describe:

- **Per-provider onboarding metadata** lives in the `[keys]` table (currently
  flat `REF = "markdown help"`). Promote each entry to a small sub-table carrying
  `effort`, `value`, and `help` — these genuinely belong to the key/account, not
  to any single model.
- **Per-model routing metadata** lives on the `[[llms]]` rows. `rate_limit` is
  one of these: limits are frequently per-model (the same key can give one model
  1,000 RPD and another 14,400 RPD — see the reference doc), so it cannot be
  hung off `api_key_ref`. Add `rate_limit` to each `[[llms]]` row and to
  `LLMConfig`.

```toml
[[llms]]
name        = "groq-llama-3.3-70b"
base_url    = "https://api.groq.com/openai/v1"
model       = "llama-3.3-70b-versatile"
api_key_ref = "GROQ_API_KEY"
rate_limit  = { rpm = 30, rpd = 1000 }

[keys.GROQ_API_KEY]
effort = "signup"
value  = "good"
help   = "Create a free account at [Groq](https://console.groq.com/keys), then New API Key."
```

The `effort` buckets, the `value` scale, and the `rate_limit` windows are defined
in [`../reference/freetier-providers.md`](../reference/freetier-providers.md).
For implementation, the load-bearing facts are: `effort` is an ordered enum whose
order is the onboarding sort key; `value` is an ordinal enum used for display/sort
only (not routing); and **none of the `rate_limit` windows are consumed by the
optimizer at runtime** — `rpm`/`rpd`/`tpm`/`tpd` are all onboarding/display
metadata (`rpm`/`rpd` drive the `env` sort order and its annotations; see Step 6).
The live cooldown is driven entirely by the provider's own `Retry-After` on the
actual response, never by a catalog number — a catalog `rpm` is nominal and
routinely differs from what the provider actually enforces (see Reliability model
below), so seeding runtime behavior from it would just be a second, unreliable
source of truth alongside the real one.

Because `rate_limit` is now part of `LLMConfig`, it flows through
`Registry.load()` to the pool with no extra accessor, available for snapshots and
admin views. The `[keys]` onboarding fields are read by the `env` command (and any
host that wants them); `Registry.key_help()` is generalized to expose the
structured per-provider fields.

### Catalog consolidation

Curate the models actually worth using (one genuinely useful model per provider
rather than padding), guided by the `value` axis. Merge into a single
`presets/freetier.toml` and delete `presets/smart-freetier.toml`.

**Drop `openrouter-nemotron-120b`.** All OpenRouter `:free` models share one
account-wide daily quota, so carrying both Nemotron and `gpt-oss-120b` under the
same `OPENROUTER_API_KEY` adds zero availability (they compete for the same
50/1,000 RPD pool) and there is no quality-aware routing to exploit the
difference — it is pure pool padding. Keep the single better general default,
`gpt-oss-120b`. This was the only thing distinguishing `smart-freetier.toml`, so
it collapses into `freetier.toml` cleanly.

The three curated providers and their `value`: Gemini 2.5 Flash (`high`),
Groq llama-3.3-70b (`good`), OpenRouter gpt-oss-120b (`good`).

### Onboarding via `env`

No new CLI command — onboarding belongs in the existing `env`. Enrich it to:

- order keys by **effort and value together**, so the user can grab the keys
  that are both easy and worth it first (effort bucket ascending, then `value`
  descending). For the current catalog this yields **Gemini → Groq →
  OpenRouter**: Gemini is the easiest bucket (`oauth`) and the highest value;
  Groq and OpenRouter share `signup`, with Groq ahead on its larger daily cap;
- show the effort and value of each key alongside its help;
- skip or annotate keys already present in the environment (handles "I already
  have some keys" for free).

### Persistence (prerequisite)

This feature adds a new per-model config field (`rate_limit`), populated for
onboarding/display use only (see Reliability model above — the optimizer does not
read it). Persisting it without a migration relies on the registry already
carrying nested/open config in a JSON column (see
[`architecture.md`](../reference/architecture.md#columns-vs-json) for the
`columns-vs-JSON` decision), which already defines the `RateLimit` type and the
`LLMConfig.rate_limit` field; this plan populates it. No new `LifecyclePhase` is
added, so the per-LLM state document's shape is unchanged by this plan.

### Reliability model: trust the provider, drop the circuit breaker

**Revises existing behavior, not just new code.** The optimizer currently runs a
four-phase FSM (`AVAILABLE`/`COOLING`/`OFFLINE`/`PROBING`): after
`max_fail_count` consecutive rate-limit failures an LLM is pulled from the queue
entirely, a background task later releases exactly one "probe" slot, and after
`max_probe_cycles` failed probes the LLM is permanently dropped. This plan
removes that machinery rather than adding a fifth phase (`EXHAUSTED`) on top of
it. Two independent problems with the four-phase design motivate this:

1. **It double-counts the same signal under two names.** A daily-capped
   provider (429 with a multi-hour `Retry-After`) and a merely-flaky one (429
   with a short `Retry-After`, repeated) both drove the *same* consecutive-fail
   counter toward the *same* `OFFLINE` transition, so distinguishing them
   required inventing a new phase (`EXHAUSTED`) whose only job was to opt out of
   an escalation path that should not have applied to it in the first place.
   Adding that phase means keeping its derivation in sync by hand across
   `reconcile()`, `InMemoryState.get_state()`, and `apply_shared_cooling` — the
   exact shape of bug that kept resurfacing across revisions of this plan (a
   stale `EXHAUSTED` override that is never cleared silently reclassifies a
   later, ordinary `COOLING` event).
2. **The escalating internal delay and the provider's stated wait must never
   share one variable.** A design that lets a long `Retry-After` feed the same
   counter that backs off on repeated short failures produces nonsense: if a
   provider says "wait a day" and the *next unrelated* rate limit says "wait 60
   seconds", multiplying the day-scale number is absurd, and shrinking a
   day-scale number by a fixed percentage per success takes dozens of
   successful calls to recover. The fix is architectural, not a tuning
   constant: the wait duration for *this* cooldown is always computed from
   *this* response's own number, never carried forward from a past one.

**Cooldown duration.** On a 429/503, read `Retry-After` from the response. If
present, that is authoritative — the provider is the source of truth for its own
quota and reset schedule, so the number is used as-is on the first offense, no
correction applied. If **this same LLM** was already mid-failure-streak
(no success since its last 429/503), scale *this response's own number* by
`backoff_factor ** consecutive_fails`, where `consecutive_fails` is how many
429/503s have landed in a row since the last success (the existing
`rl_fail_count` bookkeeping, read *before* it is incremented for this event, so
the first failure in a streak always gets exponent 0 — the provider's number
trusted verbatim). A success resets `consecutive_fails` to 0. This is why "wait a
day, then later wait 60s" never compounds into "wait 2 days": the two events are
unrelated in the counter's eyes unless a run of *consecutive* failures links
them, and even then the multiplier is applied to whichever number the provider
sent in *this* response, not to the earlier one.

If `Retry-After` is **absent**, fall back to a flat default (still
`_DEFAULT_RATE_LIMIT_SEC = 60`, unchanged) before applying the same
`backoff_factor ** consecutive_fails` scaling. This is not a hypothetical: Groq,
OpenRouter, and Gemini all document the header as conditionally present
("if provided, honor it; otherwise back off"), so the no-header path is a real,
expected case for all three curated providers, not an edge case to special-case
away. The fallback is a flat constant rather than a per-LLM `rate_limit.rpm`-derived
value on purpose: `rpm` describes *sustained pacing* (how far apart to space
calls to stay under budget), not *how long a violated window takes to reset* —
those are different quantities, and an RPM window resets on an approximately
one-minute boundary regardless of the specific `rpm` figure, so a flat ~60s
fallback is the principled choice, not an arbitrary one, and does not need a
per-provider number at all.

**No new `LifecyclePhase`, no probe loop.** `AVAILABLE`/`COOLING` remain the only
two phases; `OFFLINE` and `PROBING` are removed, not extended. After a cooldown
ends, the slot simply re-enters the normal queue rotation — the very next request
routed to it *is* the health check, with no separate synthetic probe call, no
fixed `offline_sleep` wait, and no `max_probe_cycles` retirement countdown. A
provider that keeps failing throttles itself down to `max_delay`-scale spacing
purely through the backoff formula above; nothing needs to remove its slot from
rotation to achieve that. This removes, in full: `LifecyclePhase.OFFLINE`,
`.PROBING`; the `InMemoryState._phase_override` mechanism and `_TRUST_STORED_PHASES`
(both existed only to serve OFFLINE/PROBING); `models.reconcile()`'s trust-vs-derive
branching (collapses to the two-phase `cooldown_until > now ? COOLING : AVAILABLE`
rule it already applies for COOLING); `Optimizer.decrease_factor`, `max_fail_count`,
`offline_sleep`, `max_probe_cycles`, `_probe_cycles`, `on_probing_start`,
`on_probing_success`; and `broker.py`'s `_on_go_offline`/`_probe_loop`. This also
means `apply_shared_cooling` needs **no** widening for a new phase — it already
only ever needs to recognize `COOLING`, which is unchanged.

**Fully automatic retirement — no human review step.** llmbroker pools half a
dozen free, unattended LLMs; nobody is going to read an alert about "yet another
free model acting up" and decide whether to keep it (see partial-key framing's
"no background magic" for the flip side of this same stance: things that need a
decision get logged and left alone, but *whether a given LLM is worth calling at
all* is not that kind of decision — it is exactly the kind of thing this package
exists to handle so the user does not have to). A dead-on-arrival LLM must be
pruned by the optimizer itself, the same way HTTP 401/403 already is.

The signal is the existing rolling-window `usable_rate` (fraction of recent
calls that returned `OK`) — but retirement needs its **own**, stricter
threshold, `removal_rate_floor`, distinct from the existing `usable_rate_floor`
that only *deprioritizes* a candidate in routing. Reusing `usable_rate_floor`
for outright removal would delete a model the instant it dips below the
routing floor, with no margin — exactly the still-marginally-useful-as-last-resort
case the soft floor already exists to keep around. `removal_rate_floor` sits
below it. Both share the existing `min_sample_count` gating: **no judgment
without evidence** — a new or rarely-used LLM is never retired on too few
samples.

A cumulative "time spent cooling" signal was considered and rejected: it would
punish a well-behaved daily-capped provider (e.g. Gemini, legitimately
unreachable for hours once its quota resets) exactly as hard as a genuinely
broken one, since an *honored* long wait produces no failed attempts at all
(nothing is attempted while a slot is cooling) — `usable_rate` alone already
does the right thing, because only a provider that keeps failing when actually
tried drags it down.

`should_retire(name, operation)` is evaluated after every non-`OK`, non-dead-key
outcome (see "ERROR fails over" below — this now includes generic errors, not
only rate limits) and, when it trips, calls `pool.drop(name)` directly — the
alert fired alongside it is a log entry for whoever's watching, not a request
for a decision. Unlike the durable, ratings-based `benched` verdict
`optimizer-learned-profile.md` adds on top of this plan, this is transport-level
and ephemeral (resets on restart, exactly like the rest of the optimizer's live
bookkeeping) — a persistently-broken LLM gets re-tried fresh at the next restart
and re-pruned within a handful of calls if it is still broken, which is a
bounded, cheap cost, not a reason to make this durable itself.

**ERROR fails over instead of failing the caller's request.** Today a non-rate-limit
failure (a malformed request, a 404 on a mistyped model name, a network error, an
auth failure) raises `AllLLMsFailedError` straight to the caller of *that one
request*, even when other LLMs in the pool are healthy — defeating the pool's own
purpose of hiding one provider's problem behind the others. This is not new
scope tacked onto the reliability rewrite; it is the same "the human should not
have to know or care which specific free LLM is having a bad day" principle
applied to the request path, not just to pool membership. Generic HTTP errors,
network errors, and 401/403 all now cool the slot down (same
`backoff_factor ** consecutive_fails` formula as the rate-limit path, base
`_DEFAULT_RATE_LIMIT_SEC` since there is no `Retry-After` to read) and return
`None` so `chat()`'s loop tries the next available LLM, instead of raising. The
one exception is unchanged: `wait == 0` still fails fast rather than looping
(mirrors the existing rate-limit branch). With this change, `_attempt` no longer
raises `AllLLMsFailedError` for *any* single-LLM failure — that exception is now
raised from exactly one place, the eager keyless/zero-keyed guard in `chat()`
(see partial-key framing), which sharpens its meaning to precisely what the
Problem statement promises: reserved for genuine "zero usable models," never for
"this one LLM had a bad response."

### Partial-key framing (normal mode)

Make unresolved keys read as expected, not as failure:

- soften the per-key `catalog` warning from "calls will fail" to an
  informational note that the model is simply inactive until its key appears;
- reserve the alarming path (the `AllLLMsFailedError` / alerts) for the genuine
  problem: **zero usable models**;
- reflect the same framing in `env` output and the README — having only some
  keys is the intended way to run llmbroker.

This needs a small routing change, not messaging alone: today `pool.add`
enqueues a config even when its key did not resolve, and the router then raises
`AllLLMsFailedError` the moment such a slot is acquired (`router.py` keyless
branch). A single missing key therefore reads as a hard failure. The fix is to
**not enqueue keyless configs** (they stay in `configs` for visibility but are
not routable) and to fire the alarm only when **zero keyed configs** exist. There
is no background key re-resolve loop, so a key added after startup takes effect on
the next provision/restart, or immediately if the host calls `update()` for that
config — which matches the `env → .env → restart` flow.

Two correctness traps follow directly from "don't enqueue keyless configs" and
must be closed in the same step, not left as follow-ups:

- **The zero-keyed alarm must fire eagerly, not only when a wait times out.**
  With keyless configs no longer enqueued, a pool with zero keyed configs at all
  has a permanently empty queue. `ask()`/`chat()` default to `wait=None`, which
  blocks on the queue indefinitely rather than raising — so the "genuine zero
  usable models" case would silently hang instead of producing the
  `AllLLMsFailedError` this section promises. The check must run **before**
  attempting to acquire a slot, independent of `wait`.
- **A config must not get stranded keyless forever once its key resolves.**
  `pool.add`'s enqueue decision cannot key off "is this name new to `_configs`"
  alone: a config that started keyless (added once, already present in
  `_configs`) and later gets a key via `catalog.update()` (no full broker
  restart) is not "new" on that second call, so a naive `if is_new and key`
  guard would never enqueue it — it stays unroutable until the process restarts.
  The guard must also enqueue on the keyless→keyed transition, not just on
  first insertion.

## Implementation (hand-off)

The state store already persists a JSON document and the registry already
carries nested config in a JSON column, with `RateLimit`, `LLMConfig.rate_limit`,
and the `LLMState` ⇄ dict boundary in place (see
[`architecture.md`](../reference/architecture.md#columns-vs-json)); the
`rate_limit` metadata below persists with no further migration, and no new
`LifecyclePhase` is introduced by this plan.

Ordered, self-contained steps. Each step lists the files, the change, and the
tests to add. Run `invoke pre` and `python -m pytest` after each step; both must
be green (no skips) before moving on.

### Step 1 — enums and onboarding DTOs

File: `src/llmbroker/models.py`. (`RateLimit`, `LLMConfig.rate_limit`, and the
`LLMState` (de)serialization already exist.)

- Add `class EffortLevel(Enum)` in easiest-first declaration order (reference
  doc): `OAUTH`, `SIGNUP`, `VERIFY`, `CONSOLE`, `WAITLIST`. Sort by
  `list(EffortLevel).index(...)`.
- Add `class ValueLevel(Enum)`: `HIGH`, `GOOD`, `NICHE` (descending desirability).
- Add `@dataclass(frozen=True, slots=True) class KeyInfo` (per-provider onboarding
  only, **no** rate_limit): `api_key_ref: str`, `effort: EffortLevel | None`,
  `value: ValueLevel | None`, `help: str`. Tolerate missing/unknown enum strings
  by storing `None` (never raise on an unrecognized bucket).

Tests (`tests/test_models.py`): enum order; `KeyInfo` partial & unknown fields →
`None`.

### Step 2 — parse the catalog: per-model `rate_limit` + per-provider `key_info`

Files: `src/llmbroker/standalone/registry.py`, `src/llmbroker/protocols/registry.py`.

- `_config_from_entry` (file registry): read `rate_limit` from each `[[llms]]`
  row into `LLMConfig.rate_limit` (build `RateLimit` defensively; absent ⇒
  `None`). DB registries already persist `rate_limit` via `LLMConfig.to_metadata()`/
  `from_metadata()` — the file registry only parses the TOML.
- `[keys]` entries become sub-tables carrying `effort`/`value`/`help`; a bare
  string (old flat form) is read as `help` only. Add
  `key_info_from_entry(ref, raw) -> KeyInfo` (defensive enums) in `registry.py`.
- Generalize the registry key capability: replace `KeyHelpProtocol.key_help()`
  usage with `key_info() -> dict[str, KeyInfo]` on the file `Registry` (keep a
  `key_help()` shim deriving `{ref: info.help}` only if an existing caller needs
  it). This capability is for hosts/onboarding; the broker seeding path reads
  `cfg.rate_limit` directly and does **not** use it.

Tests (`tests/test_registry_keys.py`): `[[llms]]` `rate_limit` reaches
`LLMConfig`; nested `[keys]` parses effort/value/help; flat-string `[keys]` →
`help` only; unknown `effort` → `None`.

### Step 3 — reliability model: cooldown formula, drop OFFLINE/PROBING, automatic retirement, ERROR failover

Files: `src/llmbroker/chat.py`, `src/llmbroker/broker/router.py`,
`src/llmbroker/broker/pool.py`, `src/llmbroker/broker/state.py`,
`src/llmbroker/broker/broker.py`, `src/llmbroker/optimizer.py`,
`src/llmbroker/models.py`, `specs/reference/optimizer.md`.

**This step removes existing behavior, not only adds new code** — see
"Reliability model" in Design above for the reasoning.

- `chat.py`: extend `retry_after_seconds` to also parse an HTTP-date
  `Retry-After` (seconds-from-now, floored at 0); keep the int-seconds path and
  the `default_sec` fallback unchanged.
- `models.py`: `class LifecyclePhase(Enum)` keeps only `AVAILABLE`, `COOLING` —
  remove `OFFLINE`, `PROBING`. Remove `_TRUST_STORED_PHASES` entirely. Collapse
  `reconcile()` to the two-branch rule it already applies for COOLING:
  `cooldown_until is not None and cooldown_until > now → COOLING`, else
  `AVAILABLE` with `cooldown_until = None`. Drop the `ValueError` fallback —
  with only two phases there is no longer an untrusted case it needs to guard.
- `state.py` (`InMemoryState`): remove `_phase_override`,
  `set_phase_override`, `clear_phase_override`, and the OFFLINE/PROBING branch
  in `get_state()`. `get_state()` becomes pure derivation from `_cooldown`/
  `_fail_count`, mirroring the simplified `reconcile()`.
- `pool.py`:
  - `cool_down`: simplify to `async def cool_down(self, config: LLMConfig, delay: float) -> None`
    — drop the `headers` param, the `delay_override` param, and the
    `retry_after_seconds`/`_DEFAULT_RATE_LIMIT_SEC` import. The caller
    (`router.py`, below) now always computes a concrete `delay` itself, so the
    old `delay_override is None` fallback branch is dead code on its one
    production call site (`LLMPool` is internal, not exported from
    `llmbroker/__init__.py`, so there is no external caller to preserve it
    for). Update the direct `pool.cool_down(cfg, headers)` call sites in
    `tests/test_optimizer.py` and `tests/test_pool.py` to compute the delay
    themselves and pass the number.
  - `_reenqueue_config`: drop the `phase not in (OFFLINE, PROBING)` guard —
    with only `AVAILABLE`/`COOLING` left, a cooldown timer firing always
    re-enqueues unconditionally.
  - Remove `set_offline`, `set_probing`, `set_available` — nothing calls them
    once probing is gone; a successful call already calls `clear_cooling`,
    which is all that is needed.
- `broker.py`: remove `_on_go_offline`, `_probe_loop`, and the `on_go_offline=`
  wiring into `OptimizerTelemetry`'s constructor. Update
  `_maybe_alert_underprov`'s alert text from "all LLMs are OFFLINE or COOLING"
  to "all LLMs are COOLING" — the `all()` check itself is unchanged (still
  `phase is not AVAILABLE`, still filtered to keyed configs only per the
  partial-key framing step below).
- `optimizer.py`:
  - Remove `decrease_factor`, `max_fail_count`, `offline_sleep`,
    `max_probe_cycles`, `_probe_cycles`, `increment_probe_cycles`,
    `probe_cycles`, `reset_probe_cycles`, `on_probing_start`,
    `on_probing_success`, `_current_delay`, `delay_for`, `seed_from_metrics`'s
    `max_delay`-priming. The wait duration is no longer carried as persistent
    per-LLM state between events (see the router formula below) — nothing
    needs floor/shrink math or a warm-start prime any more.
  - `on_rate_limited(llm_name)` keeps incrementing `_rl_fail_count` (its only
    remaining job: expose how many consecutive failures have happened since
    the last success, for the router's backoff exponent) but no longer
    triggers any phase transition. `rl_fail_count(llm_name) -> int` stays as
    the read accessor. It is called from exactly one place —
    `OptimizerTelemetry._drive_fsm`, below — never from `router.py` directly,
    so it is never double-incremented for the same call.
  - `on_success(llm_name)` simplifies to `self._rl_fail_count[llm_name] = 0`.
  - Add field `removal_rate_floor: float` (default well below
    `usable_rate_floor`, e.g. `0.15` vs `0.5`) — deliberately a **separate**
    field from the routing-only floor, not a reuse of it. Add
    `should_retire(llm_name, operation) -> bool`: `True` when
    `usable_rate(llm_name, operation)` is not `None` (at least
    `min_sample_count` samples exist) and below `removal_rate_floor`. No
    duration/`cooldown_seconds_total`-based signal — see "Fully automatic
    retirement" in Design above for why that was tried and rejected.
- `OptimizerTelemetry._drive_fsm` (in `optimizer.py`):
  - `RATE_LIMITED`/`UNAVAILABLE` branch: keep the existing
    `self._opt.on_rate_limited(name)` call. Replace the old
    `if rl_fail_count >= max_fail_count: set_offline/_on_go_offline`
    escalation with
    `if self._opt.should_retire(name, call.operation): self._pool.drop(name); self._opt.add_alert(f"{name}: retired — success rate too low over recent calls")`.
  - `ERROR` branch: keep the `401`/`403` immediate-drop sub-branch verbatim —
    unambiguous, stays outside the quality signal entirely. For every other
    `ERROR` (previously a no-op once `PROBING` is removed), call
    `self._opt.on_rate_limited(name)` then the same `should_retire` check —
    a generic error escalates the same consecutive-fail counter and is judged
    by the same removal signal as a rate limit. `usable_rate`'s rolling window
    already counts any non-`OK` call against the LLM regardless of which
    branch produced it; this just wires the same two calls into the branch
    that previously did nothing.
- `router.py` `_attempt`: both failure branches now cool the slot down and
  fail over to the next LLM instead of raising to the caller — see "ERROR
  fails over" in Design above. Shared setup, read **before** `record()` is
  awaited (which is what increments `rl_fail_count` via `_drive_fsm`, so the
  first failure in a streak always sees exponent 0):
  ```python
  fails_before = self._optimizer.rl_fail_count(config.name) if self._optimizer else 0
  backoff = self._optimizer.backoff_factor ** fails_before if self._optimizer else 1.0
  ```
  Rate-limit branch (429/503, unchanged trigger): `base = retry_after_seconds(headers, _DEFAULT_RATE_LIMIT_SEC)`,
  `cap = self._optimizer.max_delay if self._optimizer else base`,
  `wait_time = min(base * backoff, cap)`, `await self._pool.cool_down(config, wait_time)`,
  `await record(RATE_LIMITED or UNAVAILABLE, ...)`.
  Generic-error branch (every other `httpx.HTTPStatusError`, plus
  `httpx.TimeoutException`/`httpx.ConnectError`/`OSError` — this now includes
  401/403, whose drop-and-alert still happens inside `record()` →
  `_drive_fsm`): `base = _DEFAULT_RATE_LIMIT_SEC` (no `Retry-After` to read for
  a non-rate-limit failure), same `cap`/`wait_time` formula, `await self._pool.cool_down(config, wait_time)`,
  `await record(CallStatus.ERROR, http_status=code_or_None, error_detail=detail)`.
  `self._pool.release(config)` is **no longer called** on this branch (unlike
  today) — the slot is cooling now, not immediately re-queueable; `release()`
  stays on the success path only. Both branches end the same way:
  `if wait == 0: raise NoLLMAvailableError(f"{config.name} failed and wait=0") from exc`,
  else `return None`, mirroring the existing rate-limit `wait == 0` handling.
  `_attempt` no longer raises `AllLLMsFailedError` for any single-LLM outcome —
  the existing stale-config guard in `chat()`
  (`if config.name not in self._pool: continue`) already safely handles a
  config that `_drive_fsm` dropped out from under an in-flight `_attempt` call,
  so no new race is introduced.
- No `EXHAUSTED` phase, no long-cooldown classification anywhere: a
  multi-hour provider-stated wait is simply a `COOLING` cooldown with a
  multi-hour `cooldown_until`, handled by the existing `COOLING` code path
  with zero special-casing.
- `apply_shared_cooling`: unchanged — it already only ever needs to recognize
  `COOLING`.
- `specs/reference/optimizer.md`: rewrite to describe this model — replace the
  "Offline / Probing FSM" and "Pool retirement" sections with the
  cooldown-duration formula, the fully-automatic `should_retire` signal, and
  the ERROR-fails-over behavior; keep the auth-failure (401/403) immediate-drop
  paragraph, updated only to note it now also fails over within the same
  request rather than raising.

**Delete, do not leave passing against dead code**: every existing test in
`tests/test_optimizer.py`, `tests/test_optimizer_integration.py`,
`tests/test_pool.py`, `tests/test_state.py`, and `tests/test_router.py` that
asserts OFFLINE/PROBING transitions, probe cycles, `decrease_factor` shrink
behavior, or that a generic/network error raises `AllLLMsFailedError` — that
behavior no longer exists.

Tests: HTTP-date `Retry-After` parsing; the first 429 in a streak uses the
provider's number as-is, no scaling; a second consecutive 429 (no success in
between) scales *that response's own* number by `backoff_factor`, not the
first response's number — regression test for the compounding trap: a 429 with
`Retry-After: 86400` followed by a 429 with `Retry-After: 200` waits
`200 * backoff_factor`, not a number derived from `86400`; a success resets the
streak so a later, unrelated 429 is trusted verbatim again; a 429 with no
`Retry-After` header falls back to `_DEFAULT_RATE_LIMIT_SEC` before scaling; a
pool never transitions to anything but `AVAILABLE`/`COOLING` regardless of how
many consecutive 429s land, including past what used to be `max_fail_count`;
`reconcile()` on a stored `COOLING` row reverts to `AVAILABLE` once
`cooldown_until` has passed (the simplified, now-only case); a generic HTTP
error (e.g. 404) or a network error fails over to the next available LLM
instead of raising `AllLLMsFailedError`, and cools the failed slot down first;
a 401/403 fails over to the next LLM *within the same request* (not just drops
the LLM for future ones); a config whose sole slot gets dropped mid-`_attempt`
does not get re-acquired (stale-config guard); an LLM whose `usable_rate` falls
below `removal_rate_floor` (with `min_sample_count` samples) is dropped
automatically with no external action, whether the failures were rate limits or
generic errors; an LLM between `removal_rate_floor` and `usable_rate_floor` is
**not** dropped (still routable as a deprioritized last resort) — regression
test distinguishing the two thresholds; a well-behaved daily-capped LLM (long,
honored cooldowns but successful calls whenever actually tried) is **not**
flagged for removal, however much cumulative time it spends cooling —
regression test for the rejected duration-based signal; a 401/403 still
triggers immediate `pool.drop()` plus an alert, unchanged.

### Step 4 — onboarding via `env`

File: `src/llmbroker/cli.py` (`_cmd_env`, `_api_key_refs`).

- Read `[keys]` via `key_info_from_entry` (effort/value/help) and, per ref, the
  daily cap (max `rate_limit.rpd` across its `[[llms]]` rows, `None` if none set)
  for display and sorting. CLI parses the TOML directly — no broker needed.
- Sort refs by `(effort index, value index, -daily cap or 0, ref)`. `EffortLevel`
  and `ValueLevel` are both declared best-first (`list(...).index(...)`), so plain
  ascending order already ranks easiest/most-valuable first — do **not** negate
  the value index (that would rank `NICHE` before `HIGH`). A larger daily cap
  sorts earlier within the same effort+value bucket; `ref` is the final
  deterministic tiebreak. Unknown effort/value sort after all known values at
  the same preceding key (treat `None` as one past the last enum index). An
  unknown/absent daily cap sorts after all known caps within the same
  effort+value bucket (treat it as `0`, the lowest priority — an unrated cap
  must not be assumed to be the largest one).
- Emit each ref's help + an annotation line (effort, value, daily cap when known).
  If the ref is already in `os.environ`, annotate it "already set" instead of a
  blank assignment.

Tests (`tests/test_cli_env.py`): ordering Gemini → Groq → OpenRouter for the
shipped preset (Groq ahead of OpenRouter is the daily-cap tiebreak, since both
share `value=good`); two same-effort keys with different `value` sort by value,
not alphabetically (regression test for the sign bug — construct a case where
alphabetical `ref` order would contradict `value` order); a key with unknown
`value` sorts after known ones at the same effort; an env var already in
`os.environ` is annotated present; annotations render.

### Step 5 — catalog consolidation

Files: `presets/freetier.toml`, delete `presets/smart-freetier.toml`.

- Put `rate_limit` on each `[[llms]]` row and `effort`/`value`/`help` in the
  matching `[keys.*]` sub-table, using the reference-doc values. OpenRouter's
  RPD is variable (50 with no purchase, 1,000 after a one-time $10 top-up per
  the reference doc) — encode the **guaranteed-without-payment** figure,
  `rpd = 50`, since the catalog must not promise a cap the user has not
  unlocked yet.
- Keep the three `[[llms]]` rows (Gemini, Groq, OpenRouter gpt-oss-120b); no
  Nemotron row. Delete `smart-freetier.toml` and grep the repo (README, docs,
  tests, CLI preset list) for `smart-freetier`, removing/redirecting references.

Tests: loading `presets/freetier.toml` via the file `Registry` yields 3 configs
each with a `rate_limit`, and 3 `key_info` entries with expected effort/value.

### Step 6 — partial-key framing

Files: `src/llmbroker/broker/pool.py`, `src/llmbroker/broker/catalog.py`,
`src/llmbroker/broker/broker.py`, `src/llmbroker/broker/router.py`, `README`/docs,
`specs/reference/architecture.md`.

- `pool.py` `add`: enqueue **only when the config is keyed and was not already
  keyed** — i.e. on first insertion with a resolved key, or on the
  keyless→keyed transition of an existing entry. Gating on `is_new` alone is
  not sufficient (see the trap above): track the prior keyed state too.

  ```python
  def add(self, cfg: LLMConfig, key: str | None) -> None:
      was_keyed = cfg.name in self._resolved_keys
      is_new = cfg.name not in self._configs
      self._configs[cfg.name] = cfg
      if key is not None:
          self._resolved_keys[cfg.name] = key
      now_keyed = cfg.name in self._resolved_keys
      if now_keyed and (is_new or not was_keyed):
          self._queue.put_nowait(cfg)
  ```

  Keyless configs stay in `_configs` (visible in snapshots) but off the
  routable queue, so the router never acquires a keyless slot. An already-keyed
  config re-`add`ed (e.g. a plain metadata update) does not get a second slot,
  preserving the existing one-slot-per-config invariant. (No re-resolve loop; a
  key added later takes effect at next provision/restart, or immediately via
  `update()`.)
- `catalog.py` `_resolve_key`: change the `logger.warning(... "calls will fail")`
  to `logger.info(... "inactive until its api_key_ref is set; this is normal")`.
- Zero-usable detection: the genuine alarm is **no keyed configs at all**. Fix
  `broker.py` `_maybe_alert_underprov`: its `all_offline` check currently
  iterates `self._pool.configs` unfiltered, but a keyless config is never
  enqueued/acquired/cooled, so its in-memory state stays `AVAILABLE` forever —
  with even one keyless config present, `all_offline` can never become `True`,
  masking the real alarm even when every *keyed* config is COOLING.
  Filter the check to keyed configs only (`self._pool.has_key(name)`), and also
  fire when there are zero keyed configs at all. Treat "zero routable/keyed
  models" as the alarm, not "one model lacks a key". The `router.py` keyless
  branch becomes a defensive guard (should no longer fire in normal routing);
  downgrade its message to expected-state phrasing.
- `router.py` `chat`: add an eager guard **before** the `acquire()` loop —
  if `self._pool.configs` is non-empty but no name in it satisfies
  `self._pool.has_key(name)`, raise `AllLLMsFailedError` immediately
  (`"no LLM has a resolved api_key_ref — set at least one env var or configure
  a secrets backend"`). This must not depend on `wait` or on `acquire()` ever
  raising: with keyless configs no longer enqueued, a pool with zero keyed
  configs leaves the queue permanently empty, and the default `wait=None`
  blocks on it forever — so without this eager check, "zero usable models"
  would silently hang instead of producing the clear failure this section is
  meant to guarantee.
- README: add the framing paragraphs (below).
- `architecture.md`: rewrite the "Key acquisition help" section — it currently
  documents the flat-markdown-only `[keys]` design as deliberate ("no structured
  provider/free/url fields to keep in sync"), which Step 2's `KeyInfo`
  (`effort`/`value`/`help`) supersedes. Describe the structured shape and the
  `key_info()`/`key_help()` capabilities instead.

Tests: a missing key logs `info`, not `warning` (caplog); a pool with some keyed
and some keyless models routes over the keyed ones without raising and never
blocks on a keyless slot; a pool with **only** keyless models raises the
zero-usable alarm **immediately with the default `wait=None`** (regression test
for the eager-guard/hang fix — must not rely on a timeout); a pool with a
keyless config present *and* every keyed config COOLING still raises
the underprovisioned alarm (regression test for the `has_key` filter in
`_maybe_alert_underprov`); `pool.add()` called first with `key=None` and then
again with a resolved key for the same name enqueues exactly one slot
(regression test for the keyless→keyed transition, distinct from the existing
"re-add an already-keyed config enqueues no extra slot" case).

## Non-goals / rejected ideas

- **A telemetry / crowdsourcing pipeline for live provider stats** — our only
  current user (dinary) has too few operations to be representative, and
  crowdsourcing has no clear mechanism. Seed data is curated manually from open
  sources at catalog-build time.
- **Paid-tier presets** — defeats the premise. Anyone willing to pay uses a
  single good model and does not need llmbroker.
- **Single-provider presets** — defeats the premise. The whole point of the pool
  is surviving one provider's rate-limit exhaustion by spilling onto others.
- **Task-specialized or quality-*ranked* presets** — would only matter with
  intelligent per-request routing, which does not exist; the pool simply
  rotates. Note this is distinct from the `value` field above: `value` is
  onboarding guidance for the human ("is this key worth the effort"), not a
  routing signal.
- **A new onboarding/`keys`/`setup`/`status` CLI command** — overkill on top of
  the existing `preset` and `env`. Onboarding belongs inside `env`.
- **A background key re-resolve loop** — out of scope. Keys are resolved at
  provision; a key added later takes effect on the next provision/restart, which
  fits the `env → .env → restart` flow. (Note: keyless models *are* excluded from
  routing — see the partial-key step — but via not-enqueuing at load, not a
  periodic re-check.)
- **A distinct `EXHAUSTED` phase (and, with it, keeping `OFFLINE`/`PROBING`) for
  long provider-stated cooldowns** — rejected after several revisions kept
  reintroducing the same class of bug (a phase whose derivation duplicates
  `COOLING`'s, requiring hand-synced trust rules across `reconcile()`,
  `InMemoryState`, and `apply_shared_cooling`). A long `Retry-After` is just a
  `COOLING` cooldown with a long duration; see Reliability model above for the
  replacement.
- **Seeding the reactive backoff fallback from `rate_limit.rpm`** — an earlier
  revision of this plan seeded the no-`Retry-After` fallback delay from
  `60 / rpm` per LLM. Rejected: `rpm` describes sustainable steady-state call
  spacing, not how long a *violated* window takes to reset, which is a
  different quantity — an RPM window resets on an ~one-minute boundary
  regardless of the specific `rpm` figure, so the flat existing
  `_DEFAULT_RATE_LIMIT_SEC` (60s) is the principled fallback, not a per-provider
  number. Nothing in the current design consumes `rate_limit` at runtime; see
  the catalog knowledge section above.
- **A persistent per-LLM `_current_delay` that the backoff multiplier and the
  provider's stated wait both feed into** — rejected because it lets an
  unrelated later event compound on an earlier one (e.g. a day-long quota wait
  followed by an ordinary 60s rate limit must not become "wait two days"). The
  replacement always derives the wait from *this* response's own number scaled
  by *this streak's* consecutive-fail count, never from a carried-forward
  value.

## README framing (to add)

> Many free LLMs are unreliable and mediocre on their own. llmbroker pools them
> and routes across the pool, turning quantity into dependable, good-enough
> quality — without paying for a premium model.

Plus: running with only some of the keys is the normal, intended mode — add the
keys that are easy and worth it, and the pool assembles itself from whatever is
present.
