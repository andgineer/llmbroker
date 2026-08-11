# Presets and the free pool

Where the routed pool's model definitions come from, how a curated list reaches
an installation, and the commands that materialize one. Reaching a host's own
paid model is [`direct-aliases.md`](direct-aliases.md); how a list becomes the
registry is [`sync-merge.md`](sync-merge.md). The cross-cutting rules this file
elaborates are in [`../invariants.md`](../invariants.md).

## Preset distribution

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

## The pool is exactly the curated list

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

## Key acquisition help

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
only genuine alarm is **zero** keyed configs at all — see [`pool-health.md`](pool-health.md).

There is no background key re-resolve loop: a key added to the environment after
startup takes effect at the next provisioning (a fresh process, or an explicit
re-provision) or immediately if the host calls `sync` again, which re-bootstraps
any newly resolvable secrets — never via a polling task.

## The CLI

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
  ([`direct-aliases.md`](direct-aliases.md)). One model per line and no
  decoration, so a later filter is a projection of the same rows rather than a
  second formatter.

**No command writes a model list.** A list is filled by a sync, and a model
reached by name is declared where the application that calls it is configured, so
there is nothing left for a command to append
([`sync-merge.md`](sync-merge.md#the-model-list-file-is-written-never-authored)).

**The CLI has no merge site.** Refreshing a list is the application's own
entrypoint calling `broker.sync(...)`, built by the same factory the application
uses — the library owns the operation, the host owns the connection. A CLI that
merged would either duplicate connection config the application already owns
(syncing one database while serving from another is a silent failure) or decide
removals blind to keys only the running process can resolve.
