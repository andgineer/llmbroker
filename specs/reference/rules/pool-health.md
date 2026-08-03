# Pool health

Whether the pool can still do the one thing it exists for — fail over — and how
that reaches a log line and a host UI. The cross-cutting rules this file
elaborates are in [`../invariants.md`](../invariants.md).

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
rather than on a clock: a reconcile runs on every minute of activity, and a key
that stays missing must not fill the log.

## The alarm

It lives where membership is reconciled, so it covers provisioning, every resync
and every sync in one place: `ERROR` on the transition into one usable provider
("no failover left") and into zero ("cannot serve any request"), naming the
missing refs; one `INFO` on the way back.

These are transitions of *state*, not of severity — both are errors, and losing
the step between them would mute the moment the pool stops answering at all.
Every count that is not degraded is one state, so a healthy log carries none of
these lines, gaining a further provider is not news, and a broken pool carries
exactly one line per change.

**The measure is key presence, and it never lags behind the keys.** A ref that
stops resolving withdraws its slot on the next reconcile rather than keeping the
value it had, so a revoked or rotated key leaves the count at once instead of
after a run of requests that can only fail. The counts and the per-model
`has_key` therefore always agree. An administratively disabled entry still
counts its provider: the alarm reports the keys an installation holds, not
verdicts the host set itself and already reads per model.

## One measurement, two consumers

`snapshot()` carries the same numbers the alarm uses — the per-LLM mapping, the
counts, the missing keys with their help text, and the same `degraded`
predicate. An admin UI needs one call, and the log and the UI cannot diverge.

The help text is read from the registry only when a key is actually missing, so
a fully-keyed pool adds no registry I/O to a reconcile at all, and `snapshot()`
never performs any; a registry without key metadata yields empty help but
correct refs and names.

`snapshot()` is a view of the *live pool*, so it provisions — unlike a journal
read, which never does (invariant 6).
