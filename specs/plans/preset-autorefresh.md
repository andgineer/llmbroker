# Preset auto-refresh: `sync=` becomes the zero-admin path, the CLI becomes the review path

**Depends on `pool-lifecycle.md` and ships after it.** Three of its results are load-bearing here:
invariant 4 (a sync never takes away a model this installation can call, except by same-provider
replacement or journal-proven death) is what makes unattended application safe at all; §1.4 removes
the admin-facing WARNING from a sync outcome, which is what lets a refresh run daily without
nagging; §3 moves the genuinely alarming state (a degraded pool) to its own ERROR, so the report no
longer has to be the alarm. Do not start this plan before #1 is merged — its §1 changes the removal
rule this one automates.

Line anchors below are pre-#1 numbers; #1 moves them.

## The problem

`AsyncBroker(..., sync="freetier")` already refreshes from the catalog without a release, so the
CLI is not, and never was, a *requirement*. What is wrong is the shape of that refresh and the
position the spec takes on it.

**The spec contradicts itself.** `architecture.md` (the lockfile paragraph): *"Refreshing it from
upstream is an explicit repo or deploy action, like a lockfile upgrade — never something
application start does. Start is therefore never online."* Two screens below, "The `sync=` knob"
describes start going online. #1 §6 already schedules a fix for this, as a qualifier on the first
paragraph. A qualifier is the wrong fix: it freezes "the deploy action is the real path, the knob
is a shortcut", and this plan inverts that.

**The knob refreshes once per process, and that is the wrong clock.** `_sync_on_start`
(`broker.py:250`) is guarded by `_sync_attempted`, so:

- a server that runs for three weeks never sees a preset change for three weeks — the one case
  where automatic refresh has real value (a `model` id retired upstream, a `base_url` moved) is the
  case it does not cover;
- a short-lived process — a script, a worker-per-task, a lambda — pays a network round trip on
  *every* invocation for a file that changes a few times a year;
- a cluster rolling 40 pods pays 40 fetches inside one deploy window, against an unauthenticated
  `raw.githubusercontent.com`, and 40 concurrent rewrites of one `llms.toml`.

**It sits on the critical path.** The refresh runs inside the provision lock *before*
`_catalog.provision()`, with a 10 s fetch timeout, so the first call of a fresh process waits on
the network. It is best-effort and never raises, so this is latency, not fragility — but it is
latency for no reason in every case except onboarding.

**What the fetch actually costs, measured.** `presets/freetier.toml` is 1685 bytes;
`raw.githubusercontent.com` answers with `etag` and `cache-control: max-age=300`; a full GET is
~170 ms, dominated by connection setup. **The bytes are free; the round trip is the whole cost.**
That single measurement decides the design below: gating on *time* is worth everything, and
conditional GET is worth almost nothing.

## Design summary

1. **`sync=` gains an interval and keeps checking while the process runs.** `sync_interval`
   defaults to 24 h. `0` restores today's exactly-once-at-start behavior.
2. **Two independent gates.** The *time gate* decides whether to go to the network at all. The
   *identity gate* decides whether a fetched lineup changes anything — if the merged result equals
   what is already there, nothing is written, nothing is applied, nothing is resynced, and the log
   stays at DEBUG.
3. **No `If-None-Match`.** It would save 1.7 KB and zero round trips, while the identity gate is
   strictly stronger: it also catches "the preset changed in a way that does not change *our*
   merged result" (a provider we have no key for, an entry we already carry). One mechanism, not
   two, and it needs no stored validator.
4. **The check is lazy on activity, never a timer.** A monotonic-clock comparison at the top of
   `ensure_pool()`, exactly the `_LearningHook.maybe_rebuild` / `_REBUILD_TTL` idiom
   (`learning.py:28`). An idle broker performs no I/O and schedules no wakeups — mission item 8 —
   and the library still needs no running service of its own (positioning item 1).
5. **Off the critical path, except onboarding.** An empty registry still syncs before provisioning,
   blocking: there is nothing to provision from, and `provision()` on an empty registry raises. A
   non-empty registry provisions from what it already has, and the refresh runs in a background
   task afterwards.
6. **The stamp is per node.** A wall-clock timestamp in a sidecar next to a file registry; in
   memory otherwise. No new protocol, no backend touched.
7. **The refresh never raises and never blocks a request.** Same swallow set as `_sync_on_start`,
   plus task cancellation on `aclose()`.
