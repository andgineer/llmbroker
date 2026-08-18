# Free-tier LLM provider landscape

The curated knowledge llmbroker ships about the free LLM endpoints it pools. The
preset TOML (`src/llmbroker/presets/freetier.toml`) is the source of truth for the live values
that drive routing; this document records the surrounding knowledge — what the
limit dimensions mean, how the `effort` and `value` axes are defined, and where
to re-check the numbers when they drift.

Free-tier numbers change often. Treat the figures here as catalog-build-time
snapshots, not contracts. They are curated manually from public sources, not
from telemetry or crowdsourcing — the install base is too small for
crowdsourced numbers to be representative, and there is no live-stats
pipeline — so refreshing this document is a periodic, human-reviewed
maintenance action, done by following
[`../../src/llmbroker/presets/freetier-refresh-prompt.md`](../../src/llmbroker/presets/freetier-refresh-prompt.md)
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
  per-provider limit.) Both shapes exist, which is why no limit figure is
  carried in the preset at all: a pooled row holds only its endpoint, its key
  reference and its weight. The numbers below are curation research, read by a
  human deciding what to pool — never by the router.
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

## Weight axis

Unlike `effort` and `value`, which are onboarding guidance for a human, `weight`
is read by the router: it is the curated prior on the quality rating an entry is
expected to earn, on the same `0..1` scale as a host rating, and it sets where the
entry starts in the pool until an installation's own ratings replace it entirely
(see "Order of acquisition" in [`rules/selection.md`](rules/selection.md)). Every `[[llms]]` row carries
one — an entry without a weight starts at the bottom of the pool, which is a
silent curation failure.

| weight | meaning |
|---|---|
| 0.8–1.0 | frontier-class |
| 0.6–0.8 | strong general-purpose |
| 0.4–0.6 | usable, clearly behind the leaders |
| < 0.4 | niche or weak |

It is a judgement informed by benchmarks, not equal to any of them: benchmarks
measure a task, the weight predicts how a host will rate an ordinary answer. One
thing outranks that prediction: an endpoint that refuses most requests is carried
at the floor however well it answers when it does, because a candidate that is
usually not there costs every caller a wasted attempt before the pool moves on.
Slowness is not that fact and does not lower a weight — how slow is too slow is
the caller's `wait` to state, not curation's. The
shipped values live in the preset and rest on the evidence recorded under
"Curated providers" below; a refresh that changes one restates its evidence
there rather than here.

The paid catalog carries no weight: its entries land direct-only, outside the
routed pool, so nothing would read one.

---

## Curated providers

Last refreshed 2026-08-16.

| Key (`api_key_ref`) | Best free model | Effort | Value | RPM | RPD | Get a key |
|---|---|---|---|---|---|---|
| `GEMINI_API_KEY` | Gemini 3.5 Flash-Lite | `oauth` | `high` | 30 | 1,500 | <https://aistudio.google.com/apikey> |
| `GROQ_API_KEY` | openai/gpt-oss-120b | `signup` | `good` | 30 | 1,000 | <https://console.groq.com/keys> |
| `OPENROUTER_API_KEY` | Nemotron 3 Ultra + Laguna S 2.1 `:free` | `signup` | `high` | 20 | 50 / 1,000 | <https://openrouter.ai/keys> |
| `ZAI_API_KEY` | GLM-4.7-Flash | `signup` | `good` | not published | not published | <https://z.ai/manage-apikey/apikey-list> |

Per-provider notes:

- **Gemini** — the free tier reaches the whole Flash family, the newest one
  included; the Pro series is paid-only. Google publishes no free-tier limit
  table anywhere, pointing instead at the per-project AI Studio dashboard, so the
  figures above come from the community trackers and are nominal — they vary by
  project, region and billing state. On those figures Flash-Lite and its Flash
  siblings share one daily cap and Flash-Lite has twice the requests per minute,
  which is why Flash-Lite stays the pooled entry although the newest Flash is the
  more capable model. Moving the entry to a Flash needs a first-party number: the
  daily cap is the outage that matters, and no first-party source confirms one.
