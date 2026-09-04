# Plan — what a caller can send, and the models it can name

**Status: items 1 and 6 are functional only** — they state what a host can do and
what the evidence is, and the binding to a module, a signature and a test is the
next step, before which no `src/` line is written. Item 7 arrives with a probe of
its own, which may end it.

**Queue gate:** do that binding only after `pool-tight-budget.md` has an
implementation handover. Re-read the request path then; do not hand this file to
an implementation executor in its present state. The first executable action is
item 7's probe, because its result decides whether the shared request seam is
opened for direct calls only or for both direct and routed calls.

## Goal

Three ways a caller reaches a particular model and tells it how to answer, all on
the same path through the request. A host cannot pass a parameter to a model it
named and paid for, cannot name a model the curated catalog does not carry, and
cannot ask the pool for schema-constrained output. The first two are a request
body with four keys and no way to add a fifth, and a curated data file with no
reader. The third is the same parameter passing, routed rather than forwarded
because the pool is heterogeneous.

They are one plan because they are one path opened three times. Split apart, the
same request builder is reviewed in three separate diffs.

## Why this is not the failure mode the queue warns against

[`README.md`](README.md) names the check: a mechanism sized for a problem this
pool does not have. Items 1 and 6 fail it in the opposite direction — the request
body is already built in one place, the catalog is already a curated file the CLI
prints and a program cannot read. Item 7 carries its own argument and its own
probe below.

**What one host wrote instead:** its benchmark harness reads the preset TOML files
out of the installed package by path, parses them itself to recover a provider's
base url and key reference, and imports the env-file parser out of a standalone
submodule. That is item 6 being absent, and the next host writes it again.

## The evidence base

The numbers below come from one host — a streaming, interactive vocabulary tool on
the free pool with an opt-in paid step — over its benchmark tiers: a 120-request
burst run at two caller budgets across three source languages; a 179-fixture
comparison of the pool against a paid model under an identical prompt; six
157-fixture prompt arms; a 218-call paid tier. One host's numbers on one workload,
not a survey.

Items keep the numbers they had in the plan this was split from.

---

## 1. A named model takes request parameters

**What a caller can do after this:** call a model it named itself with the
parameters that model documents — a reasoning budget, a temperature, a token cap,
a seed — and get the same answer shape back.

**Why.** The host's paid step and its free pool were compared over 179 fixtures
under one prompt: the paid model's median whole answer was 10.0 s against the
pool's 2.2 s, and **7.06 s of those ten passed before the first character**,
against 0.91 s. Its answer was *shorter* than the pool's — 1265 characters against
1396. The cost is a thinking phase, not throughput, and the host streams, so the
number its reader feels is the 7 s.

Whether that model becomes usable at a lower reasoning effort is the host's whole
open question about paid tiers, and **it cannot be asked**: the request carries the
model, the messages, optionally the tools, and the streaming options. Every paid
measurement that host has ever taken is a measurement of a model's default effort,
and it is recorded in its own specs as such.

**Boundary.** This is the direct client — one model the caller named, whose
provider and dialect are its own responsibility. The broker does not interpret,
validate, rewrite or default the parameters, and does not promise a provider will
honor one. Passing an unsupported parameter is the caller's error, and the
provider's error message is the right report of it.

**Not in this item: the pool.** Sending an arbitrary parameter to a heterogeneous
pool trades parse failures for provider rejections and skewed routing — the reason
item 7 below routes rather than forwards. A
pooled request may only carry a parameter the broker knows how to route, through
that plan's learned-refusal machinery, and each such parameter is a decision of its
own. One observation for whoever writes that decision: the curated free pool itself
carries a reasoning model that spends 31 s of a 34 s answer before its first token,
so reasoning effort is the parameter whose absence costs a pooled interactive
caller the most, after schema constraint.

**Settled: the catalog stays about identity, the calibration is knowledge.**
Default parameters per alias — so that "the fast tier" would name the mode and not
only the model id — are not part of this plan and not a later step of it. They read
against [`decisions.md#speed-is-a-catalog-tier`](../reference/decisions.md); they
are provider-specific and move faster than a model id, so the file would rot faster
than its refresh runbook can carry it; and a default no host asked for silently
changes the answers of every host that has already measured at a model's own
default.

What is worth keeping is the **knowledge**. Whether a paid tier is usable at a low
reasoning budget is a measurement, and it belongs in prose beside what is already
recorded about models — the way the rejected reachability check resolved, where the
valuable part turned out to be the knowledge rather than the mechanism
([`README.md`](README.md)). One thing alone re-opens the data question: measurement
showing that several hosts converge on the same values.

## 6. Naming a model the catalog does not carry

**What a caller can do after this:** point the broker at a specific model of a known
provider, and enumerate what the curated catalog holds, from a program.

