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
maintenance action.

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

| Key (`api_key_ref`) | Best free model | Effort | Value | RPM | RPD | Get a key |
|---|---|---|---|---|---|---|
| `GEMINI_API_KEY` | Gemini 2.5 Flash | `oauth` | `high` | 15 | 1,500 | <https://aistudio.google.com/apikey> |
| `GROQ_API_KEY` | llama-3.3-70b-versatile | `signup` | `good` | 30 | 1,000 | <https://console.groq.com/keys> |
| `OPENROUTER_API_KEY` | gpt-oss-120b `:free` | `signup` | `good` | 20 | 50 / 1,000 | <https://openrouter.ai/keys> |

Per-provider notes:

- **Gemini 2.5 Flash** — frontier-adjacent, multimodal, ~1M context; the largest
  daily cap of the three. Google's docs publish no fixed numbers (limits are
  per-project and shown only in the AI Studio dashboard), so Gemini figures are
  nominal and may vary by project/region/billing state.
- **Groq** — solid general model; Groq's standout is extreme inference speed.
  Limits apply at the organization level. Cached tokens do not count toward
  limits.
- **OpenRouter** — capable open MoE, plus aggregator access to many free models
  under one key. The daily cap is 50 RPD with under $10 lifetime credit purchased,
  rising to 1,000 RPD once $10+ has been purchased (a one-time, never-expiring
  top-up). **All `:free` models share one account-wide daily quota**, so carrying
  more than one OpenRouter free model adds no availability.

`nvidia/nemotron-3-super-120b-a12b:free` is a notable OpenRouter free model —
stronger than gpt-oss-120b on coding/agentic work (SWE-Bench Verified ~60.5 vs
~41.9), weaker on conversational quality (Arena-Hard ~73.9 vs ~90.3), comparable
overall. It is excluded from the catalog because it shares OpenRouter's single
free quota with gpt-oss-120b and llmbroker has no quality-aware routing to exploit
the difference.

---

## Candidate providers (not yet in the catalog)

- **Cerebras** — `gpt-oss-120b` and Llama 3.1 8B free, very fast, no card
  (`signup`). A strong candidate addition.
- **Mistral La Plateforme** — free "Experiment" tier requires phone verification
  and opting into data-training (`verify`).

---

## Sources

Authoritative provider docs (best for exact numbers):

- Groq rate limits — <https://console.groq.com/docs/rate-limits>
- OpenRouter limits — <https://openrouter.ai/docs/api/reference/limits>
- Gemini rate limits — <https://ai.google.dev/gemini-api/docs/rate-limits>
  (points to the per-project dashboard at <https://aistudio.google.com/rate-limit>)

Community trackers (breadth, cross-checking):

- `cheahjs/free-llm-api-resources` — the canonical maintained list.
- `mnfst/awesome-free-llm-apis`, `amardeeplakshkar/awesome-free-llm-apis`,
  `open-free-llm-api/awesome-freellm-apis` — overlapping, machine-readable.
