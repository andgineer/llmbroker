# Preset sync: upstream refresh, paid-replacement pruning, report, lifecycle

Closes the zero-admin gap around preset updates: today refreshing the model lineup is a manual
two-command ritual (`llmbroker preset freetier --merge llms.toml`, then `llmbroker sync llms.toml
<db>`) whose only output is stdout nobody reads in automated runs, and a curated update that swaps
a provider can silently shrink the working pool. After this plan the verb is `sync` everywhere,
an update can never reduce the number of models an installation can actually call, and every
outcome lands where the admin already looks: the report in the deploy log / PR / `last_sync_report`,
the pending state in the committed file itself, the history in that file's git log.

## Design summary

1. **Two tiers, one merge site each.** This is the frame the whole design hangs on.

   | | tier 1 (the common case) | tier 2 |
   |---|---|---|
   | registry / secrets | `llms.toml` + `.env` | DB / Vault / AWS, possibly per-user (`scope`) |
   | who merges | `llmbroker preset <name> --sync llms.toml` | `broker.sync(...)` / `sync=` knob |
   | key visibility | `os.environ` + the file's sibling `.env` | the broker's own secrets backend |
   | application code | `Broker("llms.toml")` + `ask()` | the host's own factory |
   | CLI can | download, merge, `env` | download, `env` — **never merge into a registry** |

   The CLI has no `scope` parameter and no way to reach another secret store, so in tier 1 what the
   CLI resolves *is* what the application will resolve — the same `resolve_source` pair
   (`os.environ` + sibling `.env`). Tier 2 never merges from the CLI, so a key-blind merge cannot
   happen there.

2. **One verb, two sources.**
   - `sync("freetier")` — *preset name*: the only networked operation in the library. Fetch the
     curated preset from the catalog, merge against the current registry state, apply, return a
     `SyncReport`.
   - `sync("llms.toml")` / `sync(Registry(...))` — *path*: offline, no network, same merge rules.
3. **Lockfile model.** The vendored config file is the truth; refreshing it from upstream is an
   explicit repo/deploy action (human or bot), like a lockfile upgrade. Application start is never
   online and never fails because of preset staleness.
4. **A key exists or it does not — there is no third state.** A key exists when the secrets store
   returns a **non-empty** value for its ref, or when the ref is listed in `have_keys`. Absence is
   never evidence of anything: keys only ever *authorize* a removal, never demand one. That single
   asymmetry is why no "unknown key" state is needed and why per-user (`scope`) secrets degrade to
   maximal conservatism with zero configuration — nothing resolves, so nothing but a same-key
   replacement is ever removed.
5. **Removal rule — pairs, then budget.** Candidates for removal are **only** managed entries whose
   name is absent from the new lineup. An entry still present is updated in place; `[[custom]]` is
   never touched.

   > 1. An arriving entry carrying a dropped entry's `api_key_ref` *is* its replacement: the pair
   >    is removed, no key lookup at all — same quota, nothing is lost.
   > 2. Each remaining arrival whose `api_key_ref` we have a key for pays for the removal of one
   >    remaining dropped entry.
   > 3. Everything else stays.

   Hence the invariant, which is the whole safety story:

   > **The number of callable entries in the pool can never decrease as a result of a sync** —
   > every removal is paid for by an arrival that either inherits the same key or has one of its own.

6. **`have_keys` only lowers conservatism.** `AsyncBroker(..., have_keys=["OPENAI_API_KEY"])` (or
   `have_keys=True`) declares refs the broker cannot probe — per-user keys behind `scope`, a secret
   injected only in production. Declared refs count as present when paying for removals, and only
   there: `have_keys` never makes a model routable, the pool still needs a real key value. The
   parameter is a promise — declare a ref and fail to provision it, and the pool degrades (old
   entries removed, replacements inactive). Omit it and nothing breaks; the lineup just keeps
   entries a better-informed run would have pruned.
7. **Kept entries carry no marker.** Retention is recomputed from (new lineup, current, keys) on
   every merge, so a persisted flag would be an output masquerading as an input. The file writer
   groups them under a generated comment; the report names them on every run, including no-ops.
8. **Never-break is structural, not key-derived.** The rule in point 5 cannot empty a working pool,
   so no key-count refusal is needed. One structural guard remains, on both sources: refuse to
   apply a result with zero entries while the current registry has some (`SyncRefusedError`
   carrying the report). Empty-registry targets accept anything (onboarding).
