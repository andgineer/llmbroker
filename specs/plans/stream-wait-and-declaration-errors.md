# Plan — a stream says what can come back, and a paid-model error stays with that model

**Status: source-bound on `ab9cb45a7`.** Two fixes, one release. No schema change. One
public addition: a snapshot property.

Downstream: echo-words streams every word from the pool and steps up to a paid model on a
missed budget; its contract tests found both defects. dinary is unaffected by either.

§1 is the narrowed form of this plan's first attempt. That attempt required a stream to
behave exactly like `ask` before its first delta — to wait on cooling candidates and reopen
them. Three review rounds found three defects in it, each in a different part of the stream's
lane machinery (the pre-output exhaustion refresh lost its budget; a raced stream reopened a
model whose deltas the caller had read; the budget rearm disarmed the handle's deadline for
good). The machinery that carried it — a waiver of the at-most-once exclusion, a crashed-lane
exclusion, a budget rearm and a handed-over boundary — is being consolidated by
[`architecture-simplification.md`](architecture-simplification.md) anyway. So the requirement
drops to the half that the downstream problem actually needs, and the implementation drops
with it: the routing is left exactly as it is, and only the error it reports changes.

## 1. A stream reports a pool that can come back as `timeout`, not `excluded`

**Now.** Two keyed models, both answering 429 with `Retry-After: 2`, `wait=8`, measured on
`2c6f91f6a`, with and without `fastest_of=2`:

| call | outcome | provider attempts (s) |
|---|---|---|
| `ask` | `NoLLMAvailableError(reason="timeout")` after 8.0 s | 0, 0, 2, 2, 6, 6 |
| `stream` | `NoLLMAvailableError(reason="excluded")` after 0.0 s | 0, 0 |

`excluded` is documented as "no model can be used for this request; for example, providers
rejected every available key", and `usage.md` tells a host that `timeout` and `excluded` both
apply to one request while the first three reasons are configuration faults. A host therefore
reads `excluded` as "nothing here will come back" — echo-words maps it to a configuration
fault and does not step up to its paid model, so a rate-limited free pool fails a streamed
word outright, where the same pool on `ask` ends in `timeout` and the step-up happens.

**The narrowed requirement.** A stream still commits to the models that can answer when it
starts: it does not wait on a cooldown and does not reopen a model it has tried. What changes
is the report. Where the candidates it may no longer reopen are merely cooling — they come
back on their own — the stream raises `NoLLMAvailableError(reason="timeout")` carrying
`retry_at`, the same pair `ask` gives for the same pool state. `excluded` stays for the case
it names: nothing in the pool can serve this request at all.

**Do:**

1. Leave `Router._acquire`, `Router._untried`, the lane machinery and the at-most-once
   exclusion exactly as they are at `ab9cb45a7`.
2. Where the stream's initial acquisition ends in `NoLLMAvailableError(reason="excluded")`,
   ask the pool whether a candidate comes back by itself — `Pool.retry_at(payable,
   exclude=call.client_failed)`, the same call `Router._expired` makes — and, when it names a
   moment, raise `timeout` with that `retry_at` instead. A stored client error still wins, as
   it does for a routed call. The one service this needs on the streaming boundary
   (`StreamBackend`) is already in the tree.
3. Expiry before any delta carries `retry_at` by the same rule (it carried none).
4. Nothing past the first delta changes, and neither does a continuation.

**Do not:** wait on a cooldown in a stream; reopen a model the handle has tried; touch
`_untried`, the race's visible-lane selection, the alternatives contract, or the budget.

**Tests** (`tests/test_router_stream.py`, mock transport, existing clock helpers):
- every candidate 429 or 5xx and cooling: `reason="timeout"` with `retry_at` naming the
  earliest return, no waiting (the call ends at once), with and without `fastest_of=2`;
- every candidate answering 400: the stored client error, unchanged;
- a pool that genuinely cannot serve (every key dead): `excluded`, unchanged;
- expiry before any delta: `timeout` with `retry_at`;
- `ask` and `stream` report the same reason and `retry_at` for the same pool state, while
  only `ask` waits.

## 2. An unresolvable paid declaration fails only `direct()` on that handle

**Now.** `AsyncBroker(..., direct=["gpt-fast", "gpt-fats"])` with a keyed, answering pool
entry, measured on 1.10.0 and on `2c6f91f6a`: `ask`, `snapshot()` and `direct("gpt-fast")`
all raise `UnknownModelError: direct= names 'gpt-fats', which the paid catalog does not
carry — available aliases: …`, on every call (a first resolution that fails is attempted
again next time). In echo-words, a typo in one language's `api_model` makes every word on
every language fail, free pool included.

