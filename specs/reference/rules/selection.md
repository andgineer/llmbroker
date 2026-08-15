# Selection

Which model a call gets, the two mechanisms that move it — cooldown
(availability) and quality demotion (ordering) — and whether enough of the pool
is left to fail over at all. What happens once a model is picked is
[`call-path.md`](call-path.md). The cross-cutting rules this file
elaborates are in [`../invariants.md`](../invariants.md).

The `Optimizer` computes both. `optimize=True` (the default) activates it; an
explicit instance tunes it. Its lifecycle has exactly two phases, AVAILABLE and
COOLING, always derived from the recorded cooldown instant versus now.

## Cooldown: trust the provider

On a 429/503 the wait is computed from *that response's own* signal:

- `Retry-After` (seconds or an HTTP-date), when the provider sends one, is used
  as-is on the first failure of a streak.
- Mid-streak (no success since the last 429/503), the number is scaled
  exponentially in the streak length. A success resets the streak to zero.
- With no `Retry-After`, a flat base is used before the same streak scaling.
- The final wait is capped.

A generic 5xx, a transport failure, or an HTTP 200 that is not a chat completion
uses the same formula with the flat base, there being no `Retry-After` to read.
The streak follows the cooldown exactly: a failure that did not cool the model
does not advance its backoff exponent either.

