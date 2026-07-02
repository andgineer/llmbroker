# Catalog refresh — a repeatable prompt for updating the curated preset

## Plan sequence — step 2 of 2

> **Prerequisites:** the `effort`/`value`/`rate_limit` taxonomies and the "one
> useful model per provider" curation rules are already fixed and implemented
> (`EffortLevel`/`ValueLevel` in `models.py`; see
> [`freetier-providers.md`](../reference/freetier-providers.md)) — this
> runbook *consumes* them. It also references `SeedPolicy.SYNC` from
> `optimizer-learned-profile.md` (step 1) in "Interaction with the learned
> profile" — no code dependency, but that section is only real once step 1
> lands. **May run in parallel with step 1. Blocks:** nothing.

The remaining two plans form one dependency chain; execute in this order (the
storage-shape foundation — columns-vs-JSON, `RateLimit`, `LLMConfig.rate_limit`,
the `LLMState` ⇄ dict boundary, the version-gated `ensure_schema` toolkit — and
the curated catalog knowledge, effort/value onboarding, the simplified
two-phase AVAILABLE/COOLING reliability model, and the keyless-not-routable
pool change are already implemented; see
[`architecture.md`](../reference/architecture.md) and
[`optimizer.md`](../reference/optimizer.md)):

1. **`optimizer-learned-profile.md`** — the durable learned half (learned profile
   carried in the registry, bench verdict) and `SeedPolicy.SYNC`; extends the
   existing routable predicate.
2. **`catalog-refresh.md`** *(this plan)* — the manual re-curation runbook;
   consumes the already-fixed taxonomies and may run in parallel with (1).

## Problem statement

The curated catalog (`presets/freetier.toml` + the effort/value/rate_limit
metadata) is the product: maintainers curate the models and their onboarding
knowledge, and users just supply keys. But free-tier LLM offerings drift
constantly — providers change rate limits, retire models, ship better ones,
alter how a key is obtained. The catalog must be refreshed periodically, and
right now there is no repeatable procedure: the original curation was a one-off
"research phase" recorded in
[`../reference/freetier-providers.md`](../reference/freetier-providers.md).

llmbroker deliberately does **not** run a telemetry or crowdsourcing pipeline
(see [`freetier-providers.md`](../reference/freetier-providers.md)) — curation
is done **manually from open sources**. This plan makes that manual curation
repeatable by storing it as a **prompt/runbook** an LLM agent (or a human) can
execute on demand to regenerate the catalog from current sources.

## Deliverable

A stored prompt at `specs/reference/catalog-refresh-prompt.md` plus a short
`invoke` entry point that opens/prints it. Running the prompt is a manual,
periodic maintenance action — not part of the runtime.

## What the prompt instructs

The prompt is self-contained (an agent can run it with only repo access + web
access). It directs the agent to:

1. **Consult the sources**, starting from the ones already captured in
   [`../reference/freetier-providers.md`](../reference/freetier-providers.md) and
   refreshing them:
   - the OpenRouter models/rankings pages (free `:free` models, shared account
     quota, current quality ranking) — the "openrouter benchmark" reference;
   - each provider's own free-tier docs / rate-limit / status pages
     (Groq, Google AI Studio / Gemini, OpenRouter, and any candidate providers);
   - community projects that track free-tier behaviour and reliability.
2. **Re-derive the three curated axes** for each candidate provider/model, using
   the taxonomies fixed in the reference doc (do not invent new buckets without
   updating the reference doc):
   - **`rate_limit`** — the real limit windows (`rpm`/`rpd`/`tpm`/`tpd`), which
     are frequently per-model, not per-key;
   - **`effort`** — how hard the key is to obtain (the ordered `EffortLevel`
     enum: `oauth < signup < verify < console < waitlist`);
   - **`value`** — whether the provider exposes at least one genuinely useful
     model and how good it is (`ValueLevel`: `high/good/niche`), judged from the
     benchmarks/rankings.
3. **Apply the curation rules** from the onboarding plan:
   - one genuinely useful model per provider (curate, do not pad);
   - drop models that add no availability (e.g. a second OpenRouter `:free`
     model sharing the same account-wide daily quota — no quality-aware routing
     exists to exploit the difference);
   - keep the pool multi-provider (single-provider or paid-tier presets defeat
     the premise).
4. **Regenerate the outputs**:
   - rewrite `presets/freetier.toml` — `[[llms]]` rows with `rate_limit`, and
     `[keys.*]` sub-tables with `effort`/`value`/`help`;
   - update `specs/reference/freetier-providers.md` with the refreshed sources,
     exact numbers, and the date of the refresh.
5. **Validate** before proposing the change:
   - load `presets/freetier.toml` via the file `Registry` and assert every
     `[[llms]]` row parses into an `LLMConfig` with a `rate_limit`, and every
     `[keys.*]` into a `KeyInfo` with a recognised `effort`/`value`;
   - `invoke pre` and `python -m pytest` green;
   - present a diff summary (added / removed / changed models and why) for human
     review — the human decides whether to merge. The agent never auto-commits a
     catalog change.

## Guardrails baked into the prompt

- **Facts only from sources.** Every `rate_limit`/`effort`/`value` must trace to
  a cited source captured in the reference doc; no guessed numbers.
- **Curation, not accumulation.** Adding a model requires a value justification;
  the goal is a small, dependable pool, not a long list.
- **Human-in-the-loop.** The output is a reviewed PR/diff, never an unattended
  commit — matching "no telemetry pipeline, curated manually".
- **Taxonomies are fixed elsewhere.** Changing the `effort`/`value`/`rate_limit`
  shapes means updating the reference doc and the `EffortLevel`/`ValueLevel`
  enums in `models.py`, not ad-hoc drift inside the preset.

## Interaction with the learned profile

Catalog refresh only touches the **static half** of the catalog (the preset).
Under [`optimizer-learned-profile.md`](optimizer-learned-profile.md), applying a
refreshed preset to a running deployment goes through `SeedPolicy.SYNC`, which
upserts the static config and **never** touches the learned profile — so a
refresh delivers new/updated models and metadata to users **without** discarding
their per-model usefulness stats or resurrecting a model they benched as
globally useless.

## Implementation

Ordered steps. Run `invoke pre` and `python -m pytest` after each.

### Step 1 — write the prompt

File: `specs/reference/catalog-refresh-prompt.md` (new).

- Author the prompt with the sections above (sources, three axes, curation
  rules, outputs, validation, guardrails). Keep it standalone and English-only.

### Step 2 — entry point

File: `tasks.py` (invoke).

- Add `invoke catalog-refresh` that prints/opens
  `specs/reference/catalog-refresh-prompt.md` so the maintainer can hand it to an
  agent. No runtime code, no dependency on the broker.

Tests: a lightweight test asserting the prompt file exists and that the invoke
task resolves the path (no network, no LLM call — the prompt is executed
manually, not in CI).

### Step 3 — cross-link docs

Files: `README`/docs, `specs/reference/freetier-providers.md`.

- Note that the catalog is refreshed via the stored prompt, and that
  `freetier-providers.md` is the source-of-record the prompt reads and updates.

## Non-goals

- **Automated/scheduled catalog updates.** Refresh is a manual, human-reviewed
  action; there is no cron, no runtime fetch, no auto-commit.
- **A live provider-stats feed.** Curation is manual from open sources at
  refresh time (see [`freetier-providers.md`](../reference/freetier-providers.md)).
- **Redefining the taxonomies here.** `effort`/`value`/`rate_limit` shapes are
  owned by the `EffortLevel`/`ValueLevel` enums in `models.py` + the reference
  doc; this prompt *consumes* them.
