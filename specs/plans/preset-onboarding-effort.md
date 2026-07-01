# Curated catalog & key-effort onboarding

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
5. **Does the FSM need a long external cooldown?** If research confirms a daily
   (or otherwise long) cap that takes a provider offline for an extended period —
   distinct from short rate-limit spacing — the `Optimizer`/FSM is extended now
   to model that long-cooldown state. There is no v1/v2 deferral: llmbroker is
   designed as a useful tool up front, and confirmed needs are built now.

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
only (not routing); and of the `rate_limit` windows **only `rpm` is consumed by
the optimizer** (it seeds the warm-start delay). `rpd`/`tpm`/`tpd` are
display/onboarding metadata — the long cooldown is driven by the provider's own
`Retry-After` signal at runtime, not by the catalog `rpd` (see Optimizer
warm-start below).

Because `rate_limit` is now part of `LLMConfig`, it flows through
`Registry.load()` to the pool and optimizer with no extra accessor. The `[keys]`
onboarding fields are read by the `env` command (and any host that wants them);
`Registry.key_help()` is generalized to expose the structured per-provider
fields.

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

### State & optimizer persistence as a JSON document

This work adds per-LLM runtime fields (the `EXHAUSTED` phase, and room for the
optimizer's own hints — seeded delay, `rl_fail_count`, probe cycles). Today the
SQL state stores keep these as **typed columns** (`phase`, `cooldown_until`,
`fail_count`), so every new field is a schema change across four backends. That
cost buys nothing here: unlike telemetry, we never query or aggregate over state
— we only read/write the whole per-LLM blob by `(llm_name, user_id)`.

So store per-LLM runtime state (and any persisted optimizer hints) as a **single
JSON document** per `(llm_name, user_id)`:

- The one typed boundary is `LLMState` ↔ `dict` (a `to_dict`/`from_dict` pair).
- SQL backends replace the typed state columns with one JSON/TEXT `state` column,
  keyed by `(llm_name, user_id)` (keep that unique index). Redis already
  JSON-serializes per name; Mongo already stores a document — both just carry the
  full dict.
- Adding a field is then a dict key, never a migration.

State is **ephemeral** — it is a live cache of cooldown/health that the system
rebuilds from traffic. This change may drop existing rows on upgrade; that is
acceptable and needs no data migration. (Telemetry, which *is* queried, keeps its
columnar schema and is untouched.)

### Optimizer warm-start

At load, seed each LLM's starting `delay` from its own `rate_limit.rpm`
(spacing ≈ 60 / rpm seconds) instead of the flat `initial_delay = 60`. Because
`rate_limit` is now a field on `LLMConfig`, the value arrives with the config
via `Registry.load()`; the broker reads `cfg.rate_limit.rpm` directly at
provision and calls `optimizer.seed_delay(cfg.name, 60/rpm)`. No side accessor
or per-`api_key_ref` lookup is involved. This is the single highest-value seed:
it stops the cold start from either throttling a fast provider or hammering a
slow one during warm-up. Latency and success-rate are cheap to learn live and are
**not** seeded.

The seed must be **sticky**: `Optimizer.on_success` currently floors the
shrinking delay at the global `initial_delay` (60 s), which would erase a fast
provider's seed on the first success. The floor becomes per-LLM (the seeded
value, falling back to `initial_delay` when unseeded).

**Long-cooldown FSM — build it now.** A daily-capped provider returns 429 for the
*remainder of the day* — a multi-hour outage distinct from the seconds-scale
short-spacing backoff. The entry signal is the **provider's own `Retry-After`/
reset header**: it is authoritative (the provider is the source of truth for when
its quota resets) and already on the response. An internal per-provider request
counter is rejected on **accuracy**, not storage cost — it drifts because the
account is shared across keys/clients and because failed attempts also count, so
it can only ever estimate what the header states exactly. The router currently
discards `Retry-After` when the optimizer is active (it overrides the cooldown
with the short backoff); that is the gap this closes.

Behavior:

- on a 429/503, use `max(provider Retry-After, optimizer short delay)` as the
  cooldown, so a provider asking for a long wait is honored;
