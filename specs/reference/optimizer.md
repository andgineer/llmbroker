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

- **Probe succeeds** → LLM returns to AVAILABLE; delay resets to `initial_delay`.
- **Probe fails** → LLM returns to OFFLINE immediately (the probe start primes the
  failure count so one failure is enough); the probe task restarts.

Probing is intentionally passive: no synthetic request is sent. If there is no
incoming traffic, recovery is not needed and no probe fires.

---

## Alerts

When an LLM transitions to OFFLINE the optimizer emits an alert (retrievable via
`AsyncBroker.alerts()`). Alerts accumulate until fetched, then clear.

---

## Warm-start

On restart, if a queryable telemetry backend is configured, the optimizer reads the
last-known per-LLM status and primes the delay for any LLM that was rate-limited or
unavailable at shutdown to `max_delay` — a conservative starting point that prevents
hammering a still-unhealthy endpoint.

---

## Rolling aggregates

Per-`(llm, operation)` rolling call windows are tracked internally and reserved for
future phases (quality judging, tiered routing). They have no effect on current
routing decisions.
