# Optimizer

The `Optimizer` is an optional component that improves pool health over time: it computes
each cooldown from the provider's own signal on the actual response, tracks per-LLM
success-rate quality, and automatically retires a persistently unreliable LLM. There are
only two lifecycle phases, AVAILABLE and COOLING, always derived from `cooldown_until`
vs. now — no separate circuit-breaker state machine sits on top of them.

Pass `optimize=True` (the default) or an explicit `Optimizer(...)` instance to
`AsyncBroker` / `Broker` to activate it.

---

## Cooldown duration: trust the provider

On a 429/503, the wait duration is computed fresh from *that response's own* signal —
never carried forward as persistent per-LLM state between events:

- If the provider sends `Retry-After` (seconds, or an HTTP-date), that number is used
  as-is on the first failure of a streak — the provider is the authority on its own
  quota and reset schedule.
- If **this same LLM** is already mid-failure-streak (no success since its last
  429/503), the response's own number is scaled by `backoff_factor ** consecutive_fails`,
  where `consecutive_fails` counts how many 429/503s have landed in a row since the
  last success. A success resets the streak to zero. This means a day-long quota wait
  followed by an unrelated 60-second rate limit never compounds into "wait two days" —
  each event is scaled from its own number, not a carried-forward one.
- If `Retry-After` is absent, a flat default (`60s`) is used as the base before the
  same streak-scaling is applied.
- The final wait is capped at `max_delay`.

A non-rate-limit failure (a generic HTTP error, a network error, or an HTTP 401/403)
uses the same formula with the flat default as its base, since there is no
`Retry-After` to read.

---

## Automatic retirement

There is no probe/offline cycle: after a cooldown ends, the slot simply re-enters the
normal queue rotation, and the next request routed to it *is* the health check.

Instead, retirement is driven by quality: `should_retire` trips when an LLM's rolling
`usable_rate` (Laplace-smoothed OK fraction, see Rolling aggregates below) has at least
`min_sample_count` samples and sits below `removal_rate_floor` — a threshold distinct
from, and stricter than, the routing-only `usable_rate_floor` (which only deprioritizes
a candidate, with margin to keep it around as a last resort). When it trips, the LLM is
dropped from the pool and an alert is emitted. To restore it the operator must fix the
underlying issue and re-add it via `broker.add(cfg)`.

This check runs after every non-`OK` outcome, rate-limit or generic error alike. An
API key that is dead (HTTP 401 or 403) instead triggers immediate, unconditional
retirement — no amount of retrying fixes an invalid key, so it bypasses the quality
signal entirely.

A well-behaved daily-capped LLM (long, honored cooldowns, but successful whenever
actually tried) is never flagged for removal, however much cumulative time it spends
cooling: nothing is attempted while a slot is cooling, so an honored wait produces no
failed samples — only a call that is actually attempted and fails drags `usable_rate`
down.

Every non-`OK`, non-dead-key outcome fails over to the next available LLM instead of
raising to the caller of that one request — a generic HTTP error, a network error, and
now also a 401/403 all cool (or drop) the slot and let the router try the next LLM
within the same request. `AllLLMsFailedError` is reserved for the genuine "zero usable
models" case (see the registry/catalog docs for the keyless-pool behavior), never for
"this one LLM had a bad response".

---

## Alerts

Alerts are retrievable via `AsyncBroker.alerts()` and accumulate until fetched, then
clear. Three conditions emit alerts:

- **Auth failure** — a call returns HTTP 401 or 403. The LLM is immediately and
  permanently dropped; the alert names the `api_key_ref` to fix.
- **Retirement** — `usable_rate` drops below `removal_rate_floor` with enough samples.
  The LLM is permanently dropped and an alert is emitted.
- **Pool under-provisioned** — `NoLLMAvailableError` is raised and all LLMs in the
  pool are simultaneously non-AVAILABLE (COOLING). This alert is debounced per broker
  instance: at most one emission per 60 seconds.

---

## Rolling aggregates

Per-`(llm, operation)` rolling call windows of at most `rolling_window` entries are
maintained in memory. From each window two metrics are derived:

- **Usable rate** — Laplace-smoothed (Beta(1,1) prior) fraction of OK calls.
  Returns `None` when fewer than `min_sample_count` samples are available; the
  caller treats `None` as "insufficient data" rather than "bad".
- **Mean latency** — average latency of OK calls only; `None` when no OK call
  has been recorded.

---

## Selection policy

When the optimizer is active, slot acquisition is no longer pure round-robin.
After pool availability gating (only AVAILABLE-phase LLMs are candidates), the
selection goes through three tiers:

**Tier 1 — exploration reserve.**
A random fraction (`exploration_fraction`, default 10 %) of selections are routed
uniformly at random across all candidates, bypassing floor gating and ranking.
This is ε-greedy: without occasional exploration, data never accumulates on LLMs
that have been ranked out, making the ranking permanent regardless of whether the
LLM has recovered.

**Tier 2 — quality floor.**
Candidates with a Laplace-smoothed usable rate below `usable_rate_floor` (default
0.5) are excluded. An LLM with fewer than `min_sample_count` samples always passes
unconditionally — new LLMs must be tried before they can be judged. If all
candidates fail the floor, the floor is dropped for this selection and an alert is
emitted.

**Tier 3 — objective ranking.**
The surviving candidates are ranked by a two-element tuple. The objective depends
on whether the operation is listed in `background_operations`:

- **Background operation** — quality matters most: rank by `(-usable_rate, latency)`.
- **Interactive operation** (default) — latency matters most: rank by `(latency, -usable_rate)`.

An LLM with no OK calls receives `latency = ∞`, ranking last in interactive mode.
An LLM with fewer than `min_sample_count` samples receives a neutral prior rate of
0.5 in the ranking key.

### TPM awareness

A `max_tpm`-based ranking axis was considered and rejected: free-tier LLMs rarely
publish exact TPM limits, so the field would almost always be absent. Sustained
rate-limiting is already handled empirically by the cooldown formula and
`usable_rate`. TPM awareness can be revisited if a concrete use-case with known
limits emerges.
