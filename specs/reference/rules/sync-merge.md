# The merge: a curated list becomes the registry

What `sync(source)` does to the stored model list, and what it may take away.
What keeps that list current is [`list-refresh.md`](list-refresh.md); where
lists come from is [`presets.md`](presets.md). The
cross-cutting rules this file elaborates are in
[`../invariants.md`](../invariants.md).

`sync(source)` is the only registry write path and returns a `SyncReport`
describing what it did.

**One verb, one source.** A model list arrives as a curated preset name and in no
other shape: no host names a file, and no host hands over a list of its own to
merge. Fetching that preset is the only networked operation in the library. A
host that must not follow our curation supplies the whole pool instead, in a
registry it fills itself
([`decisions.md`](../decisions.md#the-model-list-is-not-a-path-a-host-names)).

**The merge writes into the registry the broker was built with, whatever brought
that registry there** — a model-list file llmbroker owns, a connection string, or an
object the host constructed. What it may touch there is decided per entry, not
per registry: see the partition below.

Concurrent nodes are safe because the merge is a pure function of (arriving
list, current entries), so every node computes the same result: the first write
settles it and the identity gate turns every other node's check into a no-op. A node must never
coerce the shared registry to a *local copy* of its own — diverging copies would
flip-flop it.

## One merge site

Every merge runs inside the broker, over the ports that broker was built with,
so it always sees the keys the application will — that is what makes a key-aware
merge safe. Nothing else merges: the CLI has no merge site of its own, and there
is no offline entry point that could decide a removal blind to keys only the
running process can resolve.

Two installation shapes remain, and they differ only in where the merged result
lands and which keys a bootstrap can see:

|  | the default | a database installation |
|---|---|---|
| registry / secrets | the model list in llmbroker's own directory, keys from the environment plus the working directory's `.env` | DB / Vault / AWS, possibly per-user (`scope`) |
| key visibility | the process environment + that `.env` | the broker's own secrets backend |

A bare `Broker()` builds the first. The second is the deploy path: the host's own
entrypoint constructs its broker and calls `sync`, so the connection config and
its secrets stay in one place.

## The partition: a sync touches only what a sync wrote

Every entry records whether a sync put it there
([`decisions.md`](../decisions.md#a-sync-touches-only-what-a-sync-wrote)), and
the merge partitions on that record. Entries a sync wrote are the ones
everything below applies to. An entry the installation stated itself is carried
through untouched, whatever the arriving list says — never removed, never
replaced, never counted in the report's added/updated/removed. The default for a
new entry is *not written by a sync*, so an entry that reached the registry
through the port — a driver the installation implements over storage of its own,
a registry object it hands over, its own mirror call — is protected without doing
anything. It yields invariant 22.

The port is the whole of the installation's write surface. It does not write our
tables by hand: invariant 13 promises nothing about a column name, so a row put
there by a deploy script breaks on an upgrade with nothing to read. An
installation stating pool members of its own implements the registry port, or
composes one around a shipped registry whose stored rows it returns alongside its
own. That composition states a union, not a concatenation — a merge persists the
entries it was handed, so a `load()` that adds an entry the store already holds
carries the same name twice and is refused.

That makes the mixed pool statable: the routed pool is whatever the registry
holds, whether it came from the curation, from the installation, or from both.
The record answers one question only — whose parameters these are — and a
registry holds pool members, so nothing else about an entry's kind is stored
([`direct-aliases.md`](direct-aliases.md#the-four-kinds-and-where-each-is-stated)).

A name the merge itself would carry twice is refused, and nothing is written.
The fix is always to rename the installation's own entry — the arriving one's
name is machine-formed and would be formed again on the next sync.

## The mirror: a sync brings its own entries into line

Entries a sync wrote are mirrored onto the arriving list, wholesale: an entry
still present is updated in place, an entry absent from it is removed, a new one
is added. Nothing weighs whether an absent entry might still work — not the key
behind it, not the journal, not the provider it belonged to
([`decisions.md`](../decisions.md#a-sync-mirrors-what-a-sync-wrote)).

The result is a pure function of (arriving list, current entries), which is what
makes repeated syncs converge instead of oscillating, and what lets concurrent
nodes compute the same answer.

**A removal reaches installations that can still call the entry**, including one
whose only key is that provider's. That is why removal is bounded where the list
is curated rather than here: an entry leaves the curated list only once it can no
longer be called, and a model not worth routing to is given the lowest weight
instead ([`../freetier-providers.md`](../freetier-providers.md)). An installation
that must not follow our curation states its whole pool in a registry it fills
itself, which is a different act from following a curation.

**A removal is not silent.** Taking away a provider an installation could reach is
exactly what moves the usable-provider count, and the alarm fires on the
transition into one usable provider and into none
([`pool-health.md`](pool-health.md)).

**When a removal orphans a key** — the ref's key *is* here and nothing in the
merged result references it any more, the installation's own entries included —
the report says the key is now unused and a human decides; a sync never deletes a
secret (invariant 15). A ref with no key behind it is nothing to revoke, and
saying otherwise would put an invented admin act into the one channel that exists
to surface the real ones, on the commonest removal of all.

**One structural guard.** Applying a result with zero entries over a registry
that has some is refused with `SyncRefusedError` carrying the report; an empty
registry accepts anything, which is onboarding. The rule above can reach that
state — an arriving list with no entries — so the guard is on the normal path,
not a backstop.

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

Where the model list lives in llmbroker's own directory, the merged result is
written back over it, which is what lets a zero-config broker keep itself current.
Provisioning against an empty registry still fails fast, naming the sync call
that would fill it. The write is atomic and preserves the target's permissions.

## The model-list file is written, never authored

**No configuration file is the host's to maintain**
([`decisions.md`](../decisions.md#the-model-list-file-is-generated-not-authored)).
The file is generated: a sync renders it in full from the merged entries,
and nothing else writes it. It says so in its own first line, and it holds one
array — a model reached by name is declared in code, so there is no second
section for one. Nothing in its previous text is preserved, so anything
llmbroker does not model — a comment, an unknown key — does not survive a sync.
A database registry is the same picture with rows instead of text.

That the file is llmbroker's own is what makes rendering from the merged entries
possible at all, and rendering is what removes the read-back check an assembled
file needed: there is no arriving text to splice, so there is nothing to verify
the result against.

**A generated file carries the key help beside the entry that needs it.** The
help is modeled rather than left as prose because it is the only guidance
llmbroker offers for the one irreducible admin act, so a sync that dropped it
would erase the instructions for a key still missing. An entry the installation
stated itself lives in a registry it fills, which the same rule protects.

## The report

- **`SyncReport`** is returned by every sync, *including no-ops*, so missing keys
  stay visible until resolved. `last_sync_report` lets a host forward it to its
  own admin channel, and is set on every outcome. The report carries no severity
  enum — the host derives criticality.
- **The stored list is the durable state**: the entries and the keys they still
  need sit in it. The sync stores nothing of its own.
- **A sync that changes nothing is indistinguishable from no sync at all.** The
  merged result is compared with what is already stored, and when they are equal
  nothing is written, nothing is applied to the live pool, and the outcome is
  logged at `debug`. The comparison is on the bytes that would be written for a
  file target, and on the entries keyed by name for a registry target — a
  database hands its rows back in its own order (invariant 3). A new persisted
  config field joins that comparison automatically. What the gate covers is the
  list: secret bootstrapping runs on every sync, since a key that has just
  appeared in the environment is the reason a host calls one.
- A sync that *did* change something logs its outcome once at `info`, whatever
  called it. Nothing in a sync outcome is admin-actionable on its own — a keyless
  entry is a normal documented state. What *is* actionable is a degraded pool, and
  [`pool-health.md`](pool-health.md) owns that alarm. The log line and
  `last_sync_report` are recorded before the pool is reconciled, so a reconcile
  that raises cannot swallow the record of a change already applied.
- A sync never writes to the journal; it only *reads* the bounded tail, and only
  when a provider it might retire has a candidate entry.
- **A sync follows no alias.** Nothing stored has one; the catalog read a sync
  makes on the way past exists only to keep the copy current for the resolution
  that does follow one, so the report carries no alias facts
  ([`direct-aliases.md`](direct-aliases.md)).
