# Decisions

Why a contested decision went the way it did. **Open one entry by its anchor —
this file is not meant to be read whole.** The rules themselves live in
[`rules/`](rules/); the ones whose violation is silent are in
[`invariants.md`](invariants.md).

**Before proposing a mechanism, check whether it is already here.** An entry
records the alternative it blocks and the counter-argument the new proposal is
about to hand-wave. If a recorded decision is genuinely wrong now, argue against
the recorded reason explicitly — never re-propose in silence.

**An entry earns a place here only if it names a plausible alternative.** A rule
nobody would dispute needs no defence, and writing one costs every future reader
the tokens to skip it.

---

## Learning and quality

### learning-from-the-journal

Everything llmbroker learns is re-derived from the append-only call journal.

**Blocks:** a summaries table with decayed aggregates and per-backend atomic
folds.
**Why:** a Wilson bound over a journal window gives the same verdicts. Decay
("multiply, then add") is not expressible as an atomic increment, so it forces
CAS folds in every backend; and an append-only journal cannot be incremented in
place at all, so the "aggregate from rows" path has to exist regardless. Even
the cheap counter form is not worth a second mechanism.

### self-contained-quality-records

A rating carries (model, operation, score) by itself and is never joined to the
call it rates.

**Blocks:** updating the call row with its score, or stitching a score to its
call by id during recomputation.
**Why:** a rating may arrive after retention has purged the call row. Self-
contained records make an arbitrarily late rating land correctly and keep the
append-only invariant absolute. A call id survives only as an opaque passthrough
for the host's own UI, never read by llmbroker.

### wilson-for-demotion

Demotion requires a minimum count and a Wilson upper bound below the floor.

**Blocks:** a raw mean; tiers with a global verdict.
**Why:** demote only when even the optimistic estimate sits below the floor.
Tiers are not a concept here — selection is a sort, and no global "bad across
everything measured" verdict exists.

### blend-for-ranking

Ordering uses a shrinkage blend of curated weight and observed mean, not the
Wilson bound.

**Blocks:** reusing the demotion bound to order the pool.
**Why:** the bound is deliberately optimistic on thin evidence, so at equal true
quality it sits *lower* for the better-sampled model. The leader would yield to
a barely-tried one, recover, and oscillate.

### curated-priority-not-round-robin

The best available model takes all traffic until it cools.

**Blocks:** round-robin across available models.
**Why:** a request that hits quota exhaustion spills to the next model
transparently. One extra roundtrip at that moment is the price of maximizing
answer quality.

### no-llm-judge

Quality ratings stay host-supplied.

**Blocks:** an LLM scoring the pool's own replies to generate ratings.
**Why:** a host that cares about quality already holds a better signal than a
judge could infer — whether the JSON parsed, whether extraction validated,
whether the user accepted the answer — and the rating API is public for exactly
that. A weaker model judging a stronger one's reply is a poor proxy. It is also
unaffordable where it would be needed: windows are per (model, operation) and
need ten ratings apiece, so a small pool sampling a fraction of its traffic
reaches a verdict slower than its free models are retired upstream, and every
judge call spends the scarce quota the pool exists to conserve.

### no-bandit-machinery

**Blocks:** ε-exploration, usable-rate floors, latency ranking, auto-retirement.
**Why:** a chronically failing model is already effectively disabled by
exponential cooldown; the only thing auto-removed is a dead key.

### budget-expiry-teaches-ordering

An expired caller budget is journaled as the budget the model failed to answer
within, and a latency lower bound is derived from the journal alongside the
cooldowns and quality windows, reordering the pool for equally tight budgets.

**Blocks:** discarding the expiry as pure loss; counting it as a failure or a
cooldown; holding the bound as pool-local state no journal read produces;
recovering it by matching the error text of a row.
**Why:** a model that never answers produces no successful rows, so this is the
only obtainable latency evidence — but blaming a model for the caller's clock
would teach the broker that healthy models are failing. Ordering only,
budget-relative, and never a withdrawal. Keeping the evidence on the row makes
the bound one more thing the single tail read derives, rather than a second
state subsystem beside it; keeping it as its own field rather than as prose in
the error detail keeps a message the library formats out of a routing decision.

---

## The call path

### client-4xx-never-cools

