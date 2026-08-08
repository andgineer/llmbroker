# Presets and the free pool

Where the routed pool's model definitions come from, how a curated list reaches
an installation, and the commands that materialize one. Reaching a host's own
paid model is [`direct-aliases.md`](direct-aliases.md); how a lineup becomes the
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

**The floor holds a lineup up; it never moves one.** The bundled copy is older
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
last saw. The difference matters most outside the library: the `preset` command
hands someone a file they will keep, and one frozen at an installed release must
not look like one just fetched.

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

## The pool is exactly the curated lineup

The pool is the curated preset and whatever a sync kept from an earlier one. A
model the host declared — in a config block or in code — is reached by name
through `direct` and is never routed, never failed over onto, never learned from
as a pool member (invariant 4,
[`decisions.md`](../decisions.md#nothing-declared-enters-the-pool)).

A pool marker left in a hand-written custom block, or in a registry row written
by an older release, is ignored rather than rejected: it is a field that no
longer exists.

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

- **`env <config-or-preset>`** emits a `.env` skeleton of `api_key_ref` names,
  in file declaration order, each with its help text. The argument is a local
  config file when one exists at that path, otherwise a preset name fetched the
  same way a preset is fetched — so a first-time user needs no local file at
  all. Onboarding is folded into this command rather than a separate
  setup/status command, to keep the CLI surface small.
- **`preset <name>`** prints a curated preset to stdout, or regenerates a lineup
  file from it: the pool entries and their keys from the preset, and every
  alias-following entry from the paid catalog. It prints the `SyncReport`
  on every run, no-ops included, and exits non-zero only on a failure — a
  pending key or a kept entry is a valid state, not an error.
- **`add-model`** picks a paid provider and model from the curated catalog and
  appends it as a custom entry, following the alias contract in
  [`direct-aliases.md`](direct-aliases.md): it follows the catalog's alias by
  default so later refreshes keep it current, and a pin flag writes a
  version-pinned entry instead, which no refresh touches. Both land in the lineup
  of the installation the command is run against, and a name or alias already in
  it is refused. It is the only way a host's own model enters a lineup by hand:
  everything else about that file is llmbroker's
  ([`sync-merge.md`](sync-merge.md#the-lineup-file-is-written-never-authored)).

**The CLI writes files only, and a DB-shaped refresh target is refused.**
Mirroring a lineup into a registry is the application's own entrypoint calling
`broker.sync(...)`, built by the same factory the application uses — the library
owns the operation, the host owns the connection. A CLI that took a DSN would
duplicate connection config the application already owns (syncing one database
while serving from another is a silent failure) and would force DB credentials
into the CLI's environment, which an application fetching its DSN from Vault
cannot supply.
