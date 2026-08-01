# Free-tier LLM provider landscape

The curated knowledge llmbroker ships about the free LLM endpoints it pools. The
preset TOML (`presets/freetier.toml`) is the source of truth for the live values
that drive routing; this document records the surrounding knowledge — what the
limit dimensions mean, how the `effort` and `value` axes are defined, and where
to re-check the numbers when they drift.

Free-tier numbers change often. Treat the figures here as catalog-build-time
snapshots, not contracts. They are curated manually from public sources, not
from telemetry or crowdsourcing — the install base is too small for
crowdsourced numbers to be representative, and there is no live-stats
pipeline — so refreshing this document is a periodic, human-reviewed
maintenance action, done by following
[`../../presets/freetier-refresh-prompt.md`](../../presets/freetier-refresh-prompt.md)
(`invoke catalog-refresh` prints it).

---

## Rate-limit dimensions

Free tiers impose several independent windows; a single number is insufficient:

- **RPM (requests per minute)** — short request spacing. Onboarding/display
  metadata only — the optimizer does not seed anything from it; the live
  cooldown is always driven by the provider's own `Retry-After` on the actual
  response (or a flat fallback if absent), never by a catalog number, so a
  nominal `rpm` figure cannot go stale in a way that affects routing.
- **RPD (requests per day)** — a hard daily cap. Once exhausted the provider
  returns 429 for the *remainder of the day* — a multi-hour outage, distinct
  from short spacing. Daily quotas reset on a wall-clock boundary (Gemini at
  midnight Pacific; Groq and OpenRouter at ≈ midnight UTC). When this happens the
  provider's own `Retry-After`/reset header carries the time-until-reset, which
  the router honors directly as an ordinary (if long) cooldown — `rpd` is
  onboarding/display metadata (and documents which providers have a daily cap),
  not a value the optimizer reads.
- **TPM / TPD** (token windows) — exist on some providers but llmbroker routes on
  requests, not tokens, so they are display-only metadata.

Two things are easy to conflate:

- **Limits are frequently per-model.** The same key can carry very different
  limits for different models — e.g. under one `GROQ_API_KEY`, `llama-3.3-70b`
  gets 1,000 RPD while `llama-3.1-8b` gets 14,400 RPD. So a rate limit is a
  property of the *model endpoint*, not of the key. (OpenRouter is the opposite
  extreme: all its `:free` models share one account-wide daily pool — a
  per-provider limit. Both shapes exist, so the catalog carries `rate_limit`
  per-model on each `[[llms]]` row.)
- **Quota scope is per account / organization / project.** Whatever the limit,
  the bucket it draws from is shared across all your keys for that provider —
  making extra keys does not multiply it. This is why pooling *across* providers
  (independent buckets) is the value, and why two free models from the same
  provider that share a pool add no availability.

---

## Effort axis

How hard the key is to obtain, ordered easiest-first. The ordering doubles as the
onboarding sort key.

| Bucket | Meaning |
|---|---|
| `oauth` | Sign in with an account the user almost certainly already has (Google), then create a key. No new account, no card, no phone. |
| `signup` | Create a new dedicated account (email or OAuth). No card, no phone. |
| `verify` | New account that additionally requires phone/card verification and/or opting into data-training. |
| `console` | Key/billing/free-tier buried in a complex cloud console (AWS Bedrock, Google Cloud / Vertex AI). |
| `waitlist` | Access gated behind approval or a waitlist. |

The current catalog uses only `oauth` and `signup`; the rest are defined so
future entries sort correctly.

---

## Value axis

How good the best model the key unlocks is — the payoff for the effort. Onboarding
guidance for the human only; **not** a routing signal (quantity is irrelevant — a
provider may expose ten models yet only one is genuinely usable).

| Level | Meaning |
|---|---|
| `high` | Frontier-class general model. |
| `good` | Capable general-purpose model. |
| `niche` | Useful only for specific tasks. |

---

## Curated providers

Last refreshed 2026-08-01.

| Key (`api_key_ref`) | Best free model | Effort | Value | RPM | RPD | Get a key |
|---|---|---|---|---|---|---|
| `GEMINI_API_KEY` | Gemini 3.5 Flash-Lite | `oauth` | `high` | 15 | 500 | <https://aistudio.google.com/apikey> |
| `GROQ_API_KEY` | openai/gpt-oss-120b | `signup` | `good` | 30 | 1,000 | <https://console.groq.com/keys> |
| `OPENROUTER_API_KEY` | Nemotron 3 Ultra + Laguna S 2.1 `:free` | `signup` | `high` | 20 | 50 / 1,000 | <https://openrouter.ai/keys> |

Per-provider notes:

- **Gemini** — the free tier reaches the whole Flash family, but at sharply
  different daily caps: the Flash models (2.5 / 3.5 / 3.6) get 20 RPD, while
  Flash-Lite gets 500 RPD at 15 RPM. Flash-Lite is therefore the pooled entry —
  25× the daily cap for the same Artificial Analysis intelligence index (50, tied
  with 3.6 Flash). Pro-series models are paid-only. Limits are per-project and
  shown in the AI Studio dashboard, so these figures are nominal and may vary by
  project/region/billing state.