**Blocks:** cooling a model on any 4xx.
**Why:** cooldown exists to stop hammering a model that is unavailable. A 4xx
that is not 429/401/403 means the request itself was rejected, which says
nothing about availability — so the call fails over, excludes the offender for
that call only, and the caller gets the provider's own actionable error rather
than a generic "no model available".

### one-error-with-a-reason

"No model available" is one exception class carrying a machine-readable reason
and, when applicable, the earliest expected return.

**Blocks:** a separate exception class per cause.
**Why:** the causes (empty pool, no key, all disabled, all excluded, timeout)
differ in data, not in handling.

### typed-exception-per-failure-state

Failure states a host must tell apart get their own types, and lifecycle
failures root at `RuntimeError`, separately from request errors.

**Blocks:** distinguishing conditions by matching message text.
**Why:** an empty registry is benign and expected; a schema version this release
cannot use is fatal and operator-actionable. Catching them together reports "not
configured yet" when the store is unusable.

### latency-budget-per-call

**Blocks:** a per-model timeout knob.
**Why:** a budget is the caller's, and a per-model number could not compose with
failover once a call spans several models.

---

## Storage

### cooldown-from-the-journal

Other instances' cooldowns are read from failing journal rows.

**Blocks:** a state store — protocol, port, dedicated table, reconcile,
short-TTL cache — and the Redis backend whose only role was to host it.
**Why:** a failing row is already written and already carries what a cooldown
needs. Coordination is advisory anyway: correctness comes from failover, and the
cost of staleness is one wasted roundtrip.

### aggregates-derived-not-accumulated

**Blocks:** stored counters with day buckets, rotation and subtraction.
**Why:** a sliding window cannot be served by a monotonic counter — old calls
must fall out on their own. Counters mean a second piece of stored state, its
own ageing logic, and an atomic UPDATE on the hot path of every call, and they
must eventually disagree with the journal after a restart, a retention purge, or
across nodes. If read volume ever justifies it, the answer is a TTL cache over
the aggregate, not counters.

### per-status-counts-not-a-failure-rate

**Blocks:** a failure-rate number computed by the library.
**Why:** llmbroker does not decide what counts as a failure, how long a window
should be, or how a model with no calls should read. A number whose meaning is
fixed by the library is wrong for the next consumer.

### store-is-not-logging

**Blocks:** plain-text log-style emission of store events into python logging.
**Why:** the store holds raw material llmbroker reads back, not a human-facing
log. The application logs by its own means.

### no-schema-migrations

**Blocks:** an `ALTER`-based migration path.
**Why:** create-if-missing plus fail-fast on a version mismatch, with
instructions. Upgrading means dropping the `llmbroker_*` objects.

### schema-marker-inside-the-namespace

**Blocks:** using SQLite's `PRAGMA user_version` as the version marker.
**Why:** the file header belongs to the embedding application, which in the
documented shared-file setup owns the whole database. A file left by a release
that stamped the header is adopted once — the marker object is created from that
value and the header handed back.

### no-wal-management

**Blocks:** a `PRAGMA journal_mode=WAL` or a configurable `busy_timeout` in the
SQLite driver.
**Why:** journal mode is a persistent, file-level property belonging to whoever
owns the file. The driver receives only a path and cannot tell a shared file
from a broker-only one, so enabling WAL during schema setup would silently flip
a shared file's mode. Cross-process schema DDL already serializes under the
default busy timeout. **Future edits must not add either.**

### scope-is-an-opaque-string

**Blocks:** a typed `user_id`, with per-user registry and learning partitions.
**Why:** storage and protocols know nothing about users; the broker builds the
prefixed ref and owns the personal-to-shared fallback, so secrets backends stay
flat key-value stores with no second dimension. Quota scoping does not depend on
any of it — it follows the hash of the key's value, so identical keys merge into
one quota scope by themselves. A string also avoids a `42` vs `"42"` collision.

### single-source-parameter

**Blocks:** a dedicated backend-bundling layer.
**Why:** registry, store and secrets all derive from one parameter recognized by
its DSN form, with the driver imported lazily. Explicit port arguments remain
for mixed configurations.

---

## The lineup

### sync-is-the-only-registry-writer

