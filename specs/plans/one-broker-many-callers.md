# One broker, many callers

A server builds the broker once and hands each request a caller, instead of
building a broker per request or per user.

## Goal

**The broker is the installation; the object a request holds is a caller.**
Ports, pool, learning, refresher and the HTTP client are provisioned once per
process and live as long as it does. `scope` stops being a constructor argument
and becomes what distinguishes one caller from another: the key it pays with and
the attribution on the journal row it writes. Creating a caller performs no I/O.

**What this removes.** Today the documented multi-user shape is
`AsyncBroker(dsn, scope=user_id)` per request, so every request re-reads the
registry, re-resolves every key, reads 300 journal rows, and opens its own HTTP
client — and every call site repeats the whole ten-argument constructor,
`direct=` included. All of it happens once, at startup, next to where the
application builds its database engine.

**What this fixes as a consequence.** `parallel` is a per-slot counter, so N live
brokers over one process allow a provider N times its cap; with one pool there is
one counter. The key probe behind a sync currently sees a scoped broker's own
refs and therefore finds nothing; the broker is now unscoped, so it probes the
shared keys as `sync-merge.md` describes.

## Not in this plan

The secrets cache. A caller built per request re-resolves its own scoped ref on
every request, which is what a per-request broker already does today, so nothing
regresses — but a metered secrets backend still pays per request until the plan
that follows this one lands. Propagation of another process's edits is plan 23's
subject and is untouched here.

## Why

One entry, to land in `decisions.md` verbatim. This plan argues nothing else
about it.

### the-broker-is-the-installation-a-caller-is-a-scope

A broker owns the installation: the ports, the pool, everything learned, the
lineup clock, the HTTP client. What a request holds is a caller — the scope it
writes on its journal rows and the keys it may pay with, over that one shared
pool.

**Blocks:** `scope` as a constructor argument; a broker built per request or per
user; a pool, learner or HTTP client per scope; `scope` as a per-call argument on
the broker.
**Why:** everything a broker owns is installation-global by invariant 16, so a
second broker for a second user duplicates every read and every connection while
duplicating no state that differs. It also breaks what the pool is for: slot
counters are what hold a provider to its `parallel` cap, and one counter per user
is not a cap. Scope reaches exactly two things — which key pays and what the
journal row is attributed to — and both are properties of the caller, not of the
installation, which is why they are the only two things a caller carries. Passing
`scope` per call instead would put a key resolution in the middle of every call
signature and leave the pool unable to tell whose key it is holding.
**Accepted cost:** two objects where hosts previously had one, and a caller
resolves its own scoped ref the first time it routes.

## Work order

Four batches. `. ./activate.sh` first; `invoke pre` and `python -m pytest` green
after each.

### 1. The key ring, and a pool that holds no keys

- New `broker/keyring.py`. `KeyRing` resolves `api_key_ref` → key for one scope,
  lazily and once, over `resolve_key` (`catalog.py:43`), with an optional
  fallback ring for the shared resolution: `resolve(ref)`, `get(ref)` for what is
  already held, `forget(ref)` for a key a 401 disproved, `set(ref, value)` for a
  resolution the process itself just wrote.
- `broker/pool.py`: `_Slot.key` goes. `LLMPool.add(cfg, order=...)` loses the key
  argument, `has_key` and `resolved_key` go with it, and `acquire` takes
  `payable: frozenset[str]` — the names the caller can pay for — which replaces
  `s.key is not None` in the candidate filter and drives the `no_keys` branch of
  `_exhaustion_reason`.
- `broker/catalog.py`: `_reconcile` fills the shared ring instead of handing keys
  to `add`, and resolves per distinct `api_key_ref` rather than per entry — with
  the key out of the slot there is nothing per-entry left to fill. `_measure` and
  `_report_missing_keys` read the ring.
- The pool's docstring says "config, key, cooldown, quality"; drop the key.

### 2. The caller, and a router that takes one

- New `broker/llms.py`. `AsyncLLMs` holds the router, the pool, its own `KeyRing`
  and its scope, and carries `ask`, `chat`, `stream`, `direct`, `get`, `count`,
  `record_quality`. It is constructed by the broker only.
- `Router` loses `_scope`. `ask`/`chat`/`stream` take the caller; `_new_attempt`
  reads the key from the caller's ring instead of `pool.resolved_key`, and
  `_record` writes the caller's scope on the row. One `httpx` client stays on the
  router, shared by every caller.