**Why.** `broker.py::AsyncBroker._resolve_declared` re-raises when there is no previous
resolution; it backs `catalog.py::Catalog._resolve_overlay`, which `Catalog.entries()`
calls, and `entries()` is on the path of `rebuild` (so provisioning and `snapshot()`) and of
`AsyncLLMs.resolve_direct`. `aliases.py::resolve_declared` raises for the whole list when
one alias is missing, so one bad handle also takes the good ones down.

**Do:**

1. `aliases.py::resolve_declared` resolves each declaration on its own. An alias the catalog
   does not carry becomes an unresolved handle carrying the message `_entry_for_alias`
   raises today; the others resolve. Where the catalog itself cannot be read on the first
   resolution (the `ValueError`/`OSError` path), every alias declaration is unresolved with
   that message, and fully stated `LLMConfig` declarations still resolve. `DeclaredModels`
   carries the unresolved handles (handle → message).
2. `AsyncBroker._resolve_declared` never raises for an unresolved handle. A re-resolution
   keeps its current behaviour (stay on the resolution in use, warn); an alias unresolved
   earlier resolves on a later refresh once the catalog carries it.
3. `catalog.py::find_declared` (and so `direct()`): a handle that is unresolved raises
   `UnknownModelError` with its stored message — the same text the host gets today, now only
   from the call that names it.
4. Pool provisioning, routing, `snapshot()` and `direct()` on resolved handles never raise
   because of an unresolved one. Emptiness (`Catalog.rebuild`: `not stored and not
   declared`) counts what the host declared, not what resolved, so a direct-only host whose
   only declaration is a typo gets the typo error from `direct()`, not `EmptyRegistryError`.
5. One ERROR log line per handle when it first becomes unresolved (deduplicated by handle,
   like the missing-key lines), carrying the message. A handle that later resolves logs
   nothing extra beyond the existing alias-move line.
6. `PoolSnapshot` gains `direct_unresolved` — a read-only mapping handle → message, beside
   `direct_missing_keys` — so a host's status screen shows the typo without making a call.

**Do not:** change `check_overlay` collisions (a declared name or alias equal to a registry
entry's): that is a conflict between two statements the host itself made, and resolving it
by dropping one would pick silently — it keeps raising as now; change what a successful
re-resolution does; fetch the network from `direct()` to retry a resolution.

**Tests** (`tests/test_direct_declaration.py`, `tests/test_aliases.py`,
`tests/test_broker_direct.py`, mock transport):
- typo + good alias + keyed pool: `ask` answers; `snapshot()` works and lists the typo in
  `direct_unresolved`; `direct("gpt-fast")` returns a client; `direct("gpt-fats")` raises
  `UnknownModelError` naming the available aliases; exactly one ERROR line across repeated
  calls;
- unreadable catalog at first resolution: the pool answers, `direct(alias)` raises with the
  unreadable-catalog message, an `LLMConfig` declaration works;
- a later refresh whose catalog carries the alias resolves it and `direct()` then works;
- direct-only host with only a typo: provisioning raises no `EmptyRegistryError`,
  `direct()` raises the typo error;
- collisions still raise as before.

## Reference and docs

- `rules/call-path.md`, "Streaming": a stream does not wait on a cooldown and does not
  reopen what it has tried; when what it may not reopen is only cooling, it says so with
  `timeout` and `retry_at`, and expiry before any delta carries `retry_at` too. Leave the
  at-most-once paragraph under "Continuing after a complete answer" as it stands.
- `rules/direct-by-name.md`: replace "Only the first resolution may fail … raises at
  provision" with the handle-scoped rule; `snapshot()` reports unresolved handles.
- `decisions.md`: add both entries below verbatim.
- `docs/src/en/async.md` / `usage.md` (and `ru`): what a stream reports when no model can
  serve it, and that `retry_at` names when one returns; `docs/src/en/direct.md` (and `ru`) "Errors": an unknown alias fails only
  `direct()` on it and appears in `snapshot().direct_unresolved`.

### Entry: `a-stream-reports-what-can-come-back`

**Blocks:** reporting a cooling pool to a stream as `excluded`; and making a stream wait on
a cooldown and reopen a tried model so that it fails exactly as `ask` does.
**Why:** a host reads `excluded` as a fault nothing will resolve, so the reason decided
whether it fell back at all — that is the half worth fixing, and it is one call to the pool.
The other half, waiting, asks the lane machinery for a waiver of at-most-once, a rearmed
budget for the one pool re-read, and a boundary for what the caller has already been handed;
three review rounds found one defect in each. A stream's own answer already carries the
moment a candidate returns, so a caller that wants to wait can, while a caller with a
fallback — the case the pool exists beside — does not pay for a wait it did not ask for.

### Entry: `a-declaration-error-stays-with-its-handle`

**Blocks:** failing provisioning, routing and every `direct()` because one declared alias
cannot be resolved.
**Why:** a typo in one paid alias disabled the free pool and every correct alias, on every
call, although routing never uses a declared model. The error stays as loud where it
matters — the `direct()` naming the handle raises the same message, the log carries it once,
and `snapshot()` shows it — and nothing that does not name it can fail because of it.

## Gate

`. ./activate.sh`, `invoke pre`, `invoke test` (both passes), and
`invoke downstream --source local` (both hosts' contract tests are committed; do not use
`--working-copy`, another session edits the dinary working tree). Expected: echo-words' contract test that pins "a rate-limited pool on a stream ends
with `excluded`" regresses — classify it in the handover (a host test pinning the defect
this plan fixes) with the host-side change; anything else is to be investigated. No version
bump, no commit.