An HTTP 401/403 is a dead key, and it withdraws the *key*, not the model: the ref
stops being payable in the ring that handed the value over, immediately and
unconditionally — no amount of retrying fixes an invalid key — logged at error
level naming the `api_key_ref`. The model is not cooled, since nothing is wrong
with it and cooling it would take it from every other caller. The withdrawal lasts
until the pool is rebuilt, which re-reads the ref and takes it back if a working
value has appeared. Which callers it reaches is in
[`backends.md`](backends.md#per-user-scoping).

Nothing about availability is on the journal tail and no failure forces a read of
its own: a cooldown and a rejected key belong to the process that found them, are
written to no row, and are read back from none (invariant 8's other half — quality
is the only thing derived from the journal).

**A cooldown belongs to the process that met it** (invariant 11). It is held in
memory, written to no row, and no process reads another's
([`decisions.md`](../decisions.md#availability-is-not-shared)). Two entries
resolving to the same key value share a quota and therefore a 429: what a rate
limit withdraws is the key's capacity, not one endpoint's, so the cooldown
applies to every entry this process is paying for with that value. A 5xx is
provider-side and withdraws the entry it came from.

The price is that each process pays its own first failing call against a model
another has already found to be cooling — one wasted call, spilled over
transparently, never seen by the caller. Nothing about availability survives a
restart either, and nothing needs to: a cooldown carries the instant it expires,
so a fresh process is at worst optimistic for one call.

## Quality demotion

Per `(model, operation)`, a window of the most recently rated calls is kept — one
entry per call, so re-rating one replaces its verdict rather than adding a second
([`decisions.md`](../decisions.md#a-rating-names-the-call-it-rates)). A bucket is
**demoted** iff it holds enough of them and their Wilson-score upper bound sits
below a floor — demote only when even the optimistic estimate is below it.
Unlabelled calls fall into their own bucket.

```python
reply = await llms.ask("Summarize this contract clause", operation="summarize")
reply.record_quality(0.9)  # rated on the "summarize" bucket specifically
```

A rating may arrive at any time after the call, not only through the live result
while the host still holds it: a rating names the call it rates, and it lands for
as long as the journal still holds that call — counting toward the model and the
operation read off it. Past retention there is no call left to name.

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
the curated list does not carry starts below every curated one without needing
a rule of its own.

**The weight decides where a model starts, never where it stays.** Evidence
displaces it by shrinkage rather than by a threshold — the weight fades as the
window fills, monotonically, and a full window leaves it contributing nothing.
So ratings reorder the pool freely against the curated order: no curated
position is beyond overturning, and a model rated badly enough measures down to
the bottom of the scale however high it was placed.

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

The expiry is journaled as the budget it missed, and the bound is derived from
the journal tail with everything else llmbroker learns. A fresh expiry may raise
the bound; two things retire it, and both are needed. A model's own success
retires every miss older than it. A window on the clock retires the rest —
because a model kept out of first place produces no successful rows either, so
success alone would let one observation invert the curated order indefinitely.
Unlike a cooldown, which carries the instant it expires and so heals on its own,
a recorded miss states only what happened; the window is what gives it an end.
Four properties keep this from becoming a penalty in disguise:

- **It is budget-relative.** A caller with a larger budget, or none, ignores the
  bound entirely, so the signal can reorder a pool but never overturn one: when
  nobody can meet a budget, every candidate carries a bound and the curated
  ranking stands.
- **It never withdraws a model.** A bounded model is still selected when it is
  the last candidate standing — exactly when a caller would rather have a slow
  answer than none.
- **It applies the moment the miss is observed, from the call itself**, not at
  the next rebuild and not only where learning is switched on. Rebuilds are rare,
  so every caller until the next one would otherwise walk into the same hang —
  the one thing this signal exists to prevent.
- **It is the one routing signal that does survive on the journal**, unlike a
  cooldown, because it is evidence about answers rather than about availability:
  it only ever reorders, never withdraws, so a bound that does not hold on this
  node costs one reordering. Latency belongs to a node's egress, region and
  resolver, which makes another node's miss weaker evidence than this node's own;
  it is admitted anyway, because partitioning it would put a node identity in the
  journal that nothing else needs.
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
  grows with the streak and resets on the next success, so a degrading model is
  withdrawn for longer and a recovering one returns at once.
- **Quality** is host-driven, sticky, and orders rather than excludes.

Availability never feeds ranking, and nothing auto-generated ever enters the
quality window — see invariant 5. That is what makes demotion safe without a
time-based recovery.

## Visibility

There is no alerts API and no status enum. The few human-actionable events —
dead key, a demotion flip, an under-provisioned pool (every keyed model COOLING
at once, debounced) — are log lines.
`snapshot()` serves each model's raw facts and metrics; the host derives
whatever presentation it wants.

## Pool health

**The measure is the provider, not the entry.** Of the distinct `api_key_ref`s
among managed entries, how many have a key: `providers_usable` of
`providers_total`. Two entries on one ref are one quota and one failure domain,
so they count once.

**One usable provider is degraded**: a single quota with nothing to fail over
to, which is the failover feature's own definition rather than a tuning knob.
Zero is a dead pool. Missing keys are never an alarm on their own — two
providers may be all a host wants.

A registry that pools nothing is not a degraded pool but the absence of one: a
host whose entries are all its own asked for no failover and is told nothing
about it. That shape is ordinary — a broker that only reaches declared paid
models has exactly it.

**A key missing on a model reached only by name is reported apart from the
pool's.** Such a model is never routed, so it can neither degrade the pool nor
be repaired by it, and folding it in would make "degraded" mean two things. It
still has to be visible — a host cannot be expected to discover by a failed call
that its paid model has no key — so it is its own list on the snapshot, named by
the handle the caller passes to `direct()` rather than by a resolved name
carrying a model version the caller never typed.

**Where a key comes from is data, and it travels with whatever knows it.** The
registry's own key help wins — a host that wrote a hint meant it — and the paid
catalog's is the fallback, carried out of the resolution because nothing stores
a declared model and no later read could recover it. That help reaches the
snapshot, the sync report, the `direct()` error, and one log line the first time
a ref turns up missing. The log is deduplicated on the set of missing refs
rather than on a clock: a rebuild can fire on every exhausted call, and a key
that stays missing must not fill the log.

### The alarm

It rides the pool rebuild, so all four triggers carry it in one place
([`model-list.md`](model-list.md)): `ERROR` on the transition into one usable
provider ("no failover left") and into zero ("cannot serve any request"), naming
the missing refs; one `INFO` on the way back. This is the only way an installation
learns that a curated removal cost it its last provider, which is what the mirror
rule leans on ([`model-list.md`](model-list.md)).

These are transitions of *state*, not of severity — both are errors, and losing
the step between them would mute the moment the pool stops answering at all.
Every count that is not degraded is one state, so a healthy log carries none of
these lines, gaining a further provider is not news, and a broken pool carries
exactly one line per change.

**The measure is key presence, and it follows the last rebuild.** Keys are read
afresh there, so a ref that has stopped resolving leaves the count at that rebuild
rather than keeping the value it had. The counts and the per-model `has_key` come
from the same measurement and therefore always agree.

**What counts is a key this installation holds for anybody**, the shared one or one
belonging to a single caller — read from the store rather than declared to it
([`decisions.md`](../decisions.md#a-key-is-found-not-declared)). Which caller is not the measure's business; that a key
is here, is. An installation that gives every user a key of its own and keeps no
shared one answers every request it is handed, so a measure seeing only the shared
value would report it dead — and then never move again, taking the removal alarm
with it, which is the safety net the mirror rule is paid for with
([`decisions.md`](../decisions.md#a-sync-mirrors-what-a-sync-wrote)). The keys held
are read from the one listing a rebuild already makes; where the secrets store
cannot list what it holds, only the shared value is visible and the measure narrows
to it ([`backends.md`](backends.md)).

An administratively disabled entry still counts its provider: the alarm reports the
keys an installation holds, not verdicts the host set itself and already reads per
model.

### One measurement, two consumers

`snapshot()` carries the same numbers the alarm uses — the per-LLM mapping, the
counts, the missing keys with their help text, and the same `degraded`
predicate. An admin UI needs one call, and the log and the UI cannot diverge.

The help text is read from the registry only when a key is actually missing, so
a fully-keyed pool adds no registry I/O to a rebuild at all, and `snapshot()`
never performs any; a registry without key metadata yields empty help but
correct refs and names.

`snapshot()` is a view of the *live pool*, so it provisions — unlike a journal
read, which never does (invariant 6).
