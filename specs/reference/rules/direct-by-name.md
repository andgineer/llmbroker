# Direct access and stable aliases

Reaching a host's own model by name, and the paid catalog whose aliases keep
that name current. The free pool is [`model-list.md`](model-list.md). The
cross-cutting rules this file elaborates are in
[`../invariants.md`](../invariants.md).

Application code must not change when a model version changes. A deployment that
follows the curated recommendation for "opus" keeps calling `direct("opus")`
while the catalog moves that alias from one generation to the next; only the
minority that genuinely needs a fixed version pins one.

## The four kinds, and where each is stated

Two axes, both carried by *where the entry is stated* rather than by a field:

| kind | stated by | parameters from |
|---|---|---|
| `pool_preset` | a sync into the registry | our curated preset |
| `pool_custom` | the installation, into its own registry | the installation |
| `direct_preset` | a curated alias passed to `direct=` | our curated paid catalog |
| `direct_custom` | a fully stated config passed to `direct=` | the installation |

The first word is the class — routed anonymously, or reached by name. The second
is who supplied the parameters. Both are legible from the call site, and no
combination outside these four can be written down.

**A model reached by name is declared in code, never stored**
([`../decisions.md`](../decisions.md#a-model-reached-by-name-is-declared-in-code)).
The registry holds pool members only, so the class is not a fact about a stored
row; in the declared form both halves are the type of the argument. Only the
second word is ever recorded, and only on a stored entry, where both values can
occur — that is the one bit the merge partitions on
([`model-list.md`](model-list.md#the-partition-a-sync-touches-only-what-a-sync-wrote),
[`../decisions.md`](../decisions.md#the-kind-of-an-entry-is-not-a-stored-field)).

**Only declared models are reachable directly.** Pointing `direct` at a stored
entry raises `PoolModelError`. The pool is anonymous by design: its members are
reached through the routing methods, which route and learn.

## The alias contract

**A declared model carries two identifiers with disjoint roles.**

- *name* — the full identity, following the convention preset entries already
  use: the provider id, then the model id. It carries the version. For a model
  declared by alias the resolution forms it, because it must change when the
  followed version does.
- *alias* — the eternal handle: what the application passes, and the id of the
  catalog line the declaration follows. It never carries a version.

**Which argument was passed is the followed/pinned switch.** A curated alias is
catalog-managed and a re-resolution rewrites its provider fields. A fully stated
config is entirely the caller's and nothing moves it — a pin needs no syntax of
its own.

**Declared models are overlaid, never stored**
([`../decisions.md`](../decisions.md#declared-models-are-not-stored)). They are
appended to what `direct()` looks up, and never to what the pool routes over.
Re-resolving instead of persisting is what keeps an alias pointing at the current
model with no sync involved.

**A declared model is re-resolved on the refresh clock, not on every read.**
`direct()` is a request path, so it reads a resolution already made; what moves
that resolution is the catalog underneath it being refreshed. The resolution
reads the copy already on the machine wherever there is one, so provisioning
does not wait on the network; where nothing is writable there is no refresh to
move it ([`model-list.md`](model-list.md)), and it stays on what the
first read reached. One refresh costs one resolution however many calls are in
flight when it lands.

**Where the process fetches nothing automatically, neither does this read**
([`model-list.md`](model-list.md)). It takes the copy on the machine, and
failing that the wheel's copy, and the alias stays frozen there until an explicit
sync moves it — the same floor the read already falls to when the network is
unreachable ([`../decisions.md`](../decisions.md#no-automatic-fetch-means-none-at-start-either)).
With neither copy present the first resolution raises and names the sync to run.

**The paid catalog carries its own refresh clock.** Every sync reads it on the
way past, and where no model list is synced — a registry a deploy job fills, and a
broker told to sync nothing — the same interval refreshes it on its own, because
otherwise a declared alias would have no clock at all and would sit on the
version the installed release shipped with. Where there is no clock at all, that
is exactly what happens and is what the operator asked for. The catalog is read
only when something actually follows an alias, so an installation with none pays
no second network read.

**A version move is reported once, where it happens.** A re-resolution that
lands a declared alias on a different model id, or on a different
`api_key_ref`, logs one line naming both — the only notice a deployment gets
that `direct("opus")` now answers from a different model. The first resolution
has nothing to compare against and reports none.

**Only the first resolution may fail; every later one keeps what works.** A
catalog that cannot be read, or that no longer carries an alias someone
declared, leaves the declared models on the resolution already in use, with a
warning: a refresh that cannot see upstream has nothing to say about where an
alias points. The wheel's copy is excluded from a re-resolution outright, since
where nothing is writable it would otherwise be the only fallback left and would
move a working alias *backwards*. Only the first resolution has nothing to keep,
and only it raises.

An alias the catalog does not carry therefore raises at provision and names the
aliases it does: a typo is the expected failure and the fix is one word. A
declared model whose name or alias is already in the registry raises too, naming
both sources — that is the one collision the registry's own uniqueness rules
cannot see. A declared model with no key behaves exactly as a keyless stored
entry: it exists, and `direct()` on it reports the missing key.

**A declared model's key is bootstrapped like a stored one.** A key the
environment holds is copied into a writable secrets backend when the declaration
resolves, the same way a sync does it for the list it writes. Without that,
declaring in code would be dead wherever secrets live in a backend rather than
the environment, while the identical stored entry worked — and nothing else
would ever carry that key across, since a declared model is never synced.

**Learning resets by name change, with no dedicated mechanism.** A re-resolution
rewrites the model id and the name together, so journal rows for the old name
orphan naturally and the new model starts clean. Scores learned for one version
never carry to another.

## Uniqueness

**The two lookup keyspaces are disjoint, and naming one is a version
assertion.** A call names either an alias or a name, never one string that could
be both — so there is no cross-uniqueness rule to enforce, call sites document
themselves, and asking for a model *by name* fails loudly once a re-resolution
has moved the alias on, instead of silently running a newer model. A miss whose
string exists in the other keyspace says so, and one whose string names a pool
member says that instead.

**Aliases are unique across the whole catalog** and permanent: a published alias
never disappears and never renames — a generation change re-points it at the
successor model. A duplicate makes the catalog invalid and is refused.

**A name identifies exactly one entry.** Every store keys on it — a DB
registry's primary key, the live pool's slot map — so a list carrying a name
twice does not raise an ambiguity to resolve later, it loses an entry at the next
sync; that is decided in the one place a list is parsed, so a stored list and
an arriving curated one are held to one judgement. Among declared models the same
holds, and so does uniqueness of aliases: both are checked where the declaration
is overlaid, against each other and against the registry, since a declared model
colliding with a stored one is the one case neither side can see alone.
