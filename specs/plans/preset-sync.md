# Preset sync: upstream refresh, coverage-based pruning, report, lifecycle

Closes the zero-admin gap around preset updates: today refreshing the model lineup is a manual
two-command ritual (`llmbroker preset freetier --merge llms.toml`, then `llmbroker sync llms.toml
<db>`) whose only output is stdout nobody reads in automated runs, and a curated update that swaps
a provider can silently shrink the working pool. After this plan, one verb — `sync` — covers the
whole flow, an update can never turn a working configuration into a dead one, and every outcome
lands where the admin already looks: the report in the deploy log / PR / `last_sync_report`, the
pending state in the committed file itself, the history in that file's git log.

## Design summary

1. **No new entity.** `AsyncBroker` stays the single class. Its pool is lazy (`ensure_pool` runs
   inside the first routed call), so a broker constructed only to `sync` never builds a pool —
   control plane and data plane are two usage sites of one class, not two classes.
2. **One verb, two semantics by source type.**
   - `sync("freetier")` — *preset name*: the only networked operation in the library. Fetch the
     curated preset from the catalog, merge against the current registry state (custom entries
     preserved, coverage rule applied), mirror the result, return a `SyncReport`.
   - `sync("llms.toml")` / `sync(Registry(...))` — *path*: offline total mirror, exactly as today,
     now also returning a `SyncReport`.
3. **Lockfile model.** The vendored config file is the truth; refreshing it from upstream is an
   explicit repo/deploy action (human or bot), like a lockfile upgrade. Application start is never
   online and never fails because of preset staleness.
4. **A keyless entry costs nothing, so the merge never reasons about keys.** An entry whose
   `api_key_ref` does not resolve is simply not a routing candidate (`pool.py`, candidate filter);
   `catalog.py` logs it at INFO as the normal state. Adding a preset entry therefore needs no key
   check at all, and the only decision left for the merge is the *removal* side: may an entry the
   new preset dropped be deleted?
5. **Coverage rule** — the whole answer to that question:

   > An entry the new preset no longer lists is deleted if the merged lineup still contains an
   > entry with the same `api_key_ref`; otherwise it is kept.

   A same-provider swap (successor carries the same ref) drops the old entry immediately — the two
   share one quota, which is why `architecture.md` requires it. A provider leaving the lineup
   leaves its entry in place, so no installation loses a model it can actually call because
   upstream moved to a provider whose key that installation does not have.
6. **The merge is a pure function of (preset, current config).** No secrets probe, no environment
   lookup, no declaration. Consequences worth the rule: the same refresh produces byte-identical
   output in CI, on a dev box, and in the deploy job; a bot with no keys cannot mis-prune; and
   nothing needs re-running when a key later appears — the new entry is already in the config and
   activates on the next pool reconcile (`Catalog.resync`, driven by the debounced journal
   rebuild), including a per-user key resolved through the scope-prefixed ref.
7. **Kept entries carry no marker.** Retention is recomputed from (preset, current) on every
   merge, so a persisted flag would be an output masquerading as an input. The file writer groups
   them under a generated comment; the report names them on every run, including no-ops.
8. **Never-break invariant** for the offline path: `sync(path)` is atomic (validate, then apply or
   leave untouched) and refuses to apply a result that would leave zero pooled entries with a
   resolvable ref while the current state has at least one; refusal raises `SyncRefusedError`
   carrying the report. Empty-registry targets accept anything (onboarding). On the preset-name
   path the coverage rule makes that transition structurally unreachable — the lineup cannot drop
   a ref and cover it at the same time — so the check there is a cheap assertion, not the
   mechanism.
9. **Visibility — admin-facing, raw facts, no severity enum.** The admin sees the outcome where
   they already look, not in a runtime API:
   - `SyncReport` — returned by every sync and printed by the CLI on every run *including
     no-ops*, so pending keys and kept entries nag in each deploy log until resolved;
     `last_sync_report` lets a host forward it to its own admin channel;
   - the committed config file is the durable state: kept entries and the keys they need sit in
     the file, so a bot refresh is reviewable in the PR diff itself, and the vendored file's git
     history is the update record — no storage of its own.
   `snapshot()` is not part of the admin story — it is runtime state for the application's own UI
   and gains nothing here. The journal stays what it is — a stream of LLM calls and quality
   ratings; a sync is a registry operation and never writes there. The host derives criticality
   (`active_after == 0` critical, `< before` degraded, pending-only informational); the
   recommended interpretation goes in docs, not code.