9. **Visibility — admin-facing, raw facts, no severity enum.**
   - `SyncReport` — returned by every sync and printed by the CLI on every run *including no-ops*,
     so kept entries and missing keys nag in each deploy log until resolved; `last_sync_report`
     lets a host forward it to its own admin channel;
   - the committed config file is the durable state: kept entries and the keys that would unlock
     their removal sit in the file, so a bot refresh is reviewable in the PR diff itself, and the
     vendored file's git history is the update record — no storage of its own.

   `snapshot()` is not part of the admin story — it is runtime state for the application's own UI.
   The journal stays what it is — a stream of LLM calls and quality ratings; a sync is a registry
   operation and never writes there. The host derives criticality; the recommended interpretation
   goes in docs, not code.
10. **Sugar**: `AsyncBroker(..., sync="freetier")` / `Broker(..., sync=...)` — best-effort refresh
    before the first provision: fetch failure or refusal logs a warning, stashes the report, and
    continues on the existing config. Start never dies because of an update. The explicit `.sync()`
    call always raises — the caller chose to sync and has a plan.
11. **The CLI writes files only; a registry is written from code.** `llmbroker preset <name> --sync
    <file.toml>` replaces `preset --merge`; the existing `llmbroker sync <file> <db>` form is
    **removed**. Rationale: a CLI that takes a DSN duplicates connection config the application
    already owns (drift = syncing one database while serving from another, a silent failure) and
    forces DB credentials into the CLI's environment, which an app fetching its DSN from Vault
    cannot supply. Mirroring into a registry is therefore done by the host's own entrypoint, built
    by the same factory the application uses — the alembic `env.py` pattern: the library owns the
    operation, the application owns the connection. No compatibility shims — no published users.
12. **`--merge` becomes `--sync`** so the CLI and the API name the same operation the same way. The
    word "merge" leaves the user-facing vocabulary entirely.
13. Out of scope (explicitly rejected during design):
    - **a `guaranteed = [...]` key declaration in the config file** — superseded by `have_keys`,
      which is a constructor parameter, not file syntax: the installations that need it (DB
      registry behind Vault, per-user keys) may have no config file at all, and a file field would
      serve only the tier whose keys are already visible in `.env`. No TOML change of any kind.
    - **a `replaces = "old-name"` curator annotation** — it encoded "this supersedes that", which
      rule 5.1 derives from the ref the arrival already carries. An annotation is also only as good
      as the curator's memory, and its failure mode (forgotten → entry deleted) is the damaging
      direction.
    - **a persisted `retained` / `LLMConfig` flag** — see design point 7.
    - **promoting a dropped entry to `[[custom]]`** — `custom` means user-owned and
      direct-reachable by name; an entry upstream abandoned is neither, and the promotion would
      also make it permanently unprunable.
    - **an "unknown key" tri-state** — unnecessary, see design point 4.
    - a Hub/composition entity, a library-level singleton, a pytest plugin validating configs in
      tests, moving removed models to direct-only.

## 1. Key presence — the cross-cutting rule

Because key presence now authorizes removals, an empty value must never pass for a key anywhere.
This lands first, in its own batch, since everything else depends on it.

- `standalone/secrets.py`, `Secrets.resolve`: an env var set to an empty (or whitespace-only) value
  counts as unset — `os.environ.get(ref)` currently returns `""` and hands it back as a valid key.
  The `.env` branch already does this (`_from_file` … `or None`); the two must agree.
- `broker/catalog.py`, `resolve_key`: returns `None` for an empty/whitespace value from **any**
  backend (Vault, AWS and DB backends can all return one). This is the single funnel the pool and
  the sync probe both use.
- `broker/catalog.py`, `_seed_secrets`: an empty existing value is not "already resolvable —
  preserve", and an empty bootstrap value is not copied.

Tests (`tests/test_secrets.py`, `tests/test_catalog.py`): exported `REF=""` resolves as absent; a
whitespace-only value likewise; a stub secrets backend returning `""` leaves the pool slot keyless;
`_seed_secrets` neither preserves nor copies empty values.

## 2. Models and exceptions

`src/llmbroker/models.py`:

- `LLMConfig` — **unchanged**. No new fields, no `to_metadata` / `from_metadata` change, no
  registry-parser change. This is the payoff of design point 7.
