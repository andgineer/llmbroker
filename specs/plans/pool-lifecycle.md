# Pool lifecycle: provider retirement, death evidence, degradation visibility

Follows `preset-sync.md`, which is implemented in the working tree but not merged. This plan
replaces that plan's removal rule and part of its report, and closes what it left open: what
happens to a provider the curated lineup retires while the installation still holds its key, and
how an installation notices that its pool has degraded. **The two ship in one release** — a
reviewer reads them together.

## What this replaces

| `preset-sync.md` | replaced by |
|---|---|
| §3.2 rule 3, "pairs, then budget" — arrivals pay for removals | §1: the provider is the unit; no budget, no ordering |
| §2, the `kept` sentence "set REF and the next sync removes it" | §1.4: unachievable as written, and verified false — see below |
| §4 step 7, WARNING when an entry is kept | §3: a kept entry asks nothing of an admin; a degraded pool does |
| §3.3, the file writer fed any source text | §4.1: a `.toml` target is synced from a curated preset only |

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
    keys_visible: bool = True,            # a failed probe is evidence here
) -> tuple[list[LLMConfig], dict[str, KeyInfo], SyncReport]
```

```python
def keys_are_visible(*, scope: str | None, have_keys: bool | Sequence[str]) -> bool:
    """Per-user keys the broker cannot probe make absence meaningless."""
    return scope is None or bool(have_keys)
```

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

With `keys_visible=False` the `kept` line reads "…keys are per user here, so no probe can prove
one missing" instead.

### 1.4 Logging

`AsyncBroker.sync` logs the report at `INFO`, always. The WARNING branch goes: nothing in a sync
outcome is admin-actionable any more — degradation is, and §3 owns it. The alias-refresh WARNING
for an alias the catalog no longer knows stays as it is.

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

## 4. Defects carried over from the review of `preset-sync.md`

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
| provider gone, key present, `[[custom]]` uses the same ref | kept; ref not in `orphan_refs` |
| provider gone, no key, ref used by nothing else | removed; ref in `orphan_refs` |
| removal leaves the lineup empty over a non-empty registry | `SyncRefusedError`, registry untouched |
| the same merge repeated three times | identical result, no duplicates, no drift |

Plus: convergence — a kept entry is removed by the next sync once its name enters `dead`; the
report's `__str__` for each new line, including the per-user wording.

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
- `server.md`: the admin-screen and alerting half of the same story for a host with a UI.
- `cli.md`: `--sync` takes a preset name and a file target; a DB target and a file source are both
  refused, each with its one-line reason.
- `index.md`: unchanged.

## Work order

Each batch ends green on `invoke pre` + `python -m pytest` (activate the venv first).

1. §4.1–4.3, the carried-over defects — small, independent, and they stop the file-corruption path
   before anything else moves.
2. §1, the merge rule and the report, with tests; `architecture.md` and `freetier-providers.md` in
   the same batch.
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
