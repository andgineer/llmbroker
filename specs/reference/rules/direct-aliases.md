# Direct access and stable aliases

Reaching a host's own model by name, and the paid catalog whose aliases keep
that name current. The free pool is [`presets.md`](presets.md). The
cross-cutting rules this file elaborates are in
[`../invariants.md`](../invariants.md).

Application code must not change when a model version changes. A deployment that
follows the curated recommendation for "opus" keeps calling `direct("opus")`
while the catalog moves that alias from one generation to the next; only the
minority that genuinely needs a fixed version pins one.

**Only user-owned entries are reachable directly.** Pointing `direct` at a
preset-managed pool entry raises `PoolModelError`. The pool is anonymous by
design: its members are reached through the routing methods, which route and
learn.

**A custom entry carries two identifiers with disjoint roles.**

- *name* — the full identity, following the convention preset entries already
  use: the provider id, then the model id. It carries the version, and the
  registry, journal, learning and visibility all key on it exactly as they do
  for pool entries. For an alias entry the tooling writes it, because it must
  change when the followed version does.
- *alias* — the eternal handle: what the application passes, and the id of the
  catalog line the entry follows. It never carries a version.

**Alias presence is the followed/pinned switch.** With an alias, the entry's
provider fields are catalog-managed and a refresh rewrites them. Without one,
the entry is entirely the user's and a refresh never touches it — a pin needs no
syntax of its own.

**So a followed entry is not the host's to hand-edit — and neither is the file it
sits in.** Both forms are added by command and stored in a file llmbroker
regenerates
([`sync-merge.md`](sync-merge.md#the-lineup-file-is-written-never-authored)); a
pin differs only in that no refresh moves it.

**A paid model is declared in code or in data, and the two forms follow the same
rule.** Declaring on the broker takes a paid-catalog alias (whose version
llmbroker tracks) or a full config (whose version the caller tracks); a custom
config block is the same two forms written in a file. What differs is only when
the alias is followed: a declared model is re-resolved at every provision, a
stored one at every sync.

**Declared models are overlaid, never stored**
([`decisions.md`](../decisions.md#declared-models-are-not-stored)). They are
appended to the lineup the pool reconciles against and to what `direct()` looks
up. Re-resolving instead of persisting is what keeps an alias pointing at the
current model with no sync involved.

**A declared model is re-resolved on the refresh clock, not on every read.**
`direct()` is a request path, so it reads a resolution already made; what moves
that resolution is the catalog underneath it being refreshed. The resolution
reads the copy already on the machine wherever there is one, so provisioning
does not wait on the network; where nothing is writable the read is a fetch,
which is the price of keeping no state at all. One refresh costs one resolution
however many calls are in flight when it lands.

**The paid catalog carries its own refresh clock.** Where a lineup is synced,
that sync reads the catalog and keeps it current. Where none is — a registry a
deploy job fills, and a broker told to sync nothing — the same interval still
refreshes the catalog on its own, because otherwise a declared alias would have
no clock at all and would sit on the version the installed release shipped with.

**Only the first resolution may fail; every later one keeps what works.** A
catalog that cannot be read, or that no longer carries an alias someone
declared, leaves the declared models on the resolution already in use, with a
warning — the same rule the stored half follows, and for the same reason: a
refresh that cannot see upstream has nothing to say about where an alias points.
The wheel's copy is excluded from a re-resolution outright, since where nothing
is writable it would otherwise be the only fallback left and would move a
working alias *backwards*. Only the first resolution has nothing to keep, and
only it raises.

An alias the catalog does not carry therefore raises at provision and names the
aliases it does: a typo is the expected failure and the fix is one word. A
declared model whose name or alias is already in the registry raises too, naming
both sources — that is the one collision the registry's own uniqueness rules
cannot see. A declared model with no key behaves exactly as a keyless stored
entry: it exists, and `direct()` on it reports the missing key.

**A declared model's key is bootstrapped like a stored one.** A key the
environment holds is copied into a writable secrets backend when the declaration
resolves, the same way a sync does it for the lineup it writes. Without that,
declaring in code would be dead wherever secrets live in a backend rather than
the environment, while the identical stored entry worked — and nothing else
would ever carry that key across, since a declared model is never synced.

**Learning resets by name change, with no dedicated mechanism.** A refresh
rewrites the model id and the entry name together, so journal rows for the old
name orphan naturally and the new model starts clean. Scores learned for one
version never carry to another.

**The two lookup keyspaces are disjoint, and naming one is a version
assertion.** A call names either an alias or a name, never one string that could
be both — so there is no cross-uniqueness rule to enforce, call sites document
themselves, and asking for an entry *by name* fails loudly once a refresh has
moved the alias on, instead of silently running a newer model. A miss whose
string exists in the other keyspace says so.

**Aliases are unique across the whole catalog** and permanent: a published alias
never disappears and never renames — a generation change re-points it at the
successor model. A duplicate makes the catalog invalid and is refused.

**A name identifies exactly one entry, across both arrays.** Every store keys on
it — a DB registry's primary key, the live pool's slot map — so a config
carrying a name twice does not raise an ambiguity to resolve later, it loses an
entry at the next sync. An alias entry's name is machine-formed in the same
convention preset pool entries use, so a catalog move can land one on the other;
that is refused where it would be introduced rather than tolerated. Uniqueness
of names and of aliases is decided when a lineup file is read, and reading one
as a registry and reading it as a sync source are one judgement, so a file
cannot be valid for one and invalid for the other. A collision the *merge*
creates between two individually valid lineups is caught separately, on the
result. Either way the error names the fix, and for an alias entry the fix is
never a rename: the name is machine-formed again on the next refresh. When a
name does
resolve, the user's own entry is what it means: `direct()` by name searches
custom entries, and a pool entry of that name only decides which error comes
back.

**An alias is followed at sync, wherever the lineup lives.** Every sync
re-points the alias entries of the lineup it is merging at what the catalog now
recommends — a config file and a database registry alike, from one place — and
prints or logs one line per change; an alias the catalog no longer knows is a
warning and its entry is left untouched. The catalog is read only when something
actually follows an alias, so an installation with none pays no second network
read, and a catalog nobody can reach leaves every alias entry exactly as it is.
Nothing consults it between syncs: a running pool never looks a model version
up.
