# Pool lifecycle: provider retirement, death evidence, degradation visibility

Follows the preset-sync change, implemented in the working tree but not merged; its own plan file
goes once this one carries everything that change's review found, so the rules below stand on
their own. This plan replaces that change's removal rule and part of its report, and closes what
it left open: what happens to a provider the curated lineup retires while the installation still
holds its key, and how an installation notices that its pool has degraded. **The two ship in one
release** — a reviewer reads them as one change.

## What this replaces

| in the tree today | replaced by |
|---|---|
| `_removal_plan`'s "pairs, then budget" — arrivals pay for removals | §1: the provider is the unit; no budget, no ordering |
| the `kept` sentence "set REF and the next sync removes it", and `SyncReport.unlocking_refs` behind it | §1.4: unachievable as written, and verified false — see below |
| `AsyncBroker.sync`'s WARNING when an entry is kept | §3: a kept entry asks nothing of an admin; a degraded pool does |
| the file writer fed any source text, via `render_lineup` | §4.1: a `.toml` target is synced from a curated preset only |

Why the budget rule cannot stand: it pays for removals out of *this run's arrivals*, but the
merged result contains both the arrival and the entry it could not pay for, so on the next run
there are no arrivals at all and the entry stays forever. Reproduced against the implementation —
after the admin sets the key the report demanded, the next sync keeps the entry and changes its
own message to "nothing arrived to replace it". The rule below depends only on the state of the
world (which providers the lineup carries, which keys exist, what the journal recorded), so it
converges.

## Design summary

1. **The provider is the unit of decision** — `api_key_ref`, not the entry. Two entries on one ref
   are one quota and one failure domain; counting them as two of anything is wrong.
2. **Merge**: entries of providers the lineup carries are refreshed from it, always; entries of a
   provider the lineup no longer carries go when no key exists for that provider, or when this
   installation's own journal proves they do not work; otherwise they stay and keep routing.
3. **Death is proven, never assumed**: at least one permanent client failure (401/403/404) and no
   success at all in the journal window. A bad week (429, 5xx) proves nothing.
4. **Invariant**: a sync never takes away a model this installation can call, except by replacing
   it with the same provider's model, or when the journal says the model does not work. The
   `mission.md` wording ("never shrinking what the pool can call") stays true as written.
5. **The key's fate is reported**: when a removal leaves a ref referenced by nothing, the report
   says the key is now unused and can be revoked. When a `[[custom]]` entry still uses it — the
   paid direct model on the same provider — it is not called orphaned and nothing is suggested.
6. **Degradation is an error, where it happens**: one usable provider is no pool (a single quota,
   nothing to fail over to), zero is a dead pool. Both log `ERROR` on the transition into that
   state, one `INFO` on the way back. Missing keys are never an alarm on their own: two providers
   may be all a host wants.
7. **`snapshot()` carries the whole pool**, so an admin UI needs one call: the per-LLM mapping it
   returns today plus the provider counts, the missing keys with their help text, and the same
   `degraded` predicate the ERROR uses. One definition, two consumers — log and UI cannot diverge.
8. Out of scope, rejected during design:
   - **`pool = false` on a key** (or any new TOML field) declaring "this key is not for the pool" —
     the state it describes is derivable, and the case it was invented for (a key kept for paid
     direct calls) is served by rule 5 plus death evidence.
   - **A key-deletion path in the secrets protocol** — a key that cannot be deleted (paid direct
     use) is the common case, so deletion can never be the retirement mechanism; llmbroker reports
     the orphan and the human decides.
   - **`sync(..., exact=True)` / `--exact`** — indistinguishable from `registry.mirror(configs)`,
     which already exists as the escape hatch; a host that wants exactness surfaces it in its own
     admin UI.
   - **"Two callable providers" as a pruning threshold** — a policy constant that discards working
     free quota. The same number survives only as the *degradation* criterion, where it is a
     description of the failover feature, not a policy that deletes anything.
   - **Probing the provider (`GET {base_url}/models`) for death evidence** — deferred. Revisit only
     if the journal proves too thin in practice.
   - **An `llm_name` filter on `QueryableStoreProtocol.calls`** — the bounded tail is enough for a
     handful of candidates; adding a filter touches every backend.

## 1. The merge rule — `broker/upstream.py`

### 1.1 Inputs

`merge_upstream` stays pure and gains two inputs, both computed by the caller:

