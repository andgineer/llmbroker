# The lineup has one source, and llmbroker owns its file

## Goal

A lineup arrives from exactly one place: a curated preset name. The hand-named
config file stops being a configuration form — no `Broker("llms.toml")`, no
`sync("vendored.toml")`, no `preset --sync FILE`. What remains is the file
llmbroker keeps in its own directory, a database registry, and a registry object
the host implements itself.

This is the first plan in the queue that removes a capability rather than
tidying one. It must be taken before the remaining simplification plans, which
would otherwise spend work tidying code this one deletes.

## Why

`mission.md` says the free-tier pool is **zero administration**, and names its
boundary precisely: *obtaining a provider key is the **one** irreducible admin
act*. A lineup file a human maintains is a second one. Positioning 4 makes the
same point from the other side — llmbroker defines itself against competitors
that "leave all of this as configuration for the operator" — and the boundary at
line 37 has already handed the host who wants their own endpoints pooled to the
neighbouring product.

One mission sentence reads the other way: *An installation that must not follow
our curation states a lineup of its own instead of freezing ours.* Look at its
form — it is an argument against adding a freeze knob, answering that request by
redirection, not a promise that a file path is a supported shape. The redirect
still has somewhere to go after this plan: a database registry, or a
`RegistryProtocol` object, which is one method.

The cost of keeping the form is not measured in lines, and this is the part the
implementation has already paid twice. When one file is both llmbroker's output
and the host's input, every change has to answer *whose file is this*:

- `lineup-file-ownership` answered it with a two-file split — a reserved
  filename, two refusals inside the second file, uniqueness across the pair, and
  a rule about which file documents which key ref. Its review round 2 reversed
  the whole thing.
- Two of that plan's three round-1 defects were in that split.
- Its round-3 review re-opened the same question as `add-model --into`.

Remove the form and the question stops being askable. That is the return, and it
is larger than the deletion.

## The shape

| what a host gives `Broker(...)` | result |
|---|---|
| nothing | the curated pool in llmbroker's home directory |
| `postgresql://…`, `mongodb://…`, `sqlite://…`, `*.db` | that database, all three ports |
| an object implementing `RegistryProtocol` | that object; the host owns what it holds |
| a path to a `.toml`/`.json` file | **`ValueError`** naming the three above |

`sync()` takes a curated preset name and nothing else. The lineup file remains —
it is what the first row keeps its pool in, rendered wholesale on every refresh
and appended to by `add-model` — but no host names its path, so it has one
author and the question of ownership does not arise.

The CLI keeps the two commands that serve the mission and loses the third:

- `env` — which keys this installation needs and where to get them. This is the
  command for the one irreducible admin act.
- `add-model` — which paid model the host wants, the one thing llmbroker cannot
  decide for it.
- ~~`preset`~~ — printing the curated text served the form being removed. Its
  one remaining consumer was vendoring a lockfile, and vendoring is an
  organisation's policy rather than a mission requirement; a host that needs it
  fetches the raw URL itself.

## What this takes away

A plan that removes a form owes an honest list, not a reassurance. Reviewed
against what the library actually does today, not against what the docs claim.

**From the mission, exactly one thing.** Line 134 promises that an installation
which must not follow our curation *states a lineup of its own*. Today that is a
file: `Broker("my.toml", sync=None)` gives a pool over the host's own endpoints,
with routing, failover, cooldowns and learning, and it works. Afterwards the
same host supplies the same list as an object implementing `RegistryProtocol` —
one method returning `list[LLMConfig]`. Nothing about the pool changes; the
format the list arrives in does. That is the whole cost against the mission, and
it is why the sentence needs rewriting rather than deleting.

**Beyond the mission, four capabilities the docs carry and the mission never
promised.** All four serve one persona: an organisation that wants to approve
and control what reaches production, rather than one that wants a free pool it
does not administer. The mission handed breadth of endpoints to the neighbouring
product (line 37); it says nothing either way about approval, so removing these
is a boundary decision taken here.

1. **Merging a lineup the host states.** `sync("my-lineup.toml")` into a
   database registry applies the removal rule, keeps entries whose key still
   works, and re-points aliases — over the host's list rather than ours.
   Afterwards a host-supplied registry is filled by the host and llmbroker does
   not merge into it.
2. **Vendoring an approved lineup.** Generating a file, reviewing it in a pull
   request, and rolling it out from a deploy job.
3. **Migrating between backends** with `sync(other_registry)`. The replacement
   is two lines the host writes itself — `await new.mirror(await old.load())` —
   and it is currently undocumented, so this plan documents it (see Docs).
4. **A reviewable refresh diff** — `preset --sync FILE` producing a file whose
   change is read in review.

**One promise is removed that the code never kept.** `usage.md:130` prints

```python
llmbroker.Broker("llms.toml", sync="my-lineup.toml")   # yours, kept current
```

and it raises: a file registry accepts a curated preset name only, so this
combination has never worked. Deleting it removes a documented lie rather than a
capability. Do not treat its disappearance as a regression during review.

## Decisions this plan makes

