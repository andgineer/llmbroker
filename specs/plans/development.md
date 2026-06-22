# llmbroker — development plan

## Goal

`llmbroker` is a standalone, host-agnostic LLM-provider broker. It provides one
thing: LLM access over a *pool of configured endpoints* (each a
`(base_url, model, api_key)` triple), rotating away from ones that are momentarily
unavailable (429/503), and accumulating enough signal to decide which to drop or add.

Three design targets:

- **Dead-simple typical use.** Fetch a curated pool file, put keys in env vars,
  write one constructor line. A typical host writes **no integration code** and
  **never puts a secret in source**.
- **Full universality.** Any storage, any secret backend, single-process or
  clustered — each is a shipped *battery*; the rare host with a non-standard
  requirement implements one small backend.
- **Self-tuning.** A background optimizer reads telemetry (per LLM *and per
  operation*) and **acts**: adjusts cooldowns, offlines and re-probes bad LLMs,
  routes each operation to the LLMs that empirically handle it best. A human is
  interrupted only by what only a human can fix (pool under-provisioned, API key
  dead).

---

## Status

Phases 1–2 shipped (broker core, file/sqlite batteries, telemetry, secrets, seeding,
the sync wrapper, the host-coexistence surface, the `state_store` seam +
single-machine `llmbroker.sqlite.StateStore`, per-user scoping, the `preset` CLI
command). The implemented surface lives in
[`specs/reference/architecture.md`](../reference/architecture.md). **Everything below
is remaining work only.**

Invariants for every phase: zero host-specific imports, every DB object
`llmbroker_`-prefixed, `ensure_schema` as sole schema owner, the Alembic coexistence
hook. Any coupling to a specific host application is a defect.

---

## Phase 3 — cross-node state + DB/secret batteries

New batteries, each behind its own optional dependency extra
(`llmbroker[redis]`, `llmbroker[postgres]`, …). Each driver is its own subpackage
(`llmbroker.postgres`, …), following the template `llmbroker.sqlite` sets: implement the
contracts from `llmbroker.protocols`, and import the driver only inside that subpackage so
a bare `import llmbroker` stays driver-free.

- **Cross-node `StateStore`:** `llmbroker.redis`/`postgres`/`mongodb` `.StateStore`.
  The single-machine `sqlite.StateStore` already ships; these extend the same
  `read`/`write` protocol to a cluster (`read()` returns the whole stored state in one
  round-trip).
- **`llmbroker.postgres`/`mongodb` `.Registry`** (with the admin CRUD of
  `MutableRegistryProtocol`) **and `.Telemetry`** (queryable read surface).
- **`llmbroker.aws`/`vault` `.Secrets`** (`MutableSecretsProtocol`, backed by AWS
  Secrets Manager / HashiCorp Vault KV).

### Share cooldown state at LLM-selection time

**The problem.** The "selection point" is the code run on every `ask`/`chat`: the
broker choosing which LLM serves this call. It is the hottest, most latency-sensitive
path. Today the broker reads the store **only** when reporting (`snapshot()`/`state()`)
and writes it **only** when an LLM cools down after a 429/503. At the selection point
it consults its own in-process state, never the store. So with more than one process
(several workers or a cluster), when process A learns an LLM is rate-limited and
cooling, process B does not see it — B picks that same LLM and earns another needless
429.

**What to build.** Make a process consult the store's shared state **at the selection
point**, so "this LLM is currently unavailable" reaches every process at the moment it
matters. Reads happen **on demand** (only at selection), never on a background timer:

- **Read at selection, but cached briefly.** When the broker is about to pick an LLM it
  reads the shared state, with a short cache lifetime so a burst of calls coalesces into
  one `read()` instead of one per call. An idle process makes **zero** store reads —
  cost scales with traffic, not with wall-clock time.
- **Write only on a real state change** (the 429/503 write-through is already wired). No
  other writes.
- **No polling for expiry.** Each process computes the end of a cooldown itself from the
  stored `cooldown_until`, so there is nothing to poll and no background loop.
- **Small races are acceptable.** Two processes may briefly both think an LLM is free;
  the cost is at most one redundant 429. There is no user-facing `refresh()`.
- Pushing changes proactively (e.g. redis pub/sub) is a later optimization, not part of
  this phase.