```python
def merge_upstream(
    new_configs: list[LLMConfig],
    new_keys: dict[str, KeyInfo],
    current_configs: list[LLMConfig],
    current_keys: dict[str, KeyInfo],
    present: set[str],
    *,
    source: str,
    dead: frozenset[str] = frozenset(),   # entry names the journal proved unusable
    keys_visible: bool = True,            # only then does a missing key mean anything
) -> tuple[list[LLMConfig], dict[str, KeyInfo], SyncReport]
```

```python
def keys_are_visible(
    present: set[str],
    *,
    scope: str | None,
    have_keys: bool | Sequence[str],
) -> bool:
    """Absence of a key is evidence only where the probe could have found one.

    Per-user keys behind ``scope`` are one such place; a probe that resolved
    nothing at all is the other — the keys live in a store this merge site
    cannot reach, and "no key anywhere" is indistinguishable from "not my keys".
    """
    return bool(present) and (scope is None or bool(have_keys))
```

**Why the empty-probe clause is not optional.** Under the rule this plan replaces, a merge site
that cannot see the keys was merely over-conservative: nothing was paid for, so nothing was
removed. Under §1.2 absence *deletes*, and the CLI merge site resolves `os.environ` plus the
target's sibling `.env` and nothing else — while "registry in `llms.toml`, secrets in
Vault/AWS/a DB" is a supported combination (it is the same one §4.3 exists for). Without the
clause, `llmbroker preset freetier --sync llms.toml` on such an installation removes every entry
of a retired provider that production can still call, which is exactly what invariant 4 forbids.
`broker.sync(...)` is unaffected — it probes the broker's own secrets backend, which is the
application's. The clause also reads correctly for onboarding: a fresh install with no keys yet
proves nothing about anything and keeps its whole lineup.

Not a tunable threshold: the clause fires only on a probe that resolved *nothing*, and `have_keys`
remains the way an installation that knows better overrides it.

### 1.2 The rule

`_removal_plan` replaces the pairs-and-budget function entirely; the `order`/`budget`/`paid`
machinery goes away. With `lineup_refs = {c.api_key_ref for c in new_managed}` and
`dropped = [managed current entries whose name is absent from the new lineup]`, in current order:

```
for d in dropped:
    if d.api_key_ref in lineup_refs:  removed.append(d)   # the lineup's models for that
                                                          # provider replace it: same key,
                                                          # same quota, no key lookup
    elif keys_visible and d.api_key_ref not in present:   removed.append(d)
    elif d.name in dead:                                  removed.append(d)
    else:                                                 kept.append(d)
```

Everything else in `merge_upstream` is unchanged: custom entries, the model-identity guard, the
name-clash check, the key table, `check_not_emptying`.

Note the guard is now reachable through the normal path (an empty lineup over a registry whose
entries are all keyless removes everything), so the test that documents it as unreachable is
rewritten.

### 1.3 Report

`SyncReport` fields after this plan — `unlocking_refs()` and its "set REF" sentence go:

```python
source: str
applied: bool
added / updated / removed / kept: tuple[str, ...]
kept_refs: tuple[str, ...]      # providers behind `kept`, distinct, in order
retired: tuple[str, ...]        # subset of `removed`: taken out on death evidence
orphan_refs: tuple[str, ...]    # refs nothing in the merged lineup references any more
pending_keys: tuple[PendingKey, ...]
active_before: int
active_after: int
keys_visible: bool
```

`orphan_refs` = refs of removed entries minus refs used by anything in the merged result,
`[[custom]]` included. That single line is what keeps a paid direct model's key out of the
"revoke it" advice.

`__str__` renders, sections with nothing to say omitted:

```
sync freetier: applied — 3 -> 2 entries with a key
  added: cerebras-gpt-oss-120b
  removed: groq-llama-3.3-70b
  retired: groq-llama-3.3-70b — 401 since 2026-07-02, no successful call since; the lineup
      dropped it too
  kept: openrouter-nemotron — the lineup no longer carries OPENROUTER_API_KEY and this
      installation has a key for it, so it stays
  unused key GROQ_API_KEY — nothing here uses it any more; revoke it at the provider if you
      do not need it
  pending key GEMINI_API_KEY — holds back gemini-3.5-flash-lite
      https://aistudio.google.com/apikey
```

With `keys_visible=False` the `kept` line says why no probe could have proven the key missing —
"keys are per user here" when `scope` is set, "no key resolved here at all, so they are not the
keys this lineup runs on" when the probe came back empty.

### 1.4 Logging

`AsyncBroker.sync` logs the report at `INFO`, always. The WARNING branch goes: nothing in a sync
outcome is admin-actionable any more — degradation is, and §3 owns it. The alias-refresh WARNING
for an alias the catalog no longer knows stays as it is.