**`Registry` leaves the public API.** Dropping the string dispatch while leaving
`llmbroker.Registry` exported would remove the shorthand and keep the shape — a
half-measure of exactly the kind this plan exists to stop. It moves out of the
top-level `__init__` and its `__all__`; it stays importable from its own module,
as the port implementation the home lineup runs on. `docs/…/secrets.md` shows it
as a recipe today and stops.

The rejected alternative — keep it public so a file registry stays reachable —
goes into `decisions.md` under `the-lineup-file-is-not-a-path-a-host-names`,
together with the read-only-file-registry variant (a file nothing generates any
more has to be written by hand, which is the administration the mission
excludes) and the frozen-snapshot variant (mission line 131: *a pinned lineup
decays into nothing*).

**`sync()` keeps no object form either.** Accepting a `RegistryProtocol` source
while refusing a path would preserve backend migration and the approval
workflow at the cost of the very thing this plan buys: one source, one sentence,
no second answer to "where can a lineup come from". Migration is two lines the
host writes with the public `load`/`mirror` pair, so what the object form
carries is the approval workflow — which the same `decisions.md` entry records
as deliberately dropped, naming the persona it served. If it comes back, it
comes back as its own decision with a use case behind it, not as a leftover
branch.

**A fetched preset may not carry `[[custom]]`.** `custom=True` means *the host's
own*, and a curated lineup declaring the host's own entries is a contradiction.
Refuse it where the https refusal already lives, so the whole file is rejected
before any merge sees it. Two consequences follow and are intended:

- `merge_upstream` loses `new_custom` and `arriving_custom`; the arriving lineup
  is managed entries only, and `current_custom` is carried as it is now.
- `SyncReport.updated` returns to counting managed entries only. This
  **deliberately reverts** the widening made in `lineup-file-ownership`'s review
  round 2, whose case — a vendored source moving a stored user model — cannot
  occur once a vendored source cannot arrive. It also closes that review's
  round-3 finding, that `added` never counted host entries, by removing the case
  rather than by adding a second branch.

**`.json` goes with the path form.** The home lineup is always `.toml` and a
preset is always `.toml`; nothing else reads a lineup. `Registry` becomes a TOML
reader.

**`default_secrets` / `default_store` lose their file branches.** They are
reached only when the host passed a registry object, and the zero-config path
supplies its own ports. A `FileRegistry` can no longer arrive there, so the
sibling-`.env` and sibling-`store/` derivations are dead and go, and
`broker/source.py` stops importing the file registry.

## Work order

Four batches. Each ends green.

### 1. The source

1. **`broker/source.py`.** Delete the `_FILE_SUFFIXES` branch of
   `resolve_source`; the final `ValueError` names the DSN forms and points at
   `Broker()` and at passing a registry object. Delete `file_target_path` — a
   file target is now only the home lineup, whose suffix llmbroker chose.
   Delete the `isinstance(FileRegistry)` branches of `default_secrets` and
   `default_store`. `lineup_path` and `zero_config_ports` are unchanged.
2. **`standalone/registry.py`.** `_read_data` reads TOML only; drop the `json`
   import, the `.json` branch and the unsupported-extension error. The module
   docstring and `Registry`'s stop naming `.json`.
3. **`__init__.py`.** Remove the `Registry` import and its `__all__` entry.

### 2. The sync source

4. **`broker/merge.py`.** `resolve_sync_source` becomes a preset-name
   validation with no path branch. `load_sync_source` takes a name, fetches off
   the event loop, parses, and returns; the `RegistryProtocol` and `Path`
   branches go, and with them the `Registry`/`KeyInfoProtocol` imports it needed
   for them. `SyncSource.preset` is always true and goes; decide while there
   whether `SyncSource` still earns its existence beside `(label, lineup)`.
5. **`broker/presets.py`.** Beside `_check_fetched_urls`, refuse a fetched
   lineup carrying `[[custom]]`, with the same "refusing the whole file" shape.
6. **`merge_upstream`.** Drop `new_custom` and `arriving_custom`; `custom` is
   `current_custom` alone. `updated` counts `new_managed`.
7. **`broker/refresher.py`.** `sync(source: str)`. The preset-only refusal in
   `_file_target` goes — it is structurally impossible now. `_stamp_key` no
   longer has to handle a `RegistryProtocol` or `Path` source.
8. **`AsyncBroker`.** `sync: str | None`, `sync(source: str)`. Its docstring
   drops the "or a vendored file" reading.

### 3. The CLI

9. Delete the `preset` subcommand: `_cmd_preset`, `_sync_preset_into`,
   `_sync_target`, `_DB_TARGET_SUFFIXES`, `_DB_TARGET_SCHEMES`. The CLI stops
   importing `KeyProbe`, `FileStore`, `Secrets`, `SyncSource`,
   `sync_lineup_file`, `alias_targets_for`, `write_stamp` and `format_report` —
   it no longer has a merge site of its own.
10. **`env`** takes an optional preset name and defaults to this installation's
    own lineup; the path form goes with the file form. `_env_source_data` loses
    its `path.exists()` branch. A host on a database registry reads the curated
    keys with `llmbroker env freetier`.