- Before `acquire`, the caller resolves what it can pay for over the pool's
  current members — one awaited step, a dict lookup for the unscoped caller, and
  for a scoped one at most one lookup per ref it has not yet seen.
- A 401 drops the key that paid for the attempt from the ring that handed it
  over, so the next resolution reads the store again. It happens where the
  attempt failed, which is the only place that knows both the caller and the
  status; the learner sees a call record and no longer knows whose key it was.

### 3. The broker surface

- `AsyncBroker.llms` — the unscoped caller, built once. `for_scope(scope)` — a
  new caller over the same pool and router; the empty string is refused here,
  where `_check_broker_args` used to refuse it. The **ring belongs to the broker,
  keyed by scope**, and the caller only points at it: callers are built per
  request, so a ring built per caller would resolve every key on every request
  and leave the cache plan behind this one nothing to hold. The map is bounded —
  a process serving many users must not accumulate a ring per user forever.
- `scope=` goes from both constructors, and `KeyProbe` is built unscoped.
- The broker keeps `ask`/`chat`/`stream`/`direct`/`get`/`count`/`record_quality`
  as delegation to `self.llms`: the one-liner in `mission.md` requirement 6 is
  `Broker().ask(...)`, and nothing about it may grow a second noun.
- `sync.py`: `LLMs` mirroring `AsyncLLMs` through `_run`, plus `llms` and
  `for_scope` on the sync `Broker`. `direct()` moves with the rest, and the
  mirror submits it to the loop thread like every other method — the private
  reach into the async side that `direct-client-seam` records must not be carried
  across into the new object.
- `__init__.py` exports `AsyncLLMs` and `LLMs`.

### 4. Docs and specs

- `docs/src/en/server.md` and the Russian copy — the multi-user section is
  rewritten around four examples: a script with no database; a long-lived process
  with one; a FastAPI cluster with shared keys; the same with per-user keys, where
  only the dependency differs. The lifespan example builds the broker where the
  application builds its database engine, and the handler receives `llms`.
  Say what a caller costs to create (nothing) and what it holds (its scope and
  its keys), and that admin verbs are on the broker on purpose. The propagation
  paragraph plan 23 puts on this page stays — re-word its bound to the process,
  which is what one broker per process makes it.
- The specs named below, in this batch.

## Tests

`tests/test_callers.py`, new:

- Two callers over one broker with different scoped keys route on their own key
  and write their own scope on the journal row.
- A caller whose scoped ref resolves to nothing falls back to the shared key; one
  where neither resolves cannot route that model, while another caller still can.
- Creating a caller performs no registry, store or secrets read — a counting
  triple of ports, asserted across many `for_scope` calls.
- `parallel=1` holds across two callers: the second call queues instead of
  running, which is the case a broker per user got wrong.
- One HTTP client for many callers.
- A dead key drops that caller's resolution only: the other caller's key on the
  same ref survives.

`tests/test_pool*.py` — `add` without a key, and `acquire` filtered by `payable`,
including the `no_keys` exhaustion reason when it is empty.

Existing tests: `scope=` appears in 103 places across 15 files; each becomes a
caller from an unscoped broker. Grep rather than trusting that count.

## Spec updates

- `decisions.md` — the entry above, verbatim.
- `rules/journal.md`, "One tail read derives everything" — plan 23 leaves the
  bound stated per live broker instance, because a host could hold many. With one
  broker per process it is the process again, and the sentence says so.
- `rules/journal.md`, "Per-user scoping" — "A broker instance is one scope's
  view" becomes the caller; the sentence that a sync here probes almost nothing
  goes, since the broker that probes is unscoped and sees the shared keys. The
  rest of the section is unchanged: scope still reaches secret refs and journal
  attribution only.
- `rules/sync-merge.md` — one clause where it explains what the probe can see:
  the probe is unscoped.
- `rules/call-path.md` — the `parallel` cap is per pool, and there is one pool
  per process.
- No new invariant. Invariant 16 already says the registry and everything learned
  are global; this plan is what makes the object model match it.

## The queue

Independent of plan 23, which it neither blocks nor is blocked by: 23 changes
when a rebuild runs, this changes who holds the pool it rebuilds. Taking 23 first
keeps its diff small.

It writes user-facing strings in the docs only, in plan 10's wording, so it does
not lengthen that plan's inventory.

The secrets cache is written against this plan's result and follows it directly.

## Gate

`invoke pre` clean and `python -m pytest` green.
