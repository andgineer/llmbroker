# Plan — schema-constrained output, routed rather than promised

## Goal

A caller can ask for schema-constrained output, and the pool answers from a model
that accepts the request. A model that refuses it is remembered as refusing *that
request*, not as broken, and the pool keeps serving.

What this is not, and the boundary is the feature: **the broker routes, it does
not guarantee conformance.** A model may take the parameter and ignore it, and no
router can detect that without knowing the caller's schema better than the caller
does. The host still validates what it gets. A caller told otherwise would drop
its own validation and be wrong roughly as often as the pool is weak.

## Why this is not the failure mode the queue warns against

[`README.md`](README.md) names the check: a mechanism sized for a problem this
pool does not have. Both dropped plans were runtime machinery built for
onboarding or for reordering five rows.

The distinction here is that **the hard half already exists**. Deciding whether a
given model can serve a given request is what this library is; a single-provider
client has nothing to decide and only forwards the parameter. What the feature
needs beyond the parameter is one more per-model fact, and the facts machinery is
built: `catalog.py` holds the disabled map and resyncs it wholesale,
`learning.py` records quality per (model, operation), cooldowns already honor
`Retry-After`. A capability is a coarser and far more stable fact than quality,
which is already learned there.

It also lands inside the mission's own rule rather than beside it: *failover is
what makes coarseness safe — the price of a stale fact is one wasted call, paid
once by one process, and the caller never sees it*. A model that refuses the
parameter refuses it with an error, on the first call, exactly like a dead key.
That is the whole learning mechanism, and it is the reason **no static capability
table is in this plan**. A table would be administration, it would rot at the
speed of the free rosters, and it would contradict requirement 2.

The pooled endpoints are OpenAI-compatible by decision
([`../reference/mission.md`](../reference/mission.md)), so this is one parameter
of one shape passed through, not a set of provider dialects.

## Step one is a probe, and it may end the plan

Before any `src/` change: measure how much of the current pool accepts the
parameter, and of those, how many honor it. A handful of calls per model over the
curated preset, asking for a small schema and checking the answer against it.

Three outcomes, and two of them stop here:

- **Almost none accept it.** The plan is dropped and the measurement goes into
  [`../reference/freetier-providers.md`](../reference/freetier-providers.md),
  where knowledge about the free tier already lives. That is a real result: it
  tells every caller not to build on this.
- **Many accept and quietly ignore it.** Also a stop, and a worse one — the
  parameter would buy a promise the pool does not keep. Record it in the same
  place.
- **A usable fraction accepts and honors it.** Build what follows.

The free tier is exactly where support for this is thinnest, so the honest prior
is that the probe decides the plan rather than confirming it. Running the probe
is most of the value of this plan even if nothing is built.

## What to build, if the probe passes

1. **The parameter, passed through.** From the caller's call down to
   `build_chat_request`. Nothing interprets it; the broker does not construct,
   validate or rewrite a schema.
2. **One learned fact: this model refuses this request.** Beside the disabled
   map, on the same coarse resync. It disables the *parameter for that model*,
   never the model — a model that cannot serve a constrained request is still the
   best answer to an unconstrained one, and conflating the two would shrink the
   pool for every other caller.
3. **Preference in the pool order, not a filter.** When the request carries a
   schema, models known to accept it sort first. A hard filter empties the pool
   the moment nobody supports it, which is the state the probe may well find; a
   preference degrades into ordinary routing instead.

## What stays out

- A static capability table, in any form, seeded from anywhere.
- Provider dialects that are not OpenAI-shaped — the pool's boundary already
  excludes them.
- Validating the answer against the schema, or retrying on non-conformance. That
  is the host's business and the host is the only party holding the schema's
  meaning.
- A second call, a repair pass, or any promise about the content of the answer.
- Extending this into a general capability system for tools, vision or context
  length. One fact, learned the way the others are. A capability *framework* is
  the failure mode this queue warns about; a capability *fact* is not.

## Tests

The refusal path is the one worth pinning: a model whose endpoint rejects the
parameter is retried on the next model within the same call, the fact is recorded
against that model alone, and a later unconstrained call still reaches it. Plus
the ordinary pass-through assertion, and the degenerate case — a constrained
request when no model is known to accept it still gets an answer.

## What moves into the specs

The routing-not-conformance boundary belongs in
[`../reference/mission.md`](../reference/mission.md#what-it-is-not); it is a
decision, and it will be re-proposed as a gap otherwise. The probe's numbers
belong in [`../reference/freetier-providers.md`](../reference/freetier-providers.md)
whichever way they fall.

## Gate

`invoke pre` and the full suite green, as ever. The probe's raw numbers are
recorded before the first `src/` line is written, so a reader can tell whether
the feature was built on evidence or on the expectation of it.
