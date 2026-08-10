# A resolution that never fetches, where the process was told not to

## Goal

`no-automatic-fetch` stopped both clocks, and the empty registry stopped being
filled behind a request. One read was left: the first resolution of a `direct=`
alias reads the paid catalog through `PresetSource.text(prefer_cache=True)`, whose
order is cache → **network** → cache → the wheel's copy. With a cold cache that
order opens a socket, and a serving pod's cache is cold whatever the deploy job
did — the job runs in a different container.

The copy in the wheel is already there and is already the floor for this exact
read; it is simply reached one step too late. After this plan, a process that
fetches nothing resolves its aliases cache → wheel, and never attempts the
network.

## The entry, to land in `decisions.md` verbatim

Appended to the existing `no-automatic-fetch-means-none-at-start-either`, whose
**Blocks** line rejects the bundled copy as a way to fill a registry. The catalog
is decided the other way and the difference belongs in the entry itself.

> **The paid catalog is the exception, and it is not the registry.** With no
> automatic fetching, a declared alias resolves from the cache, then from the
> wheel's copy, and the resolution freezes at that copy until the operator syncs
> again. An empty registry means nothing can be served at all, and the bundled
> preset would answer a question the operator said they would answer; a declared
> alias is one model the operator already named, and the wheel's copy is the floor
> that same read already falls to whenever the network is unreachable. Refusing
> here would only turn "offline because it is forbidden" into a failure that
> "offline because it is down" does not produce.

## Work order

One batch.

1. **`broker/presets.py`.** `text` takes `fetch: bool = True`. With `fetch=False`
   the order is cache → the wheel's copy (subject to `floor`), and
   `fetch_preset_text` is never called. Nothing left to read raises `ValueError`,
   as the same dead end does today; the message names `await broker.sync()`.
2. The bundled fallback logs its existing WARNING — the copy is frozen at the
   installed release either way, and which reason kept the process off the network
   changes nothing for whoever reads the log.
3. **`broker/aliases.py`.** `resolve_declared` takes `fetch: bool = True` and
   forwards it. It is the only `prefer_cache=True` caller in the library.
4. **`broker/broker.py`.** `sync_interval is not None` is already computed for
   `Catalog(autofill=...)`; it becomes one field and serves both consumers, with
   `_resolve_declared` passing it as `fetch=`.
5. `sync()` is untouched: it reads through the fetch-first path and
   `PresetSource.refresh`, neither of which takes the new flag.

## Tests

- Cold cache, switch on, `direct=["opus"]`: the alias resolves from the bundled
  copy and the fetch seam is never called.
- A warm cache still wins over the bundled copy under the switch.
- The explicit `sync()` refreshes the cache under the switch, and the next
  resolution follows what it fetched.
- Nothing cached and nothing bundled: the first resolution raises, the message
  names `broker.sync()`, and nothing was fetched.

## Spec updates

- **`rules/direct-aliases.md`** — the resolution's read: where the process fetches
  nothing, it is cache → the wheel's copy and never a fetch, and the alias is
  frozen there until an explicit sync moves it. The paragraph on the catalog's own
  clock gains the case where there is no clock.
- **`rules/lineup-refresh.md`** — one clause on the no-clock case: the request-path
  catalog read does not go to the network either.
- **`decisions.md`** — the paragraph above, appended to the existing entry.

## Docs (en and ru, in step)

- **`server.md`, the `#no-fetch` section** — one sentence: declared aliases resolve
  from what the last sync left behind, or from the copy in the package, and stay on
  that version until the job runs again.

## Gate

`invoke pre` clean and `python -m pytest` green. Docker up for the testcontainer
tests.

## Handover

**Done, in one batch, exactly as the work order reads.** `PresetSource.text` takes
`fetch`, the local-only branch is its own small method, `resolve_declared` forwards
the flag, and the broker keeps one field (`sync_interval is not None`) that feeds
both the catalog's emptiness message and this read.

**Decisions the plan did not make.**

- *The offline dead end keeps raising `ValueError`, with no new exception type.*
  "The catalog could not be read" already raises `ValueError` from the same method
  when a fetch fails with nothing behind it, and `AsyncBroker._resolve_declared`
  already catches it and re-raises on the first resolution. A new type would have
  changed that contract for a state that is not new to the host — only its cause is.
- *The bundled fallback logs its own line rather than reusing the fetch-failure
  one*: there is no failure to name, so the message says nothing was cached and
  nothing is fetched automatically. The "frozen at this llmbroker release" half is
  identical, which is what a test asserts on.

**Nothing was left out**, and nothing in the code disagreed with the plan.

**Gate:** `invoke pre` — all checks passed. `python -m pytest` — 1287 passed, zero
skips, zero errors (Docker up; the testcontainer tests ran). Four new tests, all in
`tests/test_no_automatic_fetch.py` beside plan 19's.

## Review round 1

**One defect, fixed.** Where no directory is writable, the paid-catalog refresh was
skipped rather than performed — correct while the resolution fetched for itself,
wrong the moment this plan took the network away from it. The explicit `sync()`
then returned `None` having done nothing, and the alias stayed on the wheel's copy
for the life of the deployment, while the log advised running the very call that
had just no-opped.

A refresh exists to leave a copy behind, so with nowhere to keep one it now raises
and names its two ways out — a writable directory, or an installation that fetches
nothing by itself. The skip and its comment are gone; nothing was added to hold a
copy elsewhere, which is machinery this library does not need. Two tests cover it,
one at the preset source and one at the broker.

Also from the round, none of it runtime: the deploy-job example in `server.md`
printed a dataclass repr instead of `format_report` and had no branch for the
report-less catalog sync; one sentence was stated twice in `rules/direct-aliases.md`;
the queue table had this plan's row out of order.

**Gate after the fixes:** `invoke pre` — all checks passed. `python -m pytest` —
1289 passed, zero skips, zero errors.

## Review round 2

**No runtime defect.** The round's one candidate — that a machine with nowhere
writable stops re-resolving a declared alias, since the refresh now raises before
it invalidates — was withdrawn: that installation is misconfigured, and failing
loudly with the way out is the intended answer, not a regression to repair.

Three text fixes, all of them consequences of round 1's change:

- `rules/direct-aliases.md` still said that where nothing is writable the
  resolution's read is a fetch, describing the behavior that change replaced. It
  now says there is no refresh to move the resolution there, and links the rule to
  where it is stated once.
- The refresh's error offered "run with `sync_interval=None`" as an instruction to
  the reader, which does not fit the caller of an explicit `sync()` — who may
  already have set it. The advice is now scoped to the automatic refresh; both
  ways out survive, and no call site needed to learn where it was called from.
- `PresetSource.text` had lost its pointer into the specs while gaining a flag.

**Gate:** `invoke pre` — all checks passed. `python -m pytest` — 1289 passed, zero
skips, zero errors.