## Handover

### Sections done

**§1 — a stream reports a pool that can come back as `timeout`, not `excluded`.** The
plan's measurement, re-run on this tree: two keyed models both answering 429
`Retry-After: 2`, `wait=8`, with and without `fastest_of=2`.

| call | outcome | provider attempts (s) |
|---|---|---|
| `ask` | `NoLLMAvailableError(reason="timeout")`, `retry_at` set, after 8.0 s | 0, 0, 2, 2, 4, 4, 6, 6 |
| `stream` | `NoLLMAvailableError(reason="timeout")`, `retry_at` set, after 0.0 s | 0, 0 |

- The routing is untouched. `Router._acquire`, `_untried`, the lane machinery and the
  at-most-once exclusion are exactly as they are at `ab9cb45a7`; nothing waits on a
  cooldown, nothing is reopened, and no acquisition takes a new argument. What changed
  is one report: where the stream's initial acquisition ends in `excluded` and the pool
  says a candidate comes back by itself, the stream raises `timeout` carrying that
  moment instead.
- A stored client error still wins, without a second rule for it: `Router._acquire`
  already raises the client error rather than `excluded`, so it never reaches the
  report.
- Expiry before any delta now carries `retry_at` by the same rule; it carried none.
  `Pool.retry_at` on the streaming boundary is the one service this needed, and the
  only line of the first attempt that survives.
- Nothing past the first delta changed, and neither did a continuation: the acquisition
  a continuation makes is untouched.
- Specs and docs: the "Streaming" rule in `rules/call-path.md`, the
  `a-stream-reports-what-can-come-back` entry in `decisions.md`, and
  `docs/src/{en,ru}/async.md` and `usage.md`. The at-most-once paragraph under
  "Continuing after a complete answer" stands exactly as it did at `ab9cb45a7`.
- Tests (`tests/test_router_stream.py`): every candidate 429 or 5xx and cooling ends in
  `timeout` naming the earliest return and waits for none of it
  (`test_a_cooling_pool_ends_a_stream_in_timeout_naming_when_it_is_back`, over both
  statuses × single/`fastest_of=2`); a request every candidate rejects still raises the
  provider's own error
  (`test_every_candidate_rejecting_the_request_raises_the_provider_error_on_a_stream`);
  a pool with every key dead still ends in `excluded` with no `retry_at`
  (`test_a_pool_that_cannot_serve_at_all_still_ends_a_stream_in_excluded`); an expiry
  before any delta names when the pool is back
  (`test_a_budget_spent_before_any_delta_names_when_the_pool_comes_back`); and one pool
  state reported by both surfaces, where only `ask` queues for it
  (`test_ask_and_stream_report_the_same_pool_state_and_only_ask_waits`). Six of the
  eight cases fail on the source as it stands at `ab9cb45a7`; the other two are the
  guards that `excluded` keeps the cases it still names.

**§2 — an unresolvable paid declaration fails only `direct()` on that handle.** A typo
beside a good alias no longer fails provisioning, routing, `snapshot()` or the other
handles: the pool provisions and routes, `direct("opus")` answers, `direct("opus-5")`
raises `UnknownModelError` with the same message as before, `snapshot().direct_unresolved`
maps the handle to it, and the ERROR line is logged once per handle however many calls
and rebuilds follow.

