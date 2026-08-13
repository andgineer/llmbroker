# Rating a call by an id the host already has

**Depends on `journal-lookup-keys.md`** — it supplies the `trace_id` / `call_id`
lookup this plan resolves through. Ship in that order.

## Goal

`record_quality` accepts the id the host is already holding:

```python
await broker.record_quality(0.0, trace_id="req-123")   # the host's own id
await broker.record_quality(0.0, call_id=call_id)      # llmbroker's, off a handle
await broker.record_quality(0.0, llm_name=n, operation=o)   # the primitive
```

Today only the third form exists, so every host that wants to rate anything later
must persist `llm_name` — a provider-internal string like
`groq-gpt-oss-120b` that means nothing to it, that it never asked for, and that a
re-resolved alias can change under it. `no-llm-judge` records that ratings are
host-supplied and names the signals a host actually has — whether the JSON parsed,
whether extraction validated, whether the user accepted the answer. None of them
arrives carrying a model name.

The explicit triple stays, demoted to a primitive: it is the only form that works
without a queryable store and after retention has purged the call row.

## What this does not change

The stored quality record stays exactly as `self-contained-quality-records`
requires — `(llm_name, operation, score)`, never joined to the call it rates during
recomputation. Resolution happens **once, at write time**, and produces that same
row. Nothing downstream learns that a key was involved.

## The decision entry

Lands in `specs/reference/decisions.md` under "## Learning and quality", after
`self-contained-quality-records`, in the same batch as the behavior:

```markdown
### a-rating-is-keyed-by-what-the-host-holds

A rating names the call by the id the host already has — its own `trace_id`, or the
`call_id` llmbroker handed it — and llmbroker resolves the model at write time.

**Blocks:** requiring `(llm_name, operation)` from every host that rates anything,
which is what a ban on resolving the key against the journal amounts to.
**Why:** the ban was taken to protect ratings arriving after retention purged the
call row, but the window they would land in holds the last 30 ratings per (model,
operation) and is rebuilt from a 300-row tail, against a 90-day retention. On any
installation with traffic, such a rating lands in a window that has forgotten
everything around it — a case worth little, charged to every host on every call.
Resolution at write time leaves `self-contained-quality-records` intact: the stored
row is the same self-contained triple, and recomputation still joins nothing. The
primitive form remains for the cases resolution genuinely cannot serve — a
non-queryable store, and a call the journal no longer holds.
```

## Semantics to get right

**A rating is not per-call today and does not become so.** It folds into a
`(model, operation)` window; which call produced it is stored nowhere and read by
nothing. So a key that selects several calls is not ambiguous — it is a set.

- **`trace_id`** → every **answering** row under that trace. Failover writes a row
  per attempt and a host may reuse one trace across several calls in one request;
  both mean the same thing here — those models served this request. One quality row
  is written per resolved call, each naming its own model.
- **`call_id`** → exactly one row.
- **Answering** means `kind="call"` and `status is CallStatus.OK`. An attempt that
  was rate-limited produced no answer to judge; its failure is already learned
  through cooldown and status. This filter lives inside llmbroker — it is precisely
  what a host should never have to write.
- **Nothing resolved** → raise, never silently no-op.
- **Bounded read.** Resolution reads the trace with a fixed row bound; a read that
  comes back full logs a warning naming the trace, and rates what it found. A trace
  with more attempts than that bound is pathological, not a supported shape.

## Work order

### 1. The new signature — `broker/llms.py:217`

`AsyncLLMs.record_quality` becomes:

```python
async def record_quality(
    self,
    score: float,
    *,
    trace_id: str | None = None,
    call_id: str | None = None,
    llm_name: str | None = None,
    operation: str | None = None,
) -> None:
```

Exactly one key form is required: `trace_id`, or `call_id`, or `llm_name` (with
`operation` optional beside it, since `None` is a real operation bucket). Zero or
more than one → `ValueError` naming the three forms. This reorders the positional
arguments; no compatibility shim, per the architecture note in `CLAUDE.md`.

