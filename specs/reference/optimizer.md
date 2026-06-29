# Optimizer

The `Optimizer` is an optional component that improves pool health over time by learning
per-LLM retry delay from live traffic and by managing a full lifecycle FSM that includes
OFFLINE and PROBING phases beyond the basic AVAILABLE / COOLING pair.

Pass `optimize=True` (the default) or an explicit `Optimizer(...)` instance to
`AsyncBroker` / `Broker` to activate it.

---

## Adaptive cooldown delay

Every LLM starts with an `initial_delay`. Each rate-limit or unavailability failure
multiplies the current delay by `backoff_factor`, capped at `max_delay`. Each
successful call shrinks the delay by `decrease_factor` (floored at `initial_delay`).
The delay is per-LLM and is used as the cooldown window after a failure.

---

## Offline / Probing FSM

After `max_fail_count` consecutive rate-limit or unavailability failures on one LLM,
the LLM transitions to **OFFLINE** — its slot is removed from the pool entirely, not
just cooled.

A background probe task then waits `offline_sleep` seconds and transitions the LLM to
**PROBING** by placing exactly one slot back into the pool. The next real incoming
request that lands on that slot acts as the probe:

- **Probe succeeds** → LLM returns to AVAILABLE; delay resets to `initial_delay`;
  the consecutive probe-failure counter resets to zero.
- **Probe fails** → the consecutive probe-failure counter increments; if it reaches
  `max_probe_cycles` the LLM is **permanently retired** (see Pool retirement below);
  otherwise the LLM returns to OFFLINE and the probe task restarts.

Probing is intentionally passive: no synthetic request is sent. If there is no
incoming traffic, recovery is not needed and no probe fires.

Going OFFLINE is auto-recoverable. It does not emit an alert.

---

## Pool retirement

After `max_probe_cycles` consecutive failed probe cycles on one LLM, the LLM is
permanently dropped from the pool and an alert is emitted. The probe loop does not
restart. To restore the LLM the operator must fix the underlying issue and re-add it
via `broker.add(cfg)`.

An API key that is dead (HTTP 401 or 403) triggers immediate permanent retirement
without waiting for probe cycles — no amount of retrying will fix an invalid key.
The emitted alert names the `api_key_ref` so the operator knows which credential to
fix.

---

## Alerts

Alerts are retrievable via `AsyncBroker.alerts()` and accumulate until fetched, then
clear. Three conditions emit alerts:

- **Auth failure** — a call returns HTTP 401 or 403. The LLM is immediately and
  permanently dropped; the alert names the `api_key_ref` to fix.
- **Retirement** — `max_probe_cycles` consecutive probe failures exhaust auto-recovery.
  The LLM is permanently dropped and an alert is emitted.
- **Pool under-provisioned** — `NoLLMAvailableError` is raised and all LLMs in the
  pool are simultaneously non-AVAILABLE (OFFLINE, COOLING, or PROBING). This alert is
  debounced per broker instance: at most one emission per 60 seconds.

---

## Warm-start

On restart, if a queryable telemetry backend is configured, the optimizer reads the
last-known per-LLM status and primes the delay for any LLM that was rate-limited or
unavailable at shutdown to `max_delay` — a conservative starting point that prevents
hammering a still-unhealthy endpoint.

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
rate-limiting is already handled empirically by the circuit-breaker FSM and
`usable_rate`. TPM awareness can be revisited if a concrete use-case with known
limits emerges.