- `resolve_declared` resolves each declaration on its own and carries the handles it
  could not resolve on `DeclaredModels`. A catalog that cannot be read at the first
  resolution leaves every alias declaration unresolved with that reason while fully
  stated configs still resolve.
- `find_declared` raises the stored message for an unresolved handle; emptiness counts
  what the host declared, so a direct-only host whose only declaration is a typo is told
  the typo, not that its registry is empty. `PoolSnapshot` gained `direct_unresolved`.
- A re-resolution is per handle too: an alias the catalog has dropped keeps the entry it
  is answering from — and the key help it was resolved with, since a catalog that no
  longer names that provider says nothing about where its key comes from — while every
  other declaration follows the catalog just read. An unreadable catalog still keeps the
  whole resolution: a read that failed says nothing about any alias.
- Specs and docs: the resolution rule in `rules/direct-by-name.md`, the
  `a-declaration-error-stays-with-its-handle` entry in `decisions.md`,
  `docs/src/{en,ru}/direct.md`.
- Tests: `tests/test_direct_declaration.py` (typo beside a good alias, one ERROR line,
  direct-only host with only a typo, unreadable catalog, a later refresh resolving the
  handle, a dropped alias that does not freeze the handle beside it, and
  `test_an_alias_the_catalog_dropped_still_says_where_to_get_its_key`),
  `tests/test_aliases.py` (per-declaration resolution, unreadable catalog, both halves of
  the re-resolution rule, and
  `test_a_kept_alias_keeps_its_key_help_while_the_rest_follows_the_catalog`),
  `tests/test_report.py` (what a kept alias is answering from).

### Deviations from the plan, and why

1. **A re-resolution that finds a *previously resolved* alias gone keeps that one
   handle rather than raising.** §2 asks both for "an alias the catalog does not carry
   becomes an unresolved handle" and for "a re-resolution keeps its current behaviour
   (stay on the resolution in use, warn)", which conflict for a handle that was
   resolving. `direct-by-name.md` ("a resolution that works is never lost") and the
   incident behind `test_an_alias_the_catalog_dropped_keeps_serving_and_does_not_stop_the_refresh`
   settle it: that handle keeps the entry it is answering from and is reported through
   the existing alias-fact channel at warning level, while the rest of the resolution
   follows the catalog just read — so a typo the catalog has since fixed is never frozen
   with it. Per-handle unresolution applies where there is nothing to keep: the first
   resolution, and a handle that never resolved.

2. **`find_declared` split and `PoolView` made keyword-only.** The unresolved lookup
   pushed `find_declared` past the complexity gate (11 > 10), so the alias keyspace
   moved into `_by_alias`; a sixth positional dependency on `PoolView` tripped PLR0917,
   so its dependencies are keyword arguments now. Neither was suppressed.

3. **Two existing tests were rewritten rather than left red**, both pinning behaviour
   this plan changes on purpose: `test_a_typo_raises_at_provision_and_lists_the_aliases`
   (now `test_a_typo_fails_only_the_direct_call_that_names_it`) and
   `tests/test_no_automatic_fetch.py::test_nothing_cached_and_nothing_bundled_says_what_to_run`,
   which now asserts the same "run `broker.sync()`" message reaches the host from
   `direct()` while the pool provisions and routes.

### Decisions taken that the plan did not make

- The reason a stream raises for a cooling pool says what it found rather than what it
  spent: *every LLM this stream could use is cooling down*, with `reason="timeout"` and
  `retry_at`. The reason is the contract; the text is what a host reads in a log.
- The message for an unreadable catalog names the handle:
  `direct= names 'opus', and the paid catalog could not be read: <reason>`, so the text
  stands alone wherever it surfaces — the call, the log line, the snapshot. It is keyed
  on whether the catalog could be read, never on whether the failure carried a message,
  so a failure with an empty `str()` is named by its type rather than reported as a
  missing alias.
- The ERROR line is that message verbatim (it already names `direct=` and the handle),
  deduplicated against the current unresolved set exactly as the missing-key lines are.
- `DeclaredModels.unresolved` and `PoolSnapshot.direct_unresolved` are mapping proxies,
  so what a host reads out of a snapshot is read-only.

### Left out

- No version bump, no commit, plan file kept (per the brief).
- Nothing under `scripts/downstream.py` or the workflows — that is row 2's scope.
- The waiting half of §1's first attempt is gone from the tree, including the tests that
  pinned it. `decisions.md#a-stream-reports-what-can-come-back` is where it is now
  blocked.
