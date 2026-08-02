# Preset auto-refresh: the curated lineup keeps itself current, unconditionally

**Depends on `pool-lifecycle.md` (#1) and ships after it.** Three of its results are load-bearing:
invariant 4 (a sync never takes away a model this installation can call, except by same-provider
replacement or journal-proven death) is the entire reason unattended application is safe; §1.4
removes the admin-facing WARNING from a sync outcome, which is what lets a refresh run daily
without nagging; §3 moves the genuinely alarming state (a degraded pool) to its own ERROR, so the
report no longer has to be the alarm. Do not start before #1 is merged — its §1 changes the removal
rule this plan automates.

Line anchors are pre-#1 numbers; #1 moves them.

## The problem

**A pinned free-tier lineup is a decaying one.** The pool is a list of free endpoints that providers
retire without notice — that is the premise of the whole library. An installation that stops
refreshing does not stay stable, it slowly loses models until the pool cannot serve a request, with
no signal until the first outage. Refreshing is therefore not a convenience: **it is what keeps the
product working**, and it cannot be something the user has to know about.

Today it is. `AsyncBroker(..., sync="freetier")` is opt-in, and even when passed:

- it refreshes **once per process** (`_sync_attempted`, `broker.py:250`), so a server that runs for
  three weeks never sees a preset change for three weeks — precisely the case where an automatic
  refresh has value (a retired `model` id, a moved `base_url`);
- a short-lived process — a script, a worker-per-task — pays a network round trip on *every*
  invocation for a file that changes a few times a year;
- a rolling deploy of 40 pods pays 40 fetches inside one window, against an unauthenticated
  `raw.githubusercontent.com`, and 40 concurrent rewrites of one `llms.toml`;
- it sits **on the critical path**: inside the provision lock, before `_catalog.provision()`, with
  a 10 s fetch timeout, so the first call of a fresh process waits on the network.

**The spec contradicts itself about all of this.** `architecture.md` (the lockfile paragraph):
*"Refreshing it from upstream is an explicit repo or deploy action, like a lockfile upgrade — never
something application start does. Start is therefore never online."* Two screens below, the `sync=`
section describes start going online. #1 §6 schedules a qualifier on the first paragraph; a
qualifier is the wrong fix, because it preserves the position this plan inverts.

**What the fetch actually costs, measured.** `presets/freetier.toml` is 1685 bytes;
`raw.githubusercontent.com` answers with `etag` and `cache-control: max-age=300`; a full GET is
~170 ms, dominated by connection setup. **The bytes are free; the round trip is the whole cost.**
That measurement decides the design: gating on *time* is worth everything, and conditional GET is
worth almost nothing.

## A spec clause this plan corrects

`decisions.md` records "there is no implicit seed-on-start", justified by a cluster flip-flop: N
nodes, N **local copies** of a TOML, each coercing the shared registry to its own. The rule as
written is wider than the reason that justifies it — the reason is about divergent local copies,
the rule bans every implicit refresh. §8 narrows the clause to what it proves; the narrowed form
still holds after this plan, and no exception to it is created anywhere.

## Design summary

1. **The refresh is unconditional — there is no `sync=` opt-in and no off switch.** A broker whose
   lineup came from the curated preset keeps it current on a 24 h clock. The knob that survives is
   `sync_interval`, a cadence, not a stance.
2. **Two independent gates.** The *time gate* decides whether to go to the network at all. The
   *identity gate* decides whether a fetched lineup changes anything — if the merged result equals
   what is already there, nothing is written, nothing is applied, nothing is resynced, and the log
   stays at DEBUG.
3. **No `If-None-Match`.** It would save 1.7 KB and zero round trips, while the identity gate is
   strictly stronger: it also catches "the preset moved in a way that does not change *our* merged
   result". One mechanism, not two, and it stores no validator.
4. **The check is lazy on activity, never a timer.** A monotonic comparison at the top of
   `ensure_pool()`, exactly the `_LearningHook.maybe_rebuild` / `_REBUILD_TTL` idiom
   (`learning.py:28`). An idle broker performs no I/O and schedules no wakeups — mission item 8 —
   and the library still needs no running service of its own (positioning item 1).
5. **Off the critical path, except onboarding.** An empty registry still syncs before provisioning,
   blocking: there is nothing to provision from, and `provision()` on an empty registry raises. A
   non-empty registry provisions from what it has, and the refresh runs in a background task after.
6. **One home directory** holds everything llmbroker caches or remembers outside the user's own
   config: the fetched preset text, the paid catalog, and the check stamps. Machine-scoped by
   default, overridable per broker.
7. **The refresh never raises and never blocks a request.** Failure logs and leaves the running
   configuration alone, including at start.
8. Rejected during design:
   - **An off switch for the refresh** (`sync=False`, `sync_interval=0` as a documented stance) —
     it does not buy control, it buys a pool that decays to nothing. What it appears to protect
     against (an unreviewed lineup change) is already bounded by #1's invariant 4, which is what
     this plan rests on; and an installation that genuinely wants a lineup of its own declares one
     instead of pinning ours. `sync_interval` keeps accepting small values because tests need them.
   - **A background timer task** — a coroutine sleeping on an interval keeps an idle process awake
     and must be owned, cancelled and tested against every embedding (thread-loop `Broker`,
     someone else's loop, a forked worker). A process making no calls has no lineup to keep fresh.
   - **A store protocol for the stamp** (`get_sync_stamp`/`set_sync_stamp`) so a cluster shares one
     check — it touches sqlite, postgres, mongodb, the file store and the conformance suite, for
     the reason #1 rejected an `llm_name` filter on `calls`. Unnecessary: see §3.3.
   - **A journal row as the stamp** — `architecture.md` states a sync is a registry operation and
     never writes the journal. Keep it.
   - **Pinning `_PRESET_URL` to the installed version's tag** instead of `main` — reproducible, and
     it would close §6's exposure, but a preset fix would then reach nobody until a release of
     llmbroker, which is the problem this plan exists to remove. Accepted as a trade-off in §6.
   - **Auto-refresh inside the CLI** (`preset --sync` skipping a fetch when the stamp is fresh) — an
     explicitly typed command must always do the thing. It *writes* the stamp so an app and a CLI
     run on one host share a clock, but never reads it as a gate.

## 1. The home directory — `llmbroker/home.py` (new)

Everything llmbroker caches or remembers on its own lives in one directory. It exists because three
separate things need a place and inventing three conventions would be three bugs.

```python
def home_dir(override: str | Path | None = None) -> Path | None:
    """Where llmbroker keeps what it caches on its own. ``None`` when nowhere is
    writable — every caller degrades to memory rather than failing."""
```

Resolution order, first writable wins:

1. `override` (the broker's `home=` argument, §1.2);
2. `$LLMBROKER_HOME`;
3. `$XDG_CACHE_HOME/llmbroker` on POSIX, `~/Library/Caches/llmbroker` on macOS,
   `%LOCALAPPDATA%\llmbroker` on Windows, `~/.cache/llmbroker` as the POSIX fallback;
4. a per-user subdirectory of `tempfile.gettempdir()`;
5. `None`.

**No step may raise.** A container running as `nobody` with no `$HOME`, a read-only filesystem, a CI
sandbox — each falls through to the next, and a `None` home means the cache lives in process memory
for that run. Nothing llmbroker keeps here is authoritative: the preset can be re-fetched, the
journal can start empty, the stamp only makes checks less frequent.

Writability is decided by actually creating the directory and writing a probe file, once per
process, not by `os.access` — which lies on network filesystems and under containers.

### 1.2 The `home=` argument

`AsyncBroker.__init__` and `sync.Broker.__init__` take `home: str | Path | None = None`. It is the
supported way to give a project its own isolated llmbroker state without a config file: two
projects with different `home=` share nothing.

## 2. The interval and the check — `broker/broker.py`

### 2.1 Constructor and state

```python
_DEFAULT_SYNC_INTERVAL = 86_400.0  # seconds
```

`sync_interval: float = _DEFAULT_SYNC_INTERVAL` after `sync`; `< 0` raises `ValueError` next to the
existing `scope == ""` check. Small values are for tests, and the docs do not present `0` as a
deployment stance (§8).

```python
self._sync_interval = sync_interval
self._next_refresh = float("inf")   # monotonic deadline; inf until the first check lands
self._refresh_task: asyncio.Task[None] | None = None
```

`_sync_attempted` stays: it is what keeps a *retried failed provision* from re-fetching.

The class docstring at `broker.py:153` ("refreshes the lineup once, just before the pool is first
provisioned") is rewritten around the two gates.

### 2.2 The check

`ensure_pool` (`broker.py:233`) gets the scheduling call on its fast path — the funnel every public
operation already goes through:

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
refresh, not one per call.

### 2.3 One attempt function

`_sync_on_start` and the refresh differ only in what they log, so they collapse into:

```python
async def _attempt_sync(self, reason: str) -> None:
    """Best-effort by construction: a refresh that cannot be fetched or cannot be
    applied logs and leaves the running configuration alone. A process must not
    fail to start, and a request must not fail, over a lineup refresh."""
```

Swallow set: `SyncRefusedError` (stash `exc.report`), `ValueError`, `OSError`. `OSError` already
covers the read-phase failures #1 §4.4 converts, and is what keeps a background refresh from
surfacing as an unretrieved task exception.

### 2.4 Shutdown

`aclose()` (`broker.py:344`) cancels an in-flight refresh **before** closing the ports it uses:

```python
if self._refresh_task is not None and not self._refresh_task.done():
    self._refresh_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await self._refresh_task
```

Without this, a refresh in flight during shutdown writes through a registry whose driver is closing.

## 3. The identity gate and the stamp

A refresh that changes nothing must be indistinguishable from no refresh at all: no file write, no
`catalog.apply`, no resync, no seeding, no INFO line. Otherwise a daily refresh rewrites a
version-controlled `llms.toml` (mtime churn, a spurious diff on any host that checksums its config)
and logs a report nobody needs to read.

### 3.1 File target — `broker/upstream.py`

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
a preset whose only change is a comment counts as changed — correct: the file's git history is the
update record.

### 3.2 Registry target — `broker/broker.py`

`_sync_registry_target` (`broker.py:321`) compares the merged lineup with what it loaded:
`changed = merged != current`. `LLMConfig` is a frozen dataclass, so list equality is the whole
comparison, order included (the merge preserves order, and a reordering is a change worth applying).

In `sync()` (`broker.py:277`), `seed_disabled`, the secret seeding and `_catalog.resync()` all move
behind `changed`. `last_sync_report` and the stamp are written either way.

Consequence to state in the spec: **a new key appearing in the environment is no longer picked up by
an unattended refresh** — seeding runs only when the lineup changed. It is picked up by a restart or
an explicit `sync()`, both of which a new key implies anyway (a key arriving is an admin act, and
the admin is right there). This narrows #1 §4.3 deliberately; the parity that fix established — the
file branch seeds like the registry branch — still holds on every sync that changes something.

### 3.3 The stamp

One JSON file in the home directory, `sync-stamps.json`, mapping a check to a wall-clock timestamp:

```json
{"freetier /abs/path/llms.toml": {"checked_at": 1785642486.0}}
```

**Keyed by (preset, target), not by preset alone.** The check answers "has *this installation*
looked recently", and two projects on one machine have two lineups to keep current. Keyed by preset
alone, project A's 10:00 check would gate project B every day and B's own config would never be
refreshed. The target identity is the registry path or DSN; a broker with no persistent identity
(§ the fileless broker plan) uses its home directory, which is its identity.

Wall clock, not monotonic: it exists precisely to survive process exit.

- Read once, at `_sync_on_start`. The stamp gates only when `0 <= now - checked_at < interval` — a
  timestamp in the future (clock moved back, a stamp copied between hosts) is treated as absent
  rather than as a lock that never expires.
- When it gates, **the remainder carries into the in-process clock**:
  `self._next_refresh = time.monotonic() + (interval - age)`, so a process starting 23 h into the
  window checks an hour later, not a day later.
- Written after every completed check, changed or not: it records "we looked".
- Written by the CLI's `--sync` too, never read by it.
- Every failure to read or write it is caught, logged at DEBUG, and degrades to in-process gating.

Deliberately **not** shared across a cluster. With a 24 h interval, N nodes cost N GETs of 1.7 KB
per day — unmeasurable against the fleet's own LLM traffic.

What a shared stamp would buy is avoiding concurrent *application*, and the interval is what makes
that unnecessary:

- **Correctly deployed, nodes agree.** The merge is a pure function of (arriving lineup, current
  lineup, resolved keys). The lineup is one shared URL; the keys are shared by construction, since
  a database source hands the same driver to the registry, the store and the secrets
  (`source.py:47,57,69`) — one registry means one secrets store. All nodes compute the same result,
  so the first write settles it and the identity gate turns every other node's check into a no-op.
  A cluster therefore writes **once per real preset change**, whatever its size.
- **Deployed wrong, the damage is bounded.** Divergent key visibility takes an operator explicitly
  handing nodes different `secrets=`/`have_keys=` — a deploy error, not a supported mode. Its effect
  is last-writer-wins on the registry, repeated at most once per node per day, and self-healing the
  moment the deploy is fixed. The daily add/remove pair in each node's INFO log is the trace an
  operator reads; #1 §3's degradation ERROR covers the case where a flip actually costs a node its
  failover.

`write_atomic` makes a file write last-writer-wins rather than corrupting, and #1 §4.5 keeps the
mode.

The per-node guarantee is "once per interval **per home directory**": a container with no persistent
home re-checks on each start, which is exactly today's behavior and therefore no regression.

### 3.4 The preset cache

The fetched preset text is cached in the home directory as `presets/<name>.toml`, and the paid
catalog alongside it under the same name convention — it is fetched by the same
`fetch_preset_text` seam (`upstream.py:113`) and needs the same treatment.

Unlike the stamp, the cache **is** machine-global and correctly shared: "what does the catalog say
today" does not depend on which project asks.

The cache is a fallback, not a source: a successful fetch overwrites it; a failed fetch falls back
to it; a fetch that is rate-limited by the CDN (403/429 from an unauthenticated per-IP limit — a
shared NAT or a busy CI runner will meet it) is a failed fetch like any other, logged at DEBUG, not
an error. This is what makes an offline or throttled installation keep working on what it last saw.

## 4. Placement relative to provisioning

`_sync_on_start` keeps its name and its position inside the provision lock, and decides between
three outcomes:

```
registry is empty      -> await the sync here, blocking (onboarding; provision() on an empty
                          registry raises, so there is no alternative)
stamp is fresh         -> no check; seed _next_refresh from the remainder
otherwise              -> provision first, then refresh in the background
```

Emptiness is read with `await self._registry.load()` — one registry read `provision()` would have
done a moment later anyway.

The background case is armed by setting `self._next_refresh = 0.0` rather than creating the task
inside the lock: the task calls `sync()`, `sync()` touches the catalog, and the catalog is
mid-provision. `ensure_pool()` reaches its fast path immediately after the lock releases, on the
same call.

## 5. Logging and report

- **Changed:** the report at INFO, as #1 §1.4 leaves it.
- **Unchanged:** DEBUG, one line — `sync freetier: no change`. A daily INFO saying nothing is how a
  log becomes unreadable, and #1 §3 already owns the state that must be loud.
- **Failed:** WARNING naming the reason (`start` / `refresh`), so a log reader can tell a failed
  onboarding from a failed daily check.
- `last_sync_report` is set on every outcome including no-change.
- The CLI is unchanged: `preset <name> --sync <file>` always fetches and always prints the report,
  no-ops included. It gains only the stamp write.

## 6. What a fetched preset is allowed to do

An unconditional refresh makes the catalog's `main` branch live configuration for every
installation. The exposure is small but real and belongs in writing: a preset carries no code, entry
names are immutable (`architecture.md`, model identity), and #1's invariant 4 bounds what a merge
can take away — but `base_url` decides **where the installation's API keys are sent**.

- **Hygiene, here:** a config built from a *fetched preset* must have an `https://` `base_url`;
  anything else fails the fetch with the same `ValueError` shape as invalid TOML, validated in
  `fetch_preset_text` (`upstream.py:80`) after the `tomllib.loads` check, so the whole file is
  rejected before any merge sees it. This does not defend against a compromised catalog — that
  catalog would serve `https://` too — and the plan must not pretend otherwise. It removes plaintext
  key transmission as an accident, and it is two lines.
- **There is no off switch, by decision** (design summary 8). An installation that must not follow
  our curation declares its own lineup instead — that is a different pool, not a frozen copy of
  ours.
- **Recorded, not fixed:** tag pinning is the alternative that would close this, and it is rejected
  above. `decisions.md` carries both halves.

## 7. Tests

`tests/test_preset_autorefresh.py` (new). `fetch_preset_text` is monkeypatched as
`tests/test_broker_sync_knob.py` already does; a stub that *raises on call* is how "no fetch
happened" is asserted. Time is controlled by monkeypatching `time.monotonic`/`time.time` and by
small intervals — **no test sleeps.**

| scenario | expected |
|---|---|
| interval not elapsed, many calls | fetch stub never called |
| interval elapsed | exactly one refresh task for a burst of concurrent `ask()`s |
| unchanged preset | file mtime unchanged, `catalog.apply` not called, no INFO record, `last_sync_report` refreshed |
| changed preset | written, applied, resynced, INFO record, new entry routable without a restart |
| fetch raises during a refresh | WARNING, broker keeps serving, no task exception surfaces |
| fetch returns 429 | cached text used, DEBUG, no error |
| `aclose()` with a refresh in flight | cancelled; no "Task exception was never retrieved", no write through a closed registry |
| empty registry | still blocking before provisioning — existing `test_broker_sync_knob.py` cases stay green |
| `sync_interval=-1` | `ValueError` at construction |

`tests/test_llmbroker_home.py` (new) — resolution order; `$LLMBROKER_HOME` wins; a read-only
candidate falls through to temp; nothing writable yields `None` and the broker still runs;
`home=` isolates two brokers from each other.

Stamp cases (in `test_preset_autorefresh.py`): a second broker on the same target skips the fetch;
an expired stamp fetches; a future `checked_at` is treated as absent; an unparsable stamp degrades
silently; **two brokers on different targets do not gate each other**; a stamp 90% through the
window lands `_next_refresh` at the remainder.

`tests/test_upstream.py` — a fetched preset with an `http://` `base_url` raises `ValueError` and
nothing is written; an `https://` one passes. A *file* source is unaffected: the user's own config
is not the catalog.

`tests/test_sync_roundtrip.py` — an unchanged sync leaves the file byte-identical, comments and
`[[custom]]` blocks included.

## 8. Specs (same batch as the behavior)

- `architecture.md`, the lockfile paragraph: **replaced, not qualified.** The current rule is that a
  broker on the curated preset keeps its lineup current on an interval, checking lazily on activity,
  because a free-tier lineup that stops updating decays into an unusable pool. The cluster
  flip-flop sentence stays, re-aimed: what makes concurrent nodes safe is the merge's idempotence
  plus the identity gate.
- `architecture.md`, "The `sync=` knob" → "Keeping the lineup current": the two gates, why the check
  is lazy rather than timed, the empty-registry exception, that a refresh never raises, and the
  seeding narrowing from §3.2.
- `architecture.md`, new short section "The home directory": what lives there, the resolution order,
  that nothing in it is authoritative, and `home=`.
- `decisions.md`, **three edits, not additions**:
  - the "no implicit seed-on-start" clause and the *What was dropped* row "Implicit seeding on
    startup" are **narrowed to what their own reasoning proves**: a node never coerces the registry
    to a local copy of its own — an implicit refresh follows the one shared upstream, which is why
    nodes converge instead of oscillating. State the premise that makes this safe and is currently
    left implicit: one registry means one secrets store, so all nodes resolve the same keys and
    compute the same merge;
  - the stale "sync is a total mirror … there is no separate CRUD path, merge rule" clause is
    brought in line with the merge rule #1 leaves in place;
  - one new row: unconditional interval-gated refresh with an identity gate (cost: one monotonic
    comparison per call, one small GET per node per day, zero writes when nothing changed), plus
    the `main`-as-live-config trade-off with the rejected tag-pinning alternative.
- `mission.md`, item 2: "mirrors itself into an installation via `sync(name)`" becomes true without
  qualification — the preset reaches a running installation with no admin act at all.

## 9. Docs (`docs/src/en/` + `docs/src/ru/`)

- `usage.md`: the refresh is automatic and needs no argument; an unchanged preset touches nothing on
  disk; the file is rewritten only when upstream genuinely moved. This replaces the runtime-rewrite
  warning #1 §7 adds, which the identity gate has made much narrower.
- `usage.md`: one short section on the home directory — what llmbroker keeps there, `home=`, and
  `$LLMBROKER_HOME`.
- `server.md`: the deploy job stays the way to *fill* a fresh DB; keeping it current no longer needs
  a job. Delete the pinned-deployment stance rather than documenting an off switch.
- `cli.md`: `--sync` is the reviewable path, not the only path — it always fetches, always prints
  the report, and shares the stamp with the application on the same host.
- `async.md`: drop `sync="freetier"` from the example; it is now the default behavior.

## Work order

Each batch ends green on `invoke pre` + `python -m pytest` (`. ./activate.sh` first).

1. §3.1–3.2, the identity gate, with tests. Independent and valuable alone — it is what stops
   today's sync from rewriting an unchanged config.
2. §1, the home directory, with tests. Nothing depends on it yet; landing it alone keeps the
   fallback chain reviewable on its own.
3. §2 + §4, the interval, the lazy check, the placement, the shutdown cancel, with tests; §3.3–3.4
   stamp and cache; `architecture.md` in the same batch — the spec must not be left asserting
   "start is never online".
4. §6 hygiene + `decisions.md`; §9 docs en + ru.

Version bump: none (the maintainer does it by hand).

## Verification

```bash
. ./activate.sh
invoke pre
python -m pytest
python -m llmbroker preset freetier --sync /tmp/llms-check.toml   # report, exit 0, stamp written
python -m llmbroker preset freetier --sync /tmp/llms-check.toml   # byte-identical file, exit 0
```

## Handover

Implemented in full: §1 (+§1.2), §2.1–2.4, §3.1–3.4, §4, §5, §6, §7, §8, §9 (en + ru).
Version bump skipped, per the repo rule. The plan file and its README row stay for review.

### The one thing the plan did not decide, and how it was decided

**§2.1 never says what `sync` defaults to.** It is the normative constructor section — it fixes
`sync_interval`, its validation, the three new attributes, and that `_sync_attempted` stays — and it
is silent on the change everything else rests on. Design summary 1 ("no `sync=` opt-in and no off
switch"), §5, §8's `mission.md` line and three §9 bullets ("the refresh is automatic and needs no
argument", "it is now the default behavior", "delete the pinned-deployment stance") are only true if
the default moves; the refresh has no source other than `self._sync_source`, so with `None` the
whole mechanism would be dead code for anyone who did not opt in — exactly the opt-in design summary
1 abolishes.

Implemented as **`sync: str | Path | None = "freetier"`**. `None` stays accepted and is what a
registry filled by other means passes (`registry.mirror()`, a vendored file the app cannot name);
it is not documented as a deployment stance, which is what §8 rejects. `sync_interval` follows it,
`< 0` raises.

Consequence the plan did not cover: with the default on, every broker in the suite would reach the
catalog. `tests/conftest.py` gains two autouse fixtures — an isolated `$LLMBROKER_HOME` per test,
and `urllib.request.urlopen` refusing with `URLError`. The socket seam, not `fetch_preset_text`:
tests patch both levels above it and must keep winning.

### Where the code disagreed with the plan

- **§3.2's registry-side gate compares by name, not as lists.** The plan's `merged != current` is
  order-sensitive, and every DB registry returns rows ordered by name (`backends/driver.py`
  `fetch`), so a no-op sync of a curated lineup reports "changed" every time — the gate would be
  defeated exactly where §3.3's cluster argument needs it. Proven: mirroring `[zeta, alpha]` and
  loading it back yields `[alpha, zeta]`, list-equal `False`, name-keyed `True`. When
  `pool-priority.md` adds a persisted weight to `LLMConfig`, the dict comparison picks it up with no
  further change — which is what the queue README means by "a new persisted field changes that
  comparison". Regression test:
  `test_sync_identity_gate.py::test_the_gate_ignores_the_order_a_registry_returns_rows_in`.
- **Secret seeding stays outside the gate.** §3.2 moves it behind `changed` and then states a new
  env key is still "picked up by a restart or an explicit `sync()`" — neither is true, since a
  restart does not bootstrap secrets (`test_sync_info_logs.py`) and the explicit `sync()` is the
  call the gate is inside. It also contradicts a live `architecture.md` clause the plan does not
  schedule for removal. Behind the gate now: the registry write, `seed_disabled`, `resync`, the INFO
  line. Regression test:
  `test_sync_identity_gate.py::test_an_unchanged_sync_still_bootstraps_a_key_that_arrived`.
- **§2.2 vs §4 on when the armed refresh fires.** §2.2 calls `_maybe_schedule_refresh()` only on
  `ensure_pool`'s fast path, so §4's "reaches its fast path immediately after the lock releases, on
  the same call" does not hold — the call returns instead, and the refresh would wait for the next
  public operation. The scheduling call was moved after the lock so both paths reach it.
- **§3.1 names `_verify_entry_names`; the function is `_check_render_faithful`.** Stale name from
  the plan #1 that shipped before this one.

### Decisions taken during implementation

- **The CLI writes the stamp but does not read the preset cache** (§3.4 does not say either way).
  `preset --sync` and `preset` print or write what they fetched; a silent fall back to a stale
  cached copy would put stale content on stdout or into the user's file. "An explicitly typed
  command must always do the thing" (§8) reads as: always fetch, and fail loudly when it cannot.
  The broker path uses the cache as specified.
- **Stamp target identity**: a file registry's resolved path; otherwise the string source the broker
  was constructed with (a sqlite path or a DSN); otherwise the home directory. A registry *object*
  passed directly (`AsyncBroker(SqliteRegistry(...))`) therefore has no stable identity and falls
  back to home, so two such brokers in one process on one home share a check. The cost is a delayed
  check for the second target, never a wrong lineup, and `home=` separates them. Giving every
  backend a public identity would mean touching the `Driver` protocol and its conformance suite —
  scope this plan did not ask for.
- **The stamp is written on a completed check only**, not on a failed fetch: an offline installation
  must not have a failure gate it for a day. The in-process deadline advances either way, so a
  failing broker still checks at most once per interval.
- **§5's no-op DEBUG line changed a shipped test.** `test_a_no_op_run_still_logs_its_one_line`
  asserted the INFO line the plan removes; rewritten as
  `test_a_no_op_run_says_so_at_debug_and_nowhere_else`, and `architecture.md`'s "every sync logs at
  info" clause was rewritten with it — §8 did not list that clause, but leaving it would have made
  the spec contradict §5.
- **Test placement**: the identity gate got its own file (`tests/test_sync_identity_gate.py`) rather
  than being split between `test_sync_roundtrip.py` and `test_upstream.py` as §7 suggests — it is
  one behavior with two targets, and the byte-identity case reads better next to the registry one.

### Gate

`. ./activate.sh` first; `invoke pre` — all checks passed, pyrefly 0 errors; `python -m pytest` —
**1077 passed**, zero failures, zero skips (docker tests included). The plan's manual verification
was run: two consecutive `preset freetier --sync` runs, exit 0 both times, the second leaving the
file byte-identical with an unchanged mtime, and `sync-stamps.json` written under the home
directory.