**Why.** A survey is exactly the place where an uncurated model must be reachable,
and it is not a hypothetical: the paid model this host ships was found that way —
it was **not** in the curated catalog when it was measured, and it beat the two
models that were. The library was the right tool and the surface was not: the host's
harness reads the preset TOML files out of the installed package by path, parses
them itself to recover a provider's base url and key reference, and imports the
env-file parser out of a standalone submodule.

Partly this is a documentation failure and should be recorded as one — a config
object *is* accepted for a model declared in code, so a hand-built client was never
necessary. But building that object still needs the provider's base url and key
reference, which only the catalog knows, and the catalog is a file with no reader.
The CLI already prints exactly these rows.

**Boundary.** Reading curated data that already ships, plus resolving a provider's
model id against it. Not a registry, not a fetch, not a second source of models —
[`decisions.md#nothing-declared-enters-the-pool`](../reference/decisions.md) and the
sync rules are untouched, and an uncurated model stays a named model, never a pool
entry.

## 7. Schema-constrained output, routed rather than promised

### What it is, and what it is not

A caller can ask for schema-constrained output, and the pool answers from a model
that accepts the request. A model that refuses it is remembered as refusing *that
request*, not as broken, and the pool keeps serving.

What this is not, and the boundary is the feature: **the broker routes, it does
not guarantee conformance.** A model may take the parameter and ignore it, and no
router can detect that without knowing the caller's schema better than the caller
does. The host still validates what it gets. A caller told otherwise would drop
its own validation and be wrong roughly as often as the pool is weak.

### Why the machinery here is small

[`README.md`](README.md) names the check: a mechanism sized for a problem this
pool does not have. Both rejected proposals were runtime machinery built for
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

### Step one is a probe, and it may end this item

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

### What to build, if the probe passes

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

### What stays out of this item

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

### Tests

The refusal path is the one worth pinning: a model whose endpoint rejects the
parameter is retried on the next model within the same call, the fact is recorded
against that model alone, and a later unconstrained call still reaches it. Plus
the ordinary pass-through assertion, and the degenerate case — a constrained
request when no model is known to accept it still gets an answer.

### What this item moves into the specs

The routing-not-conformance boundary belongs in
[`../reference/mission.md`](../reference/mission.md#what-it-is-not); it is a
decision, and it will be re-proposed as a gap otherwise. The probe's numbers
belong in [`../reference/freetier-providers.md`](../reference/freetier-providers.md)
whichever way they fall.

## A recorded decision whose wording outruns its own argument

[`decisions.md#no-wal-management`](../reference/decisions.md) blocks a WAL pragma
and a configurable busy timeout, because the driver receives only a path and cannot
tell a shared file from its own, so enabling WAL would silently flip a mode that
belongs to whoever owns the file.

That argument is right, and it is an argument about **inference**. The wording bans
the **explicit** form with it — a journal mode and a busy timeout passed by the host
that owns the file, which is not a driver guessing but the owner saying so.

**Settled: reword the decision, build nothing.** No behaviour changes and no work is
queued from this. What changes is what the entry claims. The principle — the driver
never chooses on its own — is kept and stated as such; the fact that the explicit
form is unbuilt for want of demand is stated as that, and not as a second principle.
As written, a future reader takes the entry to mean two processes on one store are
unsupported in principle, so the proposal returns and is met by a formulation rather
than by the argument.

The demand behind it is one host, once, and it had a working alternative: separate
homes, at the cost of two applications on one box sharing nothing. A second host
meeting the same wall is what would move this from wording to work.

## What stays out of this plan entirely

- Any promise about the *content* of an answer. None of these makes a model obey.
- A capability framework, a static capability table, or a second axis in the
  catalog file.
- Failover or learning for a model reached by name.
- Caps, budgets, quotas or spend limits of any kind.

## What the concretization step must add

For items 1 and 6, before a line of `src/` is written: the exact surface a caller
touches; which existing module owns it; whether the change is additive to a public
type or a new one; and the tests that pin the behavior, including the negative
ones. Item 7's probe runs before either is built, because its outcome decides
whether the same request path is opened once or twice.

## What moves into the specs

Item 6 draws a boundary a future reader will otherwise re-propose as a gap: what
catalog access is *not*. Item 7's routing-not-conformance boundary belongs in
[`../reference/mission.md`](../reference/mission.md#what-it-is-not), and its probe
numbers in [`../reference/freetier-providers.md`](../reference/freetier-providers.md)
whichever way they fall. Each is written in the batch that implements it.

## Gate

`invoke pre` and the full suite green, per `CLAUDE.md`. Items 1 and 6 need no
provider key: a fake provider pins both. Item 7's probe needs real keys, is run by
a person, and its raw numbers are recorded before the first `src/` line.
