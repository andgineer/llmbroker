# Freetier preset refresh prompt

Standalone runbook. Hand this whole file to an LLM agent with repo access and
web access, or follow it by hand. It regenerates `presets/freetier.toml` —
llmbroker's curated free-tier LLM pool — from current public sources.

Background reading before you start (do not skip):

- [`../specs/reference/freetier-providers.md`](../specs/reference/freetier-providers.md) —
  the rate-limit dimensions, the `effort`/`value` taxonomies, the currently
  curated providers, and the sources list. This is the document you refresh.
- [`../specs/reference/architecture.md`](../specs/reference/architecture.md) and
  [`../specs/reference/optimizer.md`](../specs/reference/optimizer.md) — the
  two-halves catalog and the sync removal rule, which govern how a refreshed
  preset lands on a running deployment. You do not implement this behavior;
  you must not violate its invariants (see Guardrails below).
- `src/llmbroker/models.py` — the `EffortLevel` and `ValueLevel` enums. These
  are fixed; do not invent new buckets.

## 1. Consult the sources

Start from the sources already listed in `freetier-providers.md` and refresh
each one:

- the OpenRouter models/rankings pages — free `:free` models, the
  account-wide shared quota, current quality ranking;
- each currently curated provider's own free-tier docs / rate-limit / status
  page (Groq, Google AI Studio / Gemini, OpenRouter);
- each candidate provider's docs (Cerebras, Mistral La Plateforme, and any
  new candidate you find worth evaluating);
- community trackers that watch free-tier behavior and reliability
  (`cheahjs/free-llm-api-resources` and the other repos listed in the
  Sources section).

Record what you actually read — every number you write down must trace back
to one of these.

## 2. Re-derive the three curated axes

For each candidate provider/model, using the taxonomies fixed in
`freetier-providers.md`:

- **`rate_limit`** — the real limit windows (`rpm`/`rpd`/`tpm`/`tpd`).
  Check whether the limit is per-model or per-account/provider (both shapes
  exist — see the "Rate-limit dimensions" section).
- **`effort`** — how hard the key is to obtain: `oauth < signup < verify <
  console < waitlist`.
- **`value`** — whether the provider exposes at least one genuinely useful
  model and how good it is: `high` / `good` / `niche`, judged from current
  benchmarks/rankings, not from the number of models offered.
- **`weight`** — mandatory on every `[[llms]]` row: the curated prior on the
  quality rating the entry is expected to earn, `0..1` on the same scale as a
  host rating. The router ranks on it until real ratings outweigh it, so a row
  left without one sinks to the bottom of every pool that adopts the refresh.
  Row order carries no priority; the weight is the only thing that does.

  | weight | meaning |
  |---|---|
  | 0.8–1.0 | frontier-class |
  | 0.6–0.8 | strong general-purpose |
  | 0.4–0.6 | usable, clearly behind the leaders |
  | < 0.4 | niche or weak |

  Informed by benchmarks, not equal to any of them — the number predicts how a
  host will rate an ordinary answer. Justify each one from the same sources as
  the axes above.

## 3. Apply the curation rules

- One genuinely useful model per provider — curate, do not pad.
- Drop a model that adds no availability: another entry drawing on the same
  quota *and* the same upstream buys nothing, since there is no quality-aware
  routing to exploit the difference.
- **An aggregator is the exception, and it must be checked, not assumed.** A
  provider that hosts nothing itself (OpenRouter) routes each model to an
  upstream, and a free model usually has exactly one upstream with no fallback
  behind it — so it inherits that upstream's outages whole. Two entries on two
  different upstreams are therefore worth two slots even though they share one
  account-wide quota: onboarding is ranked by effort, so a user may hold that
  key *alone* — precisely because one key reaches many upstreams — and a single
  entry leaves them with no failover at all. Two entries sharing an upstream
  remain worthless. Resolve the upstream per model from
  `https://openrouter.ai/api/v1/models/<author>/<slug>:free/endpoints` (the
  `provider_name` field); never infer it from the model's author, which is a
  different thing.