- `KeyInfo` — **unchanged**.
- New frozen dataclasses:

  ```python
  @dataclass(frozen=True, slots=True)
  class PendingKey:
      api_key_ref: str
      help: str
      entry_names: tuple[str, ...]      # new-lineup entries inactive until this ref resolves

  @dataclass(frozen=True, slots=True)
  class SyncReport:
      source: str                        # preset name or path as given
      applied: bool
      added: tuple[str, ...]
      updated: tuple[str, ...]
      removed: tuple[str, ...]
      kept: tuple[str, ...]              # dropped upstream, no paid replacement → left in place
      pending_keys: tuple[PendingKey, ...] = ()
      active_before: int = 0             # entries with a present key, pre-sync
      active_after: int = 0              # same, post-sync
  ```

  `__str__` renders the human-readable block the CLI and the deploy recipes print — one line per
  section, sections with nothing to say omitted, pending keys rendered with their help text. When
  `kept` is non-empty it names what would unlock the cleanup, e.g. *"kept groq-llama-3.3-70b —
  upstream dropped it and no replacement is usable; set any of GEMINI_API_KEY, OPENROUTER_API_KEY
  and the next sync removes it"*. That sentence is the whole user documentation of the rule, so it
  must read correctly with one, several, and zero unlocking refs.

`src/llmbroker/exceptions.py`: `SyncRefusedError(LLMBrokerError)` with a `report: SyncReport`
attribute; message states what was refused.

`SyncReport`, `PendingKey` and `SyncRefusedError` join the top-level `__init__.py` re-export and its
`__all__` — a caller reading `last_sync_report` or catching the refusal must not import from a
submodule.

Tests: `tests/test_models.py` — construction and `__str__` shape, including the no-op form and the
`kept` sentence with 0/1/N unlocking refs.

## 3. Merge engine — new module `src/llmbroker/broker/upstream.py`

Move (not re-export) from `cli.py` into this module, since the library must not import from the
CLI: `_PRESET_URL`, `_PRESET_NAME_RE`, `_fetch_preset_file` (rename `fetch_preset_text`),
`_catalog_alias_index`, `_refresh_alias_entries`, `_merged_name_clash`, `_custom_key_tail`,
`_custom_block`, `_write_atomic`. The alias-following custom-entry refresh against the paid catalog
keeps working exactly as today when the source is a preset name. `cli.py` imports from here (the
CLI may depend on the library; never the reverse) and keeps only argument parsing and printing.

### 3.1 Which refs are present

```python
async def present_refs(
    refs: Iterable[str],
    secrets: SecretsProtocol,
    *,
    scope: str | None,
    have_keys: bool | Sequence[str],
) -> set[str]
```

`resolve_key` (scope-prefixed first, then shared, empty → absent) decides each ref; `have_keys=True`
short-circuits to "all of them", a sequence adds its refs. Used by both sources and by the CLI,
which passes `scope=None`, `have_keys=False` and the env-backed `Secrets(target.parent / ".env")`.

### 3.2 The pure merge

```python
def merge_upstream(
    new_configs: list[LLMConfig],
    new_keys: dict[str, KeyInfo],
    current_configs: list[LLMConfig],
    current_keys: dict[str, KeyInfo],
    present: set[str],
) -> tuple[list[LLMConfig], dict[str, KeyInfo], SyncReport]
```

No I/O, no secrets calls — `present` is passed in, so the function is fully unit-testable. Rules, in
order:

1. Managed lineup = the new entries; `custom=True` entries from `current_configs` are carried over
   untouched (alias refresh happens before this call, on the raw entries, as today).
2. Model-identity guard: same name, different `model` between current and merged result is a
   `ValueError` (existing `catalog.sync` rule — keep the wording).
3. **Removal.** With
   `dropped = [managed current entries whose name is absent from the new lineup]` (current file
   order) and `arrived = [new entries whose name is absent from the current config]` (new-lineup
   order):

   ```
   remaining = list(dropped)
   removed   = []
   paid      = set()

   # 3a. same-ref pairing — an arrival carrying a dropped entry's ref replaces it
   for a in arrived:
       d = first entry in remaining with d.api_key_ref == a.api_key_ref
       if d is not None:
           removed.append(d); remaining.remove(d); paid.add(a.name)

   # 3b. budget — one removal per remaining arrival whose key is present
   budget = sum(1 for a in arrived if a.name not in paid and a.api_key_ref in present)
   order  = [d for d in remaining if d.api_key_ref not in present] \
          + [d for d in remaining if d.api_key_ref in present]
   removed += order[:budget]
   kept     = order[budget:]
   ```

   Each arrival pays for at most one removal. Step 3a needs no key knowledge: the replacement uses
   the very same key, so it works exactly when the removed entry worked. The ordering in 3b is
   deliberate: entries whose key is absent are inactive already, so spending the budget on them
   first keeps the maximum number of callable models. Both lists preserve file order, so the result
   is deterministic.
4. Kept entries are appended to the merged lineup unchanged, after the new lineup, in their previous
   relative order.