---

## Phase 4 — the `Optimizer` (autonomous control loop)

The core value. The knob already exists (`optimize: bool | Optimizer = True`,
`True` ≡ `Optimizer(judge_fraction=0.0)`) and today runs nothing — the broker is
reactive (round-robin + 429/503 cooldown). P4 builds the background loop behind that
knob with **no API change**.

**How it learns.** Off the **live event stream** — every `Telemetry.record(call)`
updates rolling per-(llm, operation) aggregates in memory (the Optimizer interposes at
the `record()` seam, e.g. a `Telemetry` decorator), so it runs on **any** backend and
boots **cold** on `Telemetry()`/`NoTelemetry()`. A **queryable** backend warm-starts
those aggregates after a restart and enables ad-hoc analysis; decide warm-start vs
cold-boot with `isinstance(telemetry, QueryableTelemetryProtocol)`, never `hasattr`.
Whether to checkpoint the projection to its own table or recompute from the journal on
start is an open question; either way the projection is **never** written back into the
append-only `llmbroker_calls`.

**Parameter tuning** — per-LLM cooldown/delay FSM:

| Current state | Event       | New state | Delay adjustment            |
|---------------|-------------|-----------|-----------------------------|
| Available     | Error 429   | Cooling   | `current_delay` (up to Max)  |
| Cooling       | Success     | Available | Decrease delay              |
| Cooling       | Fail @ Max  | Offline   | Start Offline Sleep / Alarm  |
| Offline       | Sleep End   | Probing   | Send test request           |
| Probing       | Success     | Available | Reset to Initial Delay      |
| Probing       | Failure     | Offline   | Restart Sleep / Alarm        |

**Operation routing** — bias selection of each `operation` toward the LLMs that
empirically handle it best, via a pluggable **selection policy** seam on the broker
(default round-robin; the Optimizer swaps in the ranking it maintains). The policy is
**tiered / lexicographic, not a weighted-sum scalar** (the terms are not commensurable;
a latency win must never "buy back" a quality loss):

1. **Availability gate** — candidates are LLMs not in cooldown (the FSM already drops
   Cooling/Offline); residual flakiness is a soft tiebreak.
2. **Quality floor gate** — drop LLMs whose per-`operation` usable-rate is below a
   floor. Quality is a gate, not a tradeable term.
3. **Objective ranking — the objective lives with the `operation`.** A background batch
   type (e.g. `receipt_classification`) ranks the gated set by quality; an interactive
   type ranks by latency. There is no single global weighting right for both.
4. **Tokens = a budget constraint, not a quality axis.** What matters is rate-limit
   budget (TPM) → throughput headroom and `$` when paid tiers are mixed. Tokens break
   ties / enforce a budget; they never trade against quality.

Estimates are **confidence-aware** (bandit-style): a minimum sample count before an
LLM's stats override round-robin, an exploration reserve so deprioritized LLMs keep
being sampled, and a Bayesian usable-rate for the **sparse** quality signal. Concrete
thresholds and the bandit flavor are open; the tiering and the per-`operation`-objective
principle are the decided shape. Selection strategy: first 0-wait LLM, else minimal
remaining wait — biased by the ranking.

**Pool hygiene** — automatically deprioritize/retire consistently-useless LLMs.

**The only thing surfaced to a human** is what a human alone can fix: `await
llms.alerts()` (empty when `optimize=False`) returns the rare actionable items — *the
whole pool is under-provisioned for your request rate*, *this API key looks dead* — not
a feed of trivia about individual free LLMs.

---

## Phase 5 — LLM-in-the-loop deepening (future, not scheduled here)

The Optimizer's *optional* use of an LLM, enabled only by
`optimize=Optimizer(judge_fraction>0)`:

- **Quality judging** — sample that fraction of outputs per (llm, operation) and score
  them with an LLM-as-judge, closing the quality loop *without* the host calling
  `record_quality()`. The judge call goes through the broker itself (dogfooding) under a
  low-priority `operation` and degrades gracefully if no LLM is free.
- **Ambiguous tuning/routing judgement** when threshold rules are inconclusive.

Plus richer fail statistics (API-key-expiration diagnostics) and per-LLM
Initial/Min/Max delay tuning.
