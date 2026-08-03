# Selection

Which model a call gets, and the two mechanisms that move it: cooldown
(availability) and quality demotion (ordering). What happens once a model is
picked is [`call-path.md`](call-path.md). The cross-cutting rules this file
elaborates are in [`../invariants.md`](../invariants.md).

The `Optimizer` computes both. `optimize=True` (the default) activates it; an
explicit instance tunes it. Its lifecycle has exactly two phases, AVAILABLE and
COOLING, always derived from `cooldown_until` versus now.

## Cooldown: trust the provider

On a 429/503 the wait is computed from *that response's own* signal:

- `Retry-After` (seconds or an HTTP-date), when the provider sends one, is used
  as-is on the first failure of a streak.
- Mid-streak (no success since the last 429/503), the number is scaled by
  `backoff_factor ** consecutive_fails`. A success resets the streak to zero.
- With no `Retry-After`, a flat base is used before the same streak scaling.
- The final wait is capped at `max_delay`.

A generic 5xx, a transport failure, or an HTTP 200 that is not a chat completion
uses the same formula with the flat base, there being no `Retry-After` to read.
The streak follows the cooldown exactly: a failure that did not cool the model
does not advance its backoff exponent either.

An HTTP 401/403 is a dead key: the model drops from the pool immediately and
unconditionally — no amount of retrying fixes an invalid key — logged at error
level naming the `api_key_ref`. The drop holds as long as journal rows carrying
that key digest remain inside the rebuild tail; replacing the secret resolves to
a different digest, the old rows stop matching, and the model revives on a
following rebuild.

**Sharing across instances.** Every failed call journals `cooldown_until` and
`key_hash` (a short digest of the resolved key value) on its row. The debounced
tail read applies the newest `cooldown_until` per model to every instance's
pool, and is forced out of turn by the instance's own failures. A 5xx cooldown
applies unconditionally — it is provider-side and shared by everyone. A 429 or
401/403 cooldown applies only where `key_hash` matches this instance's own
resolved key for that model: quota belongs to the key, so a shared key shares
its cooldown and a personal key cools only its owner.

Coordination is advisory. Correctness comes from failover; the cost of
staleness is one wasted roundtrip with a transparent spillover, and a stateless
process starts informed from its first journal read.

## Quality demotion

Per `(model, operation)`, a window of the last `quality_window` ratings is kept.
A bucket is **demoted** iff it holds at least `quality_min_count` ratings and
their Wilson-score upper bound sits below `quality_floor`. Calls made without
`operation=` fall into the `None` bucket.

```python
reply = await llms.ask("Summarize this contract clause", operation="summarize")
reply.record_quality(0.9)  # rated on the "summarize" bucket specifically
```

A rating may arrive at any time after the call, not only through the live result
while the host still holds it: a host that persists the rating identity can
record the verdict days or months later and it lands on the same bucket.
Self-contained quality records are what makes an arbitrarily late rating safe,
since retention may already have purged the original call row.

There is no global verdict — demotion is always per `(model, operation)`.
Recovery is exactly: new ratings that push the window's bound back above the
floor, or last-resort traffic when nothing else is available. There is no
time-based recovery, no probation traffic and no quality reset. A flip logs a
warning (demoted) or info (cleared) naming the model, operation and bound.

Own ratings apply to the in-memory window immediately, before any rebuild.

## Order of acquisition

Slot acquisition sorts on one key, in this precedence:

1. a slot that recently failed to answer within a budget as small as the one on
   offer sorts last among its peers;
2. a slot quality-demoted for the requested operation sorts after every
   non-demoted slot;
3. the higher priority wins;
4. as a tiebreaker keeping the choice deterministic, the entry's registry/preset
   position.

**Priority is the entry's curated weight, displaced by observed host ratings as
they accumulate.** The weight is a prior on the rating the entry is expected to
earn, on the same `0..1` scale as a rating, defaulting to `0.0` — so an entry
the curated lineup does not carry starts below every curated one without needing
a rule of its own.

**The weight decides where a model starts, never where it stays.** Evidence
displaces it by shrinkage rather than by a threshold: with no ratings the
priority is exactly the weight; the weight is worth `prior_strength`
pseudo-ratings against an empty window and proportionally fewer as the window
fills; a full window leaves the priority equal to the observed mean, with the
weight contributing nothing. Ratings can therefore reorder the pool freely
against the curated starting order — no curated position is beyond overturning,
and a model rated badly enough measures down to the bottom of the scale however
high it was placed. The fade is monotone: each rating moves the priority toward
the window's mean and never back.

Demotion is soft. A demoted slot with no alternative is still acquired, and so
is a slot whose priority has collapsed: priority orders the pool, it never
withdraws from it. `parallel` caps simultaneous in-flight requests per slot;
parallel requests to one model are allowed by default, and `parallel = 1`
serializes them.

**The bound must not be used for ranking, and the blend must not be used for
demotion** — the two answer different questions
([`decisions.md`](../decisions.md#blend-for-ranking)).

## A budget expiry teaches ordering

An expired `wait` never cools a model (see [`call-path.md`](call-path.md)), but
it is evidence, and the only evidence obtainable: a model that never answers
produces no successful rows, so its latency cannot be measured any other way.
What the expiry proves is a lower bound — "this one did not answer within X
seconds" — and that is enough to stop handing it to the next caller whose budget
is no larger, so a hung endpoint costs one caller rather than all of them.

A fresh expiry extends the window and may raise the bound; a window allowed to
lapse retires the bound with it, so stale evidence is never the floor a later,
smaller miss builds on; a successful answer erases it outright. Four properties
keep this from becoming a penalty in disguise:

- **It is budget-relative.** A caller with a larger budget, or none, ignores the
  bound entirely, so the signal can reorder a pool but never overturn one: when
  nobody can meet a budget, every candidate carries a bound and the curated
  ranking stands.
- **It never withdraws a model.** A bounded model is still selected when it is
  the last candidate standing — exactly when a caller would rather have a slow
  answer than none.
- **It is node-local**, because latency is a property of the *path* — this
  node's egress, region, resolver. A cooldown is shared precisely because a
  quota is a property of the *key*, which genuinely is shared.
- **It is one signal for both routing paths, deliberately approximate.** A
  stream contributes the budget it missed reaching the first delta, a completion
  the budget it missed answering in full, and neither is scaled. Ordering is all
  it can affect, so a second signal with its own window would cost more on the
  acquisition path than the sharper bound is worth.

An expiry that fired before the attempt reached the provider teaches nothing:
the model never got a chance, and recording it would blame the model for the
caller's clock.

## The two axes, and the invariant that keeps them apart

- **Cooldown** is provider-driven, self-healing, and a hard exclusion: the wait
  scales with the streak and resets on the next success, so a degrading model is
  withdrawn for longer and a recovering one returns at once.
- **Quality** is host-driven, sticky, and orders rather than excludes.

Availability never feeds ranking, and nothing auto-generated ever enters the
quality window — see invariant 5. That is what makes demotion safe without a
time-based recovery.

## Visibility

There is no alerts API and no status enum. The few human-actionable events —
dead key, a demotion flip, an under-provisioned pool (every keyed model COOLING
at once, debounced) — are log lines on the `llmbroker.broker` logger.
`snapshot()` serves each model's raw facts and metrics; the host derives
whatever presentation it wants.