10. **Sugar**: `AsyncBroker(..., sync="freetier")` / `Broker(..., sync=...)` — best-effort refresh
    before the first provision: fetch failure or refusal logs a warning, stashes the report, and
    continues on the existing config. Start never dies because of an update. The explicit
    `.sync()` call always raises — the caller chose to sync and has a plan.
11. **The CLI writes files only; the DB is written from code.** `llmbroker sync <preset-name>
    <file.toml>` replaces `preset --merge`; the existing `llmbroker sync <file> <db>` form is
    **removed**. Rationale: a CLI that takes a DSN duplicates connection config the application
    already owns (drift = syncing one database while serving from another, a silent failure) and
    forces DB credentials into the CLI's environment, which an app fetching its DSN from Vault
    cannot supply. Mirroring into a registry is therefore done by the host's own entrypoint, built
    by the same factory the application uses — the alembic `env.py` pattern: the library owns the
    operation, the application owns the connection. The two halves also split cleanly by nature:
    the online half (fetch + merge) runs on the *file* in CI/bot/dev where no DB is reachable, the
    offline half (mirror file → registry) runs in the deploy job with the app's own secrets. No
    compatibility shims — no published users.
12. Out of scope (explicitly rejected during design):
    - **a `guaranteed = [...]` key declaration in the config file** — it existed only to let the
      merge decide removals by key availability. With the coverage rule the merge takes no key
      input, so the declaration has no consumer. Its motivating case (a CI bot that can reach
      neither DB nor Vault) is exactly the case where a key-driven rule is least trustworthy.
    - **a `replaces = "old-name"` curator annotation** — it encoded "this supersedes that", which
      coverage derives from the ref the successor already carries. An annotation is also only as
      good as the curator's memory, and its failure mode (forgotten → entry deleted) is the
      damaging direction.
    - **a persisted `retained` / `LLMConfig` flag** — see design point 7.
    - **promoting a dropped entry to `[[custom]]`** — `custom` means user-owned and
      direct-reachable by name; an entry upstream abandoned is neither, and the promotion would
      also make it permanently unprunable, losing the "preset covers the ref again → delete"
      path.
    - a Hub/composition entity, a library-level singleton, a pytest plugin validating configs in
      tests, moving removed models to direct-only, per-scope key validation (per-user keys are
      runtime visibility via a scoped broker's `snapshot()`, never a lineup input).

## 1. Models and exceptions

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
      entry_names: tuple[str, ...]      # entries inactive until this ref resolves

  @dataclass(frozen=True, slots=True)
  class SyncReport:
      source: str                        # preset name or path as given
      applied: bool
      added: tuple[str, ...]
      updated: tuple[str, ...]
      removed: tuple[str, ...]
      kept: tuple[str, ...]              # dropped upstream, ref not covered → left in place
      pending_keys: tuple[PendingKey, ...] = ()
      active_before: int = 0             # pooled entries with a resolvable shared ref, pre-sync
      active_after: int = 0              # same, post-sync
  ```

  The three key-derived fields default to empty/zero because the merge does not compute them (see
  §2): a caller that has a secrets backend fills them in afterwards. `__str__` renders the
  human-readable block the CLI and the deploy recipes print — one line per section, sections with
  nothing to say omitted, pending keys rendered with their help text.

`src/llmbroker/exceptions.py`: `SyncRefusedError(LLMBrokerError)` with a `report: SyncReport`
attribute; message states what was refused and which refs would fix it.

`SyncReport`, `PendingKey` and `SyncRefusedError` join the top-level `__init__.py` re-export and
its `__all__` — a caller reading `last_sync_report` or catching the refusal must not import from
a submodule.

Tests: `tests/test_models.py` — `SyncReport` / `PendingKey` construction and `__str__` shape
(including the no-op form). No metadata round-trip work: nothing was added to `LLMConfig`.

## 2. Merge engine — new module `src/llmbroker/broker/upstream.py`

Move (not re-export) from `cli.py` into this module, since the library must not import from the
CLI: `_PRESET_URL`, `_PRESET_NAME_RE`, `_fetch_preset_file` (rename `fetch_preset_text`),
`_catalog_alias_index`, `_refresh_alias_entries`, `_merged_name_clash`, `_custom_key_tail`. The
alias-following custom-entry refresh against the paid catalog keeps working exactly as today when
the source is a preset name. CLI keeps only argument parsing and printing.

Core pure function (no I/O, no secrets, fully unit-testable):

```python
def merge_upstream(
    preset_configs: list[LLMConfig],
    preset_keys: dict[str, KeyInfo],
    current_configs: list[LLMConfig],
    current_keys: dict[str, KeyInfo],
) -> tuple[list[LLMConfig], dict[str, KeyInfo], SyncReport]
```

Rules, in order:

1. Managed lineup = preset entries; `custom=True` entries from `current_configs` are carried over
   untouched (alias refresh happens before this call, on the raw entries, as today).
2. Model-identity guard: same name, different `model` between current and merged result is a
   `ValueError` (existing `catalog.sync` rule — keep the wording).
3. **Coverage rule.** For each managed (`custom=False`) entry in `current_configs` whose name is
   absent from `preset_configs`: drop it if any preset entry carries the same `api_key_ref`,
   otherwise append it to the merged lineup unchanged and record it in `report.kept`. Order: kept
   entries come after the preset lineup, in their previous relative order.
4. Keys: the merged key table is the preset's `[keys]` plus, for every ref used by a kept or
   custom entry and absent from the preset's table, the entry the current file already had. A kept
   entry without key help is not an error — it renders with an empty help string.
5. Name-clash check between managed (incl. kept) and custom entries → error, nothing emitted.
6. Report facts: `added` / `updated` / `removed` against `current_configs` (managed only; a kept
   entry is neither removed nor added, in any run), `kept` as above. The three key-derived fields
   stay at their defaults here.

Key facts are filled by a separate async helper, used by every caller that has a secrets backend:

```python
async def with_key_facts(
    report: SyncReport,
    before: list[LLMConfig],
    after: list[LLMConfig],
    keys: dict[str, KeyInfo],
    secrets: SecretsProtocol,
) -> SyncReport
```

Returns a `dataclasses.replace` copy carrying `pending_keys`, `active_before`, `active_after`.
Shared refs only, never scope-prefixed: per-user keys are not part of an installation's baseline.
A ref that raises `KeyError` counts as unresolved. Nothing in the merge decision reads these
numbers — they are report facts and the offline path's guard input, nothing else.

Two writers around the pure merge:

- **File target**: preset text verbatim first (comments preserved, as `_merge_preset` does today),
  then a generated comment header plus one `[[llms]]` block per kept entry, then the `[[custom]]`
  blocks, then the `[keys.REF]` tail. Atomic: any error leaves the target untouched.

  **Writer hazard, verified against the installed `tomli_w`:** `tomli_w.dumps({"custom": [...]})`
  renders an array of dicts *inline* (`custom = [ { ... } ]`) whenever the rendered line fits
  `MAX_LINE_LENGTH = 100`, and only then falls back to `[[custom]]` array-of-tables form. Appended
  after preset text that ends in a `[keys.GEMINI_API_KEY]` table — which `presets/freetier.toml`
  does — an inline top-level key parses as a *member of that table*, so the entries silently
  vanish from the top level. Today's `_merge_preset` escapes this only because realistic
  `base_url` + `api_key_ref` values push every entry past 100 chars. The new writer must not
  inherit the hazard: emit each entry as an explicit `"[[llms]]\n" + tomli_w.dumps(entry)` /
  `"[[custom]]\n" + tomli_w.dumps(entry)` section rather than dumping the array. (Nested
  dict-of-dicts such as `keys` are unaffected — they always render as tables.)
- **Registry target**: `MutableRegistryProtocol.mirror(merged)` — unchanged protocol.

Tests (`tests/test_upstream.py`):

- same-provider swap (successor carries the ref) drops the old entry immediately;
- cross-provider swap keeps the old entry, with no key present anywhere and again with the
  successor's key present — **identical output both times** (purity);
- the provider returning to the preset with a new model deletes the kept entry;
- removal while a sibling entry of the same provider stays in the preset → deleted;
- kept entries survive an arbitrary number of consecutive merges without accumulating markers or
  duplicating;
- custom entries, their aliases and their `[keys]` survive; alias refresh still rewrites provider
  fields;
- name clash refused; model-identity change refused;
- key help for a kept entry is carried over from the current file;
- **writer regression**: a `[[custom]]` and a kept `[[llms]]` entry short enough to render inline
  still round-trip as top-level entries after a preset whose text ends in a `[keys.*]` table;
- `with_key_facts` — pending keys grouped by ref with help text, `active_*` counts, unresolved
  refs, scope-prefixed refs ignored;
- report field assertions for each scenario, including the no-op run.

## 3. `sync` on the broker — `broker/broker.py`, `broker/catalog.py`

Signature: `async def sync(self, preset: RegistryProtocol | str | Path) -> SyncReport` (both
`AsyncBroker` and the `Broker` wrapper in `sync.py`).

Source dispatch for `str` (same precedence `cli._env_source_data` already uses): an existing path
→ path semantics; else matches `^[a-zA-Z0-9_-]+$` and has no suffix → preset name; else error
naming both accepted forms.

Preset-name path:

1. `fetch_preset_text(name)` — network errors raise (`urllib` errors wrapped into a clear
   message); nothing has changed yet.
2. Load current state from the broker's own registry; key infos via `KeyInfoProtocol` probe (file
   registries have them, DB registries do not).
3. `merge_upstream(...)`, then `with_key_facts(...)` using the broker's own secrets backend — so a
   DB/Vault installation reports accurately without declaring anything.
4. Assert the never-break invariant (`active_after == 0 < active_before` → `SyncRefusedError`).
   Unreachable under the coverage rule; kept as a guard because it is two comparisons.
5. Apply: file registry → rewrite the TOML file (the broker's file registry is the target — this
   is the write path that makes `Broker("llms.toml", sync="freetier")` self-contained); mutable
   DB registry → `mirror`. A read-only registry without a file path keeps today's `TypeError`.
6. Existing post-mirror steps unchanged: `_seed_secrets`, disabled-map seeding, immediate resync
   when provisioned.
7. **Log the outcome, then return the report.** One line per sync, on both source paths, so the
   operation reports itself wherever it runs and no caller has to remember to read the return
   value: `logger.warning` when the update needed something from an admin — a pending key, or an
   entry kept because nothing else covers its ref — and `logger.info` otherwise, including the
   no-op. Never both, never per-entry. `SyncReport.__str__` is the message.

Path source: today's total mirror plus report computation (diff before/after) and
`with_key_facts`, then the never-break check — here it is load-bearing, since an arbitrary file is
not constrained by the coverage rule — then apply, return the report.

Tests (`tests/test_broker_sync_upstream.py` + extend existing sync tests): sqlite registry
end-to-end with a stubbed fetch (monkeypatched `fetch_preset_text` — no network in tests); a
cross-provider preset change keeps the old entry in the DB and the pool keeps routing over it;
file registry write-back preserves `[[custom]]` and comments; the offline path's refusal leaves
the registry byte-identical and raises with the report; report returned on both paths; existing
sync callers keep passing (return value ignored is fine); via `caplog` — a run that keeps an entry
or leaves a pending key logs exactly one WARNING, a clean change and a no-op log exactly one INFO,
on both the preset path and the path source.

## 4. Sugar: `sync=` constructor knob

- `AsyncBroker.__init__(..., sync: str | Path | None = None)` — stored, not executed (constructor
  stays synchronous and offline).
- Executed **at the top of `ensure_pool()`, inside the provision lock, before
  `_catalog.provision()`**. That single placement gets the ordering right everywhere: entering
  the context manager provisions eagerly (`__aenter__` → `ensure_pool`, `broker.py:245`), and
  provisioning an empty registry raises `EmptyRegistryError` — so a sync that runs *after* it
  could never populate a fresh registry. Running it first also makes the knob work for callers
  who never use a context manager, since every public operation funnels through `ensure_pool`.
- Best-effort: `SyncRefusedError` → stash `exc.report`; fetch/network errors → stash nothing;
  both log one `logger.warning` and continue on the existing configuration. Never raises.
- Success is not silent, and the knob adds nothing for it: `sync()` itself logs the outcome (§3
  step 7), so the sugar inherits one INFO/WARNING line per process. The knob's own logging is only
  the failure half above.
- Guarded by its own once-flag set before the attempt, not by `_provisioned`, so a failed
  provision retried later does not re-fetch.
- `Broker` (sync wrapper) takes the same keyword and inherits the behavior through the same call
  path.
- `last_sync_report: SyncReport | None` public attribute on both.

Tests: fresh empty sqlite registry + `sync="llms.toml"` → `async with` succeeds (sugar populates
before provisioning) where it raises `EmptyRegistryError` without the knob; sugar runs for a
caller that never enters the context manager; fetch failure and refusal both keep the old config
and stash/log; the fetch is attempted once across repeated calls; sync-wrapper parity.

## 5. CLI — `cli.py`

`llmbroker sync <preset-name> <file.toml>` — fetch, merge, write the file. That is the whole
command: **the target is always a `.toml` file, never a DSN.**

- A DB-shaped target (`.db`, `sqlite://`, `postgresql://`, `mongodb://`) is rejected with a
  message pointing at the code path: mirroring into a registry is the application's own
  entrypoint, so the connection config and its secrets stay in one place.
- The old `llmbroker sync <file> <db>` form is removed together with `preset --merge` (`preset`
  keeps its print-to-stdout form). Update the module docstring's subcommand list.
- Key facts in the printed report come from `Secrets(target.parent / ".env")` — the same resolver
  `resolve_source` gives a file registry. Absent keys change nothing about the merge result; they
  only shorten the report.
- Human-readable report print on every run, including no-ops (added/updated/removed/kept/pending
  keys with help texts); exit 0 on success even with pending keys (a pending transition is a
  valid state), exit 1 on fetch errors, clashes, and `SyncRefusedError`.
- `add-model`, `env` unchanged.
- `catalog.py`'s `EmptyRegistryError` message currently suggests `python -m llmbroker sync` for a
  DB registry — repoint it at `await broker.sync(...)` from the host's entrypoint.

Tests: adapt the existing `preset --merge` CLI tests to `sync <name> <file>`; DSN target rejected
with a non-zero exit and the pointer message; report printed on a no-op run; a run with no keys in
the environment produces the same file as a run with all keys.

## 6. Preset curation

No preset schema change: the coverage rule reads `api_key_ref`, which every entry already carries.

Curation rules → `specs/reference/freetier-providers.md`:

- a same-provider replacement removes the old entry, unchanged from today — downstream the
  coverage rule deletes it immediately, since the successor carries the same ref;
- dropping the last entry of a provider is the one removal downstream installations do **not**
  follow: they keep the entry as long as nothing else uses that ref. So a provider leaves the
  preset when it is no longer worth a slot, and installations that still have its key keep a
  working model; the sync report names it on every run so an admin can delete it by hand.

Also fix `presets/freetier-refresh-prompt.md`: its "removal turns into deprecation at deployments,
a reversible demotion" wording describes a `SeedPolicy` that no longer exists — `catalog.sync` is
a total mirror. Restate it as the coverage rule.

## 7. Spec updates (same batch as the behavior each describes)

- `architecture.md`, the sync/provider-seeding sections: name-vs-path semantics; the online
  boundary (path sync is offline always; preset-name sync is the library's only networked
  operation and runs only on explicit call); the coverage rule and why the merge takes no key
  input (a keyless entry is inactive, not an error, so only removals need a rule, and a pure merge
  is reproducible in CI and cannot mis-prune); that retention is recomputed, never stored; the
  never-break invariant and `SyncRefusedError`, load-bearing on the offline path and an assertion
  on the preset path; visibility as admin-facing raw facts — the report where syncs run, the
  pending state in the committed file, history in its git log; the journal stays calls and quality
  ratings only (a sync is a registry operation and never writes there), and `snapshot()` stays
  application-UI runtime state, not an admin channel; sugar `sync=` best-effort semantics; the
  lockfile lifecycle (start never online, refresh is a repo/deploy action — this also subsumes the
  existing seed-on-start cluster warning); and the ownership rule behind the CLI's file-only
  target — the library owns the sync operation, the host owns the connection, so a registry is
  only ever written from host code built by the host's own factory (no DSN and no DB credentials
  in llmbroker's CLI), while the networked half runs on the file where neither is reachable.
  The same-provider-replacement paragraph (currently ~line 406) gains the downstream half: the
  next `sync` deletes the old entry because the successor covers its ref.
- `mission.md` item 2: one added clause — the preset mirrors itself via `sync(name)`; the one
  irreducible admin act is obtaining a new provider key, and it is surfaced, never silently
  absorbed.
- `decisions.md`: one row for the merge engine + report (requirement 2/5, ~line counts, runtime
  cost: zero outside explicit sync).

## 8. Docs (`docs/src/en/` + mirror in `docs/src/ru/`)

Three recipes carry the whole story; each shows both the sync and a call.

- `index.md` Quick start: directly after the one-liner, the second bite:

  ```python
  llms = llmbroker.Broker("llms.toml", sync="freetier")   # keep the pool current
  print(llms.ask("Hello, how are you?").text)
  ```

  One sentence: the curated preset evolves; `sync` refreshes your file, your `[[custom]]` models
  and keys survive, a model whose provider left the preset stays as long as you have its key, and
  `llms.last_sync_report` says if the new lineup wants a key.
- `usage.md`: new "Keeping the pool fresh" subsection under "Model pool": the `sync=` knob, the
  explicit `llms.sync("freetier")`, reading the report, what a pending key means (a model waiting
  for a key — harmless until then) and what a kept entry means (upstream moved on, you still have
  the key, delete it from the file when you want).
- `async.md` / `server.md` — the sqlite recipe: one process, registry and secrets in the DB file,
  sugar does the refresh:

  ```python
  async with llmbroker.AsyncBroker("broker.db", sync="freetier") as llms:
      print((await llms.ask("Hello")).text)
  ```

  Note that the DB starts empty and the sugar populates it before the pool provisions, so no
  separate init step exists, and the outcome lands in the log by itself. Do **not** show the
  explicit `await llms.sync("freetier")` as an alternative here — the two differ in failure
  semantics, not in output, and the sqlite recipe is the case that wants to start anyway. The
  explicit call belongs to `server.md`, where a deploy job must fail loudly.
- `server.md`, "Shared DB" — rewrite around the two-place split:
  - refreshing the vendored file is a repo action (`llmbroker sync freetier .deploy/llms.toml`,
    by a human or a bot, reviewed as a PR);
  - mirroring it into the registry is the host's own deploy entrypoint, run in the same step as
    `alembic upgrade`, built by the same factory the application uses so the DSN and its secrets
    (Vault included) live in exactly one place:

    ```python
    llms = build_broker()                    # the app's own factory
    try:
        print(await llms.sync("llms.toml"))  # offline mirror; SyncRefusedError → non-zero exit
    finally:
        await llms.aclose()
    ```

    State plainly why this is not `async with`: entering provisions the pool, and a fresh
    registry is empty, so the context manager would raise `EmptyRegistryError` before the sync
    could populate it.
  - the serving half is a plain broker with no `sync=` knob — the deploy job already did it:

    ```python
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.llms = build_broker()
        async with app.state.llms:
            yield
    ```

  - it is a one-shot job (release phase, k8s Job, init container), **not** a per-node startup
    step — N nodes syncing their own file copies is the flip-flop the seed-on-start rule already
    forbids. A single-node app may put the offline sync in `lifespan`; apply-or-refuse makes
    that safe.
  - headless visibility: the job's exit code and log are the admin channel a failed migration
    already uses, and the never-break invariant means a failed job never takes the service down;
    `last_sync_report` for hosts forwarding to their own channel; the committed file's diff and
    git history for the bot-refresh half.
  - keys: nothing to declare anywhere. The merge does not read keys; the report does, through
    whatever secrets backend the broker already has.
- `cli.md`: `sync <preset-name> <file.toml>`; `preset --merge` and the DSN target both gone, with
  the one-line reason and a pointer to the `server.md` entrypoint recipe.

## Work order

Each batch ends green on `invoke pre` + `python -m pytest` (activate the venv first).

1. `SyncReport` / `PendingKey` / `SyncRefusedError` (§1) with tests.
2. Merge engine `upstream.py` (§2) with tests, including the writer regression; CLI temporarily
   keeps its own copies until batch 5.
3. Broker `sync` dispatch, file write-back, never-break, report (§3), with tests; spec updates
   for these behaviors (§7) in the same batch.
4. Sugar `sync=` (§4) with tests.
5. CLI unification (§5), delete the moved helpers from `cli.py`, adapt CLI tests; curation rules
   (§6).
6. Docs en + ru (§8).

Version bump: none (maintainer does it by hand).

## Verification

```bash
. ./activate.sh
invoke pre
python -m pytest
python -m llmbroker sync freetier /tmp/llms-check.toml   # live smoke, prints the report
```
