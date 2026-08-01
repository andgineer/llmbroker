# Preset sync: upstream refresh, key-aware retention, report, lifecycle

Closes the zero-admin gap around preset updates: today refreshing the model lineup is a manual
two-command ritual (`llmbroker preset freetier --merge llms.toml`, then `llmbroker sync llms.toml
<db>`) whose only output is stdout nobody reads in automated runs, and a curated update that swaps
a provider can silently shrink the working pool. After this plan, one verb — `sync` — covers the
whole flow, an update can never turn a working configuration into a dead one, and every outcome is
visible programmatically (report) and durably in the registry state itself (snapshot); update
history is the vendored file's git history.

## Design summary

1. **No new entity.** `AsyncBroker` stays the single class. Its pool is lazy (`ensure_pool` runs
   inside the first routed call), so a broker constructed only to `sync` never builds a pool —
   control plane and data plane are two usage sites of one class, not two classes.
2. **One verb, two semantics by source type.**
   - `sync("freetier")` — *preset name*: the only networked operation in the library. Fetch the
     curated preset from the catalog, key-aware merge against the current registry state
     (custom entries preserved, retention applied), mirror the result, return a `SyncReport`.
   - `sync("llms.toml")` / `sync(Registry(...))` — *path*: offline total mirror, exactly as today,
     now also returning a `SyncReport`.
3. **Lockfile model.** The vendored config file is the truth; refreshing it from upstream is an
   explicit repo/deploy action (human or bot), like a lockfile upgrade. Application start is never
   online and never fails because of preset staleness.
4. **`replaces` annotation** in preset entries carries the curator's knowledge "this entry
   supersedes that one" — the only party who knows whether a removal is a death or a hand-off.
5. **Guaranteed keys declaration**: `[keys.REF] guaranteed = true` in the user's config file
   declares "this installation is promised this key". Refs are names, not secrets — the
   declaration is committable, reviewable, and checkable with zero secret access. When no ref is
   declared, the effective set defaults to refs resolvable from env/`.env` at merge time (keeps
   the simplest case zero-config).
6. **Retention rule**: a preset entry carrying `replaces = "old-name"` whose `api_key_ref` is NOT
   in the effective guaranteed set keeps `old-name` alive (marked `retained = true`) as long as
   the old entry's own ref IS guaranteed. Recomputed on every merge as a pure function of
   (preset, current state, guaranteed set) — the tail cleans itself the moment the key is
   declared or the curator drops the annotation. Same-provider swaps (successor ref already
   guaranteed) drop the old entry immediately: no tail.
7. **Never-break invariant**: `sync` is atomic (validate, then apply or leave untouched) and
   refuses to apply a result that would leave zero pooled entries with a guaranteed ref while the
   current state has at least one. Refusal raises `SyncRefusedError` carrying the report.
   Empty-registry targets accept anything (onboarding: a keyless model is inactive, not an error).
8. **Visibility, two channels, all raw facts / no severity enum** (same philosophy as
   `snapshot()` and `stats()`):
   - `SyncReport` return value — push at the moment of change; a host that wants its own update
     history persists the report itself;
   - `snapshot()` `has_key` / retained flag — the registry state is the durable signal between
     syncs, it cannot expire or be missed.
   Update history needs no storage of its own: in the lockfile model every refresh is a commit,
   so the vendored file's git history is the record. The journal stays what it is — a stream of
   LLM calls and quality ratings; a sync is a registry operation and never writes there.
   The host derives criticality (`active_after == 0` critical, `< before` degraded, pending-only
   informational); the recommended interpretation goes in docs, not code.
9. **Sugar**: `AsyncBroker(..., sync="freetier")` / `Broker(..., sync=...)` — best-effort refresh
   on `__aenter__` (async) or construction (sync wrapper): fetch failure or refusal logs a
   warning, stashes the report, and continues on the existing config. Start never dies because of
   an update. The explicit `.sync()` call always raises — the caller chose to sync and has a plan.
10. **CLI unification**: `llmbroker sync <source> <target>` where source is a preset name or a
    `.toml` file and target is a `.toml` file or a DB URL. `preset --merge` is removed (`preset`
    keeps its print-to-stdout form). No compatibility shims — no published users.
