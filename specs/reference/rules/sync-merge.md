# The merge: a lineup becomes the registry

What `sync(source)` does to the stored lineup, and what it may take away. What
keeps a lineup current is [`lineup-refresh.md`](lineup-refresh.md); where
lineups come from is [`presets.md`](presets.md). The
cross-cutting rules this file elaborates are in
[`../invariants.md`](../invariants.md).

`sync(source)` is the only registry write path and returns a `SyncReport`
describing what it did.

**One verb, one source.** A lineup arrives as a curated preset name and in no
other shape: no host names a file, and no host hands over a lineup of its own to
merge. Fetching that preset is the only networked operation in the library. A
host that must not follow our curation supplies the whole pool instead, in a
registry it fills itself
([`decisions.md`](../decisions.md#the-lineup-file-is-not-a-path-a-host-names)).

**The merge writes into the registry the broker was built with, whatever brought
that registry there** — a lineup file llmbroker owns, a connection string, or an
object the host constructed. What it may touch there is decided per entry, not
per registry: see the partition below.

Concurrent nodes are safe because the merge is a pure function of (arriving
lineup, current lineup, resolved keys), and one registry means one secrets
store, so every node computes the same result: the first write settles it and
the identity gate turns every other node's check into a no-op. A node must never
coerce the shared registry to a *local copy* of its own — diverging copies would
flip-flop it.

## One merge site

Every merge runs inside the broker, over the ports that broker was built with,
so it always sees the keys the application will — that is what makes a key-aware
merge safe. Nothing else merges: the CLI has no merge site of its own, and there
is no offline entry point that could decide a removal blind to keys only the
running process can resolve.

Two installation shapes remain, and they differ only in where the merged lineup
lands and which keys the merge can see:

|  | the default | a database installation |
|---|---|---|
| registry / secrets | the lineup in llmbroker's own directory, keys from the environment plus the working directory's `.env` | DB / Vault / AWS, possibly per-user (`scope`) |
| key visibility | the process environment + that `.env` | the broker's own secrets backend |

A bare `Broker()` builds the first. The second is the deploy path: the host's own
entrypoint constructs its broker and calls `sync`, so the connection config and
its secrets stay in one place.

## The partition: a sync touches only what a sync wrote

Every entry records whether a sync put it there
([`decisions.md`](../decisions.md#a-sync-touches-only-what-a-sync-wrote)), and
the merge partitions on that record. Entries a sync wrote are the ones
everything below applies to. An entry the installation stated itself is carried
through untouched, whatever the arriving lineup says — never removed, never
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

## The removal rule: the provider is the unit

Only entries a sync wrote whose name is absent from the arriving lineup are
candidates for removal. An entry still present is updated in place.

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

A sync is therefore not a blind mirror: an entry the curated lineup drops is
removed only under the rule above, and the report says why it was kept. A host
that needs an exact lineup with nothing carried over writes it into its registry
itself, which is a different act from following a curation.

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
merged lineup references it any more, the installation's own entries included —
the report says the key is now unused and a human decides; the key's help
section is kept while any entry still references it. A ref with no key behind it
is nothing to revoke,
and saying otherwise would put an invented admin act into the one channel that
exists to surface the real ones, on the commonest removal of all.

**Retention is recomputed, never stored.** Which entries are kept follows from
(arriving lineup, current lineup, keys) on every merge, so a persisted flag
would be an output masquerading as an input. Nothing records it; the report names
the kept entries on every run, including no-ops.

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

Where the lineup lives in llmbroker's own directory, the merged result is written
back over it, which is what lets a zero-config broker keep itself current.
Provisioning against an empty registry still fails fast, naming the sync call
that would fill it. The write is atomic and preserves the target's permissions.

## The lineup file is written, never authored

**No configuration file is the host's to maintain**
([`decisions.md`](../decisions.md#the-lineup-file-is-generated-not-authored)).
The lineup file is generated: a sync renders it in full from the merged entries,
and nothing else writes it. It says so in its own first line, and it holds one
array — a model reached by name is declared in code, so there is no second
section for one. Nothing in its previous text is preserved, so anything
llmbroker does not model — a comment, an unknown key — does not survive a sync.
A database registry is the same picture with rows instead of text.

That the file is llmbroker's is what makes rendering from the merged entries
possible at all, and rendering is what removes the read-back check an assembled
file needed: there is no arriving text to splice, so there is nothing to verify
the result against.

**A generated file still carries every entry the merge kept.** A kept entry is
part of the merge like any other — a name it uses is taken, a key it needs is
reported as pending, a ref it references is not an orphan, and the help for that
ref is rendered back beside it. That last one is the whole reason the help is
modeled rather than left as prose: it is the only guidance llmbroker offers for
the one irreducible admin act, so a sync that dropped it would erase the
instructions for a key still missing. An entry the installation stated itself
lives in a registry it fills, which the same rule protects.

## The report

- **`SyncReport`** is returned by every sync, *including no-ops*, so kept entries
  and missing keys stay visible until resolved. `last_sync_report` lets a host
  forward it to its own admin channel, and is set on every outcome. The report
  carries no severity enum — the host derives criticality.
- **The report also says whether a missing key was evidence at all** at that
  merge site, and which of the two ways it was not — the probe resolved nothing,
  or keys are per-user behind `scope` (`keys_visible`, `keys_scoped`). A host
  reading `kept` needs both: where key presence proves nothing, "kept" means the
  merge declined to decide, not that the entry was weighed and spared.
- **The lineup itself is the durable state**: kept entries and the keys they
  still need sit in it. The sync stores nothing of its own.
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
- **A sync follows no alias.** Nothing stored has one; the catalog read a sync
  makes on the way past exists only to keep the copy current for the resolution
  that does follow one, so the report carries no alias facts
  ([`direct-aliases.md`](direct-aliases.md)).