The log line and `last_sync_report` both land **before** `_catalog.resync()`, not after it as
today: a resync that raises must not swallow the record of a change already applied.

## 2. Death evidence — `broker/upstream.py` + `broker/broker.py`

### 2.1 Who is a candidate

Evidence is only ever needed for entries that would otherwise be kept: managed, name absent from
the arriving lineup, provider absent from it too, key present. **When there are no candidates —
every ordinary sync — the journal is not read at all.** This is the whole cost story: zero.

### 2.2 The criterion

```python
async def dead_entries(
    names: Iterable[str],
    store: StoreProtocol | None,
    *,
    limit: int = _EVIDENCE_LIMIT,
) -> frozenset[str]
```

Returns the subset of `names` for which the journal tail holds at least one `kind="call"` row with
`http_status` in (401, 403, 404) and no row with `status == CallStatus.OK`. Not queryable store,
no rows, or any row that succeeded → not dead. No key-hash condition: in a scoped installation the
rule reads as "nobody could call it", which is exactly the evidence wanted there.

`_EVIDENCE_LIMIT` reuses `_DEFAULT_STATS_LIMIT`, the tail the broker already reads for stats — one
`calls(limit=..., kind="call")` call, filtered in memory. A busy pool may push a once-a-day
failure out of that tail; then there is no evidence and the entry stays. Conservative on purpose.

### 2.3 Wiring

- `AsyncBroker.sync` computes candidates from the loaded lineups, calls `dead_entries` with
  `self._base_store`, passes the result into the merge (both the registry and the file branch).
- The CLI has no broker. `preset <name> --sync <file>` opens the default journal by the same
  convention `_default_store` uses — `FileStore(<target dir>/store)` when that directory exists —
  and passes the same evidence. No directory, no evidence, nothing removed.

## 3. Degradation visibility

### 3.1 The measure

Distinct `api_key_ref` among **pooled** entries that have a key: `providers_usable` of
`providers_total`. `degraded` is `providers_usable < 2` — one provider is a single quota with
nothing to fail over to, which is the failover feature's own definition, not a tuning knob.

### 3.2 The ERROR

Lives in `Catalog`, right after `_reconcile` — so it covers provisioning, every resync, and every
sync, in one place:

- `providers_usable == 0` → `ERROR`, "pool cannot serve any request";
- `providers_usable == 1` → `ERROR`, "no failover left";
- back to ≥ 2 → one `INFO`, "pool recovered: N of M providers usable".

Logged on the transition only (the catalog remembers the last level it reported), so a healthy log
carries none of these lines and a broken one carries exactly one per change. Both ERROR lines name
the missing refs. `_maybe_alert_underprov` is untouched — it is about cooldowns, a different fault.

### 3.3 `PoolSnapshot`

```python
@dataclass(frozen=True, slots=True)
class PoolSnapshot(Mapping[str, LLMSnapshot]):
    """Point-in-time view of the whole pool. Iterate it like a dict of
    ``name -> LLMSnapshot``; the fields describe the pool as a whole."""

    _llms: Mapping[str, LLMSnapshot]
    providers_usable: int
    providers_total: int
    missing_keys: tuple[PendingKey, ...]

    @property
    def degraded(self) -> bool: ...
    def __getitem__(self, name: str) -> LLMSnapshot: ...
    def __iter__(self) -> Iterator[str]: ...
    def __len__(self) -> int: ...
```

Verified: `Mapping` defines `__slots__ = ()`, so a frozen `slots=True` dataclass may subclass it,
and `items()/keys()/get()/in` come from the mixin. `_llms` is private so the public surface is
three fields and one predicate; today's callers (`snap[name]`, `.items()`) keep working unchanged.

`PoolView.snapshot()` builds it. `missing_keys` reuses `PendingKey` (ref, help, entry names it
holds back); the help text comes from a `KeyInfo` cache the catalog fills during `_reconcile` from
`registry.key_info()` when the registry has one — so `snapshot()` performs no extra registry I/O,
and a DB registry without key info yields empty help but correct refs and names.

`Broker.snapshot()` returns the same object; its annotation changes.

## 4. Defects carried over from the review of the preset-sync implementation

### 4.1 A `.toml` target is synced from a preset only

`broker.sync(<path>)` into a **file** registry currently writes the arriving text verbatim and then
appends the current file's `[[custom]]` blocks, so a source that carries its own `[[custom]]`
duplicates them; the file no longer parses and the broker will not start. Reproduced with
`broker.sync("llms.toml")` on a broker opened on that same file.