5. Keys: the merged key table is the new lineup's `[keys]` plus, for every ref used by a kept or
   custom entry and absent from that table, the entry the current file already had. A kept entry
   without key help is not an error — it renders with an empty help string.
6. Name-clash check between managed (incl. kept) and custom entries → error, nothing emitted.
7. Report facts: `added` / `updated` / `removed` / `kept` as above (managed only; a kept entry is
   neither removed nor added, in any run). `pending_keys` are the refs of merged-result entries not
   in `present`, grouped with their help text and the entries they hold back. `active_before` /
   `active_after` count entries whose ref is in `present`.

The same rules apply to both sources. A path source is *not* a blind total mirror: an operator who
deliberately deletes an entry from the vendored file gets it removed only when the rule pays for it,
and the report says why it was kept. The escape hatch for a forced lineup is
`registry.mirror(configs)` directly — documented, not wired into `sync`.

### 3.3 Writers

- **File target**: new-lineup text verbatim first (comments preserved, as `_merge_preset` does
  today), then a generated comment header plus one `[[llms]]` block per kept entry, then the
  `[[custom]]` blocks, then the `[keys.REF]` tail. Atomic: any error leaves the target untouched.

  **Writer hazard, verified against the installed `tomli_w`:** `tomli_w.dumps({"custom": [...]})`
  renders an array of dicts *inline* (`custom = [ { ... } ]`) whenever the rendered line fits
  `MAX_LINE_LENGTH = 100`, and only then falls back to `[[custom]]` array-of-tables form. Appended
  after preset text that ends in a `[keys.GEMINI_API_KEY]` table — which `presets/freetier.toml`
  does — an inline top-level key parses as a *member of that table*, so the entries silently vanish
  from the top level. Today's `_merge_preset` escapes this only because realistic `base_url` +
  `api_key_ref` values push every entry past 100 chars. The new writer must not inherit the hazard:
  emit each entry as an explicit `"[[llms]]\n" + tomli_w.dumps(entry)` / `"[[custom]]\n" +
  tomli_w.dumps(entry)` section rather than dumping the array. (Nested dict-of-dicts such as `keys`
  are unaffected — they always render as tables.)
- **Registry target**: `MutableRegistryProtocol.mirror(merged)` — unchanged protocol.

### 3.4 Tests (`tests/test_upstream.py`)

Removal rule, one test per row of the table (all with an explicit `present` set, no secrets):

| scenario | expected |
|---|---|
| same-provider replacement (arrival carries the ref) | removed, `present` empty |
| cross-provider swap, arrival's key present | removed |
| cross-provider swap, arrival's key absent | kept |
| provider dropped, nothing arrived | kept |
| lineup shrinks 5→3, every key present, no arrivals | both kept (budget 0) |
| 2 dropped, 1 usable arrival | exactly 1 removed — the one whose own key is absent |
| 2 dropped sharing a ref, 1 arrival with that ref | 1 removed (an arrival pays once), 1 kept |
| nothing present at all (`present=set()`) | only same-ref pairs removed |
| `have_keys` covering the arrival's ref | removed, without any resolvable key |

Plus: kept entries survive an arbitrary number of consecutive merges without accumulating markers or
duplicating; custom entries, their aliases and their `[keys]` survive, alias refresh still rewrites
provider fields; name clash refused; model-identity change refused; key help for a kept entry is
carried over; `present_refs` honors scope-prefixed refs, `have_keys=True`, a sequence, and empty
values; **writer regression** — a `[[custom]]` and a kept `[[llms]]` entry short enough to render
inline still round-trip as top-level entries after a preset whose text ends in a `[keys.*]` table;
report field assertions for each scenario, including the no-op run.

## 4. `sync` and `have_keys` on the broker — `broker/broker.py`, `broker/catalog.py`

- `AsyncBroker.__init__(..., have_keys: bool | Sequence[str] = False)`, stored; same keyword on the
  `Broker` wrapper in `sync.py`, forwarded.
- `async def sync(self, source: RegistryProtocol | str | Path) -> SyncReport` on both.

Source dispatch for `str` (same precedence `cli._env_source_data` already uses): an existing path →
path semantics; else matches `^[a-zA-Z0-9_-]+$` and has no suffix → preset name; else error naming
both accepted forms.

Preset-name path:

1. `fetch_preset_text(name)` — network errors raise (`urllib` errors wrapped into a clear message);
   nothing has changed yet.
2. Load current state from the broker's own registry; key infos via `KeyInfoProtocol` probe (file
   registries have them, DB registries do not).
3. `present_refs(...)` with the broker's own secrets, its `scope` and its `have_keys`, then
   `merge_upstream(...)`.