- Keep the pool multi-provider. A single-provider or paid-tier preset
  defeats the premise of pooling independent quota buckets.

## 4. Respect the catalog-identity invariants

- **A model bump is a new entry with a new name.** Never change the `model`
  field of an existing `[[llms]]` entry — encode the version in the entry
  name instead (e.g. `groq-llama-3.3-70b` → `groq-llama-4-70b`) so old and
  new versions coexist as distinct entries. A sync refuses an in-place `model`
  change; the deployment's learned evidence under the old name is about the
  old model.
- **When a strictly better sibling replaces an old model, remove the old
  entry.** They usually share one provider quota; leaving both in the preset
  means a still-endorsed old model keeps burning shared quota on worse
  answers. Downstream this costs nothing: the arrival carries the same
  `api_key_ref`, so the sync pairs the two and drops the old entry without
  consulting any key.
- **Dropping the last entry of a provider only prunes downstream when the same
  update gives installations a replacement they can call.** A sync removes an
  entry the preset dropped only if an arrival pays for it — with the same key,
  or with one the installation has. So an update that removes a provider
  without adding one prunes nothing downstream: those installations keep a
  working model, and their sync report names it on every run until an admin
  sets the key that would unlock the cleanup. That is intended. Do not "fix"
  it by removing more.
- Keep sibling models from one provider in the preset simultaneously only as
  a deliberate decision (genuinely different quota pools, or an aggregator's
  genuinely different upstreams), never as leftovers from a half-finished
  refresh. State which of the two it is in the diff summary.
- **A model `presets/paid-catalog.toml` lists does not belong in this pool.**
  Pool entries here are named `<provider id>-<model id>`, and an entry added
  from the catalog takes a machine-formed name of exactly that shape, so a model
  held by both files makes a name no config can carry twice — `add-model` and
  `preset --sync` refuse it. If the
  model belongs in the free pool, take it out of the catalog instead: the
  endpoint and the `api_key_ref` are the same either way, and the billing tier
  lives in the user's provider account, not in a config field.

## 5. Regenerate the outputs

- Rewrite `presets/freetier.toml`: one `[[llms]]` row per curated model
  (with `rate_limit` and `weight`), one `[keys.<API_KEY_REF>]` sub-table per
  provider (with `effort`, `value`, `help`).
- Update `specs/reference/freetier-providers.md`: the curated-providers
  table, the candidate-providers list, the per-provider notes, the sources
  list, and the refresh date.

## 6. Validate before proposing the change

- Load `presets/freetier.toml` through `llmbroker.standalone.registry.Registry`
  and confirm every `[[llms]]` row parses into an `LLMConfig` with a
  `rate_limit`, and every `[keys.*]` row parses into a `KeyInfo` with a
  recognized `effort` and `value` (not `None`).
- Confirm every `[[llms]]` row carries a `weight` within `[0, 1]`. The parser
  refuses a malformed one, but it accepts a missing one as `0.0` — so this is a
  check to run, not one to rely on the loader for.
- Cross-check `presets/paid-catalog.toml`: no `[[llms]]` `name` may equal a
  `<provider id>-<model id>` pair from the catalog.
- `invoke pre` and `python -m pytest` are green.
- Present a diff summary — added / removed / changed models, and the
  sourced reason for each — for human review. Do not commit the change
  yourself; the human decides whether to merge.

## Guardrails

- **Facts only from sources.** Every `rate_limit`/`effort`/`value` value must
  trace to a cited source captured in `freetier-providers.md`. Never guess a
  number.
- **Curation, not accumulation.** Adding a model requires a value
  justification. The goal is a small, dependable pool.
- **Human-in-the-loop.** Output a reviewed diff, never an unattended commit.
- **Taxonomies are fixed elsewhere.** If the `effort`/`value`/`rate_limit`
  shapes themselves need to change, that is a separate change to
  `freetier-providers.md` and the `EffortLevel`/`ValueLevel` enums in
  `models.py` — not something to do inside a routine refresh.
- **Entry identity is immutable.** Never repoint an existing entry name at a
  different `model`.