Fix: a file target accepts a curated preset name only. A file or registry source syncs into a
database registry — the vendored-lockfile deploy path, where the merge already dedupes custom
entries correctly. `AsyncBroker.sync` raises `ValueError` naming both forms. `render_lineup` and
its call site disappear with the case they existed for.

Belt and braces, since this is the one code path that can destroy a user's config: `sync_file`
parses the rendered text before `write_atomic` and refuses to write when its entry names differ
from the merge result.

### 4.2 `have_keys="GEMINI_API_KEY"`

A bare string is a `Sequence[str]`, so it is silently taken apart into characters and the
declaration is lost. `present_refs` normalizes a `str` to a one-element list.

### 4.3 Key seeding parity

`seed_secrets` runs on the registry branch but not on the file branch, so "registry in a TOML file,
secrets in a DB/Vault" never picks up a new key from `.env`. The file branch calls
`catalog.seed_secrets(...)` after the write, like the registry branch does.

`architecture.md` already states this unconditionally ("Beyond writing the lineup, `sync`
bootstraps secrets…"), so the fix makes the spec true and the spec needs no edit.

### 4.4 A fetch failure is a `ValueError` — including one that happens mid-read

`fetch_preset_text` catches `HTTPError` and `URLError`, which cover the connect phase only.
Anything raised inside `resp.read()` or the decode — `TimeoutError`, `ConnectionResetError`,
`http.client.IncompleteRead` — passes through, though the function's own docstring promises
"every failure is a `ValueError` carrying an admin-readable message". Two live consequences,
both reproduced:

- the CLI (`except (SyncRefusedError, ValueError)`) prints a traceback instead of `error: …`;
- the `sync=` knob stops being best-effort. `_sync_on_start` catches `(ValueError, OSError)`, and
  `IncompleteRead` is an `HTTPException`, so a truncated response from the catalog **kills process
  start** — the exact failure §5 of the replaced plan existed to prevent ("Never raises", "start
  never dies because of an update").

Fix: wrap the read and decode in the same `try`, raising `ValueError` for `OSError` and
`http.client.HTTPException` alike; add `OSError` to the CLI's `except`, which also turns the
writer's own failures (`--sync out/llms.toml` with no `out/` directory currently tracebacks with
`FileNotFoundError`) into exit 1 with a message.

### 4.5 `write_atomic` resets the target's permissions

`tempfile.NamedTemporaryFile` creates at `0600` and `os.replace` carries that onto the config
file: `0644` before a sync, `0600` after. Harmless while only the CLI wrote files; now a server
does it at runtime, and a deploy job running as one user can lock the serving process out of its
own config. Copy the existing target's mode onto the temp file before the rename when the target
exists.

### 4.6 A `.json` registry can never be synced

`Registry` accepts `.toml` and `.json`; `sync_file` refuses anything but `.toml`, so
`broker.sync(...)` on a `.json`-configured broker raises `ValueError: sync target must be a .toml
file` — and under the `sync=` knob that is swallowed into a warning, leaving an installation that
silently never updates. §4.1 already narrows what a file target accepts; state the extension there
too and reject a non-`.toml` file registry at dispatch, so the message names the registry rather
than a target the caller never passed.

## 5. Tests

`tests/test_upstream.py` — the rule table is rewritten around providers:

| scenario | expected |
|---|---|
| lineup carries the provider with a new model | old entry removed, no key involved |
| lineup carries the provider, two old entries on that ref | both removed |
| provider gone, no key | removed |
| provider gone, key present | kept, `kept_refs` names the ref |
| provider gone, key present, name in `dead` | removed, listed in `retired` |
| provider gone, `keys_visible=False` | kept regardless of `present` |
| provider gone, key absent, but the probe resolved nothing at all | kept — `keys_are_visible` is false |
| provider gone, key present, `[[custom]]` uses the same ref | kept; ref not in `orphan_refs` |
| provider gone, no key, ref used by nothing else | removed; ref in `orphan_refs` |
| removal leaves the lineup empty over a non-empty registry | `SyncRefusedError`, registry untouched |
| the same merge repeated three times | identical result, no duplicates, no drift |

Plus: convergence — a kept entry is removed by the next sync once its name enters `dead`; the
report's `__str__` for each new line, including both `keys_visible=False` wordings.

**A convergence test feeds the previous merge's result back in as `current`.** The rule this plan
replaces shipped green with a report that promised a cleanup the next run could not perform,
because the test for it handed the second merge a fresh arrival instead of the state the first
merge produced. Any test whose name says "the next sync" chains the merges.

The carried-over defects of §4 get their own tests, all in `tests/test_upstream.py` unless noted:
a `urlopen` whose `read()` raises (`TimeoutError`, `IncompleteRead`) surfaces as `ValueError`,
exits 1 from the CLI (`tests/test_cli.py`) and does not stop `sync=` from starting the broker
(`tests/test_broker_sync_knob.py`); a `--sync` target whose parent directory is missing exits 1;
a `0644` target is still `0644` after a sync; a `.json` file registry is refused by name;
`have_keys="GEMINI_API_KEY"` declares that one ref rather than its characters.

`tests/test_upstream_evidence.py` (new) — `dead_entries`: permanent failure with no success →
dead; a success anywhere in the tail → alive; 429/503 only → alive; empty journal → alive;
non-queryable store → alive; the candidate set is empty → the store is never queried (a stub that
raises on `calls`).

`tests/test_broker_sync_upstream.py` — end-to-end on sqlite: a retired provider with a key stays
while the journal is clean and goes once the journal holds 401s; the file branch seeds secrets;
a file source into a file registry is refused; a preset into a file registry keeps `[[custom]]`.

`tests/test_pool_health.py` (new) — `PoolSnapshot` is a mapping and carries the counts; `degraded`
at 0/1/2 usable providers; two entries on one ref count as one provider; `missing_keys` carries
help text from `[keys.REF]`; ERROR on the transition into 1 and into 0, one INFO on recovery, and
nothing logged while the pool stays healthy (via `caplog`).

`tests/test_models.py` — report rendering; `tests/test_cli.py` — the CLI reads the sibling
`store/` when it exists and prints the retirement line.

## 6. Specs (same batch as the behavior)

- `architecture.md`, "Syncing the lineup": replace the pairs-and-budget section with the
  provider rule, the death-evidence criterion and the invariant; state that retention is still
  recomputed and never stored; state that a sync never deletes a secret and keeps `[keys.REF]`
  while any entry — `[[custom]]` included — references it. New short section on pool health: the
  measure, why one provider is degraded, where the ERROR is emitted and that `snapshot()` carries
  the same predicate.
- `architecture.md`, the two-tier table: tier 1 is a file registry **on its default env/`.env`
  secrets**. A file registry paired with a Vault/AWS/DB secrets backend is tier 2 and syncs from
  code, because only the broker can see those keys — the reason §1.1's empty-probe clause exists.
  The current text claims what the CLI decides is what the application would have decided; that
  holds only under the qualifier.
- `architecture.md`, the lockfile paragraph: "Start is therefore never online" contradicts the
  `sync=` section two screens below, which goes online at the first `ensure_pool()`. Qualify the
  first — start goes online only when asked to via `sync=`, and never fails when it does.
- `mission.md`: no change to item 2 (the invariant it states remains true); item 5 gains the pool
  as a first-class object of visibility.
- `freetier-providers.md`: the curation rules change — dropping a provider now *does* prune
  downstream for anyone without its key, and prunes for everyone whose journal proves the model
  dead. The "prunes nothing downstream" bullet goes.
- `decisions.md`: one row — provider-unit retirement with journal evidence; runtime cost zero
  outside an explicit sync, one bounded journal read only when a provider was retired.

## 7. Docs (`docs/src/en/` + `docs/src/ru/`)

- `usage.md`: rewrite the "kept entry" paragraph — a model whose provider left the lineup stays
  while you can call it, disappears by itself once it stops working, and the report tells you when
  a key has become unused. New subsection "Watching the pool": the `snapshot()` example from the
  design (counts, `degraded`, `missing_keys` with where to get each key) and the two ERROR lines to
  alert on.
- `usage.md`, the `sync=` paragraph: one sentence that on a file registry the knob rewrites that
  file on disk at the first call — the recipe it shows is `Broker("llms.toml", sync="freetier")`,
  and the file is usually the one under version control.
- `server.md`: the admin-screen and alerting half of the same story for a host with a UI.
- `cli.md`: `--sync` takes a preset name and a file target; a DB target and a file source are both
  refused, each with its one-line reason. One sentence on the key assumption: `--sync` decides
  removals from the environment and the target's sibling `.env`, so a config whose keys live in
  Vault/AWS/a DB is refreshed by `broker.sync("freetier")` from the application instead.
- `index.md`: unchanged.

## Work order

Each batch ends green on `invoke pre` + `python -m pytest` (activate the venv first).

1. §4.1–4.6, the carried-over defects — small, independent, and they stop the file-corruption path
   and the start-time crash before anything else moves.
2. §1, the merge rule and the report, with tests — §1.1's empty-probe clause lands **with** the
   rule that makes absence delete, never in a later batch; `architecture.md` (including its two
   qualifiers) and `freetier-providers.md` in the same batch.
3. §2, death evidence, with tests.
4. §3, `PoolSnapshot` and the degradation ERROR, with tests; `architecture.md` health section.
5. §7, docs en + ru.

Version bump: none (the maintainer does it by hand).

## Verification

```bash
. ./activate.sh
invoke pre
python -m pytest
python -m llmbroker preset freetier --sync /tmp/llms-check.toml   # prints the report, exits 0
```

---

## Handover

All five batches of the work order are implemented. Gate after each batch and at the end:
`invoke pre` clean (ruff, ruff-format, pyrefly, hygiene hooks), `python -m pytest` → **1022
passed**, zero skips, zero errors (Docker up, testcontainers included). The §Verification CLI
command prints the report and exits 0.

### Done, by section

- **§4.1–4.6** all fixed. `render_lineup` is gone with its call site; `AsyncBroker.sync` refuses a
  non-preset source into a `.toml` registry and a non-`.toml` file registry at dispatch (each with
  its own message); `sync_file` parses the rendered text and compares entry names against the merge
  result before `write_atomic`; `present_refs` normalizes a bare `str` in `have_keys`; the file
  branch seeds secrets; `fetch_preset_text` wraps read+decode and converts `OSError`/`HTTPException`
  to `ValueError`; the CLI catches `OSError`; `write_atomic` copies the target's mode.
- **§1** the pairs-and-budget machinery is gone. `_removal_plan` is the provider rule,
  `keys_are_visible` is the empty-probe/scope clause, the report gained `kept_refs`, `retired`,
  `orphan_refs`, `keys_visible`; `unlocking_refs()` and the "set REF" sentence are gone. The sync
  WARNING branch is gone — one `INFO` per run — and both the log line and `last_sync_report` land
  before `_catalog.resync()`.
- **§2** `dead_entries` + `retirement_candidates` in `upstream.py`, wired into both broker branches
  and into the CLI (which opens `FileStore(<target dir>/store)` when that directory exists).
- **§3** `PoolHealth` + `PoolSnapshot` in `models.py`, measured in `Catalog._reconcile`, ERROR/INFO
  on the transition only, `snapshot()` returns `PoolSnapshot` (exported from the package root).
- **§6 specs** and **§7 docs (en + ru)** written in their batches.

### Done differently from the plan, and why

1. **`SyncReport` carries two booleans, not one.** The plan lists `keys_visible: bool` but §1.3/§5
   also require the `kept` line to render *both* `keys_visible=False` wordings ("keys are per user
   here" vs "no key resolved here at all"). One bool cannot discriminate them, and deriving it from
   `active_before` would be fragile. Added `keys_scoped: bool` alongside — two plain facts, each
   meaning one thing, no enum machinery.
2. **`PoolSnapshot` holds a `PoolHealth` and exposes the three fields as properties**, rather than
   duplicating them as dataclass fields. The plan's demand was "one definition, two consumers"; the
   catalog measures once into `PoolHealth`, the alarm and `snapshot()` both read it, and the
   `degraded` predicate exists in exactly one place. The public read surface is identical to the
   plan's (`snap.providers_usable`, `.degraded`, `.missing_keys`).
3. **`_EVIDENCE_LIMIT` is its own constant in `upstream.py`, not a reuse of
   `broker._DEFAULT_STATS_LIMIT`.** Importing it would be circular (`broker.py` imports
   `upstream.py`). Same value (1000), with a comment; they are also genuinely different knobs — the
   stats window is a public default, the evidence window is not.
4. **`_sync_file_target` loads the current lineup and probes keys once more** to compute the
   retirement candidates, so `sync_file` receives a ready `dead` set exactly as the plan specifies.
   That is one extra file read plus one secrets probe per file sync — a deploy-path cost, not a hot
   path. The alternative (threading a probe callback into `sync_file`) buys nothing and complicates
   a pure-ish function.
5. **`FileSyncOutcome` gained `configs`.** §4.3 needs the merged lineup for `seed_secrets`; carrying
   it out beats re-reading the file the sync just wrote.
6. **The kept/retired/unused-key report lines are not hand-wrapped.** The plan's §1.3 sample shows
   6-space continuation lines; wrapping makes substring assertions brittle for no operational gain,
   so each fact is one (long) log line. Content matches the sample.
7. **`_mock_urlopen` in `tests/test_cli.py` had `__exit__ = MagicMock()`**, which returns a truthy
   mock and silently swallowed anything raised inside the `with`. Harmless while the decode happened
   outside it; §4.4 moves the read inside, so the helper now returns `False`. Worth knowing: that
   helper was masking exceptions in every CLI fetch test.

### Decisions taken that the plan did not make

- **`decisions.md` records §8's rejections, not just the one row the plan named.** `CLAUDE.md`
  requires reading "What was dropped" before proposing a mechanism; six weighed-and-rejected ideas
  (`pool = false`, key deletion in the secrets protocol, `exact=True`, two-providers-as-threshold,
  provider probing, an `llm_name` journal filter) belong there or the next plan re-proposes them.
- **`optimizer.md` §Seeding and `architecture.md` §Preset distribution were also corrected** — both
  stated the pairs-and-budget rule in passing. Not named in §6, but they would have been left
  asserting a rule that no longer exists.
- **Wording of the degradation ERROR**: `"pool degraded, no failover left: 1 of N providers
  usable — no key for REF, REF"` and `"pool cannot serve any request: no provider has a key — …"`.
  The plan fixed the semantics, not the strings.
- **`missing_keys` lists only refs no pooled entry can use.** Two entries on one ref where the ref
  resolves means the provider is usable, so nothing is held back — consistent with the provider
  being the unit everywhere else.

### Deliberately left out

- **Nothing from the plan's scope.** §8's rejected items stay rejected (now recorded).
- **`LLMPool.add` still leaves a prior key intact on a `None` key**, so a key *removed* from the
  secrets backend does not deactivate a live slot until the process restarts. Pre-existing,
  documented behavior of the pool, outside this plan; it means the degradation ERROR fires on
  membership changes and on keys appearing, but not on a key being deleted at runtime. Worth a look
  if pool health is meant to be a live signal — flagged, not changed.

### Tests

New: `tests/test_upstream_evidence.py` (13), `tests/test_pool_health.py` (13). The §5 rule table
rewrote `tests/test_upstream.py`'s removal-rule section one test per row, including the convergence
test that feeds the previous merge's own result back in. Tests that documented the replaced rule
were rewritten around the new one rather than deleted: `test_sync_roundtrip.py` (two providers
instead of two entries on one ref), `test_broker_sync_upstream.py` (kept/removed/scoped/log-level),
`test_models.py` (the report's new lines), `test_broker.py` + `test_sync.py` (a file source into a
file registry is now a refusal).

---

## Post-review changes

A review of the implementation above found six things worth changing. All were
fixed; the gate after them is `invoke pre` clean and `python -m pytest` → **1031
passed**, zero skips, zero errors. Two of the six go outside what this plan asked
for, and are called out as such below.

### Defects fixed

1. **The degradation alarm was keyed on the log level, so the 1 → 0 transition
   was silent.** Both degraded states are `ERROR`, so a pool that had already
   reported "no failover left" said nothing at all when its last provider went —
   the outage itself. The catalog now remembers the *state* (0 usable, 1 usable,
   or healthy-whatever-the-count), which also keeps a fourth provider from being
   worth a line. §3.2's own wording ("the catalog remembers the last level it
   reported") is what led there; the three states it lists are the real contract.

2. **`providers_usable` counted keys that no longer resolve — and the pool kept
   routing at them.** `LLMPool.add` treated a `None` key as "leave the prior key
   intact", so a revoked or rotated key stayed live in its slot until the journal
   condemned it, burning real requests on guaranteed 401s. That guard protected no
   caller: `Catalog._reconcile` is the only one, and it always passes a freshly
   resolved value. Removing it fixes routing, `snapshot()[name].has_key` and the
   provider count from one source of truth.

   **Outside this plan's diff** (`broker/pool.py`), and the original handover
   deliberately flagged it as out of scope. Taken anyway: §3.3 publishes the count
   as an admin-facing API, and shipping a number known to be wrong in exactly the
   "a key died" case it exists for is worse than not shipping it. The test that
   documented the old contract was rewritten around the new one, not deleted.

3. **"unused key REF — revoke it at the provider" fired for keys that never
   existed.** `orphan_refs` did not consider whether a key was actually there, so
   the commonest curated change of all — a provider dropped that this installation
   had no key for — told every admin to go revoke something they never had. Now
   filtered on `present`. §1.5's case (a retirement whose key is still here) is
   unchanged, and `[[custom]]` still keeps a paid model's key out of the advice.

4. **A retirement did not show its evidence.** §1.3's sample line reads `401 since
   2026-07-02, no successful call since`; the implementation rendered only "the
   journal holds only permanent failures", which an admin cannot check without
   querying the journal themselves — for the one action that deletes an entry from
   their own version-controlled config. `dead_entries` now returns the evidence it
   already had in hand, `SyncReport.retired` is a tuple of `Retirement`
   (name/http_status/since, exported from the package root alongside `PendingKey`),
   and the rendered line matches the plan's sample. `since` is the oldest failure
   in the window, so "since" means what it says.

5. **A scoped installation could never retire anything.** §2.1 defines a candidate
   as "key present", but §2.2 promises that under per-user keys the rule reads as
   "nobody could call it — exactly the evidence wanted there". Both cannot hold:
   with `scope` set and no `have_keys`, `present` is empty, so there were no
   candidates and the journal was never read. `retirement_candidates` now takes
   `keys_visible` and, where a missing key proves nothing, treats every dropped
   entry as a candidate.

   **This contradicts §2.1 as written**, deliberately: journal evidence ("someone
   called it and got a 401, and nobody ever succeeded") is strictly stronger than
   key absence, so it is safe exactly where key absence is not. Without it the
   per-user mode — a first-class mode per `mission.md` item 4 — gets no benefit
   from this whole plan. The correct definition of a candidate is "an entry the
   removal rule would otherwise keep", which is what the code now computes.

6. **The key table was re-read on every reconcile.** `registry.key_info()` ran on
   each debounced resync purely to have help text ready for missing keys that
   usually do not exist. It is now read only when a pooled ref actually has no key,
   so a healthy pool adds no registry I/O at all. `decisions.md`'s cost row was
   updated to match, and gained a row for the merge report.

### Reviewed and deliberately not changed

- **Managed entries with no `api_key_ref` are removed unconditionally.** Checked
  `LLMPool.acquire`: it filters on `slot.key is not None`, so such an entry can
  never be routed at under any circumstances. "Never shrinking what the pool can
  call" is not violated, and §1.2 allows for it explicitly.
- **A newly created config file gets `0600`.** §4.5 fixed the existing-target
  case; the create path is pre-existing behavior, is covered by a test that asserts
  no mode is read, and no mission requirement bears on it. Changing it would be
  picking a permissions policy without a reason to.

### Specs and docs

`architecture.md` gained the candidate rule, the retirement-evidence requirement,
the state-not-severity wording for the alarm, the paragraph on what the measure
is, and the narrowed orphan advice. `decisions.md` cost rows updated. `usage.md`
and `server.md` (en + ru) carry the new `retired:` sample, the narrowed unused-key
wording, and the corrected alerting contract.

---

## Second review round

Three fixes; the gate after them is `invoke pre` clean and `python -m pytest` →
**1034 passed**, zero skips, zero errors.

1. **A registry that pools nothing raised `ERROR pool cannot serve any request: no
   provider has a key` at every provision.** `pool = false` entries are a
   supported shape — reachable through `direct`, never pool members — so a
   registry made only of them measures `0` of `0` and fell into the zero-usable
   branch, naming a cause that was not the case: the key was right there. An empty
   pool is now its own state in the alarm's state machine — silent, and silent on
   the way out too, since losing the last pooled entry is a membership change and
   not a repair to announce. `PoolHealth.degraded` is false there for the same
   reason, so the log and `snapshot()` still read one measurement.

   Worth knowing for `fileless-broker.md`: deleting `pooled` does not remove this
   case, it renames it — a registry of only `[[custom]]` entries lands in exactly
   the same place, and `Broker(direct=[...])` with no curated lineup is a shape
   that plan actively builds toward.

2. **A retirement showed the oldest status in the window, not the current one.**
   `dead_entries` kept one row per name and let the newest-first scan overwrite it
   to the end, so a provider answering `401` today after a `404` months ago was
   reported as `404 since <then>`. The date was right — that is where the run
   starts — but the code an admin is sent to go and check has to be the one they
   will see. Status now comes from the newest permanent failure, `since` still
   from the oldest.

3. **`architecture.md` claimed the counts and the routing decision "always
   agree".** They do not: `LLMPool.acquire` also excludes administratively
   disabled slots, so a pool disabled down to nothing reports itself healthy. The
   measure is plan-conformant (§3.1 defines it on key presence) — the sentence was
   not, and with `preset-autorefresh.md` making refresh unattended this alarm
   becomes the signal that a sync broke something. The paragraph now says what the
   measure is (key presence, never lagging behind the keys) and states the
   disabled case outright; `usage.md` and `server.md` (en + ru) carry the same
   correction.

   The behavioral question — whether a disabled entry should keep counting its
   provider — is deliberately **not** settled here. It changes the meaning of a
   published `snapshot()` field, and `fileless-broker.md` §3 already reopens the
   measure's definition; a line there asks that plan to settle it once.