- when that cooldown exceeds a `long_cooldown_threshold`, the LLM enters
  **EXHAUSTED** — a first-class `LifecyclePhase`, persisted in the per-LLM state
  document alongside `cooldown_until` (see the state-storage step; adding this
  phase costs nothing);
- an EXHAUSTED LLM does **not** escalate toward OFFLINE (its `rl_fail_count` is
  reset), so a daily-capped provider is not flapped through the probe loop; the
  slot simply re-enqueues when `cooldown_until` passes;
- if a provider sends no usable `Retry-After`, behavior degrades gracefully to
  today's OFFLINE→probe cycling (a 429 during probing is `RATE_LIMITED`, not
  `ERROR`, so it never retires the LLM).

This keeps `AllLLMsFailedError`/alerts reserved for genuine "zero usable models".

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
the next provision/restart — which matches the `env → .env → restart` flow.

## Implementation (hand-off)

Ordered, self-contained steps. Each step lists the files, the change, and the
tests to add. Run `invoke pre` and `python -m pytest` after each step; both must
be green (no skips) before moving on.

### Step 1 — DTOs, enums, and config/state fields

File: `src/llmbroker/models.py`.

- Add `class EffortLevel(Enum)` in easiest-first declaration order (reference
  doc): `OAUTH`, `SIGNUP`, `VERIFY`, `CONSOLE`, `WAITLIST`. Sort by
  `list(EffortLevel).index(...)`.
- Add `class ValueLevel(Enum)`: `HIGH`, `GOOD`, `NICHE` (descending desirability).
- Add `@dataclass(frozen=True, slots=True) class RateLimit`:
  `rpm: int | None = None`, `rpd: int | None = None`, `tpm: int | None = None`,
  `tpd: int | None = None`.
- **`rate_limit` is per-model:** add `rate_limit: RateLimit | None = None` to
  `LLMConfig` (limits differ by model even under one key — see reference doc).
- Add `@dataclass(frozen=True, slots=True) class KeyInfo` (per-provider onboarding
  only, **no** rate_limit): `api_key_ref: str`, `effort: EffortLevel | None`,
  `value: ValueLevel | None`, `help: str`. Tolerate missing/unknown enum strings
  by storing `None` (never raise on an unrecognized bucket).
- Add `LifecyclePhase.EXHAUSTED = "exhausted"`.
- Add `LLMState.to_dict()` / `LLMState.from_dict()` (the single typed⇄JSON
  boundary for state persistence — Step 2). Serialize `phase.value`,
  `cooldown_until` (ISO or null), `fail_count`, and leave the dict open for
  future keys.

Tests (`tests/test_models.py`): enum order; `KeyInfo`/`RateLimit` partial &
unknown fields → `None`; `LLMState` round-trips through `to_dict`/`from_dict`
including `EXHAUSTED` and tz-aware `cooldown_until`.

### Step 2 — state persistence as a JSON document

Files: `src/llmbroker/protocols/state_store.py`, the four state stores
(`sqlite`, `postgres`, `mongodb`, `redis`), and `sqlite`/`postgres` schema.

- Keep the protocol shape (`read() -> dict[str, LLMState]`,
  `write(name, state)`), but persist each `(llm_name, user_id)` as **one JSON
  document** built from `LLMState.to_dict()`.
- SQL backends: replace the typed `phase`/`cooldown_until`/`fail_count` columns
  with a single `state` TEXT (sqlite) / `JSONB` (postgres) column; keep
  `llm_name`, `user_id`, and the unique index on `(llm_name, COALESCE(user_id))`.
  On read, `LLMState.from_dict(json.loads(state))`; apply the same "expired
  cooldown ⇒ AVAILABLE / trust OFFLINE·PROBING·EXHAUSTED" reconciliation that
  lives in the stores today, but off the parsed dict.
- Redis already stores a JSON string per name in a hash, and Mongo stores a
  document — switch both to the full `to_dict()` payload.
- Bump the sqlite `_SCHEMA_VERSION`. State is **ephemeral** (a live cache rebuilt
  from traffic), so this may drop existing state rows on upgrade — acceptable, no
  data migration. Telemetry schema is untouched.

Tests: each backend round-trips an `LLMState` (incl. `EXHAUSTED` + future extra
key) through `write`/`read`; expired `cooldown_until` reads back `AVAILABLE`;
testcontainers cover postgres/mongo, `fakeredis` covers redis (no skips).

