# The journal

The only state llmbroker keeps beyond the static registry: how it is read, what
is re-derived from it, and how `scope` attributes rows. Where it is stored is
[`backends.md`](backends.md). The cross-cutting rules this file elaborates are
in [`../invariants.md`](../invariants.md).

## The read path

The journal has two read forms, both newest-first and both over the same store
port: a tail of raw records, and a per-model aggregate of call records over a
time window. Both narrow by an inclusive lower time bound, by record kind, and
by operation.

The kind filter matters because the two record kinds interleave in one stream
and a quality record carries no status, so a host aggregating call outcomes
without it gets a silently wrong denominator. The operation filter matters
because the journal is shared by everything the broker calls.

The operation filter matches a named operation only: an unset filter means "do
not filter", so calls journaled without an operation label cannot currently be
isolated as a group. A host that labels none of its calls therefore has two
readings — everything, or one named operation — and neither is "mine". This is
sound while the broker journals no traffic of its own; it stops being sound the
moment the broker writes rows under its own operation name, which is the point
at which the filter needs a way to select the unlabelled bucket.

Every instant crossing the boundary is UTC in both directions (invariant 9), and
the row limit must be at least 1: backends disagree on what zero means — one
reads it as "no limit" — so a caller's shrinking budget must not decay into a
full scan. Both are enforced at the public API as well as in the shipped
backends, so the guarantee does not depend on a host's own store implementation
upholding it.

Window aggregates are derived per request, never accumulated into stored
counters ([`decisions.md`](../decisions.md#aggregates-derived-not-accumulated)).
The library returns per-status counts and leaves failure policy to the host: the
aggregate carries only statuses actually observed, so "how many were not OK" is
a subtraction rather than an assumption about the status enum's shape.

## One tail read, and quality is what it derives

A read of the most recent records re-derives what the host has taught this
installation: quality-window verdicts, the latency bound an expired budget left,
and the snapshot metrics. It runs when the pool is rebuilt and at no other time
([`list-refresh.md`](list-refresh.md)), which is also when the registry and the
admin disabled-verdict map are re-read, so another process's edits reach a
running broker without a restart.

**Availability is not among them** (invariant 11). A cooldown and a dead key
belong to the process that found them, are written to no row and read back from
none, so nothing about them is on the tail and no failure forces a read of its
own.

A call record carries evidence rather than a summary of it: one that ran out the
caller's budget carries the budget it missed (see
[`selection.md`](selection.md)). Nothing derived is recovered by reading back a
message the library formatted.

The tail is shared across all models and operations, so a chatty model can crowd
a quiet model's ratings out of it. This is an accepted consequence, and the tail
limit is the tuning knob.

Persistence is the store by default; an explicit in-memory opt-out degrades to
session-scoped learning. That degradation is what the forward fold of invariant 8
carries: a store with no read path never contributes a tail, so a rating and a
missed budget reach the live state only as the row is written, and nothing
survives the process. The journal forgets via retention — every backend
self-purges records older than its retention horizon — and there is no public
purge operation.

The admin disabled-verdict map is the one **excluding** verdict, orthogonal to
quality demotion: values are written only by the disable verb, and llmbroker
only seeds missing names. It survives a sync by construction, since a
sync only touches the registry, and it works identically for file and DB
sources. Lifting a verdict simply lifts it; rehabilitation happens through new
ratings displacing old ones in the window.

## Per-user scoping

A multi-user host can give each end user its own LLM API key over one shared
registry and store. `scope` is the one knob — an opaque string, with the empty
string rejected in favour of the unscoped `None`
([`decisions.md`](../decisions.md#scope-is-an-opaque-string)).

- **The registry and everything learned are user-agnostic** (invariant 16).
  There is no per-tenant registry partition, and storage and the protocols have
  no user concept at all: `scope` is interpreted by the broker, never passed to
  a backend or protocol method.
- **Secrets are the one thing that is actually per-scope.** Key resolution tries
  the scope-prefixed ref first and falls back to the plain ref — an own key if
  one is set, the shared key otherwise. The fallback policy lives entirely in
  the broker; secrets backends stay plain exact-lookup key-value stores and
  never see the scope string itself, only the already-prefixed ref.
- **The journal carries the scope as a plain attribution field**, filterable on
  read, but it does not partition learning — the tail read is unscoped by design.
- **A dead key is dropped for whoever spent it**, since the key that paid is the
  caller's own, and a cooldown a provider imposes withdraws the model for the
  process that met it. Neither leaves the process (invariant 11), so neither
  needs a partition anywhere.
