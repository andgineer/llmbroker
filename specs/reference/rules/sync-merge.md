# The merge: a lineup becomes the registry

What `sync(source)` does to the stored lineup, and what it may take away. What
keeps a lineup current is [`lineup-refresh.md`](lineup-refresh.md); where
lineups come from is [`presets.md`](presets.md). The
cross-cutting rules this file elaborates are in
[`../invariants.md`](../invariants.md).

`sync(source)` is the only registry write path and returns a `SyncReport`
describing what it did.

**One verb, two sources.** A *preset name* fetches the curated lineup from the
catalog — the only networked operation in the library. A *path* or a *registry*
is offline. Both then run the identical merge, so every rule below holds
whatever named the lineup.

Concurrent nodes are safe because the merge is a pure function of (arriving
lineup, current lineup, resolved keys), and one registry means one secrets
store, so every node computes the same result: the first write settles it and
the identity gate turns every other node's check into a no-op. A node must never
coerce the shared registry to a *local copy* of its own — diverging copies would
flip-flop it.

## Three tiers, one merge site each

|  | tier 0 (the default) | tier 1 (a declarative config) | tier 2 |
|---|---|---|---|
| registry / secrets | the home-directory lineup on env plus the CWD `.env` | a config file on its default env/`.env` secrets | DB / Vault / AWS, possibly per-user (`scope`) |
| who merges | the broker itself | the CLI, into the file | `broker.sync(...)` |
| key visibility | the process environment + the CWD `.env` | the process environment + the file's sibling `.env` | the broker's own secrets backend |

Tier 0 is what a bare `Broker()` builds; tier 1 is for a lineup a team wants
under version control and reviewable in a diff; tier 2 is the deploy path into a
database registry.

Each tier has exactly one merge site, and that site sees the same keys the
application will. **A file registry paired with a Vault/AWS/DB secrets backend
is tier 2, not tier 1**: only the broker can see those keys, so that
installation refreshes from code even though its lineup lives in a file. Tier 2
never merges from the CLI at all. That is what makes a key-aware merge safe — no
merge ever runs blind to the keys the program consuming its output will have.

A file target is written from a curated preset only. A file or registry source
syncs into a database registry — the vendored-lockfile deploy path, where the
merge dedupes custom entries; rendering an arbitrary source into a live config
file cannot.

## The removal rule: the provider is the unit

Only managed entries whose name is absent from the arriving lineup are
candidates for removal. An entry still present is updated in place, and custom
entries are never pruned.

**The unit of decision is the `api_key_ref`, not the entry.** Two entries on one
ref are one quota and one failure domain. For each dropped entry, in order:

1. The arriving lineup still carries that `api_key_ref` — its models replace the
   entry: same key, same quota, removed with no key lookup at all.
2. Otherwise, if keys are visible here and no key exists for that ref, the entry
   is removed: nothing could call it anyway.
3. Otherwise, if this installation's own journal proves the entry does not work,
   it is removed and reported as **retired**.
4. Otherwise it stays — "kept" — and keeps routing.

The rule depends only on the state of the world — which providers the lineup
carries, which keys exist, what the journal recorded — which is what makes
repeated syncs converge instead of oscillating. It yields invariant 11.

A path source is therefore not a blind mirror: an operator who deletes an entry
from the vendored file gets it removed only under the rule above, and the report
says why it was kept. The escape hatch for a forced lineup is mirroring the
configs into the registry directly.

**Death is proven, never assumed.** An entry is dead when this installation's
journal window holds at least one permanent client failure (401/403/404) and no
successful call at all. A bad week — 429s, 5xx — proves nothing. The journal is
read only when there is a candidate to decide about, which is never on an
ordinary sync; a busy pool that pushes the failure out of the bounded tail
leaves no evidence, and the entry stays. Conservative on purpose.

A candidate is any entry the rule above would otherwise keep. Where a missing
key *is* evidence it covers entries whose key is here; where it is not, it
covers every dropped entry, because "nobody could call it and lived" is then the
only evidence that installation can produce, and it is strictly stronger than
key absence. Without that, a per-user host could never retire anything.

**A retirement shows its evidence.** Deleting an entry from the installation's
own configuration is the one destructive thing a sync does, so the report
carries the permanent status the provider answers now and how far back the run
of failures reaches in the window that was read. An admin can check the verdict
without opening the journal.