Resolution path: `self.calls(limit=..., kind="call", trace_id=…|call_id=…)` — the
scoped read, so a scoped caller resolves its own rows — then keep the `OK` rows and
write one quality record per row through the existing store call, which already
carries `scope=self.scope` and folds into the learner.

### 2. The lookup failure — `exceptions.py`

Add `UnknownCallError(LLMBrokerError)`: the journal holds no answering call for
that key. `LLMBrokerError` is the storage-side base ("a lifecycle failure —
provisioning or storage, not one request"), which is what this is. Export it from
`llmbroker/__init__.py` beside its siblings.

A non-queryable store surfaces the `TypeError` `calls()` already raises — the same
message pointing at a queryable backend. Do not catch and re-wrap it: the fix is
the host's store choice, not its key.

### 3. Pass-through

- `src/llmbroker/broker/broker.py:411` — `AsyncBroker.record_quality`, same
  signature, delegating to the unscoped caller.
- `src/llmbroker/sync.py:160` and `:278` — `LLMs.record_quality` and
  `Broker.record_quality`, same signature without `await`.

`AsyncResult.record_quality(score)` and its sync twin are unchanged: the handle
holds the triple and calls the store directly, resolving nothing.

### 4. Docs

`docs/src/{en,ru}/usage.md`, "Quality rating" → "Rate it later": the section
currently teaches persisting `reply.llm_name`. Rewrite it around the key forms —
pass your own id as `trace_id` at call time and rate by it later, or keep the
`call_id` off the handle if you have no id of your own. Show the triple last, as
the primitive, with its two conditions (queryable store; call still in the
journal). Say that one `trace_id` covering several calls rates them all, and that
failed attempts are not rated.

Also note that a scoped caller must rate through the same scope
(`broker.for_scope(x).record_quality(...)`): the scope comes from the caller
object, not from the key, so a rating sent through the bare broker lands unscoped.

Both languages in the same batch.

### 5. Specs

`specs/reference/rules/backends.md`, "The journal": one paragraph — a rating may
name its call by `trace_id` or `call_id`, resolved once at write time to the
answering rows, with the stored record unchanged. It belongs with the journal, not
in `invariants.md`, being local to that subsystem.

### 6. Version

`invoke ver-feature` — the maintainer's, skipped by the implementer.

## Tests

New file `tests/test_rating_by_key.py`:

- `test_rating_by_call_id_names_the_model_that_answered`
- `test_rating_by_trace_id_rates_every_answering_call` — one trace over two calls
  to different models; two quality rows, one per model.
- `test_rating_by_trace_id_skips_failed_attempts` — a trace whose first attempt
  was rate-limited and whose second answered; exactly one quality row, naming the
  second.
- `test_rating_by_an_unknown_key_raises` — `UnknownCallError`, for both key kinds.
- `test_rating_requires_exactly_one_key_form` — parametrized over: no key, two
  keys, all three.
- `test_rating_by_key_folds_into_the_optimizer_window` — the resolved rating moves
  `quality_score` the same way the explicit form does.
- `test_scoped_caller_resolves_and_writes_its_own_scope` — two scopes hold rows
  under the same trace; each rates only its own.
- `test_rating_by_key_on_a_non_queryable_store_raises_typeerror` — `InMemoryStore`.
- `test_explicit_triple_still_works_without_a_queryable_store` — the primitive's
  reason for existing.

In `tests/test_score_validation.py`, extend the existing broker-level pair (lines
35, 45) so score validation is checked on a key form too — the score is validated
before any lookup.

No test may use `pytest.skip`/`importorskip`.

## Gate

`invoke pre` clean (ruff, ruff-format, pyrefly, docstring cap) and
`python -m pytest` reporting `N passed` with zero failures, errors or skips.