11. Out of scope (explicitly rejected during design): a Hub/composition entity, a library-level
    singleton, a pytest plugin validating configs in tests (secrets in tests is an antipattern;
    the guaranteed declaration makes update-time checks secret-free instead), moving removed
    models to direct-only, per-scope key validation (declarations describe the shared set only —
    per-user keys are runtime visibility via a scoped broker's `snapshot()`).

## 1. Models and exceptions

`src/llmbroker/models.py`:

- `KeyInfo`: add `guaranteed: bool = False`. Parsed in
  `standalone/registry.py::key_info_from_entry` from `[keys.REF] guaranteed = true`; everything
  else stays in `extra` passthrough.
- `LLMConfig`: add `replaces: str | None = None` and `retained: bool = False`. Extend
  `to_metadata` / `from_metadata` (store only non-defaults; update the doctests) so DB registries
  persist both through the existing JSON metadata blob with no schema change. Parse both in
  `standalone/registry.py::_config_from_entry`.
- New frozen dataclasses:

  ```python
  @dataclass(frozen=True, slots=True)
  class PendingKey:
      api_key_ref: str
      help: str
      entry_names: tuple[str, ...]      # entries inactive until this ref is guaranteed

  @dataclass(frozen=True, slots=True)
  class RetainedEntry:
      name: str                          # the kept old entry
      successor: str                     # preset entry whose `replaces` names it
      pending_ref: str                   # the ref the successor waits for

  @dataclass(frozen=True, slots=True)
  class SyncReport:
      source: str                        # preset name or path as given
      applied: bool
      added: tuple[str, ...]
      updated: tuple[str, ...]
      removed: tuple[str, ...]
      retained: tuple[RetainedEntry, ...]
      pending_keys: tuple[PendingKey, ...]
      active_before: int                 # pooled entries with a guaranteed ref, pre-sync
      active_after: int                  # same, post-sync (retained entries count)
  ```

`src/llmbroker/exceptions.py`: `SyncRefusedError(LLMBrokerError)` with a `report: SyncReport`
attribute. Raised only on the working→dead transition; message states what was refused and which
refs would fix it.

Tests: `tests/test_models.py` (or the existing model test module) — metadata round-trip for the
new fields, doctest updates, `SyncReport` construction.

## 2. Merge engine — new module `src/llmbroker/broker/upstream.py`

Move (not re-export) from `cli.py` into this module, since the library must not import from the
CLI: `_PRESET_URL`, `_PRESET_NAME_RE`, `_fetch_preset_file` (rename `fetch_preset_text`),
`_catalog_alias_index`, `_refresh_alias_entries`, `_merged_name_clash`, `_custom_key_tail`. The
alias-following custom-entry refresh against the paid catalog keeps working exactly as today when
the source is a preset name. CLI keeps only argument parsing and printing.

Core pure function (no I/O, fully unit-testable):

```python
def merge_upstream(
    preset_configs: list[LLMConfig],
    preset_keys: dict[str, KeyInfo],
    current_configs: list[LLMConfig],
    current_keys: dict[str, KeyInfo],
    guaranteed: set[str],
) -> tuple[list[LLMConfig], dict[str, KeyInfo], SyncReport]
```

Rules, in order:

1. Managed lineup = preset entries; `custom=True` entries from `current_configs` are carried over
   untouched (alias refresh happens before this call, on the raw entries, as today).
2. Model-identity guard: same name, different `model` between current and merged result is a
   `ValueError` (existing `catalog.sync` rule — keep the wording).
3. Retention: for each preset entry `e` with `e.replaces` set, `e.api_key_ref not in guaranteed`,
   and `current` containing an entry named `e.replaces` whose own ref IS in `guaranteed` → append
   that old entry with `retained=True`. An entry only reachable through a `replaces` chain is not
   followed transitively — one hop; the curator renames the annotation when the chain moves on.
4. Name-clash check between managed (incl. retained) and custom entries → error, nothing emitted.
5. Report facts: added/updated/removed against `current_configs` (managed only; retained entries
   are neither added nor removed while they persist), `pending_keys` = refs of merged pooled
   entries not in `guaranteed`, grouped with `KeyInfo.help`, `active_*` counts as defined above.

Effective guaranteed set resolution (separate helper): refs with `KeyInfo.guaranteed` in
`current_keys`; if none is declared anywhere, fall back to probing each referenced ref against
env secrets (`llmbroker.standalone.secrets.Secrets`, with the config file's sibling `.env` for
file targets — same rule `resolve_source` already applies). Shared refs only, never
scope-prefixed.

Two writers around the pure merge:

- **File target**: emit preset text verbatim (comments preserved, as `_merge_preset` does today),
  then generated `[[llms]]` blocks for retained entries (`retained = true`), then the `[[custom]]`
  + `[keys]` tail via `tomli_w`. Atomic: any error leaves the target untouched.
- **Registry target**: `MutableRegistryProtocol.mirror(merged)` — unchanged protocol.

Tests (`tests/test_upstream.py`): same-key swap drops the old entry immediately; cross-provider
swap retains until the ref is guaranteed; declaring the ref cleans the tail on the next merge;
removal without `replaces` deletes regardless of keys; unrelated new keyless provider retains
nothing; custom entries and their keys survive; alias refresh still rewrites provider fields;
name clash refused; model-identity change refused; env-fallback guaranteed set; declared set
overrides env; report field assertions for each scenario.

## 3. `sync` on the broker — `broker/broker.py`, `broker/catalog.py`

Signature: `async def sync(self, preset: RegistryProtocol | str | Path) -> SyncReport` (both
`AsyncBroker` and the `Broker` wrapper in `sync.py`).

Source dispatch for `str` (same precedence `cli._env_source_data` already uses): an existing path
→ path semantics; else matches `^[a-zA-Z0-9_-]+$` and has no suffix → preset name; else error
naming both accepted forms.

Preset-name path:

1. `fetch_preset_text(name)` — network errors raise (`urllib` errors wrapped into a clear
   message); nothing has changed yet.
2. Load current state from the broker's own registry; key infos via `KeyInfoProtocol` probe
   (file registry has them; DB registries do not → env-fallback guaranteed set).
3. `merge_upstream(...)`.
4. Never-break check: `active_after == 0 < active_before` → raise `SyncRefusedError(report)`.
   Registry untouched.
5. Apply: file registry → rewrite the TOML file (the broker's file registry is the target — this
   is the write path that makes `Broker("llms.toml", sync="freetier")` self-contained); mutable
   DB registry → `mirror`. A read-only registry without a file path keeps today's `TypeError`.
6. Existing post-mirror steps unchanged: `_seed_secrets`, disabled-map seeding, immediate resync
   when provisioned.
7. Return the report.

Path source: today's flow plus report computation (diff before/after), the same never-break
check (this is what makes the lifespan offline-sync recipe safe), return report.

Tests (`tests/test_broker_sync_upstream.py` + extend existing sync tests): sqlite registry
end-to-end with a stubbed fetch (monkeypatched `fetch_preset_text` — no network in tests); file
registry write-back preserves `[[custom]]` and comments; refusal leaves registry byte-identical
and raises with the report; report returned on the offline path too; existing sync callers keep
passing (return value ignored is fine).

## 4. Sugar: `sync=` constructor knob

- `AsyncBroker.__init__(..., sync: str | Path | None = None)` — stored, not executed (constructor
  stays sync and offline).
- Executed once in `__aenter__` before returning `self`: `try: self.last_sync_report = await
  self.sync(self._sync_source)` — on `SyncRefusedError` stash `exc.report`, on fetch/network
  errors stash nothing; both log one `logger.warning` and continue. Never raises. Without a
  context manager the knob is inert — documented; explicit `.sync()` is the alternative.
- `Broker` (sync wrapper): same best-effort during construction on its loop thread.
- `last_sync_report: SyncReport | None` public attribute on both.

Tests: enter with fetch failure → warning + old config serves; refusal → old config serves +
report stashed; success → applied + report stashed; wrapper parity.

## 5. CLI — `cli.py`

`llmbroker sync <source> <target>`:

| source \ target | `.toml` file | sqlite path / `postgresql://` / `mongodb://` |
|---|---|---|
| preset name | fetch + merge into file (replaces `preset --merge`) | fetch + merge against DB state, mirror |
| `.toml` file | error (nothing to do) | offline mirror (today's `sync`) |

- Human-readable report print on every run (added/removed/retained/pending keys with help
  texts); exit 0 on success even with pending keys (a pending transition is a valid state), exit
  1 on fetch errors, clashes, and `SyncRefusedError`.
- Remove the `--merge` flag from `preset` (plain stdout print stays). Update the module
  docstring's subcommand list.
- `add-model`, `env` unchanged.

Tests: adapt the existing `preset --merge` CLI tests to `sync <name> <file>`; add name→db and
refusal exit-code cases.

## 6. Preset schema and curation

- `presets/freetier.toml`: `[[llms]]` entries may carry `replaces = "old-entry-name"`. No entry
  needs it initially; the field lands with the parser.
- Curation rule → `specs/reference/freetier-providers.md`: a successor at a *different* provider
  (new `api_key_ref`) must carry `replaces`; a same-provider swap needs nothing; a dead model is
  removed without annotation; `replaces` is dropped once the old provider actually retires the
  old model — the annotation is the retention window, and it encodes the one fact only the
  curator knows.

## 7. Spec updates (same batch as the behavior each describes)

- `architecture.md`, the sync/provider-seeding sections: name-vs-path semantics; the online
  boundary (path sync is offline always; preset-name sync is the library's only networked
  operation and runs only on explicit call); guaranteed-keys declaration (refs are names, not
  secrets; shared set only); the retention rule and its recompute-not-state nature; the
  never-break invariant and `SyncRefusedError`; report + snapshot as the two visibility channels
  (raw facts, host derives severity; the journal stays calls and quality ratings only — a sync is
  a registry operation and never writes there; update history is the vendored file's git
  history); sugar `sync=` best-effort semantics; the lockfile lifecycle (start never online,
  refresh is a repo/deploy action — this also subsumes the existing seed-on-start cluster
  warning).
- `mission.md` item 2: one added clause — the preset mirrors itself via `sync(name)`; the one
  irreducible admin act is obtaining a new provider key, and it is surfaced, never silently
  absorbed.
- `decisions.md`: one row for the merge engine + report (requirement 2/5, ~line counts, runtime
  cost: zero outside explicit sync).

## 8. Docs (`docs/src/en/` + mirror in `docs/src/ru/`)

- `index.md` Quick start: directly after the one-liner, the second bite:

  ```python
  llms = llmbroker.Broker("llms.toml", sync="freetier")   # keep the pool current
  ```

  One sentence: the curated preset evolves; `sync` refreshes your file, your `[[custom]]` models
  and keys survive, and `llms.last_sync_report` says if the new lineup wants a key.
- `usage.md`: new "Keeping the pool fresh" subsection under "Model pool": the `sync=` knob, the
  explicit `llms.sync("freetier")`, reading the report, what a pending key / retained entry
  means, the `guaranteed` declaration in `[keys]`.
- `server.md`: in "Shared DB", right after the CLI sync block — run it in the same deploy step as
  `alembic upgrade`; the lifespan recipe with offline `sync(vendored-file)` and its
  apply-or-refuse guarantee; headless visibility — the report return value (persist it host-side
  if an update-history screen is wanted) and `snapshot()` for the current pending state, with the
  vendored file's git history as the record of what changed when; for DB installs the
  `guaranteed` declaration lives
  in the vendored file (keys' values live wherever they live — env, DB, Vault).
- `cli.md`: the unified `sync` matrix, `preset --merge` gone.
- `secrets.md`: a short note that `guaranteed` declares ref *names*, never values.

## Work order

Each batch ends green on `invoke pre` + `python -m pytest` (activate the venv first).

1. Models + exceptions + parsers (§1) with tests.
2. Merge engine `upstream.py` (§2) with tests; CLI temporarily keeps its own copies until batch 5.
3. Broker `sync` dispatch, file write-back, never-break, report (§3), with tests; spec updates
   for these behaviors (§7) in the same batch.
4. Sugar `sync=` (§4) with tests.
5. CLI unification (§5), delete the moved helpers from `cli.py`, adapt CLI tests; preset schema +
   curation rule (§6).
6. Docs en + ru (§8).

Version bump: none (maintainer does it by hand).

## Verification

```bash
. ./activate.sh
invoke pre
python -m pytest
python -m llmbroker sync freetier /tmp/llms-check.toml   # live smoke, prints the report
```