**Blocks:** a CRUD path (add/update/remove); registry cloning.
**Why:** the registry is a projection of the arriving lineup merged with what is
already there. Admin runtime verbs are disable/enable and keys; learning writes
nowhere. A node must never coerce the shared registry to a local copy of its
own — diverging copies would flip-flop it in a cluster.

### unconditional-lineup-refresh

**Blocks:** a switch that freezes the model list while the process keeps serving
from it; pinning the fetch to the installed version's tag.
**Why:** a pinned free-tier lineup decays, because providers retire free
endpoints without notice. What such a switch appears to protect against — an
unreviewed lineup change — is already bounded by the removal rule, and what it
buys is a pool that decays to nothing. Pinning to a tag would mean a preset fix
reaches nobody until a release of llmbroker, which is the problem the refresh
exists to remove. The accepted cost: the catalog's default branch is live
configuration everywhere, bounded by requiring `https://` endpoints in a fetched
preset. Switching the *automatic* fetching off is a different act and is offered,
because it moves the fetching rather than ending it
([`no-automatic-fetch-means-none-at-start-either`](#no-automatic-fetch-means-none-at-start-either)).

### no-automatic-fetch-means-none-at-start-either

Turning off the automatic refresh turns off the fetch that fills an empty
registry at the first provision. An installation that has said it does its own
fetching gets an error naming that, not a fetch it forbade.

**Blocks:** keeping the start-fill alive as "just once per process"; a second
switch to control it separately; falling back to the bundled preset to fill a
registry the operator meant to fill itself.
**Why:** "once per process" is not a bound an operator can hold — a serving fleet
restarts, scales out and cycles pods, so a fetch that happens once per process is
a fetch that happens on request traffic, which is exactly what the switch was
thrown to prevent. The failure it produces instead is the good one: it happens at
the first call, names the missing deploy step, and cannot be mistaken for a
network problem. A second switch would let the two be set inconsistently, and
there is no installation that wants a fetch at start but not later — the reason
to forbid one forbids both.
**Accepted cost:** an operator who sets the switch and forgets the deploy step
gets a broker that will not serve. That is the intended outcome; the alternative
is a broker that serves and quietly fetched to do it.

**The paid catalog is the exception, and it is not the registry.** With no
automatic fetching, a declared alias resolves from the cache, then from the
wheel's copy, and the resolution freezes at that copy until the operator syncs
again. An empty registry means nothing can be served at all, and the bundled
preset would answer a question the operator said they would answer; a declared
alias is one model the operator already named, and the wheel's copy is the floor
that same read already falls to whenever the network is unreachable. Refusing
here would only turn "offline because it is forbidden" into a failure that
"offline because it is down" does not produce.

### admin-verdict-in-the-store

**Blocks:** a disabled field on the registry; a sibling-JSON overlay next to the
config.
**Why:** it survives a sync by construction, since a sync only touches the
registry, and it works identically for file and DB sources. It is not a journal
write either — retention would silently erase the verdict.

### zero-config-default

A bare broker runs the curated pool out of the home directory.

**Blocks:** a mandatory config file for everyone; selecting curated pool models
by name.
**Why:** the file used to be required of everyone while carrying a decision for
almost nobody, and now that the lineup refreshes itself there is nothing in a
copy to maintain — asking a user to keep one is asking them to hold our state,
and no host names a lineup file at all any more
([`the-lineup-file-is-not-a-path-a-host-names`](#the-lineup-file-is-not-a-path-a-host-names)).
Selection by name was rejected with it: free-tier entry names carry the model version and are
rewritten on every bump, so it would need a permanent per-entry handle the
preset does not have and the curator would have to guarantee forever — and the
case is thin, since a model with no key is already inactive.

### nothing-declared-enters-the-pool

**Blocks:** a pool flag on an entry; `direct=` reaching the router.
**Why:** what the pool sells is failover across interchangeable free providers.
A self-hosted endpoint or a company gateway dropped into it by a constructor
argument would be spilled onto by a 429 it has nothing to do with. So pool
membership is not a field — it is "not reached by name" — and a field that is
always another field's negation is a way to disagree with yourself later. Putting
an endpoint of one's own into one's *registry* is a different act, made
deliberately and recorded there
([`a-sync-touches-only-what-a-sync-wrote`](#a-sync-touches-only-what-a-sync-wrote)).

### a-sync-touches-only-what-a-sync-wrote

Every registry entry records whether a sync put it there. A merge partitions on
that record: entries a sync wrote are the ones the removal rule may retire and
the arriving lineup may replace; an entry the installation stated itself is
carried through untouched, whatever the arriving lineup says. The default for a
new entry is *not written by a sync*, so anything reaching a registry by any
other route — a driver the host implements, a registry object it hands over, a
host's own mirror call — is protected without doing anything.

**Blocks:** deciding what a merge may remove from where the registry came from,
or from whether the broker was constructed with an object; inferring ownership
from the entry's shape; a per-registry "read-only" flag; a host write path into
the shipped backends' tables.
**Why:** ownership is a property of the entry, not of the backend class or the
constructor call. A host may implement a driver of its own that holds our
curated pool, and may equally hand over a registry object holding entries it
states itself — the construction path predicts nothing. It also makes the mixed
pool statable: the routed pool is whatever the registry states as pool members,
whether it came from the curation, from the host, or from both. The host's side
of that is always the port, never our table: invariant 13 promises nothing about
a column name, so a hand-written row is a deploy script that breaks on an
upgrade with no error to read.
**Accepted cost:** one more fact stored per entry. It rides in the metadata
column that already carries the optional fields, so no schema changes; in the
lineup file the fact is structural already — the file is llmbroker's own output,
so everything in it came from a preset — and the file format does not change
either.

### who-builds-the-registry-states-what-it-follows

A broker constructed with no registry, or with a connection string, follows the
curated preset by default: the installation is llmbroker's own and the string
only says where to keep it. A broker handed an already-constructed registry
object must be told what that registry follows — a preset name, or nothing at
all — and refuses to be built otherwise.

**Blocks:** a silent default for a host-supplied registry, in either direction;
deriving the default from the registry's contents; requiring the argument in the
zero-config or connection-string forms.
**Why:** in the object form both silent readings are wrong — a host that wanted
the curation would quietly not get it, and a host that did not would quietly get
its pool mixed with ours. One error message prevents both, and it fires at
construction, where the caller is looking. Deriving the default from what the
registry already holds was rejected for making the same call behave differently
on an empty and a populated database: a host that adds an entry of its own a
year later would silently lose the refresh.
**Accepted cost:** a cluster that constructs its registry object by hand writes
the preset name once in the factory it already has. The connection-string form —
what the docs use for that case — is unaffected.

### declared-models-are-not-stored

**Blocks:** persisting a declared model to the registry.
**Why:** it would create two sources of truth for one list — the constructor
call and the stored row — and re-introduce exactly the drift alias-following
exists to prevent. There is also no stored named model to persist it *as*:
declaring in code is the only form
([`a-model-reached-by-name-is-declared-in-code`](#a-model-reached-by-name-is-declared-in-code)).

### the-paid-catalog-is-curated-too

**Blocks:** dropping the curated paid catalog and alias-following, leaving a
host to state every paid model in full for itself.
**Why:** weighed against the volume it costs and kept. An alias is the only
thing that survives a model version bump, so without a curated catalog every
host pins a version and silently runs a retired model until it breaks. The
curation rides the same configuration path as the free lineup — unreachable
changes nothing, wrong cannot destroy a working config — so it adds no runtime
dependency, only code.

### a-model-reached-by-name-is-declared-in-code

A model an application calls by name is stated where that application is
configured: `direct=` takes a curated alias as a string, or a fully stated model
as a config object. The registry holds pool members only. The CLI shows what the
curated catalogs carry and writes nothing.

**Blocks:** a stored entry reachable by name; a lineup-file section for one; a
command that adds a model to a lineup; a pinned entry written by tooling.
**Why:** the stored form buys one thing — reaching a model by name without
touching application code — and pays for it with an entry class that is
routed-or-named, curated-or-stated, followed-or-pinned in every combination the
storage can express, of which half are meaningless and are held out by rules that
must each be enforced at every write and every read. The declaration form encodes
the same choice in the type of an argument, where an invalid combination cannot
be typed. A deployment that wants the name without a redeploy is asking for
configuration outside its own configuration, which is what a registry entry is
for pool members and is not for a model one line of code calls by name.
**Accepted cost:** an installation that reached a paid model by name after
running one command now writes one line where it constructs the broker, and a
cluster writes it once in the factory it already has. The catalog is still what
supplies the alias, and the CLI still prints it.

### the-kind-of-an-entry-is-not-a-stored-field

One fact is recorded on an entry: whether our curated preset supplied its
parameters. Which class it belongs to — routed, or reached by name — is not, and
neither is the combination of the two. A stored entry is a pool member because a
registry holds nothing else; a declared one is followed or stated by the type of
the argument that declared it.

**Blocks:** a `kind` enum on the entry; **a pair of booleans for class and
source**; a per-entry flag for reachable-by-name.
**Why:** an enum would carry four values of which storage could ever hold two,
and the other two would exist to be validated against — the shape this decision
exists to remove, re-introduced under a better name. The boolean pair is the same
objection arithmetic: the class bit is constant in every row that could hold it,
so it carries no information and buys no filter, because nothing holds routed and
named entries in one collection to filter. What remains is one bit, and it is the
one the merge already partitions on.
**Accepted cost:** a host reading an entry cannot ask it what kind it is. Nothing
does — the pool takes what the registry holds, `direct()` searches what was
declared, and the merge partitions on the one recorded bit.

### the-lineup-file-is-generated-not-authored

There is one lineup file and it is llmbroker's output. A sync renders it in full
and nothing else writes it; nothing invites a human to write in it.

**Blocks:** splitting it into an llmbroker half and a host-owned half; editing it
in place through a style-preserving TOML document library so hand-written
comments survive; refusing to sync a file that carries a comment.
**Why:** the alternatives all exist to protect text a human typed into the file,
and no path puts it there. Both forms a host declares — a model described in full
and a paid-catalog alias — arrive through `direct=` in code, so the file has
exactly one author. Splitting by ownership of the entry was
tried and reversed: it made "is this entry rewritten" a question about which file
an entry is in, but half the host's own models (the alias-following ones) still
lived in the generated half, so it bought comment preservation for some of them
and charged for all of them — a reserved filename, two refusals inside the second
file, uniqueness across the pair, and a rule about which file documents which key
ref that silently deleted key help. A document library would keep every comment
but returns the writer to editing a live file in place, which is the shape this
design left. Refusing on a comment stops a refresh the mission promises is
unconditional, and llmbroker's own curated presets carry comments. The accepted
cost is that a comment or an unknown key does not survive a sync; the note a
command may one day attach to an entry belongs on the entry as data.

### the-lineup-file-is-not-a-path-a-host-names

A lineup reaches an installation as a curated preset name and in no other shape.
The lineup file exists, but only as llmbroker's own storage inside its own
directory: no host passes its path to the broker, and no host hands it to `sync`.
A host that will not follow our curation supplies the whole pool through a
registry object it implements, or fills a database registry itself — and stops
following the curation there, since a registry that follows it is one the
refresh rewrites, host-supplied or not
([`sync-merge.md`](rules/sync-merge.md)).

**Blocks:** accepting a `.toml` path as the broker's source; accepting a path or
a registry object as a sync source; keeping the file registry on the public API
so a path stays reachable through it; a read-only file registry; a
freeze/pin knob that stops the refresh.
**Why:** the mission names obtaining a provider key as the *one* irreducible
admin act, and a lineup file a human maintains is a second one. The cost of
keeping the form was never lines of code: while one file is both llmbroker's
output and the host's input, every change to it has to answer *whose file is
this*, and answering that produced a two-file split — a reserved filename, two
refusals inside the second file, uniqueness across the pair — that was then
reversed whole ([`the-lineup-file-is-generated-not-authored`](#the-lineup-file-is-generated-not-authored)).
Removing the form makes the question unaskable. The variants all fail on the
same point: a read-only file registry still has to be written by hand, which is
the administration the mission excludes, and a frozen snapshot decays into
nothing because free endpoints are retired without notice. Keeping the file
registry public would drop the shorthand and keep the shape — the half-measure
this decision exists to stop; it stays importable from its own module as the
port the home lineup runs on. Accepting an object source while refusing a path
would preserve one workflow at the cost of the whole point: one source, one
sentence, no second answer to "where can a lineup come from".
**Accepted cost:** an organisation that wanted to approve a lineup in review and
roll it out from a deploy job loses that workflow — generating a file, reading
its diff in a pull request, and merging it into a database registry under the
removal rule. That persona wanted *control over what reaches production*, which
is a different product from a free pool nobody administers; if it returns it
returns as its own decision with a use case behind it, not as a leftover branch.
Moving an installation between backends is unaffected: it is the public
load/mirror pair, two lines in the deploy script that already holds both DSNs.

### the-floor-is-not-seeded-into-the-cache

The wheel's copy of a curated text is a fallback branch, read at the end of the
chain; it is never written into this machine's cache.

**Blocks:** seeding the cache from the bundled copy on first read, so the
precedence collapses to "cache, then network" and the flag that drops the floor
disappears.
**Why:** that flag exists because a read deciding where an entry someone
*already has* should point may not serve a copy older than the repository. Once
the wheel's copy is in the cache it is indistinguishable from one the machine
fetched, so a cold-cache offline sync would re-point stored alias entries
*backwards* to whatever the installed release shipped with — silently, and for
as long as it stayed installed. Keeping that guarantee under seeding needs a
marker on the cache entry and a tri-state read of it, which is more machinery
than the flag it removes. The accepted cost is that the floor stays a branch of
the fallback chain rather than a value in the cache.

### keyinfo-is-a-passthrough

**Blocks:** a closed effort/value vocabulary a preset section must conform to.
**Why:** an unknown value would otherwise be a parse error, and extending the
vocabulary would require a code release. Onboarding order comes from section
order in the file — the preset is already curated.

---

## Surface and shape

### no-alerts-api

**Blocks:** a pull-drain events/alerts API with debounce maps and realert
intervals.
**Why:** current state is visible from `snapshot()`'s raw fields plus the
pool-wide counts and the degraded predicate; the few human-actionable events are
log lines. There is no status enum and no priority rule — the UI chooses the
presentation.

### sync-wrapper-on-a-background-thread

**Blocks:** `asyncio.Runner` on the calling thread.
**Why:** `Runner` would serialize calls from multiple threads behind a mutex, so
a multi-threaded synchronous host would queue up its parallel LLM calls. A
background loop gives N threads honest parallelism.

### no-rate-limits

**Blocks:** tracked request/token caps per minute or per day.
**Why:** nothing tracks them anywhere. The per-LLM concurrency cap is the only
knob, and it serializes calls to one model rather than throttling by rate.

---

## Rejected

Mechanisms weighed and dropped that do not attach to a decision above.

- **Seeding the registry from a node's own local copy** — a node never coerces
  the shared registry to a copy of its own; the refresh follows the one shared
  upstream, and an explicit sync is what mirrors a vendored file into a database
  registry.
- **A deprecation-tier field** — an entry a lineup drops is either removed
  (losing nothing: keys live in the secrets store, statistics derive from the
  journal) or kept exactly as it was. There is no third, demoted state to
  represent.
- **A registry-stored learning profile written by llmbroker** — manual blocking
  is an admin verdict in the store; learning writes nowhere.
- **An explicit quality-reset operation** — rehabilitation happens through new
  ratings displacing old ones in the window.
- **A public manual-purge operation** — retention is automatic.
- **A key-deletion path in the secrets protocol** — a key that cannot be
  deleted, because it still pays for direct calls, is the common case, so
  deletion could never be the retirement mechanism. llmbroker reports the
  orphaned ref and a human decides.
- **An exact/mirror flag on sync** — indistinguishable from mirroring configs
  into the registry directly, which already exists as the escape hatch for a
  forced lineup.
- **"Two callable providers" as a pruning threshold** — a policy constant that
  would discard working free quota. The same number survives only as the
  *degradation* criterion, where it describes the failover feature rather than
  deleting anything.
- **Probing the provider for death evidence** — deferred; the journal is the
  evidence. Revisit only if it proves too thin.
- **A model-name filter on the journal's tail query** — the bounded tail is
  enough for a handful of candidates, and a filter would touch every backend.
- **A pool marker on a key** (or any field declaring "this key is not for the
  pool") — the state it describes is derivable, and the case it was invented for
  — a key kept for paid direct calls — is served by the unused-key report line
  plus death evidence.
- **A queue-plus-timer scheduling model** — a slot table with a condition
  variable, and no loop-bound timer state.
