# Paid catalog refresh prompt

Standalone runbook. Hand this whole file to an LLM agent with repo access and
**web access**, or follow it by hand. It regenerates `src/llmbroker/presets/paid-catalog.toml`
— the curated catalog of paid providers and the current models worth calling by
name, one line per tier, that `llmbroker list` prints and `direct=` resolves an
alias against.

Run it in the same pass as `freetier-refresh-prompt.md`: the free pool and the
paid catalog are refreshed together, from the providers' own current pages.

Background reading before you start (do not skip):

- `src/llmbroker/broker/aliases.py` — how a declared alias resolves against this
  file. The catalog's fields must map cleanly onto what a declared model needs:
  `base_url`, `model`, `api_key_ref`, and a key help blurb.
- [`../../../specs/reference/rules/direct-by-name.md`](../../../specs/reference/rules/direct-by-name.md) —
  llmbroker calls **only OpenAI-compatible** `/chat/completions` endpoints. A
  provider without an OpenAI-compatible endpoint cannot be in this catalog.

## 0a. The alias contract (read first, it constrains every edit below)

Every `[[provider.models]]` line carries an `alias` — the eternal handle an
application passes to `direct("opus")`. It is the one field in this file a user's
config depends on, so it is governed by a permanence contract:

- **A published alias never disappears and never renames.** A generation change
  re-points the existing alias at the successor model; it does not mint a new one.
  Dropping a model that still has a live alias breaks every model list following it —
  either keep the alias pointing at the provider's successor, or accept that
  every refresh will warn on it forever.
- **No version substring in an alias.** `opus`, `gpt-mini`, `flash` are aliases;
  `opus-4-8`, `gpt-5`, `flash-2-5` are not. The alias outlives the version by
  construction, so a version inside it is a contradiction.
- **The fast tier's handle follows one convention**, because a word coined for it
  here is permanent: `<provider handle>-fast`, unless the provider's own product
  name already supplies a non-version word for it (Google's `flash`). This binds
  aliases you are minting now — a published one never renames, so spellings
  already in the file stay exactly as they are.
- **Aliases are unique across the whole catalog**, not per provider. A duplicate
  makes the catalog invalid and `llmbroker` refuses it with an error.
- **A model a shipped preset already pools does not belong here.** An alias
  entry's `name` is machine-formed `<provider id>-<model id>`, the same
  convention preset pool entries are named by, so a model this file shares with
  `src/llmbroker/presets/freetier.toml` produces a name that collides with a pool
  entry, and a provision refuses it outright. Nor is there anything to add: the endpoint and the `api_key_ref` are the same ones the
  preset already uses, and the billing tier lives in the user's provider
  account, not in any config.

Aliases are what a re-resolution follows: for each alias a deployment declared it
reads `model`, `name`, `base_url` and `api_key_ref` from this file. That is the
whole point — so an alias whose target you change here changes the model of every
deployment following it.

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

## 2. Curate the tiers the provider actually has

Per provider, write **one line per distinct tier** — not a fixed number of lines:

- the **top-capability** flagship;
- the provider's own **recommended default**, where it differs from the flagship;
- a **cheaper-but-strong** sibling;
- the **fast** tier — the one a caller streaming into a UI would choose. Include
  it *even when a stronger sibling exists*: it wins on an axis the others are not
  ranked by, and an alias is the only way an application can ask for the fast one.
  A model level with its stronger sibling on quality and several times quicker to
  the first token is a tier, not a duplicate.

Skip a tier the provider does not meaningfully have. This is a curation, not a
dump: do not list every snapshot, dated alias, or legacy id — that, and not a
count, is what keeps the file from accumulating.

Prefer the **stable/latest** id the provider recommends for production over a
dated snapshot, unless only dated snapshots exist.

## 3. Write `src/llmbroker/presets/paid-catalog.toml`

Exact schema (one `[[provider]]` block per provider, one `[[provider.models]]`
per curated model):

```toml
# Curated paid providers and the current models worth calling by name, one per tier.
# Read by `llmbroker list` and by `direct=`. Refreshed via paid-catalog-refresh-prompt.md.
# refreshed = "YYYY-MM-DD"

[[provider]]
id          = "anthropic"                          # short, stable slug
label       = "Anthropic"                          # human name for the menu
base_url    = "https://api.anthropic.com/v1"       # OpenAI-compatible endpoint (verified)
api_key_ref = "ANTHROPIC_API_KEY"                  # env var / secrets ref name
key_help    = "Create a key at https://console.anthropic.com/ (paid)."

  [[provider.models]]
  alias    = "opus"                                # eternal handle — see §0a
  model    = "claude-opus-4-8"                     # exact API id (verbatim from the reference)
  label    = "Claude Opus 4.8 — highest-quality reasoning"
  verified = "https://docs.anthropic.com/en/docs/about-claude/models (YYYY-MM-DD)"
```

Rules:

- `alias` obeys §0a: carried over unchanged from the previous catalog whenever the
  model it names is being replaced by a successor, never version-bearing, unique
  across the file.
- `model` is the exact API id, copied verbatim from the provider's reference.
- `verified` is the **provider-own** URL you read the id from, plus today's date.
- `base_url` is the OpenAI-compatible endpoint you confirmed this pass.
- `api_key_ref` is a stable env-var name (reuse the conventional one per
  provider, e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`).
- Set the top `# refreshed = "YYYY-MM-DD"` to today.

## 4. Validate before proposing the change

- Parse `src/llmbroker/presets/paid-catalog.toml` with `tomllib` and confirm every
  `[[provider]]` has `id`, `base_url`, `api_key_ref`, and at least one
  `[[provider.models]]` with an `alias`, `model`, `label`, and `verified`.
- Confirm the alias contract mechanically: aliases unique across the whole file,
  none carrying a version substring, and every alias present in the previous
  revision still present in the new one.
- Cross-check every shipped preset in `src/llmbroker/presets/`: no `<provider id>-<model id>`
  pair in this file may equal a preset pool entry's `name`.
- Spot-check: for at least one provider whose key you hold, confirm each `model`
  id is accepted (a real request, or the provider's models-list endpoint). This
  is an optional cross-check, not a substitute for §0–§1.
- **Report the tier-by-tier decision, not only the diff.** Per provider, list the
  tiers you found on its models page and what you did with each: included (with
  the alias) or skipped (with the reason). A model that falls outside the curation
  axis otherwise leaves no trace at all in a diff of what changed, which is how a
  provider's fast tier can stay missing for a whole refresh cycle without anyone
  having a line to notice.
- Present a diff summary — providers/models added, removed, changed, each with
  its `verified` source — for human review. **Do not commit yourself.**

## Guardrails

- **Aliases are permanent.** Re-point one at a successor model; never rename or
  drop one, never put a version in one — see §0a.
- **Ids only from the provider's own official docs, verbatim.** Official domains
  reached via redirects count; third-party lists, marketing names, and memory do
  not. Cite every id with the URL you actually read.
- **Fail closed.** Unverifiable id → omit the model.
- **OpenAI-compatible only.** No OpenAI-compatible `/chat/completions` endpoint →
  the provider is out of scope for this catalog.
- **Curation, not accumulation.** One line per distinct tier the provider has,
  its fast tier included; no snapshots, dated aliases or legacy ids. There is no
  cap on the count — the exclusions are what bound the file.
- **Human-in-the-loop.** Output a reviewed diff, never an unattended commit.