8. Rejected during design:
   - **A background timer task** — a coroutine sleeping on an interval keeps an idle process awake
     and has to be owned, cancelled, and tested against every embedding (thread-loop `Broker`,
     someone else's loop, a forked worker). The lazy check gives the same freshness to any process
     that is actually being used, and a process making no calls has no lineup to keep fresh.
   - **A `SyncStampProtocol` on the store** (`get_sync_stamp`/`set_sync_stamp`) so a cluster shares
     one check — it touches sqlite, postgres, mongodb, the file store and the conformance suite,
     for the same reason #1 rejected an `llm_name` filter on `calls`. It is also unnecessary: see
     §3.2, per-node checking costs N small GETs *per day*, not per start, and concurrent nodes
     converge because the merge is idempotent (#1 §5 tests exactly that).
   - **A journal row as the stamp** — `architecture.md` states a sync is a registry operation and
     never writes the journal. Keep it that way.
   - **Pinning `_PRESET_URL` to the installed version's tag** instead of `main` — reproducible and
     it closes §6's exposure, but it also means a preset fix reaches nobody until a release of
     *llmbroker*, which is the exact problem this plan exists to remove. Named as an accepted
     trade-off in §6 instead.
   - **Auto-refresh inside the CLI** (`preset --sync` skipping the fetch when a stamp is fresh) —
     an explicitly typed command must always do the thing. It *writes* the stamp, so an app and a
     CLI run on one host share the clock, but it never reads it as a gate.
   - **A `--check`/dry-run mode** printing what a refresh would do — `preset <name>` already prints
     the lineup and `--sync` already prints a report; a third form earns nothing.

## 1. The interval and the check — `broker/broker.py`

### 1.1 Constructor

```python
_DEFAULT_SYNC_INTERVAL = 86_400.0  # seconds
```

`AsyncBroker.__init__` (`broker.py:157`) gains `sync_interval: float = _DEFAULT_SYNC_INTERVAL`,
after `sync`. `sync_interval < 0` raises `ValueError` next to the existing `scope == ""` check;
`0` means "check once at start, never again" (today's behavior). The value is inert when `sync` is
`None` — a broker with no sync source never checks, and passing an interval without a source is
not an error, just unused.

`sync.Broker.__init__` (`sync.py:104`) takes and forwards the same argument — the wrapper mirrors
the async signature and nothing else.

The class docstring at `broker.py:153` ("refreshes the lineup once, just before the pool is first
provisioned") is now wrong and is rewritten with the two-gate story in one sentence.

### 1.2 State

```python
self._sync_interval = sync_interval
self._next_refresh = float("inf")   # monotonic deadline; inf until the first check lands
self._refresh_task: asyncio.Task[None] | None = None
```

`_sync_attempted` stays: it is what keeps a *retried failed provision* from re-fetching, and it
still marks the start-time attempt specifically.

### 1.3 The check

`ensure_pool` (`broker.py:233`) gets the scheduling call on its fast path, which is the funnel every
public operation already goes through:

```python
async def ensure_pool(self) -> None:
    if self._provisioned:
        self._maybe_schedule_refresh()
        return
    async with self._provision_lock:
        ...
```

```python
def _maybe_schedule_refresh(self) -> None:
    """Fire a background refresh when the interval has elapsed. Synchronous by
    design: the hot path pays one monotonic comparison and nothing else."""
    if time.monotonic() < self._next_refresh:
        return
    if self._refresh_task is not None and not self._refresh_task.done():
        return
    self._next_refresh = time.monotonic() + self._sync_interval
    self._refresh_task = asyncio.create_task(self._attempt_sync("refresh"))
```

The deadline moves **before** the task is created, so a burst of concurrent `ask()`s schedules one
refresh, not one per call. `_next_refresh` stays `inf` when `_sync_source is None` or
`_sync_interval == 0`, which is what makes both no-ops free.

### 1.4 One attempt function

`_sync_on_start` and the refresh differ only in what they log, so they collapse into:

```python
async def _attempt_sync(self, reason: str) -> None:
    """Best-effort by construction: a refresh that cannot be fetched or cannot be
    applied logs and leaves the running configuration alone. A process must not
    fail to start, and a request must not fail, over a lineup refresh."""
    try:
        await self.sync(self._sync_source)
    except SyncRefusedError as exc:
        self.last_sync_report = exc.report
        logger.warning("sync %s refused (%s), continuing on the current config: %s", ...)
    except (ValueError, OSError) as exc:
        logger.warning("sync %s failed (%s), continuing on the current config: %s", ...)
```

`OSError` in the swallow set already covers the read-phase failures #1 §4.4 converts, and is now
also what keeps a background refresh from surfacing a write failure as an unretrieved task
exception.

### 1.5 Shutdown

`aclose()` (`broker.py:344`) cancels an in-flight refresh before closing the ports it uses:

```python
if self._refresh_task is not None and not self._refresh_task.done():
    self._refresh_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await self._refresh_task
```

Without this, a refresh in flight during shutdown writes through a registry whose driver is being
closed. The cancel goes first in `aclose`, before the `for port in (...)` loop.

## 2. The identity gate

A refresh that changes nothing must be indistinguishable from no refresh at all: no file write, no
`catalog.apply`, no resync, no seeding, no INFO line. Otherwise a daily refresh rewrites a
version-controlled `llms.toml` (mtime churn, and with it a spurious deploy diff on any host that
checksums its config) and logs a report nobody needs to read.

### 2.1 File target — `broker/upstream.py`

`sync_file` (`upstream.py:529`) already builds the full text before writing. The gate goes next to
the parse-verification #1 §4.1 adds:

```python
text = render_merged_toml(new_text, kept, custom_entries, tail)
changed = text != _current_text(target)      # "" when the target does not exist
if changed:
    _verify_entry_names(text, merged)        # #1 §4.1
    write_atomic(target, text)
```

`FileSyncOutcome` gains `changed: bool`. Comparison is on the exact bytes we would have written, so
a preset whose only change is a comment *does* count as changed — correct: the file's git history
is the update record and a comment change is part of it.

### 2.2 Registry target — `broker/broker.py`

`_sync_registry_target` (`broker.py:321`) compares the merged lineup with what it loaded:

```python
changed = merged != current
if changed:
    await self._catalog.apply(merged)
```

`LLMConfig` is a frozen dataclass, so list equality is the whole comparison, order included (the
merge preserves order, and a reordering is a change worth applying).

### 2.3 What an unchanged sync skips

In `sync()` (`broker.py:277`), `seed_disabled`, the secret seeding, and `_catalog.resync()` all
move behind `changed`. `last_sync_report` and the stamp are written either way — the report is
still the answer to "what does my lineup look like right now", and a caller polling
`last_sync_report` must see a fresh one.

Consequence worth stating in the spec: **a new key appearing in the environment is no longer picked
up by an unattended refresh** — seeding runs only when the lineup itself changed. It is picked up
by a restart or an explicit `sync()`, both of which are what a key arriving implies anyway (a new
key is an admin act, and the admin is right there). This is a deliberate narrowing of #1 §4.3, not
a regression of it: the parity that fix established — the file branch seeds like the registry
branch — still holds on every sync that changes something.

## 3. The stamp

### 3.1 What it is and where it lives

One JSON object keyed by sync source, holding a wall-clock timestamp:

```json
{"freetier": {"checked_at": 1785642486.0}}
```

Wall clock, not monotonic: it must survive process exit, which is the only reason it exists.

Location, decided by the registry:

- **file registry** (`Registry`, the tier-1 case): `<config dir>/.llmbroker-sync.json`. The
  directory is writable by definition — we rewrite the config in it.
- **anything else**: no file. A DB-backed installation is a long-lived server whose in-process
  clock already covers it.

That split is the whole rule: the file-registry case is the one that may be a short-lived script
invoked in a loop, and it is also the one with a guaranteed writable directory.

### 3.2 Why per node is enough

The stamp is deliberately not shared across a cluster. With a 24 h interval, N nodes cost N GETs of
1.7 KB per day — against the fleet's own LLM traffic that is not measurable. What the shared stamp
would have bought is avoiding concurrent *application* of the same update, and that is already
safe: the merge is deterministic and idempotent given the same current state (#1 §5's "the same
merge repeated three times" and the convergence test), `write_atomic` makes the file write
last-writer-wins rather than corrupting, and #1 §4.5 keeps the mode. Nodes converge on the same
lineup whichever order they arrive in.

### 3.3 Reading and writing

- Read once, at `_sync_on_start`. `age = time.time() - checked_at`; the stamp gates only when
  `0 <= age < interval` — a timestamp in the future (clock moved back, a stamp copied between
  hosts) is treated as no stamp at all rather than as a lock that never expires.
- When it gates, the start-time fetch is skipped **and the remainder carries into the in-process
  clock**: `self._next_refresh = time.monotonic() + (interval - age)`, so a process that starts 23 h
  into the window checks an hour later rather than a day later.
- Written after every completed check — changed or not, since it records "we looked", not "we
  applied". Written by the CLI's `--sync` too (§5), never read by it.
- Every failure to read or write it (missing, unparsable, unwritable directory, a race with another
  process) is caught, logged at DEBUG, and degrades to in-process-only gating. The stamp is an
  optimization; nothing may fail because of it.
- `.llmbroker-sync.json` is node-local state and belongs in `.gitignore` — stated in the docs
  (§8), and added to this repo's own `.gitignore` since the test suite produces it.

## 4. Placement relative to provisioning

`_sync_on_start` keeps its name and its position inside the provision lock, but decides between
three outcomes instead of always fetching:

```
registry is empty                    -> await the sync here, blocking (onboarding; provision()
                                        on an empty registry raises, so there is no alternative)
stamp is fresh                       -> no check; seed _next_refresh from the remainder
otherwise                            -> provision first, then schedule the refresh in background
```

Emptiness is read with `await self._registry.load()` — one registry read that `provision()` would
have done anyway a moment later.

The background schedule is armed by setting `self._next_refresh = 0.0` (monotonic time is never
negative, so the next `ensure_pool()` fires immediately) rather than by creating the task inside the
lock: the task calls `sync()`, `sync()` touches the catalog, and the catalog is mid-provision.
`ensure_pool()` reaches its fast path immediately after the lock releases, on the very same call.

`architecture.md`'s existing justification for the pre-provision placement stays true where it still
applies, and is rewritten to name the empty-registry case as the reason rather than as an aside.

## 5. Logging, report, CLI

- **Changed:** the report at INFO, as #1 §1.4 leaves it.
- **Unchanged:** DEBUG, one line — `sync freetier: no change`. A daily INFO saying nothing is how a
  log becomes unreadable, and #1 §3 already owns the state that must be loud.
- **Failed:** WARNING from `_attempt_sync`, naming the reason (`start` / `refresh`) so a log reader
  can tell a failed onboarding from a failed daily check.
- `last_sync_report` is set on every outcome including no-change, so a host's admin screen shows
  the current lineup verdict rather than the last one that happened to differ.
- The CLI is unchanged in behavior: `preset <name> --sync <file>` always fetches and always prints
  the report, no-op included (`architecture.md`'s "printed on every run *including no-ops*" is
  about the CLI and stays literally true). It gains only the stamp write, through the same helper
  the broker uses.

## 6. What a fetched preset is allowed to do

Making auto-refresh the default makes the catalog's `main` branch live configuration for every
installation. The exposure is small but real and must be written down rather than discovered:
a preset carries no code, entry names are immutable (`architecture.md`, model identity), and #1's
invariant 4 bounds what a merge can take away — but `base_url` decides **where the installation's
API keys are sent**.

- **Hygiene, in this plan:** a config built from a *fetched preset* must have an `https://`
  `base_url`; anything else fails the fetch with the same `ValueError` shape as invalid TOML.
  Validated in `fetch_preset_text` (`upstream.py:80`), after the `tomllib.loads` check, so the
  whole file is rejected before any merge sees it. This does not defend against a compromised
  catalog — that catalog would serve `https://` too — and the plan must not pretend otherwise. It
  removes plaintext key transmission as an accident, and it is two lines.
- **The real control is the interval:** `sync_interval=0` plus a vendored, reviewed `llms.toml` is
  the lockfile discipline, still available, now an explicit opt-out instead of the default. Docs
  name it for installations that need the pinned path (§9, `server.md`).
- **Recorded, not fixed:** pinning to a release tag is the alternative that would close this, and
  it is rejected in the design summary. `decisions.md` carries the trade-off with both halves.

## 7. Tests

`tests/test_preset_autorefresh.py` (new) — the gates and the scheduling. `fetch_preset_text` is
monkeypatched throughout, as `tests/test_broker_sync_knob.py` already does; a stub that *raises on
call* is how "no fetch happened" is asserted.

| scenario | expected |
|---|---|
| interval not elapsed, many calls | fetch stub never called; one monotonic comparison per call is the whole cost |
| interval elapsed | exactly one refresh task, and only one for a burst of concurrent `ask()`s |
| refresh with an unchanged preset | file mtime unchanged, `catalog.apply` not called, no INFO record, `last_sync_report` refreshed |
| refresh with a changed preset | written, applied, resynced, INFO record, new entry routable without a restart |
| fetch raises during a refresh | WARNING, broker keeps serving the old lineup, no task exception surfaces |
| `sync_interval=0` | today's behavior exactly — one check at start, none after |
| `sync_interval=-1` | `ValueError` at construction |
| `aclose()` with a refresh in flight | task cancelled, no "Task exception was never retrieved", no write through a closed registry |
| empty registry | still blocking before provisioning — the existing `test_broker_sync_knob.py` cases stay green unchanged |

Stamp cases, same file, file-registry brokers on `tmp_path`:

| scenario | expected |
|---|---|
| second broker in the same directory, stamp fresh | no fetch at start |
| stamp older than the interval | fetch at start |
| stamp `checked_at` in the future | treated as absent, fetch happens |
| stamp unparsable / directory read-only | DEBUG, refresh proceeds, nothing raises |
| stamp holds another preset's key | that key is not consulted; each source gates independently |
| a gating stamp 90% through the window | `_next_refresh` lands at the remainder, not a full interval — asserted through the monotonic clock, not by sleeping |

Time is controlled by monkeypatching `time.monotonic`/`time.time` in the broker module and by
small interval values. **No test sleeps.**

`tests/test_upstream.py` — a fetched preset with an `http://` `base_url` raises `ValueError` and
nothing is written; an `https://` one passes. (A *file* source is unaffected: the user's own config
is not the catalog.)

`tests/test_broker_sync_knob.py` — `sync_interval` reaches `AsyncBroker` through `sync.Broker`.

`tests/test_sync_roundtrip.py` — an unchanged sync leaves the file byte-identical, including its
comments and `[[custom]]` blocks.

## 8. Specs (same batch as the behavior)

- `architecture.md`, the lockfile paragraph: **rewritten, not qualified.** The current text
  ("never something application start does", "start is therefore never online") is replaced by the
  current rule: a broker with `sync=` keeps its lineup current on an interval, checking lazily on
  activity; a vendored config reviewed in a pull request is the explicit opt-out
  (`sync_interval=0`), and it remains the right choice where every change must be reviewable. The
  cluster flip-flop sentence stays, re-aimed: what makes concurrent nodes safe is the merge's
  idempotence plus the identity gate, not the absence of node-level refresh.
- `architecture.md`, "The `sync=` knob" → "Keeping the lineup current": the two gates, why the
  check is lazy rather than timed, the empty-registry exception to the off-critical-path rule, and
  that a refresh never raises. State the seeding narrowing from §2.3 explicitly.
- `architecture.md`, the two-tier table: tier 1's "who merges" is the *application*, with the CLI
  as the reviewable path; #1 §6 already rewrites this table's key-visibility qualifier, so this is
  an edit on top of that batch, not a competing one.
- `decisions.md`: two rows — interval-gated auto-refresh with an identity gate (cost: one monotonic
  comparison per call, one small GET per node per day, zero writes when nothing changed); and the
  `main`-as-live-config trade-off with the rejected tag-pinning alternative.
- `mission.md`, item 2: "mirrors itself into an installation via `sync(name)`" becomes true without
  qualification — the preset now reaches a running installation without any admin act, and the one
  irreducible act stays what it was, obtaining a key.

## 9. Docs (`docs/src/en/` + `docs/src/ru/`)

- `usage.md`: the `sync=` paragraph gains the interval — what it defaults to, that an unchanged
  preset touches nothing on disk, and that `sync_interval=0` restores check-once-at-start. This
  replaces the one-sentence runtime-rewrite warning #1 §7 adds, which the identity gate has made
  much narrower: the file is rewritten only when upstream genuinely moved.
- `usage.md`: one line on `.llmbroker-sync.json` — what it is, that it is node-local, and that it
  belongs in `.gitignore`.
- `server.md`: the two deployment stances side by side — auto-refresh (default; a preset fix
  reaches production without a release) versus pinned (`sync_interval=0` + vendored config; every
  change arrives through a reviewed pull request), with §6 as the reason a host might choose the
  second.
- `cli.md`: `--sync` is the reviewable path, not the only path — it always fetches, always prints
  the report, and it shares the stamp with the application on the same host.
- `async.md`: the existing `sync="freetier"` example gets one sentence, no new example.

## Work order

Each batch ends green on `invoke pre` + `python -m pytest` (`. ./activate.sh` first).

1. §2, the identity gate, with tests. Independent of everything else and valuable on its own — it
   is what stops today's once-per-start sync from rewriting an unchanged config.
2. §1 + §4, the interval, the lazy check, the placement, the shutdown cancel, with tests;
   `architecture.md`'s rewritten lockfile paragraph and knob section in the same batch — the spec
   must not be left asserting "start is never online" once this lands.
3. §3, the stamp, with tests; `.gitignore`.
4. §6 hygiene + `decisions.md`; §9 docs en + ru.

Version bump: none (the maintainer does it by hand).

## Verification

```bash
. ./activate.sh
invoke pre
python -m pytest
python -m llmbroker preset freetier --sync /tmp/llms-check.toml   # report, exit 0, stamp written
python -m llmbroker preset freetier --sync /tmp/llms-check.toml   # byte-identical file, still exit 0
```
