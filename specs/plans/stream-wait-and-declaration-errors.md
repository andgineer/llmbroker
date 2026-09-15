# Plan — a stream waits like a call, and a paid-model error stays with that model

**Status: source-bound on `2c6f91f6a`** (plus the uncommitted `invoke downstream` tooling).
Two fixes, one release. No schema change. One public addition: a snapshot property.

Downstream: echo-words streams every word from the pool and steps up to a paid model on a
missed budget; its contract tests (uncommitted in its working tree) found both defects.
dinary is unaffected by either.

## 1. Before its first delta, a stream waits within `wait` as a routed call does

**Now.** Two keyed models, both answering 429 with `Retry-After: 2`, `wait=8`, measured on
`2c6f91f6a`, with and without `fastest_of=2`:

| call | outcome | provider attempts (s) |
|---|---|---|
| `ask` | `NoLLMAvailableError(reason="timeout")` after 8.0 s | 0, 0, 2, 2, 6, 6 |
| `stream` | `NoLLMAvailableError(reason="excluded")` after 0.0 s | 0, 0 |

With 500 instead of 429: `ask` times out at 8.0 s, `stream` raises `excluded` at 0.0 s.
Both contradict `rules/call-path.md`: "Every failure before the first delta — 429, 5xx … —
cools the model and moves to the next candidate … there is no second failure surface for
streams", and the `wait` rule ("`None` waits as long as at least one model can still come
back by itself — a cooldown expiring"). `excluded` is documented as "no model can be used
for this request"; a cooling model can.

Effect downstream: echo-words reads `excluded` as a configuration fault and does not step
up, so a rate-limited free pool fails a streamed word at once, where the same pool on `ask`
waits out the budget, times out and steps up.

**Why.** `router.py::Router._acquire` excludes `call.attempted` whenever `eligible_names`
is passed, and a stream always passes it (`stream_owner.py::StreamOwner._acquire` →
`backend.acquire(call, deadline, payable, frozenset(self._eligible_names))`). So after
every captured model has been attempted once, `Pool.acquire_many` has no candidates and
raises `excluded` instead of waiting on their cooldowns. The routed call passes no
`eligible_names` and waits. The rule behind the stream's exclusion is in "Continuing after a
complete answer": "The candidate set and attempted names belong to the whole handle. A model
is tried at most once" — written for the alternatives a handle returns, and applied to the
initial routing as well.

**Do:**

1. The initial acquisition of a stream (`StreamOwner._acquire(initial=True)`, reached when
   no lane of the current race is live) stays restricted to the captured set but excludes
   only what a routed call excludes — `call.client_failed` — so it waits on cooling
   candidates within the deadline exactly as `Pool.acquire_many` does for `ask`, and
   reopens them when their cooldown ends. Keep `call.attempted` exclusion for everything
   that must never wait or must never repeat a model: `Router._untried` (refilling an
   emptied lane while other lanes race) and every continuation after a complete answer.
   Find the smallest change that separates "restrict to the captured set" from "exclude
   what this handle attempted" at the `Router._acquire` call site; do not add a mode flag
   to the public API.
2. When the deadline expires before any delta, the stream raises
   `NoLLMAvailableError(reason="timeout")` with `retry_at` by the same rule `ask` uses
   (`Router._expired`: a stored client error still wins). `excluded` remains what it is for
   a routed call: every candidate excluded by a client error.
3. `wait=None` and `wait=0` on a stream keep the meanings the `wait` section gives them for
   a routed call; verify each against `ask` in a test rather than assuming.
4. A model retried after its cooldown is a new attempt: new row, new slot, same failure
   classification. The captured set does not grow, so "a later pool refresh cannot admit
   an endless sequence of new names" still holds.

**Do not:** change continuation (`another()`), the race's visible-lane selection, or the
alternatives contract (`decisions.md#streamed-alternatives-live-until-close`); retry a model
after its first delta (invariant 18); make `_untried` wait.

**Tests** (`tests/test_router_stream.py`, mock transport, no sleeps racing real time —
drive cooldowns with the existing clock/time helpers the router tests use):
- all candidates 429 with a short cooldown inside `wait`: the stream waits, the retried
  model answers, deltas arrive; same with `fastest_of=2`;
- all candidates 5xx, cooldown longer than `wait`: `reason="timeout"` at the deadline
  (within `CLOCK_SLACK`), `retry_at` set, no `excluded`;
- all candidates answer 400: the stored client error is raised, unchanged;
- `wait=None`: waits for the cooldown and answers; `wait=0`: same outcome and reason as
  `ask(wait=0)` in the same pool state;
- a continuation after a complete answer still never reopens a model the handle attempted;
- a raced stream whose hidden lane fails refills only from untried models and never waits.

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

- `rules/call-path.md`, "Continuing after a complete answer": the at-most-once rule applies
  to the answers a handle returns and to refilling a race; before its first delta a stream
  waits on cooling candidates within `wait` like a routed call. "Streaming": state that
  expiry before any delta is `timeout`.
- `rules/direct-by-name.md`: replace "Only the first resolution may fail … raises at
  provision" with the handle-scoped rule; `snapshot()` reports unresolved handles.
- `decisions.md`: add both entries below verbatim.
- `docs/src/en/async.md` / `usage.md` (and `ru`): a stream's pre-output failure behaviour
  matches `ask`; `docs/src/en/direct.md` (and `ru`) "Errors": an unknown alias fails only
  `direct()` on it and appears in `snapshot().direct_unresolved`.

### Entry: `a-stream-waits-before-its-first-delta`

**Blocks:** a stream trying each captured model at most once before any output, and
raising `excluded` when all of them have failed.
**Why:** before the first delta nothing has reached the caller, so a stream has no reason
to give up sooner than a call: the same pool state must not time out and step up on `ask`
and fail at once on `stream`. At-most-once protects what a handle returns — its answers and
the lanes racing beside the visible one — and the captured set still bounds the names a
stream can reach, so waiting on a cooldown admits nothing new.

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