- `direct(name=X)` where `X` is an unresolved *alias* handle still reports "no model
  named X was declared with direct=" rather than pointing at the alias keyspace: an
  unresolved handle has no config to cross-reference, and nothing asks for it.
- No `[[host.accepted]]` entry for the echo-words regression below — only the maintainer
  adds one.

### Open divergence, for the maintainer

**On a stream, a budget expiry is checked before the acquisition, so the one pre-output
exhaustion refresh never fires when a lane blows the budget.** `ask` runs that refresh
one level up and does fire it, so the two surfaces disagree on the pool state that the
refresh exists for: a key landing mid-call after a lane has already spent the budget.
Measured on this tree with one keyed model that opens and then says nothing, `wait=0.3`,
and a refresh hook that adds a payable model — `ask` answers `'fresh'` in 0.31 s having
fired the refresh once and journaled `a ERROR`, `c OK`; the stream raises
`NoLLMAvailableError(reason="timeout")` in 0.30 s, having fired the refresh zero times
and journaled only `a ERROR`. The same run against `ab9cb45a7` gives the same two lines,
so this is **pre-existing** and not something this plan introduced — it is outside both
sections and is left untouched here. Repro:
`review7/r12_expiry_skips_refresh.py` (session scratchpad).

### Gate

`. ./activate.sh` before each.

- `invoke pre` — every hook "All checks passed!", pyrefly `0 errors (25 suppressed)`.
- `invoke test` — pass 1: `1808 passed`; pass 2 (`-p coarse_clock`): `1317 passed, 491
  deselected` (the docker tests the coarse pass skips). Zero failures, zero skips.
- `invoke downstream --source local` (both hosts' contract tests as committed, tree
  untouched for the whole run):

```
== dinary: ok ==
llmbroker baseline HEAD (ab9cb45a7) 1.10.3 -> candidate working tree 1.10.3
baseline: 1460 passed; type check: 0 errors
candidate: 1460 passed; type check: 0 errors
regressions (0):
type-check lines added (0):

== echo-words: FAIL ==
llmbroker baseline HEAD (ab9cb45a7) 1.10.3 -> candidate working tree 1.10.3
baseline: 847 passed; type check: 0 errors
candidate: 846 passed, 1 failed; type check: 0 errors
regressions (1):
  tests/test_llmbroker_contract.py::test_a_stream_every_pool_model_rate_limits_is_a_failure_and_not_a_budget_miss [failed]
      AssertionError: Regex pattern did not match.
        Expected regex: 'excluded'
        Actual message: 'the pool missed the answer budget (NoLLMAvailableError: every LLM
        this stream could use is cooling down)'
type-check lines added (0):
```

**dinary: no regression.** Same 1460 passed before and after, no type-check line added.

**echo-words: one regression, and it is an intended change** — a host test pinning the
report this plan replaces. `test_a_stream_every_pool_model_rate_limits_is_a_failure_and_not_a_budget_miss`
asserts that a stream over a pool where every model answers 429 ends as a `BackendError`
matching `excluded` and is *not* a `BudgetMissError`; its own comment states the reason
("a stream does not wait for a lane it has already tried: llmbroker ends it at once as
`excluded`"). The first half of that comment is still true here — the stream still ends
at once — and only the reason changed, which `llm_backend.pool_error` maps to
`BudgetMissError`, and that is exactly what makes echo-words step up to its paid model on
a streamed word. Host-side change: drop that test and extend
`test_a_rate_limited_pool_asked_for_a_whole_answer_is_a_budget_miss_once_it_waited` to
both adapters (`@pytest.mark.parametrize("adapter", ["whole", "streamed"])`); the
streamed case needs no short `POOL_WAIT_SECONDS`, since it does not wait. Nothing here is
llmbroker's to fix, and no host was touched.

### Files changed

Source: `src/llmbroker/broker/stream_owner.py`, `router.py`, `streaming.py`,
`aliases.py`, `catalog.py`, `broker.py`, `llms.py`, `pool_view.py`, `report.py`,
`src/llmbroker/models.py`.
Tests: `tests/test_router_stream.py`, `tests/test_direct_declaration.py`,
`tests/test_aliases.py`, `tests/test_broker_direct.py`, `tests/test_no_automatic_fetch.py`,
`tests/test_report.py`.
Specs: `specs/reference/rules/call-path.md`, `specs/reference/rules/direct-by-name.md`,
`specs/reference/decisions.md`.
Docs: `docs/src/en/{async,usage,direct}.md`, `docs/src/ru/{async,usage,direct}.md`.
