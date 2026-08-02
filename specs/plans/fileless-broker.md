# The fileless broker: `Broker()` for the free pool, `direct=` for paid models

**Depends on `preset-autorefresh.md` (#2) and ships after it.** #2 builds the three things this plan
stands on: the home directory, the cached preset text with the bundled/offline fallback chain, and
the unconditional daily refresh. Without them a registry-less broker would fetch on every start and
die without network.

Both halves below ship in **one release**. A zero-config broker with no way to declare a paid model
is incomplete — the config file is currently the only place to declare one, and this plan removes
the need for the file.

Line anchors are current-main numbers; #1 and #2 move them.

## The problem

**The file exists for two unrelated reasons, and neither needs a file.**

1. *It declares the pool.* Today the user is told to materialize the curated preset by hand:
   `llmbroker preset freetier > llms.toml`. This is a ritual with no decision in it — the user does
   not choose anything, does not edit anything, and (after #2) does not maintain anything either.
   The pool is ours; asking the user to keep a copy of it is asking them to hold our state.
2. *It declares paid direct models.* `[[custom]]` entries — a paid model reached by name through
   `direct()`, never routed by the pool. This is a real declaration with real content, but it is
   two facts (which model, whose endpoint), not a file format.

The result is a library whose first instruction is a shell redirect, and whose config file is
mandatory for everyone though it carries a decision for almost nobody.

**And the same confusion is in the code.** `LLMConfig.pooled` allows a `[[custom]]` entry to join
the pool (`cli.py:275`, `add-model --pool`), which blurs the one boundary that keeps the design
comprehensible.

## A spec clause this plan extends

`decisions.md` records "the data source is given by a single parameter" —
`Broker("config.toml")` / `Broker("llm.db")` / a URL. §1 makes *no* parameter a recognized source
(the curated pool), which is that principle carried one step further rather than an exception to
it. The clause is restated in §7 to include the no-argument form. `direct=` is not a data source at
all: it declares models the pool never touches.

## Design summary

1. **The pool is exactly the curated preset.** Nothing a user declares ever enters it. This is the
   invariant the rest of the plan follows from.
2. **`Broker()` works with no arguments** — the free pool, keys from the environment, everything
   llmbroker needs to remember in the home directory from #2.
3. **`direct=` declares paid models**, in two forms and no others: a *catalog alias* (`"opus"`) whose
   version we track, or a full `LLMConfig` whose version the user tracks. The parameter is named for
   what those models are — they are reached with `broker.direct(...)`, never by the router.
4. **`LLMConfig.pooled` is deleted**, because rule 1 makes it derivable: `pooled == not custom`. A
   field that is always the negation of another field is a way to disagree with yourself later.
5. **Declared direct models are not persisted.** They are code; they overlay the registry at
   provision time and resolve their alias from the cached paid catalog on every provision, so a
   model version is always current and nothing can drift.
6. **The file keeps working, for the people it is actually for**: a declarative config under version
   control, and the vendored-lockfile deploy path into a database registry.
7. Rejected during design:
   - **Selecting curated pool models** (`models=["gemini", ...]`) — free-tier entry names carry the
     model version (`google-gemini-3.5-flash-lite`) and are rewritten on every bump, so selection by
     name would need a permanent per-entry handle the preset does not have and the curator would
     have to guarantee forever. And the case is thin: a model with no key is already inactive and
     never routed, so "only what I have keys for" needs no API at all.
   - **A user endpoint in the pool** (the current `add-model --pool`) — rule 1. A self-hosted or
     company-gateway endpoint is declared with `direct=` like any other non-curated model.
   - **Persisting `direct=` entries into the registry** — it would create two sources of truth for
     the same list (the constructor call and the stored row) and re-introduce the drift that alias
     following exists to prevent.
   - **Removing the `add-model` CLI command** — it was written for the file path and still serves
     it; it only loses `--pool` (§3). Deleting a working command is scope this plan was not asked
     for.

## 1. `Broker()` — no registry

`AsyncBroker.__init__` (`broker.py:171`) currently raises `ValueError("AsyncBroker requires a
registry source")` when `registry is None`. Instead it builds the default installation:

| port | zero-config default | why |
|---|---|---|
| registry | `Registry(<home>/lineup.toml)`, seeded on first use from the cached preset | the merged lineup is ours to keep; it is not the cache (the cache is pristine upstream, the lineup is what we run) |
| secrets | `Secrets(Path(".env"))` — process env first, the CWD `.env` as fallback | where a script's keys are; matches what a file registry already does with its sibling `.env` |
| store | `FileStore(<home>/store)` | §1.2 |

With no writable home (#2 §1 returns `None`), the registry is an in-memory one seeded per run from
the cached-or-bundled preset and the store is `InMemoryStore` — the broker still works, it just
remembers nothing between runs.

The refresh from #2 applies unchanged: the lineup file is a file registry like any other.

### 1.2 Why the journal is machine-global here, and why that is right

A registry-less broker has no config directory, so `_default_store` (`broker.py:97`) would give it
`FileStore(Path("store"))` under the CWD — a script invoked from different directories would
scatter its journal, and with it the cooldowns. Every run would then re-discover the same 429 and
pay for it again.

Sharing one journal across every project on the machine is not a compromise but the accurate model:
in this mode the keys come from the environment, so **the quota being tracked really is one pool**.
The case where it is not — two projects with different keys — is already handled, because 429 and
dead-key detection scope by `key_hash` (`learning.py:168`). A project that wants full isolation
passes `home=` (#2 §1.2).

## 2. `direct=`

```python
direct: Sequence[str | LLMConfig] = ()
```

on `AsyncBroker` and `sync.Broker`, next to `registry`. A `str` is a paid-catalog alias; an
`LLMConfig` is a complete declaration the caller owns.

```python
llms = llmbroker.Broker(direct=["opus"])
llms.ask("...")                    # the free pool, routed and learned
llms.direct("opus").ask("...")     # Claude Opus, current version, never pooled
```

The name is the contract: these models are what `direct()` reaches, and rule 1 says nothing else
can happen to them.

### 2.1 Resolution

At provision, in `Catalog`, after the registry load and before `_reconcile`:

- an `LLMConfig` is taken as given, forced to `custom=True`;
- a `str` is resolved against the paid catalog — cached in the home directory and refreshed on #2's
  clock — through the existing `catalog_alias_index` (`upstream.py:119`), producing an entry with
  `alias` set to the requested handle, `model` and `base_url` and `api_key_ref` from the catalog,
  and `name` formed as `f"{provider_id}-{model_id}"` (the same shape `cli.py:280` writes today);
- an unknown alias raises at provision, naming the aliases the catalog does carry. The message must
  list them: a typo (`"opus-5"`) is the expected failure and the fix is one word.

Resolved entries are **overlaid, not stored**: they are appended to the config list the catalog
reconciles, and nothing writes them to the registry. Re-resolving on every provision is what keeps
`"opus"` pointing at the current Opus with no sync involved.

A declared model whose `api_key_ref` has no key behaves exactly as a keyless `[[custom]]` entry does
today — it exists and `direct()` on it reports the missing key. No new state.

### 2.2 Name clashes

A declared entry whose name or alias collides with one already in the registry (a `[[custom]]` block
in a file registry declaring the same alias) raises at provision, naming both sources. The existing
`_check_name_clash` / `check_unique_aliases` (`upstream.py:311`, `backends/ports.py:48`) give the
shape; this adds the overlay as a third source they must consider.

## 3. Deleting `LLMConfig.pooled`

`pooled` becomes `not custom` everywhere:

- `models.py`: the field goes; `to_metadata`/`from_metadata` stop reading and writing `"pool"`; the
  docstring loses "a custom entry may be pooled (a user's own extra pool model)" and states rule 1.
- `catalog.py:84`: `pooled = [c for c in configs if c.pooled]` → `if not c.custom`.
- `upstream.py:374` (the entry writer) and `standalone/registry.py:32`: drop the `pool` key.
- `cli.py`: `add-model` loses `--pool`, its `_prompt_yes_no("Add to the pool (failover)?")` prompt,
  and the `"pool": pooled` it writes.
- `pool-lifecycle.md` §3.1 shipped the degradation measure as "distinct `api_key_ref` among
  **pooled** entries"; that code and the `architecture.md` sentence it produced become "among
  **managed** entries". Same set, one definition.
- **Decide here whether an administratively disabled entry still counts its provider.** It does
  today, and `architecture.md` says so: the alarm reports the keys an installation holds, while
  `LLMPool.acquire` also excludes disabled slots — so a pool disabled down to nothing raises no
  ERROR. Defensible (the host set those verdicts and reads them per model in `snapshot()`), but
  this plan is the one place that reopens the measure's definition, so settle it rather than
  leaving two readings of "usable" in the codebase.

A `pool = true` left in a hand-written `[[custom]]` block is ignored rather than rejected: it is a
field that no longer exists, and there are no published users to migrate (CLAUDE.md).

## 4. A live defect this plan must fix first

**A database registry never refreshes a paid model version.** `refresh_alias_entries` is reachable
only through `sync_file`'s `fetch_catalog` argument (`broker.py:313`), and `_sync_registry_target`
(`broker.py:321`) knows nothing about aliases. An installation on sqlite/postgres/mongodb with a
`[[custom]]` alias entry sits on whatever model id it was first synced with, forever — silently,
since nothing reports a version it never looked up.

Reproduce before fixing: sync a lineup carrying an alias entry into a sqlite registry, move the
catalog's model id, sync again, observe the stored entry unchanged.

Fix: the alias refresh moves out of `sync_file` and into `AsyncBroker.sync`, applied to both
branches from one place. §2.1's overlay does not cover this — that path is for models declared in
code, and a `[[custom]]` entry in a file or a DB is declared in data.

This is a prerequisite, not a side quest: it is the same catalog-following machinery `direct=`
needs, and shipping `direct=` on top of a broken version-follower would bake the defect in.

## 5. Shipping the preset in the wheel

`presets/*.toml` sit in the repository root while the package is built from `src/`
(`pyproject.toml:37-40`), so **no preset reaches the installed wheel**. #2's fallback chain
therefore has no floor: a first run with no network and a cold cache has nothing to fall back to.

- `presets/freetier.toml` and `presets/paid-catalog.toml` move to `src/llmbroker/presets/`, declared
  as package data (`*.toml` only).
- `_PRESET_URL` (`upstream.py:32`) follows the new repository path. No published users, so the old
  URL needs no compatibility (CLAUDE.md).
- The curator refresh prompts move with them and are **excluded** from package data — they are
  instructions for the maintainer, not payload.
- The lookup order becomes **cache → bundled → network failure is an error only if both are
  missing**, which is what makes `Broker()` unable to fail on a cold offline start.
- Bonus, and worth a line in `cli.md`: `llmbroker preset` and `llmbroker env` start working offline.

## 6. Tests

`tests/test_fileless_broker.py` (new):

| scenario | expected |
|---|---|
| `Broker()` with a cold cache and a stubbed fetch | pool provisions, lineup file appears under `home=` |
| `Broker()` twice | second run does not fetch (stamp), same lineup |
| `Broker()` with no network and a cold cache | provisions from the bundled preset |
| `Broker()` with `home=` pointing nowhere writable | provisions in memory, still routes |
| two brokers with different `home=` | independent lineups, independent journals |
| keys come from CWD `.env` | a `.env` in the working directory is read; an exported var wins |

`tests/test_direct_declaration.py` (new):

| scenario | expected |
|---|---|
| `direct=["opus"]` | entry resolved from the catalog, reachable by `direct("opus")`, **absent from the pool** |
| the catalog's model id moves, broker restarts | the resolved model follows it, no sync involved |
| `direct=[LLMConfig(...)]` | used verbatim, never pooled, version untouched by any refresh |
| `direct=["opus-5"]` (typo) | raises at provision, message lists the available aliases |
| declared alias collides with a `[[custom]]` alias in the registry | raises, naming both sources |
| declared model with no key | exists; `direct()` reports the missing key, the pool is unaffected |
| `count()` / `snapshot()` | direct entries are not pool members anywhere |

`tests/test_broker_sync_upstream.py` — the §4 repro: a DB registry with an alias entry follows the
catalog's model id across syncs (fails before the fix).

`tests/test_models.py`, `tests/test_registry.py`, `tests/test_cli.py` — `pool` is gone from what is
written and ignored where read; `add-model` has no `--pool`; a legacy `pool = true` in a
`[[custom]]` block loads without error and does not pool the entry.

`tests/test_packaging.py` (new) — the bundled preset is importable package data and parses; both
preset files are present.

## 7. Specs (same batch as the behavior)

- `architecture.md`: rule 1 as a stated invariant — the pool is the curated preset, and a declared
  model is never routed. This is the sentence `pooled`'s deletion rests on.
- `architecture.md`, the two-tier table: a third row above the others — no registry at all, the
  default. Tier 1 (a file) becomes what it actually is: the declarative option, not the entry point.
- `architecture.md`: paid models are declared in code or in `[[custom]]`, and an alias is followed
  at provision (code) or at sync (data) — one rule for both, after §4.
- `decisions.md`: one row — the fileless default, why the pool takes no user entries, and why
  declared models are resolved rather than stored. The "single parameter" row gains the no-parameter
  form; its `pool`-related wording, if any survives §3, goes with the field.
- `mission.md`, item 6 ("one-liner and cluster"): the one-liner no longer includes a TOML.
- `freetier-providers.md`: unaffected; the curation contract does not change (no per-entry handle is
  introduced — design summary 7).

## 8. Docs (`docs/src/en/` + `docs/src/ru/`)

This is where the plan pays off, and it is a rewrite, not an edit.

- `index.md`, quick start: **the shell redirect goes.** Install, set a key, call:

  ```python
  llms = llmbroker.Broker()
  print(llms.ask("Hello, how are you?").text)
  ```

  `llmbroker env` still prints which keys to get and where — it just no longer needs a file to
  print them from.
- `usage.md`: "Model pool" stops opening with file creation. The file becomes a named section for
  people who want a declarative config, with the honest reason to want one.
- `direct.md`: `direct=["opus"]` becomes the primary form; the `[[custom]]` block is its file
  spelling. State plainly that a direct model is never routed by the pool and never can be.
- `server.md`: unchanged in substance — a DB installation still fills its registry from a deploy
  job; add that paid aliases now follow the catalog there too (§4).
- `cli.md`: `preset` and `env` work offline; `add-model` no longer asks about the pool.

## Work order

Each batch ends green on `invoke pre` + `python -m pytest` (`. ./activate.sh` first).

1. §4, the alias-refresh defect, repro test first. It is a bug on today's main and everything after
   depends on the machinery it fixes.
2. §5, the bundled preset and the packaging move, with tests — #2's fallback chain gets its floor.
3. §3, deleting `pooled`, with the spec sentence that replaces it.
4. §1, the fileless broker, with tests.
5. §2, `direct=`, with tests.
6. §8, docs en + ru — last, when the API they describe is final.

Version bump: none (the maintainer does it by hand).

## Verification

```bash
. ./activate.sh
invoke pre
python -m pytest
python -c "import llmbroker; print(llmbroker.Broker().ask('hi').text)"   # no file, no arguments
```