### Step 3 — parse the catalog: per-model `rate_limit` + per-provider `key_info`

Files: `src/llmbroker/standalone/registry.py`, `src/llmbroker/protocols/registry.py`,
the DB registries (`sqlite`, `postgres`, `mongodb`) + their schema.

- `_config_from_entry`: read `rate_limit` from each `[[llms]]` row into
  `LLMConfig.rate_limit` (build `RateLimit` defensively; absent ⇒ `None`).
- DB registries must persist `rate_limit`. Store it as a JSON column on the
  registry row (sqlite TEXT / postgres JSONB / mongo field) rather than four
  scalar columns — same rationale as state, and it round-trips `RateLimit`
  wholesale. Bump the sqlite schema version (additive column).
- `[keys]` entries become sub-tables carrying `effort`/`value`/`help`; a bare
  string (old flat form) is read as `help` only. Add
  `key_info_from_entry(ref, raw) -> KeyInfo` (defensive enums) in `registry.py`.
- Generalize the registry key capability: replace `KeyHelpProtocol.key_help()`
  usage with `key_info() -> dict[str, KeyInfo]` on the file `Registry` (keep a
  `key_help()` shim deriving `{ref: info.help}` only if an existing caller needs
  it). This capability is for hosts/onboarding; the broker seeding path does
  **not** use it (it reads `cfg.rate_limit` directly).

Tests (`tests/test_registry_keys.py`): `[[llms]]` `rate_limit` reaches
`LLMConfig`; DB registry round-trips `rate_limit`; nested `[keys]` parses
effort/value/help; flat-string `[keys]` → `help` only; unknown `effort` → `None`.

### Step 4 — optimizer: sticky per-LLM seed

File: `src/llmbroker/optimizer.py`.

- Add `_base_delay: dict[str, float]` (init=False) and
  `seed_delay(self, llm_name, delay)` setting both `_base_delay[name]` and
  `_current_delay[name]`.
