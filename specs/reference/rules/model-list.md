# The model list

Where the routed pool's models come from, how a curated list becomes this
installation's registry, and what keeps it current. Reaching a host's own paid
model by name is [`direct-by-name.md`](direct-by-name.md). The cross-cutting
rules this file elaborates are in [`../invariants.md`](../invariants.md).

## Where the definitions come from

### Preset distribution

Curated LLM lists ship in the wheel and are also fetched over HTTPS from the
repository's default branch, so a list update is a plain commit independent of
any package version.

**The bundled copy is a floor, never a source.** The lookup order is fetch, then
the machine's cached copy, then the copy in the wheel; only when all three are
missing is a preset an error. Without the last step a first run with no network
and a cold cache would have nothing to start from, which is exactly the case a
zero-config broker has to survive. It also makes the CLI work offline.

**One object holds that precedence, and a read names its purpose rather than its
mechanism.** Every read of a curated text goes through it, and only two things
vary: whether the copy already here is preferred to a fetch, and whether the
wheel's copy is in the chain at all. Both are decided at the one call site whose
decision they belong to, so no branch of the fallback chain is threaded through
the callers as a flag.

**The floor holds a list up; it never moves one.** The bundled copy is older
than the repository by construction — frozen at the release the user installed —
so it is dropped from the chain wherever a read decides where an *existing*
entry should point: a stored entry a sync re-points, and a declared model
already resolved once. A refresh that can reach neither upstream nor the cache
re-points nothing, and it is never allowed to prefer the wheel's copy over a
fetch; either would roll a model backwards to whatever the installed release
shipped with, silently and for as long as it stayed installed. It is a branch of
the chain and not a value in the cache, which is what keeps the two
distinguishable
([`decisions.md`](../decisions.md#the-floor-is-not-seeded-into-the-cache)).

**A resolution on the request path prefers the copy already here.** Provisioning
resolves a declared alias, so that read takes the cache before the network and
fetches only when the machine has nothing; what moves it forward is the refresh
clock overwriting the cache underneath it, not the resolution going to look.

**And when the floor is used, it says so.** Serving the wheel's copy is reported
at warning level, unlike the cached fallback, which serves what this machine
last saw: a copy frozen at an installed release must not pass for one just
fetched, since it decides what an installation runs on until the next release.

Presets are curated, multi-provider free-tier pools only. A paid-tier preset
defeats the point — anyone willing to pay uses one good model directly — and a
single-provider preset defeats it too, since the pool's resilience comes from
spilling onto other providers when one is rate-limited. Presets are not
task-specialized or quality-ranked: one genuinely useful model per provider,
not several ranked ones.

When curation replaces a model with a strictly better sibling from the same
provider, the old entry is removed rather than left alongside the new one: the
two usually share one provider quota. How the free-tier preset is kept current
is in [`../freetier-providers.md`](../freetier-providers.md).

### The pool is exactly the curated list

The pool is the curated preset and whatever a sync kept from an earlier one. A
model the host declared in code is reached by name through `direct` and is never
routed, never failed over onto, never learned from as a pool member (invariant 4,
[`decisions.md`](../decisions.md#nothing-declared-enters-the-pool)).

A marker in a registry row written by an older release is ignored rather than
rejected: it is a field that no longer exists.

**A model list states the pool and nothing else**, wherever it is read — the
installation's own file, or a curated one just fetched. A list carrying the
section a past release used for models reached by name is refused whole, naming
where those models go now; silently dropping them would take models out of an
installation without saying so. Curation names endpoints worth pooling; what is
*the host's own* is knowledge no curator has.

### Key acquisition help

A config source may carry, per `api_key_ref`, a short markdown `help` string (a
link plus a step or two) plus a free-form passthrough of whatever else that
key's section holds — llmbroker has no taxonomy opinion on it, it just relays
whatever the preset author put there. It is keyed by the env-var name, not by
LLM, because one key is typically shared by several LLMs.

The same data feeds two consumers: the `env` command prints keys in file
declaration order, each with its help line above its variable; and a host can
pull the passthrough to render its own setup UI.

Surfacing it is an **optional registry capability**, independent of the broker.
A registry that has the metadata exposes it; one that does not simply omits the
capability. Hosts query whichever registry they hold — no coupling between
obtaining the help and routing.

An unresolved `api_key_ref` is normal, not an error: the pool routes over
whatever keys are present, and a config without a resolvable key simply stays
inactive (logged at `info`, not `warning`) rather than enqueued for routing. The
only genuine alarm is **zero** keyed configs at all — see [`selection.md`](selection.md).

### The CLI

Two commands, and they are the two the mission asks for: the one irreducible
admin act, and the one thing llmbroker cannot decide for a host.

- **`env`** emits a `.env` skeleton of `api_key_ref` names, in declaration order,
  each with its help text. It takes a curated preset name and nothing else, and
  fetches that preset the same way a sync would — one form that works on every
  backend, including an installation whose registry is a database and has no
  local list to read. Its output is a function of its argument alone: it never
  reads the environment of the process that runs it, since the file being
  generated is normally the one that will supply those variables. Onboarding is
  folded into this command rather than a separate setup/status command, to keep
  the CLI surface small.
- **`list`** shows what the two curated lists carry and writes nothing: the
  routed pool's models, and the paid ones a host may reach by name. A paid line
  carries what a caller needs to reach that model — the alias to declare, and the
  provider fields a version-pinned declaration states for itself
  ([`direct-by-name.md`](direct-by-name.md)). One model per line and no
  decoration, so a later filter is a projection of the same rows rather than a
  second formatter.

**No command writes a model list.** A list is filled by a sync, and a model
reached by name is declared where the application that calls it is configured, so
there is nothing left for a command to append
([below](#the-model-list-file-is-written-never-authored)).

**The CLI has no merge site.** Refreshing a list is the application's own
entrypoint calling `broker.sync(...)`, built by the same factory the application
uses — the library owns the operation, the host owns the connection. A CLI that
merged would either duplicate connection config the application already owns
(syncing one database while serving from another is a silent failure) or decide
removals blind to keys only the running process can resolve.

## The merge: a curated list becomes the registry

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

### One merge site

Every merge runs inside the broker, over the ports that broker was built with.
Nothing else merges: the CLI has no merge site of its own, and there is no
offline entry point that could write a registry the running process would then
disagree with.

Two installation shapes remain, and they differ only in where the merged result
lands and where the keys it reports on come from:

|  | the default | a database installation |
|---|---|---|
| registry / secrets | the model list in llmbroker's own directory, keys from the environment plus the working directory's `.env` | DB / Vault / AWS, possibly per-user (`scope`) |

A bare `Broker()` builds the first. The second is the deploy path: the host's own
entrypoint constructs its broker and calls `sync`, so the connection config and
its secrets stay in one place.

### The partition: a sync touches only what a sync wrote

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
([`direct-by-name.md`](direct-by-name.md#the-four-kinds-and-where-each-is-stated)).

A name the merge itself would carry twice is refused, and nothing is written.
The fix is always to rename the installation's own entry — the arriving one's
name is machine-formed and would be formed again on the next sync.

### The mirror: a sync brings its own entries into line

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
([`selection.md`](selection.md)).

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

### What a sync also does

It bootstraps secrets: for each config whose `api_key_ref` the configured
secrets backend cannot resolve, it tries the environment and, if found, persists
the value. Existing secrets are never overwritten — admin-edited values win. It
also seeds the store's disabled map with any missing model names, never touching
existing verdict values.

Where the model list lives in llmbroker's own directory, the merged result is
written back over it, which is what lets a zero-config broker keep itself current.
Provisioning against an empty registry still fails fast, naming the sync call
that would fill it. The write is atomic and preserves the target's permissions.

### The model-list file is written, never authored

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

### The report

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
  [`selection.md`](selection.md) owns that alarm. The log line and
  `last_sync_report` are recorded before the pool is reconciled, so a reconcile
  that raises cannot swallow the record of a change already applied.
- A sync never touches the journal, in either direction. Which refs a key
  resolves for is read once and reported; nothing in the merge decides on it.
- **A sync follows no alias.** Nothing stored has one; the catalog read a sync
  makes on the way past exists only to keep the copy current for the resolution
  that does follow one, so the report carries no alias facts
  ([`direct-by-name.md`](direct-by-name.md)).

## Keeping the list current

**A broker following the curated preset re-checks it on an interval**, lazily on
activity. What an installation may switch off is the *automatic* fetching, never
the currency of the list: a process that may make no outbound connection while it
serves stops every clock it has and does the fetching itself, in a job it runs
deliberately ([`decisions.md`](../decisions.md#no-automatic-fetch-means-none-at-start-either)).
Freezing the list while the process keeps serving from it is not offered
([`decisions.md`](../decisions.md#unconditional-list-refresh)). An
installation that must not follow our curation fills a registry of its own,
which is a different pool rather than a frozen copy of ours; one that wants both
keeps following it, since a refresh leaves its own entries alone.

`sync=` names the curated preset an installation follows, `None` for a registry
filled by other means. Unstated it is the curated preset — except where the
broker was handed a registry *object*, which must say what it follows or the
constructor refuses
([`decisions.md`](../decisions.md#who-builds-the-registry-states-what-it-follows)).
Either way a refresh only rewrites what a sync itself wrote — the partition
above is what decides that.

### The four triggers

**The live pool is rebuilt on four triggers and at no other time.** A rebuild
re-reads everything the installation states and re-derives everything it has
learned, wholesale: the keys every caller pays with, the registry, pool
membership, the admin disabled map, and quality from the journal tail. There is
no partial variant and no mode flag — what makes that affordable is that a
rebuild happens per process per day, not per request
([`decisions.md`](../decisions.md#the-broker-is-the-installation-a-caller-is-a-scope)).

1. **At start**, when the pool is provisioned. This one alone refuses an
   installation with nothing at all, naming the deploy step that would fill it.
2. **On the refresh clock** below, whatever the check decided — the clock is what
   re-reads a peer's registry edit and re-derives quality daily, so it rebuilds
   even when the arriving list changed nothing.
3. **On an explicit `sync()`**, applied or not: a sync bootstraps keys from the
   environment on every run, and a key that has just appeared is the reason a host
   calls one.
4. **On pool exhaustion**, debounced, and only for a caller that is not already
   holding every key the pool names. This is the reactive path: a key just stored
   is what it exists to find, so a caller with the full set has nothing to look
   for and the ports are left alone — its pool is exhausted for some other reason,
   which no key would fix. An empty pool is not a full set: there the registry
   itself is what needs re-reading. The debounce is what stops a pool that stays
   exhausted under traffic from asking the ports on every call.

   **The call that carried the re-read takes the answer with it.** The re-read
   happens inside that request, so the caller has already paid its latency; leaving
   it with the error as well would be paying twice for nothing. It takes one more
   pass over the pool, and that pass queues for nothing — a caller's `wait` is a
   promise about how long it may be held, and a second pass may not spend it again.
   So the pass succeeds exactly when the re-read made a model answerable at once,
   which is the case it exists for. Nothing is retried past the first delta of a
   stream (invariant 18).

   **A key that is not the only one a caller holds waits for the slow clock**, and
   that is deliberate: while some model still answers, the caller is being served,
   and a second key changes nothing until the first stops working — at which point
   the pool exhausts and this trigger fires.

**Keys ride the rebuild**, and are never declared to it or polled for
([`decisions.md`](../decisions.md#a-key-is-found-not-declared)). One enumeration of the secrets store per rebuild
answers which refs are held, so a ref nobody has a key for costs no read however
many callers ask for it; a value is read on first use and dropped at the next
rebuild, which is how a value an admin replaces takes effect within one period.
A backend that cannot enumerate is asked ref by ref instead
([`backends.md`](backends.md)). A key a provider has just rejected is dropped at
once, and a rebuild that re-reads the same value keeps the rejection
([`selection.md`](selection.md)).

**The pool-health alarm rides the rebuild too**, so the usable-provider count and
the missing-key report describe the last rebuild and nothing older
([`selection.md`](selection.md)).

### Two gates

- The **time gate** decides whether to go to the network at all. It is a
  monotonic comparison at the top of the lazy pool initializer, the funnel every
  public operation already passes through, so an idle process performs no I/O
  and schedules no wakeups — the library needs no running service of its own. A
  background timer would have to be owned, cancelled and tested against every
  embedding, for a process that has no list to keep fresh.
- The **identity gate** decides whether what arrived changes anything (see
  ["The report"](#the-report) above). It also removes the need for a
  conditional GET, which would save a kilobyte and no round trip while proving
  strictly less.

**Where the automatic fetching is off there is no time gate at all**: nothing is
armed, and an empty registry is not filled before provisioning either —
provisioning raises and the error names the deploy step that would have filled it.
The curated preset and the paid catalog stop together, since the reason to forbid
one forbids the other, and the catalog read a declared alias makes on its way to
being resolved stops with them ([`direct-by-name.md`](direct-by-name.md)) — the
process opens no connection of its own at all.

**The explicit sync is never gated by any of this**, whatever the clocks are set
to: the caller asked for it and handles what it raises. With no argument it syncs
what this installation follows — its preset, or, where it follows none, the paid
catalog alone, which merges nothing into the registry and therefore returns no
report. An installation following neither has nothing to fetch and does not.

**A check that just happened is remembered across process exits**, per (list,
target), so a short-lived process does not pay a round trip per invocation and a
rolling deploy does not fetch once per pod. The record only ever makes checks
less frequent: it is not authoritative, a timestamp in the future counts as
absent, and losing it costs one extra fetch. It is deliberately not shared
across a cluster — N nodes cost N small GETs a day, unmeasurable against the
fleet's own LLM traffic, and the identity gate already makes concurrent
application a no-op.

**The same interval carries the paid catalog.** A declared alias rides on that
one clock and no other, whether or not this installation syncs a list at all
([`direct-by-name.md`](direct-by-name.md)). A catalog nobody can reach may not
fail the sync of the model list itself.

**A fetched list is cached, and the cache is a fallback rather than a
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

### Failure is never the caller's problem

**The refresh is off the critical path, with one exception.** An empty registry
is filled before provisioning, blocking, because provisioning an empty registry
raises and there is no alternative; a registry that already holds a list is
provisioned from it and refreshed afterwards, so the first call of a fresh
process does not wait on the network.

It is **best-effort and never raises**: a fetch failure or a refusal logs a
warning naming which check it was, stashes the report where there is one, and
continues on the existing configuration. Neither a start nor a request ever
fails over a list refresh. The explicit `sync()` call raises instead — that
caller chose to sync and has a plan. The start attempt is guarded by its own
flag, so a provision that failed for another reason and is retried does not
re-fetch.

**Picking up another process's edits may never fail the call that carried it
here.** The re-read rides on a rebuild whose reactive trigger is an exhausted
pool — a call that has already failed. A registry that cannot be read, or that
has been edited into a state the broker rejects, logs and leaves the live pool
exactly as it is; it never surfaces out of the caller's own `ask`, and never
replaces one error with another.
Only the paths a caller asked for by name may raise: provisioning, an explicit
`sync`, and a `direct()` naming one model — a `direct()` whose target has become
ambiguous has no answer to give and must say so rather than guess.

The background refresh runs as a detached task, so anything it does not catch is
lost as an unretrieved exception and the refresh silently stops for the life of
the process. It therefore catches everything, rather than the failures a given
code path happened to be written with.

### The accepted exposure

The catalog's default branch is live configuration for every installation
([`decisions.md`](../decisions.md#unconditional-list-refresh)). A `base_url`
decides where an installation's API keys are sent, so a config built from a
*fetched preset* must carry `https://` ones.