- **Groq** — only Groq's *production* models belong in the pool; preview models
  are documented as evaluation-only. `openai/gpt-oss-120b` is the strongest of
  those, and carries double the daily token cap of llama-3.3-70b-versatile
  (200K vs 100K TPD) at the same 1,000 RPD. Groq's standout is extreme inference
  speed. Limits apply at the organization level; cached tokens do not count
  toward them.
- **OpenRouter** — aggregator access to many free models under one key, but the
  free lineup churns hard: endpoints are delisted with no notice (`openai/gpt-oss-120b:free`
  disappeared entirely, leaving only the paid variant), so this entry needs
  re-checking against the live models API every refresh. `nvidia/nemotron-3-ultra-550b-a55b:free`
  is the strongest survivor — 550B/55B-active hybrid Mamba-Transformer MoE, 1M
  context, Artificial Analysis intelligence index 47.7, the highest of any US
  open-weight model and far above gpt-oss-120b (33.3). The free endpoint requires
  consenting to NVIDIA's data collection. `poolside/laguna-s-2.1:free` is the
  second entry, chosen for its upstream as much as its strength: Poolside serves
  its own model, so its failures are uncorrelated with Nvidia's. It is a
  118B/8B-active MoE with 1M context, built for agentic coding — SWE-Bench
  Multilingual 78.5%, SWE-Bench Pro 59.4%, Terminal-Bench 2.1 70.2%, ahead of
  Nemotron 3 Ultra on that ground while answering general prompts fine.
  The daily cap is 50 RPD with under $10 lifetime credit purchased, rising to
  1,000 RPD once $10+ has been purchased (a one-time, never-expiring top-up).
- **OpenRouter hosts nothing itself** — every model is routed to an upstream, and
  a `:free` model has exactly one upstream endpoint with no fallback behind it
  (the paid slug of the same model has several, and OpenRouter switches between
  them). So a free entry inherits its upstream's outages directly, and an
  occasional HTTP 200 carrying an error body instead of a completion is expected
  rather than exceptional. The pool absorbs it: the router classifies that as a
  failover error and cools the entry.

An aggregator therefore earns more than one pool entry, on the strict condition
that the entries resolve to *different* upstreams. The quota argument cuts one way
only: **all `:free` models share one account-wide daily quota**, so a second entry
adds no requests, and quota exhaustion — the free tier's longest outage, lasting
until the daily reset — is not helped by it at all. What a second entry does add is
independence from one upstream's failures, and that matters most in the pool's
smallest configuration. Onboarding is ranked by effort, so a user may hold the
OpenRouter key alone, precisely because one key reaches many upstreams; with a
single entry that user has no failover whatsoever. Two entries on two upstreams
give them one. Entries sharing an upstream (the seven `nemotron` models all resolve
to Nvidia) add nothing on either axis.

---

## Curation rules for adding and removing entries

What a curated update may do is bounded by what a downstream sync will do with
it (see "Syncing the lineup" in [`architecture.md`](architecture.md)).

- **A same-provider replacement removes the old entry.** The two usually share
  one provider quota, and a still-endorsed old model would keep spending it on
  worse answers. Downstream this is free: the arrival carries the old entry's
  `api_key_ref`, so the sync pairs them and removes it with no key involved.
- **Dropping the last entry of a provider is a removal downstream installations
  follow only when the same update gives them a usable replacement.** A provider
  therefore leaves the preset when it is no longer worth a slot; installations
  that cannot use the newcomer keep a working model instead of losing one, and
  the sync report names it on every run so an admin can act.
- **Consequently a curated update that drops a provider without adding one
  prunes nothing downstream.** That is intended, not a gap to close: the
  alternative is an update that silently shrinks a pool. A future curator should
  not try to "fix" it by dropping more.
- **A model bump is a new entry name**, never an in-place `model` change — a
  sync refuses that, since learned quality is bound to the entry name.

---

## Candidate providers (not yet in the catalog)

- **Cerebras** — extremely fast, and the only free tier here with no daily
  *request* cap (5 RPM, 30K TPM, 1M TPD). Held back for two reasons: its one
  production model is the `gpt-oss-120b` the Groq entry already carries, so it
  buys another quota bucket rather than another capability (its `zai-glm-4.7` and
  `gemma-4-31b` are preview-only); and free credits now require adding a verified
  payment method, which makes it `verify`, not `signup`.
- **Mistral La Plateforme** — free "Experiment" tier requires phone verification
  and opting into data-training (`verify`).

---

## Sources

Authoritative provider docs (best for exact numbers):

- Groq rate limits — <https://console.groq.com/docs/rate-limits>; production vs
  preview tier — <https://console.groq.com/docs/models>
- OpenRouter limits — <https://openrouter.ai/docs/api/reference/limits>; the live
  free lineup — <https://openrouter.ai/api/v1/models>, filtered on ids ending
  `:free`. That endpoint is the only reliable check that a `:free` model still
  exists, and needs no key.
- Gemini free-tier availability — <https://ai.google.dev/gemini-api/docs/pricing>;
  rate limits — <https://ai.google.dev/gemini-api/docs/rate-limits> (which publishes
  no free-tier table, pointing instead at the per-project dashboard at
  <https://aistudio.google.com/rate-limit>)

Community trackers (breadth, cross-checking):

- `cheahjs/free-llm-api-resources` — the canonical maintained list.
- `mnfst/awesome-free-llm-apis`, `amardeeplakshkar/awesome-free-llm-apis`,
  `open-free-llm-api/awesome-freellm-apis` — overlapping, machine-readable.