- Add `base_delay(self, llm_name) -> float` → `_base_delay.get(name, initial_delay)`.
- Floor `on_success` / `on_probing_success` (and `delay_for`'s fallback) at
  `base_delay(name)` instead of the global `initial_delay`.
- Add field `long_cooldown_threshold: float = 600.0`.

Tests (`tests/test_optimizer.py`): after `seed_delay(n, 2.0)`, repeated
`on_success` floors at 2.0; unseeded LLM still floors at `initial_delay`.

### Step 5 — seed the delay at provision

File: `src/llmbroker/broker/broker.py` (`ensure_pool`, after
`self._catalog.provision()` and **before** `seed_from_metrics`).

- For each `cfg in self._pool.configs.values()`: if
  `self._optimizer and cfg.rate_limit and cfg.rate_limit.rpm:`
  `self._optimizer.seed_delay(cfg.name, 60.0 / cfg.rate_limit.rpm)`.
- Order matters: seed from `rate_limit` first, then `seed_from_metrics` so a
  last-known-unhealthy LLM still starts at `max_delay`.

Tests (`tests/test_warm_start.py`): a broker on a preset with `rpm=30` seeds
`delay_for("groq-...") == 2.0`; a config without `rate_limit` stays at
`initial_delay`.

### Step 6 — long-cooldown: router, `EXHAUSTED`, FSM

Files: `src/llmbroker/chat.py`, `src/llmbroker/broker/router.py`,
`src/llmbroker/broker/state.py`, `src/llmbroker/broker/pool.py`,
`src/llmbroker/optimizer.py`.

- `chat.py`: extend `retry_after_seconds` to also parse an HTTP-date `Retry-After`
  (seconds-from-now, floored at 0); keep the int path and default.
- `router.py` `_attempt`, rate-limit branch: `provider_retry = retry_after_seconds(headers, _DEFAULT_RATE_LIMIT_SEC)`,
  `opt_delay = optimizer.delay_for(name)`, `delay = max(provider_retry, opt_delay)`.
  If `optimizer and delay >= optimizer.long_cooldown_threshold`, cool down as
  **exhausted**; else the normal cooldown.
- `pool.py` / `state.py`: add `set_exhausted(name, cooldown_until)` — stores
  `phase=EXHAUSTED` **and** `cooldown_until` in the JSON state doc and schedules
  the same re-enqueue at `cooldown_until` as `cool_down`. `get_state` returns
  `EXHAUSTED` while `cooldown_until > now` when the stored phase is `EXHAUSTED`
  (falling back to `AVAILABLE` once it passes), analogous to how OFFLINE/PROBING
  are trusted. (First-class stored phase — cheap now that state is a JSON doc.)
- `optimizer.py` `_drive_fsm`, `RATE_LIMITED`/`UNAVAILABLE` branch: if the pool
  phase for `name` is `EXHAUSTED`, reset `rl_fail_count` and do **not**
  `set_offline`/`_on_go_offline`; otherwise keep today's escalation.

Tests: HTTP-date parsing; a 429 with a 4 h `Retry-After` → `EXHAUSTED`, no OFFLINE
transition, slot re-enqueues after the window (small threshold + monkeypatched
clock); a 429 with 30 s `Retry-After` → `COOLING` with the existing escalation
unchanged; `EXHAUSTED` persists and reloads via the state store.

### Step 7 — onboarding via `env`

File: `src/llmbroker/cli.py` (`_cmd_env`, `_api_key_refs`).

- Read `[keys]` via `key_info_from_entry` (effort/value/help) and, per ref, the
  daily cap from its `[[llms]]` `rate_limit.rpd` for display. CLI parses the TOML
  directly — no broker needed.
- Sort refs by `(effort order, -value order, ref)`; unknown effort/value sort last.
- Emit each ref's help + an annotation line (effort, value, daily cap when known).
  If the ref is already in `os.environ`, annotate it "already set" instead of a
  blank assignment.

Tests (`tests/test_cli_env.py`): ordering Gemini → Groq → OpenRouter for the
shipped preset; an env var already in `os.environ` is annotated present;
annotations render.

### Step 8 — catalog consolidation

Files: `presets/freetier.toml`, delete `presets/smart-freetier.toml`.

- Put `rate_limit` on each `[[llms]]` row and `effort`/`value`/`help` in the
  matching `[keys.*]` sub-table, using the reference-doc values.
- Keep the three `[[llms]]` rows (Gemini, Groq, OpenRouter gpt-oss-120b); no
  Nemotron row. Delete `smart-freetier.toml` and grep the repo (README, docs,
  tests, CLI preset list) for `smart-freetier`, removing/redirecting references.

Tests: loading `presets/freetier.toml` via the file `Registry` yields 3 configs
each with a `rate_limit`, and 3 `key_info` entries with expected effort/value.

### Step 9 — partial-key framing

Files: `src/llmbroker/broker/pool.py`, `src/llmbroker/broker/catalog.py`,
`src/llmbroker/broker/broker.py`, `src/llmbroker/broker/router.py`, `README`/docs.

- `pool.py` `add`: enqueue a new config **only when it has a resolved key**.
  Keyless configs stay in `_configs` (visible in snapshots) but off the routable
  queue, so the router never acquires a keyless slot. (No re-resolve loop; a key
  added later takes effect at next provision/restart.)
- `catalog.py` `_resolve_key`: change the `logger.warning(... "calls will fail")`
  to `logger.info(... "inactive until its api_key_ref is set; this is normal")`.
- Zero-usable detection: the genuine alarm is **no keyed configs at all**. Make
  `broker.py` `_maybe_alert_underprov` (and the `NoLLMAvailableError` path) treat
  "zero routable/keyed models" as the alarm, not "one model lacks a key". The
  `router.py` keyless branch becomes a defensive guard (should no longer fire in
  normal routing); downgrade its message to expected-state phrasing.
- README: add the framing paragraphs (below).

Tests: a missing key logs `info`, not `warning` (caplog); a pool with some keyed
and some keyless models routes over the keyed ones without raising and never
blocks on a keyless slot; a pool with **only** keyless models raises the
zero-usable alarm.

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

## README framing (to add)

> Many free LLMs are unreliable and mediocre on their own. llmbroker pools them
> and routes across the pool, turning quantity into dependable, good-enough
> quality — without paying for a premium model.

Plus: running with only some of the keys is the normal, intended mode — add the
keys that are easy and worth it, and the pool assembles itself from whatever is
present.