**Absence of a key is evidence only where the probe could have found one.** Two
merge sites cannot prove absence, and at both a dropped provider's entry is kept
regardless: an installation whose keys are per user behind `scope`, and one
whose probe resolved *nothing at all* — there the keys live in a store this
merge site cannot reach, and "no key anywhere" is indistinguishable from "not
the keys this lineup runs on".

**`have_keys` only lowers conservatism.** It declares refs the broker cannot
probe — per-user keys behind `scope`, a secret injected only in production.
Declared refs count as present when the merge weighs a removal, and only there:
it never makes a model routable, the pool still needs a real key value. It is a
promise — declare a ref and fail to provision it, and the pool degrades. Omit it
and nothing breaks; the lineup just keeps entries a better-informed run would
have pruned. This is the only reason the parameter exists.

**When a removal orphans a key** — the ref's key *is* here and nothing in the
merged lineup references it any more, custom entries included — the report says
the key is now unused and a human decides; the key's help section is kept while
any entry still references it. A ref with no key behind it is nothing to revoke,
and saying otherwise would put an invented admin act into the one channel that
exists to surface the real ones, on the commonest removal of all.

**Retention is recomputed, never stored.** Which entries are kept follows from
(arriving lineup, current lineup, keys) on every merge, so a persisted flag
would be an output masquerading as an input. Nothing records it; the file writer
groups kept entries under a generated comment, and the report names them on
every run, including no-ops.

**One structural guard.** Applying a result with zero entries over a registry
that has some is refused with `SyncRefusedError` carrying the report; an empty
registry accepts anything, which is onboarding. The rule above can reach that
state — an empty lineup over a registry whose entries are all keyless removes
everything — so the guard is on the normal path, not a backstop.

Nothing is lost by a removal that does happen: keys live in the secrets store,
learned state derives from the journal, and admin verdicts live in the store's
disabled map, so a model returning later is re-added and its old ratings and
verdict resurface.

## What a sync also does

It bootstraps secrets: for each config whose `api_key_ref` the configured
secrets backend cannot resolve, it tries the environment and, if found, persists
the value. Existing secrets are never overwritten — admin-edited values win. It
also seeds the store's disabled map with any missing model names, never touching
existing verdict values.

A file registry is a legitimate target for a curated preset: the merged lineup
is written back to the file, preserving its comments and custom entries, which
is what lets a file-configured broker keep itself current. Provisioning against
an empty registry still fails fast, naming the sync call that would fill it.

The write is atomic and preserves the target's permissions, and what is about to
replace a live config is parsed and checked against the merge result first —
this is the one code path that can destroy a user's configuration.

## The report

- **`SyncReport`** is returned by every sync and printed by the CLI on every run
  *including no-ops*, so kept entries and missing keys nag in each deploy log
  until resolved. `last_sync_report` lets a host forward it to its own admin
  channel, and is set on every outcome. The report carries no severity enum —
  the host derives criticality.
- **A committed config file is the durable state**: kept entries and the keys
  they still need sit in the file, so a bot refresh is reviewable in the pull
  request diff itself. The sync stores nothing of its own.
- **A sync that changes nothing is indistinguishable from no sync at all.** The
  merged result is compared with what is already stored, and when they are equal
  nothing is written, nothing is applied to the live pool, and the outcome is
  logged at `debug`. The comparison is on the bytes that would be written for a
  file target, and on the entries keyed by name for a registry target — a
  database hands its rows back in its own order (invariant 3). A new persisted
  config field joins that comparison automatically. What the gate covers is the
  lineup: secret bootstrapping runs on every sync, since a key that has just
  appeared in the environment is the reason a host calls one.
- A sync that *did* change something logs its outcome once at `info`, whatever
  called it. Nothing in a sync outcome is admin-actionable — a kept entry is a
  working model that keeps routing, and a keyless entry is a normal documented
  state. What *is* actionable is a degraded pool, and
  [`pool-health.md`](pool-health.md) owns that alarm. The log line and
  `last_sync_report` are recorded before the pool is reconciled, so a reconcile
  that raises cannot swallow the record of a change already applied.
- A sync never writes to the journal; it only *reads* the bounded tail, and only
  when a provider it might retire has a candidate entry.