11. **`add-model`** is unchanged except its description, which stops saying
    "a broker whose registry is a database" and says instead that a host owning
    its own registry declares its models with `direct=[…]`.

### 4. Specs, docs and the queue

Listed below; they are part of the work, not a sweep after it.

## Tests

The file form is load-bearing in four test modules, and none of them may simply
be deleted — each holds a behaviour that survives in a different shape.

- **`tests/test_cli.py`** loses its `preset command` and `preset --sync`
  sections (lines 96–420 today) and the `a DB target belongs to the application`
  section, which tested the refusal inside a command that no longer exists.
  **The `env -> sync -> ask round trip (mission #2)` section stays and is
  rewritten** against `env` + `broker.sync("freetier")` + `ask` — it is the one
  test that walks the mission's onboarding end to end, and losing it would be
  the real cost of this plan.
- **`tests/test_source_dispatch.py`**: the file-extension case becomes an
  assertion that a `.toml` path is refused, naming the forms that work.
- **`tests/test_env_file_secrets.py`** and **`tests/test_file_learning.py`**
  build brokers on a file path to get a file-backed installation. They get one
  from `AsyncBroker(home=tmp_path)` instead. Note while retargeting them that a
  zero-config broker resolves keys from the environment with the *working
  directory's* `.env`, not a sibling of its lineup — the sibling behaviour is
  what step 1 deletes, so any assertion resting on it is testing the removed
  form and goes with it.
- New: a preset carrying `[[custom]]` is refused whole, with nothing written.
- New: `Broker("llms.toml")` raises, and the message names `Broker()`, a DSN,
  and a registry object.
- `tests/test_packaging.py` asserts the public surface; update it for
  `Registry`.

## Spec updates

- **`mission.md`** — three passages, verified against the code before editing:
  - Rewrite the *states a lineup of its own* sentence. The redirect is now to a
    registry the host implements, and the rewrite must not imply that llmbroker
    keeps such a lineup current: it does not merge into a registry it was
    handed. Say what is true, at the level of intent, without naming the
    protocol.
  - *"naming one source is enough to derive all three"* (the three-ports
    paragraph) — still true, but it now rests on the DSN forms alone. Re-read
    and decide whether the sentence still predicts correctly; it may need
    nothing.
  - Requirement 8, *"a bare broker needs no database at all"* — checked, stays
    true: the zero-config installation runs on the home file with no database.
    It is only a *host-supplied* lineup that now needs a registry of its own.
    Left alone unless the rewrite above disturbs it.
- **`rules/sync-merge.md`** — "A file target is written from a curated preset
  only" stops being a rule and becomes a structural fact; the paragraph on a
  file registry as a legitimate target loses the hand-maintained reading. State
  the current shape only.
- **`rules/presets.md`** — the CLI section goes from three commands to two.
- **`rules/backends.md`** — the config-port row and the source-parameter
  dispatch paragraph: a config file extension is no longer a dispatch form.
- **`rules/direct-aliases.md`** — check the sentence added by
  `lineup-file-ownership` about the file a followed entry sits in; it stays
  true, but verify it does not imply a path a host names.
- **`decisions.md`** — one new entry,
  `the-lineup-file-is-not-a-path-a-host-names`, as described above. It belongs
  beside `the-lineup-file-is-generated-not-authored`, which it completes.
- **`invariants.md`** — no entry references the file source; verify and leave it
  alone. This plan does not earn an entry: nothing it removes was a rule whose
  violation was silent.

## Docs (en and ru, in step)

- `usage.md` — the *Where the lineup lives* section loses its second half; the
  *Keeping the pool fresh* section loses the `preset --sync` command and both
  file examples at its end. One of those, `sync="my-lineup.toml"`, raises today
  — see *What this takes away*; it goes as a correction, not as a removal.
- `cli.md` — rewritten around two commands; the whole `preset --sync` section
  and the DB-target refusal go.
- `server.md` — the `Broker("llms.toml")` line in the source list, and the
  vendored-file reading of `#sync`; the deploy job calls `sync("freetier")`.
  **Add the migration recipe**, which is undocumented today and is the
  replacement for `sync(other_registry)`: moving an installation between
  backends is `await new.mirror(await old.load())`, two lines in the same deploy
  script that already holds both DSNs. Without this the capability disappears
  silently, which is the one way this plan could cost a user something without
  anybody noticing.
- `direct.md` — `add-model` prose is already right; check the surrounding
  claims about where entries live.
- `secrets.md` — `llmbroker env llms.toml`, and both `registry=Registry(...)`
  recipes.
- `index.md`, `installation.md`, `disable.md` — grep for `llms.toml` and for
  `preset`.

## The queue

`specs/plans/README.md` says of the current batch that *no functionality is
removed by any of them*. That stops being true, and the header says so. The row
for this plan goes first among the queued ones, before `models-purity`.

Re-read the queued skeletons for references to what this plan deletes —
`declared-out-of-catalog` reasons about `Catalog.entries()` over a file
registry, which still exists as the home lineup, so its finding survives; verify
rather than assume.

## Gate

`invoke pre` clean and `python -m pytest` green after each of the four batches.
Docker up for the testcontainer tests.
