# Optimizer

The `Optimizer` is an optional component that computes cooldowns from each
failure's own signal and derives per-operation quality demotions from rated
scores. There are only two lifecycle phases, AVAILABLE and COOLING, always
derived from `cooldown_until` vs. now. Quality demotion is a separate, softer
axis: it reorders selection, it never excludes.

Pass `optimize=True` (the default) or an explicit `Optimizer(...)` instance to
`AsyncBroker` / `Broker` to activate it.

---

## Cooldown: trust the provider

On a 429/503, the wait is computed from *that response's own* signal:

- `Retry-After` (seconds, or an HTTP-date), if the provider sends one, is used
  as-is on the first failure of a streak.
- If this LLM is already mid-failure-streak (no success since its last
  429/503), the number is scaled by `backoff_factor ** consecutive_fails`
  (default `backoff_factor = 2.0`). A success resets the streak to zero.
- With no `Retry-After`, a flat `60s` base is used before the same
  streak-scaling.
- The final wait is capped at `max_delay` (default `3600s`).

A generic 5xx or network failure uses the same formula with the flat base,
since there is no `Retry-After` to read. A client-side request error (any
4xx other than 429/401/403) never cools the model down and never counts
toward its failure streak — it is excluded for the rest of the current call
only, so a different request may use it again immediately. An HTTP 401/403
(dead key) instead drops the LLM from the pool immediately and
unconditionally — no amount of retrying fixes an invalid key — logged at
`logger.error` naming the `api_key_ref`. The drop holds as long as journal
rows carrying that key digest remain inside the rebuild tail; replacing the
secret resolves to a different digest, the old 401/403 rows stop matching, and
the model revives on a following rebuild.

**Sharing across instances.** There is no state store: every failed call
journals `cooldown_until` and `key_hash` (a short digest of the resolved key
value) on its row. A debounced tail read (below) applies the newest
`cooldown_until` per model to every instance's pool, and forces an
out-of-band read on the instance's own failures. A 5xx cooldown applies
unconditionally (provider-side, shared by everyone); a 429 or a 401/403
cooldown applies only where `key_hash` matches the instance's own resolved
key for that model (quota belongs to the key — a shared key shares its
cooldown, a personal key cools only its owner).

---

## Scoping

The registry and everything the optimizer learns are always global — one
model list, one set of quality windows and cooldowns, shared by every scope.
`scope` is an opaque string the broker turns into a secret-ref prefix (its
own key, falling back to the shared ref) and a journal attribution field
(`Call.scope`, filtered by `calls(scope=...)`); no typed user concept exists
in storage or protocols.

---

## Quality demotion

Per `(model, operation)`, the optimizer keeps a window of the last
`quality_window` ratings (default 30). A bucket is **demoted** iff it holds
at least `quality_min_count` ratings (default 10) and their Wilson-score
upper bound (confidence `quality_confidence`, default 0.95) sits below
`quality_floor` (default 0.3). Calls made without `operation=` fall into the
`None` bucket.

```python
reply = await llms.ask("Summarize this contract clause", operation="summarize")
reply.record_quality(0.9)  # rated on the "summarize" bucket specifically
```

A rating may arrive at any time after the call, not only through the live
result while the host still holds it: the host that persists the rating
identity can record the verdict days or months later, and it lands on the same
`(model, operation)` bucket. Self-contained quality records — never joined
against the call they rate — are what makes an arbitrarily late rating safe,
since retention may already have purged the original call row.

There is no global verdict — demotion is always per `(model, operation)`.
Recovery is exactly: new ratings that push the window's bound back above the
floor, or last-resort traffic when nothing else is available — there is no
time-based recovery, no probation traffic, and no quality reset. A flip
(demoted ⟷ not demoted) logs a warning (demoted) or info (cleared) line
naming the model, operation, and bound.

---

## Derived state: no second storage subsystem

The call journal is the only place llmbroker keeps state beyond the static
registry, and it is strictly append-only in every backend — `record_quality`
appends its own self-contained record (`llm_name`, `operation`, `score`,
optional `call_id` as an opaque host-UI passthrough, never joined against a
call row) rather than updating an existing row.

A debounced read of the most recent records (`quality_rebuild_limit`,
default 300; at most once every 60 seconds, forced on a failure) re-derives
the state below. The tail is shared across all models and operations — a
chatty model can crowd a quiet model's ratings out of the last
`quality_rebuild_limit` records; this is an accepted consequence, and the
limit is the tuning knob. The same read re-derives:
quality-window verdicts, shared cooldowns (above), snapshot metrics, pool
membership (re-reads the registry), and the admin disabled-verdict map — so
edits from another process or node reach a running broker without a
restart. Own ratings apply to the in-memory window immediately, before any
rebuild.

Persistence is `store/` by default (a day-split JSON-lines journal plus a
YAML disabled-verdict document); an explicit in-memory opt-out degrades to
session-scoped learning. The journal forgets via retention — every backend
self-purges records older than its `retention` horizon (default 90 days).

The admin disabled-verdict map (`set_disabled(name, flag)`) is the one
**excluding** verdict, orthogonal to quality demotion: values are written
only by that call or by hand; llmbroker only seeds missing names (with
`disabled: false`) at `sync` or provisioning. It is read via the `get(name)`
handle's `disabled` property and via `snapshot()`. Nothing but `sync` writes
the registry.

---

## Seeding

The preset file is the only source of model definitions. `sync(preset)`
mirrors it into the registry — add new entries, update existing ones, delete
entries absent from the preset — and preserves the disabled map (sync only
seeds missing names, never changes existing values). There is no model CRUD;
provisioning against an empty registry fails fast, telling the caller to
call `sync(preset)` first.

---

## Selection

Slot acquisition sorts on one key: a slot quality-demoted for the requested
operation sorts after every non-demoted slot; among slots with the same
demotion verdict, curated priority wins (registry/preset position — lower is
better). Demotion is soft — a demoted slot with no alternative is still
acquired. `LLMConfig.parallel` caps simultaneous in-flight requests per slot.

---

## Visibility

There is no alerts API. The few human-actionable events — dead key, quality
demotion flip, an under-provisioned pool (all keyed models COOLING
simultaneously, debounced to once per 60 seconds) — are log lines on the
`llmbroker.broker` logger.

There is no status enum. `snapshot()` serves each model's raw facts —
`disabled`, `has_key`, `cooldown_until`, `demoted_operations` — and its
metrics (call count, last status, last call time), served from the rebuild's
cached tail with zero DB reads. The host derives whatever presentation it
wants. The DB schema is not a public contract; a host that queries
`llmbroker_calls` directly does so at its own risk.
