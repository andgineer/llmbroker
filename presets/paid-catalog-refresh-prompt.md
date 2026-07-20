# Paid catalog refresh prompt

Standalone runbook. Hand this whole file to an LLM agent with repo access and
**web access**, or follow it by hand. It regenerates `presets/paid-catalog.toml`
— the curated catalog of paid providers and their current flagship models that
`llmbroker add-model` reads to help a user drop a paid model into their config.

Run it in the same pass as `freetier-refresh-prompt.md`: the free pool and the
paid catalog are refreshed together, from the providers' own current pages.

Background reading before you start (do not skip):

- `src/llmbroker/standalone/registry.py` — how `[[custom]]` entries and
  `[keys.*]` are parsed. The catalog's fields must map cleanly onto what a
  `[[custom]]` entry needs: `base_url`, `model`, `api_key_ref`, and a `[keys]`
  help blurb.
- [`../specs/reference/architecture.md`](../specs/reference/architecture.md) —
  llmbroker calls **only OpenAI-compatible** `/chat/completions` endpoints. A
  provider without an OpenAI-compatible endpoint cannot be in this catalog.

## 0. What "verified" means here (read first)

Every model id you write is an **API id** — the exact string a caller puts in
the `model` field of an OpenAI-compatible request body (e.g. `claude-opus-4-8`),
**not** a marketing name (e.g. "Claude Opus 4.8"). Marketing names do not work
in `model=`. You must take the id verbatim from the provider's own API/models
reference page.

**Fail closed.** If you cannot find a model's exact API id on the provider's
**own authoritative page**, do not include that model. A short verified catalog
beats a longer one with one hallucinated id that silently 404s at call time.

## 1. Providers and their authoritative pages

For each provider, open its **own official docs** (do not trust third-party
lists, blogs, or aggregators for ids or endpoints) and confirm two things live:
(a) the **flagship model API id(s)** on the models/reference page, and (b) the
**OpenAI-compatible base_url** — the host serving `/chat/completions`. Note two
realities the "one domain" instinct gets wrong:

- **Provider docs move across official domains and 301-redirect.** Follow the
  redirect to the provider's current canonical domain — do not reject it. Seen
  in practice: `docs.anthropic.com` → `platform.claude.com`,
  `platform.openai.com` → `developers.openai.com`. "Official domain(s)" is the
  rule, not one fixed host.
- **The base_url host usually differs from the docs host, and that is correct**
  (e.g. ids verified on `ai.google.dev`, endpoint `generativelanguage.googleapis.com`).
  The models page may not print the base_url at all; take it from the provider's
  API-reference / OpenAI-compatibility page. For OpenAI specifically, you need
  the OpenAI-compatible **Chat Completions** endpoint (`https://api.openai.com/v1`),
  **not** the Responses API the docs may foreground.

Seed providers (extend only with providers that genuinely expose an
OpenAI-compatible endpoint) — entry points, re-follow redirects each pass:

- **Anthropic** — models:
  `https://platform.claude.com/docs/en/docs/about-claude/models/overview`
  (base_url `https://api.anthropic.com/v1`).
- **OpenAI** — models: `https://developers.openai.com/api/docs/models`
  (base_url `https://api.openai.com/v1`).
- **Google (Gemini)** — models: `https://ai.google.dev/gemini-api/docs/models`;
  OpenAI-compat: `https://ai.google.dev/gemini-api/docs/openai`
  (base_url `https://generativelanguage.googleapis.com/v1beta/openai`).
- **xAI** — `https://docs.x.ai/docs/models` (base_url `https://api.x.ai/v1`).
- **Mistral** — `https://docs.mistral.ai/getting-started/models/models_overview/`
  (base_url `https://api.mistral.ai/v1`).
- **DeepSeek** — `https://api-docs.deepseek.com/` (base_url `https://api.deepseek.com`).

If a docs page is JS-only and unreadable, or a link 404s, navigate from the
provider's docs root on the same official domain, or read the ids from the
provider's models-list API endpoint. If the docs never state a base_url, use the
provider's well-known OpenAI-compatible host and flag it in the diff for the
human to confirm — never invent a novel host. Never carry an id or base_url over
from memory or from this file without re-reading it live this pass.

## 2. Curate the flagship model(s)

Per provider, pick **one to three** models a user would pay for on quality, as
distinct tiers — the top-capability flagship, the provider's recommended default
if different, and at most one cheaper-but-strong sibling. Skip a tier that is not
meaningfully distinct. This is a curation, not a dump: do not list every
snapshot, dated alias, or legacy id.

Prefer the **stable/latest** id the provider recommends for production over a
dated snapshot, unless only dated snapshots exist.

## 3. Write `presets/paid-catalog.toml`

Exact schema (one `[[provider]]` block per provider, one `[[provider.models]]`
per curated model):

```toml
# Curated paid providers and their current flagship models.
# Consumed by `llmbroker add-model`. Refreshed via paid-catalog-refresh-prompt.md.
# refreshed = "YYYY-MM-DD"

[[provider]]
id          = "anthropic"                          # short, stable slug
label       = "Anthropic"                          # human name for the menu
base_url    = "https://api.anthropic.com/v1"       # OpenAI-compatible endpoint (verified)
api_key_ref = "ANTHROPIC_API_KEY"                  # env var / secrets ref name
key_help    = "Create a key at https://console.anthropic.com/ (paid)."

  [[provider.models]]
  model    = "claude-opus-4-8"                     # exact API id (verbatim from the reference)
  label    = "Claude Opus 4.8 — highest-quality reasoning"
  verified = "https://docs.anthropic.com/en/docs/about-claude/models (YYYY-MM-DD)"
```

Rules:

- `model` is the exact API id, copied verbatim from the provider's reference.
- `verified` is the **provider-own** URL you read the id from, plus today's date.
- `base_url` is the OpenAI-compatible endpoint you confirmed this pass.
- `api_key_ref` is a stable env-var name (reuse the conventional one per
  provider, e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`).
- Set the top `# refreshed = "YYYY-MM-DD"` to today.

## 4. Validate before proposing the change

- Parse `presets/paid-catalog.toml` with `tomllib` and confirm every
  `[[provider]]` has `id`, `base_url`, `api_key_ref`, and at least one
  `[[provider.models]]` with a `model`, `label`, and `verified`.
- Spot-check: for at least one provider whose key you hold, confirm each `model`
  id is accepted (a real request, or the provider's models-list endpoint). This
  is an optional cross-check, not a substitute for §0–§1.
- Present a diff summary — providers/models added, removed, changed, each with
  its `verified` source — for human review. **Do not commit yourself.**

## Guardrails

- **Ids only from the provider's own official docs, verbatim.** Official domains
  reached via redirects count; third-party lists, marketing names, and memory do
  not. Cite every id with the URL you actually read.
- **Fail closed.** Unverifiable id → omit the model.
- **OpenAI-compatible only.** No OpenAI-compatible `/chat/completions` endpoint →
  the provider is out of scope for this catalog.
- **Curation, not accumulation.** One to three distinct tiers per provider.
- **Human-in-the-loop.** Output a reviewed diff, never an unattended commit.