- **Groq** — only Groq's *production* models belong in the pool; preview models
  are documented as evaluation-only. `openai/gpt-oss-120b` is the strongest of
  those, and carries double the daily token cap of llama-3.3-70b-versatile
  (200K vs 100K TPD) at the same 1,000 RPD. Groq's standout is extreme inference
  speed. Limits apply at the organization level; cached tokens do not count
  toward them.
- **Z.AI** — the GLM Flash models are free on Z.AI's own price list rather than
  trial credits, and the key is an email signup with no card. No request-rate
  table is published for them; the trackers report a one-concurrent-request cap,
  which a pool absorbs — a busy moment fails over instead of queueing. The pooled
  entry is the newest free text model; the full GLM flagship line is paid, so
  this key unlocks one genuinely useful model rather than a family.
- **OpenRouter** — aggregator access to many free models under one key, but the
  free roster churns hard: endpoints are delisted with no notice (`openai/gpt-oss-120b:free`
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

## What one real workload met (measured 2026-08-17)

The figures above are curated from public sources. This section is the other
kind of knowledge: what a single application actually met when it drove the
pool hard, on one key per provider, in one afternoon. It is one workload on one
day, not a contract and not telemetry — but it is the only place here where the
numbers come from calls rather than from documentation, and it is what makes
the pool's *practical* ceiling visible, which no published RPM does.

The workload: a vocabulary tool sending one prompt of ~1.2K tokens and reading
back ~800, streamed.

**A burst exhausts the best model in about a minute.** Four requests in flight,
120 requests over seven minutes. The highest-weighted model answered the first
14, then cooled; over the whole run it took 83, and the rest spilled down the
list — 30, 3 and 1 to the three siblings. Three requests found nothing free
within a 45-second budget and failed. Every one of the 16 cooldowns used the
flat base: **not one provider sent a `Retry-After`**, so the published RPM
figures are the only warning a host gets, and the router is guessing the wait
every time.

**Paced, the same pool never cools at all.** One request at a time, five seconds
apart, 20 requests: zero cooldowns, zero failover, the highest-weighted model
answered all 20. The gap between this and the burst above is the whole story of
what the free tier is for.

**Sustained through one key, the practical rate is ~15 requests a minute.** Two
in flight with four seconds of pacing, 120 requests, no failures — but only
because the caller retried on 429 with backoff. That is the ceiling of a single
free key for a single model, and it is a *per-minute* ceiling: no daily cap was
reached in any run.

**The pool's latency spread is about thirtyfold, and weight does not encode
it.** Median whole answer, per model that actually answered: 1.6–1.7 s for the
highest-weighted one, 2.8–3.5 s for the fastest sibling, 36–57 s (worst
observed 101 s) and 43 s for the two that carry the *second and third* highest
weights. So a host whose first choice cools does not degrade gently — its
answers get an order of magnitude slower, while the fastest alternative sorts
last. Anyone pooling for an interactive workload should read the weights as
what they are, a quality prior, and expect no help with latency.

**The slow models spend that time before the first token, not between tokens.**
For the slowest entry the median wait to the first token was 23 s paced and 42 s
under burst, and the worst single answer spent 88 of its 101 seconds there; once
text starts it arrives steadily, about 0.05 s per token, and no streaming phase
in any run exceeded 32 s. The fast entries open in well under a second. So a
first-token budget separates this pool almost as well as a whole-answer budget
would, while a bound on the *gap between* tokens separates nothing: no gap
observed anywhere in the run came close to a second.

**An empty completion is not hypothetical.** One model returned a well-shaped
answer carrying no text at all on 3 of 14 requests for one language, in under a
second each. Hosts that treat "no error" as "usable answer" will ship those
straight through.

**The Z.AI entry works through the pool — and is still unusable for an
interactive caller.** Driven with that key alone, so routing had exactly one
candidate, it answered 1 request in 8: a complete, well-formed answer naming
itself on the handle, so nothing about the endpoint's shape troubles the router.
The 429s were classified as rate limits, the cooldown grew 60 → 120 → 240 → 480
seconds exactly as documented, and the pool reported itself degraded and
under-provisioned throughout. What makes it unusable is the other number: when it
did answer, **the first token arrived after 31 seconds**. `glm-4.7-flash` is a
reasoning model, and it thinks for half a minute before it says anything. So even
a perfectly available Z.AI would miss a first-content budget of a few seconds by
sixfold. What carries the entry at the floor weight is the availability, not the
latency: one answer in eight makes it the last candidate wherever another key
exists, and it stays in the list because where no other key exists it is the pool.

**Z.AI's free tier is oversubscribed enough to be unreliable.** Its 429 carries
code 1305, "the service may be temporarily overloaded", and no `Retry-After`;
availability swung from 2 answers in 6 attempts to 26 consecutive refusals
within the same hour, for short prompts and real ones alike. Two traps worth
knowing: the free flash model does **not** appear in that key's `/models`
listing, so the listing is not a reachability check; and the paid models on the
same key answer `1113 Insufficient balance`, which reads like an account
problem and is not one.

---

## Curation rules for adding and removing entries

**A removal here reaches every installation that follows the list.** A sync
mirrors what it wrote, weighing nothing
([`decisions.md`](decisions.md#a-sync-mirrors-what-a-sync-wrote)), so an entry
taken out of the curated list is taken out of installations that hold a working
key for it too — including one whose *only* key is that provider's, which is left
with no pool at all. Curation carries that risk, and the rules below are how it
is carried.

- **An entry leaves the list only when it can no longer be called**: the endpoint
  is withdrawn, or its free tier ended. Never for being weak, never for being
  redundant, never on taste.
- **A model still callable but not worth routing to gets the lowest weight
  instead.** Weight orders the pool, so an installation with better providers
  never reaches it, while an installation holding only that provider's key keeps
  a working pool. This is what makes the rule above affordable: the list does not
  have to choose between recommending a weak model and stranding whoever depends
  on it.
- **A same-provider replacement removes the old entry.** The two usually share
  one provider quota, and a still-endorsed old model would keep spending it on
  worse answers. Nobody is stranded: the arriving list carries the same
  `api_key_ref`, so whoever could call the old entry can call the new one.
- **A model bump is a new entry name**, never an in-place `model` change — a
  sync refuses that, since learned quality is bound to the entry name.

---

## Candidate providers (not yet in the catalog)

- **Cerebras** — extremely fast, and the only free tier here with no daily
  *request* cap (5 RPM, 30K TPM, 1M TPD, every catalog model). Held back for two
  reasons: its strongest model is the `gpt-oss-120b` the Groq entry already
  carries, so it buys another quota bucket rather than another capability; and
  what is free is $5 of credits that expire 30 days after they are granted, which
  makes it a trial rather than a standing free tier — a pooled entry would go
  dead a month after onboarding.
- **Mistral La Plateforme** — free "Experiment" tier requires phone verification
  and opting into data-training (`verify`).

---

## Sources

Authoritative provider docs (best for exact numbers):

- Groq rate limits — <https://console.groq.com/docs/rate-limits>; production vs
  preview tier — <https://console.groq.com/docs/models>
- OpenRouter limits — <https://openrouter.ai/docs/api/reference/limits>; the live
  free roster — <https://openrouter.ai/api/v1/models>, filtered on ids ending
  `:free`. That endpoint is the only reliable check that a `:free` model still
  exists, and needs no key.
- Gemini free-tier availability — <https://ai.google.dev/gemini-api/docs/pricing>;
  rate limits — <https://ai.google.dev/gemini-api/docs/rate-limits> (which publishes
  no free-tier table, pointing instead at the per-project dashboard at
  <https://aistudio.google.com/rate-limit>)
- Z.AI free models — <https://docs.z.ai/guides/overview/pricing>; model ids —
  <https://docs.z.ai/guides/llm/glm-4.7>; OpenAI-compatible endpoint —
  <https://docs.z.ai/api-reference/introduction>
- Cerebras free-trial limits — <https://inference-docs.cerebras.ai/support/rate-limits>

Community trackers (breadth, cross-checking):

- `mnfst/awesome-free-llm-apis` — the freshest of them, and the only source for
  the Gemini free-tier numbers, which no first-party page carries.
- `amardeeplakshkar/awesome-free-llm-apis`,
  `open-free-llm-api/awesome-freellm-apis` — overlapping and machine-readable,
  but both lag a model generation behind, so cross-check a number before using it.
