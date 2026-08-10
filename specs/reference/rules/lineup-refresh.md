# Keeping the lineup current

What makes a stored lineup follow the curated one without an admin act. What the
merge itself does is [`sync-merge.md`](sync-merge.md). The cross-cutting rules
this file elaborates are in [`../invariants.md`](../invariants.md).

**A broker following the curated preset re-checks it on an interval**, lazily on
activity. What an installation may switch off is the *automatic* fetching, never
the currency of the list: a process that may make no outbound connection while it
serves stops every clock it has and does the fetching itself, in a job it runs
deliberately ([`decisions.md`](../decisions.md#no-automatic-fetch-means-none-at-start-either)).
Freezing the list while the process keeps serving from it is not offered
([`decisions.md`](../decisions.md#unconditional-lineup-refresh)). An
installation that must not follow our curation fills a registry of its own,
which is a different pool rather than a frozen copy of ours; one that wants both
keeps following it, since a refresh leaves its own entries alone.

`sync=` names the curated preset an installation follows, `None` for a registry
filled by other means. Unstated it is the curated preset — except where the
broker was handed a registry *object*, which must say what it follows or the
constructor refuses
([`decisions.md`](../decisions.md#who-builds-the-registry-states-what-it-follows)).
Either way a refresh only rewrites what a sync itself wrote
([`sync-merge.md`](sync-merge.md)).

## Two gates

- The **time gate** decides whether to go to the network at all. It is a
  monotonic comparison at the top of the lazy pool initializer, the funnel every
  public operation already passes through, so an idle process performs no I/O
  and schedules no wakeups — the library needs no running service of its own. A
  background timer would have to be owned, cancelled and tested against every
  embedding, for a process that has no lineup to keep fresh.
- The **identity gate** decides whether what arrived changes anything (see "The
  report" in [`sync-merge.md`](sync-merge.md)). It also removes the need for a
  conditional GET, which would save a kilobyte and no round trip while proving
  strictly less.

**Where the automatic fetching is off there is no time gate at all**: nothing is
armed, and an empty registry is not filled before provisioning either —
provisioning raises and the error names the deploy step that would have filled it.
The curated preset and the paid catalog stop together, since the reason to forbid
one forbids the other, and the catalog read a declared alias makes on its way to
being resolved stops with them ([`direct-aliases.md`](direct-aliases.md)) — the
process opens no connection of its own at all.

**The explicit sync is never gated by any of this**, whatever the clocks are set
to: the caller asked for it and handles what it raises. With no argument it syncs
what this installation follows — its preset, or, where it follows none, the paid
catalog alone, which merges nothing into the registry and therefore returns no
report. An installation following neither has nothing to fetch and does not.

**A check that just happened is remembered across process exits**, per (lineup,
target), so a short-lived process does not pay a round trip per invocation and a
rolling deploy does not fetch once per pod. The record only ever makes checks
less frequent: it is not authoritative, a timestamp in the future counts as
absent, and losing it costs one extra fetch. It is deliberately not shared
across a cluster — N nodes cost N small GETs a day, unmeasurable against the
fleet's own LLM traffic, and the identity gate already makes concurrent
application a no-op.

**The same interval carries the paid catalog.** A declared alias rides on that
one clock and no other, whether or not this installation syncs a lineup at all
([`direct-aliases.md`](direct-aliases.md)). A catalog nobody can reach may not
fail the sync of the model list itself.

**A fetched lineup is cached, and the cache is a fallback rather than a
source**: a successful fetch overwrites it, a failed one — offline, or throttled
by the CDN's per-IP limit — falls back to it. Unlike the check record, the cache
is machine-global: what the catalog says today does not depend on which project
is asking.

**A refresh with nowhere to keep what it fetched fails and says so.** Where no
directory is writable the copy a refresh exists to leave behind cannot exist, so
it is an error naming its two ways out — make a directory writable, or run an
installation that fetches nothing by itself — and never a round trip whose result
is dropped. Ordinary reads keep working there, as everywhere else with a cold
cache.

## Failure is never the caller's problem

**The refresh is off the critical path, with one exception.** An empty registry
is filled before provisioning, blocking, because provisioning an empty registry
raises and there is no alternative; a registry that already holds a lineup is
provisioned from it and refreshed afterwards, so the first call of a fresh
process does not wait on the network.

It is **best-effort and never raises**: a fetch failure or a refusal logs a
warning naming which check it was, stashes the report where there is one, and
continues on the existing configuration. Neither a start nor a request ever
fails over a lineup refresh. The explicit `sync()` call raises instead — that
caller chose to sync and has a plan. The start attempt is guarded by its own
flag, so a provision that failed for another reason and is retried does not
re-fetch.

**Picking up another process's edits may never fail the call that carried it
here.** The re-read rides on the journal rebuild, whose commonest trigger is a
rate limit — the very thing the pool exists to absorb. A registry that cannot be
read, or that has been edited into a state the broker rejects, logs and leaves
the live pool exactly as it is; it never surfaces out of the caller's own `ask`.
Only the paths a caller asked for by name may raise: provisioning, an explicit
`sync`, and a `direct()` naming one model — a `direct()` whose target has become
ambiguous has no answer to give and must say so rather than guess.

The background refresh runs as a detached task, so anything it does not catch is
lost as an unretrieved exception and the refresh silently stops for the life of
the process. It therefore catches everything, rather than the failures a given
code path happened to be written with.

## The accepted exposure

The catalog's default branch is live configuration for every installation
([`decisions.md`](../decisions.md#unconditional-lineup-refresh)). A `base_url`
decides where an installation's API keys are sent, so a config built from a
*fetched preset* must carry `https://` ones.