4. Structural guard: merged empty while current is not → `SyncRefusedError` with the report.
5. Apply: file registry → rewrite the TOML file (this is what makes `Broker("llms.toml",
   sync="freetier")` self-contained); mutable DB registry → `mirror`. A read-only registry without a
   file path keeps today's `TypeError`.
6. Existing post-mirror steps unchanged: `_seed_secrets`, disabled-map seeding, immediate resync
   when provisioned.
7. **Log the outcome, then return the report.** One line per sync, on both sources, so the operation
   reports itself wherever it runs and no caller has to remember to read the return value:
   `logger.warning` when the update needs something from an admin — an entry kept for lack of a paid
   replacement — and `logger.info` otherwise, including the no-op and pending keys on their own.
   A pending key is not a warning: a keyless entry is a normal, documented state
   (`catalog.py`'s own INFO on it), and in a per-user installation every shared probe fails by
   design. Never both, never per-entry. `SyncReport.__str__` is the message.

Path source: same steps minus the fetch.

`catalog.sync` keeps ownership of the mirror + `_seed_secrets` half; the merge decision lives in
`upstream.py`. `EmptyRegistryError`'s message currently suggests `python -m llmbroker sync` for a DB
registry — repoint it at `await broker.sync("freetier")` from the host's entrypoint.

Tests (`tests/test_broker_sync_upstream.py` + extend existing sync tests): sqlite registry
end-to-end with a stubbed fetch (monkeypatched `fetch_preset_text` — no network in tests); a
cross-provider change with no keys present keeps the old entry in the DB and the pool keeps routing
over it; the same change with the arrival's key present removes it; `have_keys=["X"]` reproduces the
latter without any key in the store, and `have_keys` alone never makes the model routable (the pool
slot stays keyless); a scoped broker counts `scope/REF` as present; file registry write-back
preserves `[[custom]]` and comments; the structural refusal leaves the registry byte-identical and
raises with the report; report returned on both sources; existing sync callers keep passing (return
value ignored is fine); via `caplog` — a run that keeps an entry logs exactly one WARNING, a clean
change, a pending-key-only run and a no-op log exactly one INFO, on both sources.

## 5. Sugar: `sync=` constructor knob

- `AsyncBroker.__init__(..., sync: str | Path | None = None)` — stored, not executed (constructor
  stays synchronous and offline).
- Executed **at the top of `ensure_pool()`, inside the provision lock, before
  `_catalog.provision()`**. That single placement gets the ordering right everywhere: entering the
  context manager provisions eagerly (`__aenter__` → `ensure_pool`, `broker.py:245`), and
  provisioning an empty registry raises `EmptyRegistryError` — so a sync that runs *after* it could
  never populate a fresh registry. Running it first also makes the knob work for callers who never
  use a context manager, since every public operation funnels through `ensure_pool`.
- Best-effort: `SyncRefusedError` → stash `exc.report`; fetch/network errors → stash nothing; both
  log one `logger.warning` and continue on the existing configuration. Never raises.
- Success is not silent, and the knob adds nothing for it: `sync()` itself logs the outcome (§4 step
  7), so the sugar inherits one INFO/WARNING line per process. The knob's own logging is only the
  failure half above.
- Guarded by its own once-flag set before the attempt, not by `_provisioned`, so a failed provision
  retried later does not re-fetch.
- `Broker` (sync wrapper) takes the same keyword and inherits the behavior through the same call
  path.
- `last_sync_report: SyncReport | None` public attribute on both.

Tests: fresh empty sqlite registry + `sync="llms.toml"` → `async with` succeeds (sugar populates
before provisioning) where it raises `EmptyRegistryError` without the knob; sugar runs for a caller
that never enters the context manager; fetch failure and refusal both keep the old config and
stash/log; the fetch is attempted once across repeated calls; sync-wrapper parity.

## 6. CLI — `cli.py`

`llmbroker preset <name> [--sync <file.toml>]` — with `--sync`: fetch, merge, write the file;
without it, print the preset to stdout as today.

- `--merge` is renamed `--sync`; the flag name is the only user-visible change to that command, so
  the CLI and the API name the same operation identically.
- The old `llmbroker sync <file> <db>` subcommand is **removed** — parser, handler and the module
  docstring's subcommand list.
- A DB-shaped `--sync` target (`.db`, `sqlite://`, `postgresql://`, `mongodb://`) is rejected with a
  message pointing at the code path: mirroring into a registry is the application's own entrypoint,
  so the connection config and its secrets stay in one place.
- Keys come from `Secrets(target.parent / ".env")` — the same resolver `resolve_source` hands a file
  registry, which is why the CLI's decision equals the application's. `scope=None`, `have_keys=False`;
  neither has a CLI form, because a CLI invocation is single-tenant by construction.
- Human-readable report on every run, including no-ops; exit 0 on success even with pending keys or
  kept entries (both are valid states), exit 1 on fetch errors, clashes, and `SyncRefusedError`.
- `add-model`, `env` unchanged.

Tests: adapt the existing `preset --merge` CLI tests to `--sync`; DB-shaped target rejected with a
non-zero exit and the pointer message; report printed on a no-op run; a run with the arrival's key
in the environment removes the dropped entry, the same run with an empty `REF=""` does not.

## 7. Preset curation

No preset schema change: the rule reads `api_key_ref`, which every entry already carries.

Curation rules → `specs/reference/freetier-providers.md`:

- a same-provider replacement removes the old entry, unchanged from today — downstream, rule 3a
  pairs them by ref and removes it with no key involved;
- dropping the last entry of a provider is the removal downstream installations follow **only when
  the preset gives them a usable replacement in the same update**. A provider therefore leaves the
  preset when it is no longer worth a slot, and installations that cannot use the newcomer keep a
  working model; the sync report names it on every run so an admin can act;
- consequently a curated update that drops a provider without adding one prunes nothing downstream.
  That is intended, and worth stating so a future curator does not "fix" it.

Also fix `presets/freetier-refresh-prompt.md`: its "removal turns into deprecation at deployments, a
reversible demotion" wording describes a `SeedPolicy` that no longer exists. Restate it as the
pairs-and-budget rule.

## 8. Spec updates (same batch as the behavior each describes)

- `architecture.md`, the sync/provider-seeding sections: name-vs-path semantics; the online boundary
  (path sync is offline always; preset-name sync is the library's only networked operation and runs
  only on explicit call); the two-tier frame from design point 1 and why it makes a key-aware merge
  safe (exactly one merge site per tier, each with the key visibility its own application has); the
  removal rule and the callable-count invariant; that a key is present only when non-empty;
  `have_keys` as a promise that lowers conservatism and never makes a model routable; that retention
  is recomputed, never stored; the structural refusal; visibility as admin-facing raw facts — the
  report where syncs run, the pending state in the committed file, history in its git log; the
  journal stays calls and quality ratings only, and `snapshot()` stays application-UI runtime state;
  sugar `sync=` best-effort semantics; the lockfile lifecycle (start never online, refresh is a
  repo/deploy action — this also subsumes the existing seed-on-start cluster warning); and the
  ownership rule behind the CLI's file-only target — the library owns the sync operation, the host
  owns the connection.
  The same-provider-replacement paragraph (currently ~line 406) gains the downstream half: the next
  sync pairs the arrival with the old entry by ref and removes it.
  The "Per-user scoping" section gains one paragraph: shared-ref probes fail by design there, which
  is exactly why absence authorizes nothing and why `have_keys` exists.
- `mission.md` item 2: one added clause — the preset mirrors itself via `sync(name)`; the one
  irreducible admin act is obtaining a new provider key, and it is surfaced, never silently absorbed.
- `decisions.md`: one row for the merge engine + report (requirement 2/5, runtime cost: zero outside
  explicit sync).

## 9. Docs (`docs/src/en/` + mirror in `docs/src/ru/`)

- `index.md` Quick start: directly after the one-liner, the second bite — refreshing the file is a
  CLI command, not code:

  ```bash
  llmbroker preset freetier --sync llms.toml
  ```

  One sentence: the curated preset evolves; `--sync` refreshes your file, your `[[custom]]` models
  and keys survive, and a model whose provider left the preset stays until a replacement you can
  actually call arrives.
- `usage.md`: new "Keeping the pool fresh" subsection under "Model pool": the CLI refresh, the
  `sync=` knob, the explicit `llms.sync("freetier")`, reading the report, what a pending key means
  (a model waiting for a key — harmless until then) and what a kept entry means (upstream moved on,
  nothing usable replaced it, here is the key that would let the next sync clean it up).
- `async.md` / `server.md` — the sqlite recipe: one process, registry and secrets in the DB file,
  sugar does the refresh:

  ```python
  async with llmbroker.AsyncBroker("broker.db", sync="freetier") as llms:
      print((await llms.ask("Hello")).text)
  ```

  Note that the DB starts empty and the sugar populates it before the pool provisions, so no
  separate init step exists. Do **not** show the explicit `await llms.sync("freetier")` as an
  alternative here — the two differ in failure semantics, not in output.
- `server.md`, "Shared DB" — rewrite around the split:
  - the deploy job runs the sync from the host's own factory, in the same step as `alembic upgrade`,
    so the DSN and its secrets (Vault included) live in exactly one place:

    ```python
    llms = build_broker()                     # the app's own factory
    try:
        print(await llms.sync("freetier"))    # SyncRefusedError → non-zero exit
    finally:
        await llms.aclose()
    ```

    State plainly why this is not `async with`: entering provisions the pool, and a fresh registry
    is empty, so the context manager would raise `EmptyRegistryError` before the sync could populate
    it.
  - the serving half is a plain broker with no `sync=` knob — the deploy job already did it;
  - it is a one-shot job (release phase, k8s Job, init container), **not** a per-node startup step —
    N nodes syncing is the flip-flop the seed-on-start rule already forbids. A single-node app may
    put the sync in `lifespan`; the structural refusal makes that safe.
  - **per-user keys**: `have_keys` documented here, with the honest failure mode — omit it and the
    lineup keeps entries it could have pruned; declare a ref and never provision it and the pool
    degrades. Nothing else to declare anywhere.
  - headless visibility: the job's exit code and log are the admin channel a failed migration
    already uses; `last_sync_report` for hosts forwarding to their own channel.
- `cli.md`: `preset <name> --sync <file.toml>`; `--merge` and the `sync <file> <db>` subcommand both
  gone, with the one-line reason and a pointer to the `server.md` entrypoint recipe.

## Work order

Each batch ends green on `invoke pre` + `python -m pytest` (activate the venv first).

1. Empty-key rule (§1) with tests — everything downstream depends on it.
2. `SyncReport` / `PendingKey` / `SyncRefusedError` (§2) with tests.
3. Merge engine `upstream.py` (§3) with tests, including the removal table and the writer
   regression; `cli.py` temporarily keeps its own copies until batch 6.
4. Broker `sync` dispatch, `have_keys`, file write-back, structural guard, report and logging (§4),
   with tests; spec updates for these behaviors (§8) in the same batch.
5. Sugar `sync=` (§5) with tests.
6. CLI unification (§6), delete the moved helpers from `cli.py`, adapt CLI tests; curation rules
   (§7).
7. Docs en + ru (§9).

Version bump: none (maintainer does it by hand).

## Verification

```bash
. ./activate.sh
invoke pre
python -m pytest
python -m llmbroker preset freetier --sync /tmp/llms-check.toml   # live smoke, prints the report
```

## Handover

### Status: all seven batches implemented

| § | Batch | Where |
|---|---|---|
| 1 | Empty-key rule | `standalone/secrets.py`, `broker/catalog.py` (`_resolve_non_empty`) |
| 2 | `SyncReport` / `PendingKey` / `SyncRefusedError` | `models.py`, `exceptions.py`, `__init__.py` |
| 3 | Merge engine | new `broker/upstream.py` (~520 lines) |
| 4 | `sync` dispatch, `have_keys`, logging, specs | `broker/broker.py`, `broker/catalog.py`, `sync.py`, `specs/reference/*` |
| 5 | `sync=` knob | `broker/broker.py::_sync_on_start`, `sync.py` |
| 6 | CLI unification + curation rules | `cli.py`, `freetier-providers.md`, `freetier-refresh-prompt.md` |
| 7 | Docs | `docs/src/en/*`, `docs/src/ru/*` |

New test files: `tests/test_upstream.py` (49), `tests/test_broker_sync_upstream.py` (18),
`tests/test_broker_sync_knob.py` (9). `tests/test_cli_sync_roundtrip.py` was renamed (`git mv`)
to `tests/test_sync_roundtrip.py`, since the workflow it covers is no longer a CLI one.

### Decisions the plan did not make

- **`merge_upstream` takes a keyword-only `source: str`.** `SyncReport.source` is required and
  the merge is the only place the report is constructed; the alternative was a `replace()` at
  every call site.
- **Custom entries: the arriving lineup wins, and current custom entries are never pruned.**
  The plan's rule 1 ("custom entries from `current_configs` are carried over") describes the
  preset case, where a preset never carries custom entries. Taken literally it would break the
  tier-2 path the plan also requires — `broker.sync("llms.toml")` into a DB registry must carry
  the file's own `[[custom]]` edits into the DB, and today's code already does. Implemented as:
  merged custom = the new lineup's custom entries, plus current custom entries absent from it.
  Both readings agree on tier 1.
- **`SyncReport.unlocking_refs()` is public**, deriving the refs named in the "kept" sentence
  from `pending_keys ∩ added`. The plan specified the sentence but not where the refs come from;
  a caller rendering its own admin UI needs the same derivation.
- **Alias-refresh notices/warnings are returned, not printed.** `refresh_alias_entries` returns
  an `AliasRefresh`; the CLI prints (stdout/stderr, exactly as before) and the broker logs
  (INFO for notices, WARNING for an alias the catalog no longer knows). The §4-step-7 "one line
  per sync" rule is about the outcome line; an unknown alias is a separate, genuinely
  admin-actionable fact and is not suppressed to protect that count.
- **`fetch_preset_text` raises `ValueError`** for every failure mode (invalid name, HTTP, decode,
  bad TOML) rather than returning `None` as the CLI helper did. No new exception type: §2 lists
  only `SyncRefusedError`. `paid_catalog_text()` wraps it so **every** network read resolves
  `fetch_preset_text` in `upstream`'s own namespace — one patch point for tests. This matters:
  the first draft of the broker tests patched `broker.fetch_preset_text` and silently hit
  GitHub instead.
- **`_merged_name_clash` did not move as a separate function**; the check is rule 6 inside
  `merge_upstream` (`_check_name_clash`), which is where the plan puts it. Its message is
  unchanged apart from dropping the CLI's `error: ` prefix, which `cli.py` now adds.
- **`Catalog.sync(preset)` became `Catalog.apply(configs)`** and `_seed_secrets` became public
  `seed_secrets`, per §4's "catalog keeps the mirror + `_seed_secrets` half". The model-identity
  guard moved with the merge decision into `upstream.py`.
- **`config_from_entry` made public** in `standalone/registry.py` (was `_config_from_entry`) so
  the merge engine can build configs from the raw dicts it has already parsed and alias-refreshed.

### Behavior changes reviewers should look at deliberately

- **`broker.sync(...)` into a file registry now rewrites that file** instead of raising
  `TypeError`. §4 step 5 requires it. Two existing tests asserted the old refusal and were
  rewritten (`test_broker.py::test_sync_into_a_file_registry_rewrites_the_file`,
  `test_sync.py::test_broker_sync_into_a_file_registry_returns_the_report`); a new test covers
  the case that still raises — a read-only registry object with no path.
- **A path source is no longer a total mirror.** `test_cli_sync_is_a_total_mirror_on_rerun`
  became `test_a_shrunk_lineup_keeps_the_dropped_entry`. Deleting an entry from a vendored file
  now removes it from the DB only when the rule pays for it.
- **`test_broker_disable.py::test_remove_then_readd_same_name_is_routable_after_disable`** is a
  regression test for a real bug (a stale benched latch), so it was kept and re-aimed: it used
  an empty source to remove an entry, which no longer removes anything. It now removes via a
  same-ref replacement, which is how an entry actually leaves a registry.
- **`EmptyRegistryError`'s message** changed (two tests assert on it).

### Worth knowing

- **The structural guard (§8/§4 step 4) is unreachable through `merge_upstream`.** A removal
  requires an arrival, and every arrival is itself in the result, so the merged lineup can never
  be empty while the current one is not. The guard is implemented and tested directly
  (`check_not_emptying`), and a test documents *why* it cannot fire through the normal path. The
  plan's own design point 8 says the same thing ("the rule in point 5 cannot empty a working
  pool"); this just makes it explicit for the reviewer who goes looking for the trigger.
- **`urlopen` runs in `asyncio.to_thread`** (`load_sync_source`, `sync_file`), not on the event
  loop. Not in the plan; a 10-second blocking fetch inside a server's `lifespan` would otherwise
  stall it.
- **CLI DB-target detection reads the raw argument, not a `Path`** — `Path("postgresql://h/db")`
  collapses the `//`, so the scheme check silently failed. Caught by the test for it.
- The plan's `docs/.../index.md` snippet and the `server.md` recipe are in as specified. The
  `direct.md` example that did `await llms.sync("llms.toml")` on a broker already opened on
  `llms.toml` was dropped in both languages: it was a no-op before and would now rewrite the
  user's file.

### Not done

- **No version bump** — per `CLAUDE.md`, the maintainer does it by hand.
- **Nothing committed.**

### Gate

Both green after every batch and at the end:

- `invoke pre` — all hooks pass, pyrefly `0 errors`
- `python -m pytest` — **967 passed**, 0 failures, 0 skips (was 867 before this plan)
- Live smoke: `python -m llmbroker preset freetier --sync <file>` prints the report and exits 0;
  re-run is a no-op that still reports; a hand-added stale entry is kept under the generated
  header with its `[[custom]]` sibling and keys intact.
