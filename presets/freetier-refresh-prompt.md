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
  two-halves catalog and `SeedPolicy.SYNC`, which govern how a refreshed
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

## 3. Apply the curation rules

- One genuinely useful model per provider — curate, do not pad.
- Drop a model that adds no availability: e.g. a second OpenRouter `:free`
  model sharing the same account-wide daily quota as one already in the
  catalog — there is no quality-aware routing to exploit the difference.
- Keep the pool multi-provider. A single-provider or paid-tier preset
  defeats the premise of pooling independent quota buckets.

## 4. Respect the catalog-identity invariants

- **A model bump is a new entry with a new name.** Never change the `model`
  field of an existing `[[llms]]` entry — encode the version in the entry
  name instead (e.g. `groq-llama-3.3-70b` → `groq-llama-4-70b`) so old and
  new versions coexist as distinct entries. `SeedPolicy.SYNC` refuses an
  in-place `model` change with an alert; the deployment's learned evidence
  under the old name is about the old model.
- **Removing an entry from the preset is safe and cheap.** At deployments,
  `SYNC` turns a removal into deprecation — a reversible, last-resort
  demotion, not data loss.
- **When a strictly better sibling replaces an old model, remove the old
  entry.** They usually share one provider quota; leaving both in the preset
  means a still-endorsed old model keeps burning shared quota on worse
  answers.
- Keep sibling models from one provider in the preset simultaneously only as
  a deliberate decision (e.g. genuinely different quota pools), never as
  leftovers from a half-finished refresh.

## 5. Regenerate the outputs

- Rewrite `presets/freetier.toml`: one `[[llms]]` row per curated model
  (with `rate_limit`), one `[keys.<API_KEY_REF>]` sub-table per provider
  (with `effort`, `value`, `help`).
- Update `specs/reference/freetier-providers.md`: the curated-providers
  table, the candidate-providers list, the per-provider notes, the sources
  list, and the refresh date.

## 6. Validate before proposing the change

- Load `presets/freetier.toml` through `llmbroker.standalone.registry.Registry`
  and confirm every `[[llms]]` row parses into an `LLMConfig` with a
  `rate_limit`, and every `[keys.*]` row parses into a `KeyInfo` with a
  recognized `effort` and `value` (not `None`).
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
