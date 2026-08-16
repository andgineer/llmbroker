# Every routed call hands back what answered it

**Depends on nothing.** Rating by key has already shipped, which removed the
urgency this plan used to carry and rewrote the docs it touches.

## Goal

One flat rule:

> Every routed call returns an object naming the model that answered. Direct calls
> are outside this — they are not routed and not journaled.

`ask` and `chat` already do. Two shapes drop it at the boundary although the
router held it:

- **`stream()`** is an async generator yielding bare `str`.
- **`run_tool_loop` / `arun_tool_loop`** are annotated `-> str` (`chat.py:329`,
  `309`), discarding the result their own final `chat` call produced.

## Why still, now that rating takes a key

Rating by key removed the main reason: a host no longer needs `llm_name` to rate a
streamed call, it rates by its own `trace_id`. What is left is real but smaller,
and this plan should be judged on it alone:

- **`usage`** — token counts exist nowhere else in the caller's reach. Both shapes
  discard them entirely today.
- **Which model answered, without a journal read.** A status screen naming the
  model that served the last request should not have to query the journal for it,
  and on a non-queryable store it cannot.
- **Rating a streamed call when no `trace_id` was passed.** A delayed rating takes
  exactly one of the two keys and a stream hands back neither, so such a host must
  invent a key or look the id up in the journal — and a non-queryable store has no
  lookup at all, leaving the call unrateable.

If none of those matter to the maintainer, this plan is droppable. The first two
buy consistency; the third is the one capability nothing else reaches.

## The decision entry

Lands in `specs/reference/decisions.md` under "## The call path", in the same
batch as the behavior:

```markdown
### identity-rides-the-object-a-call-returns

Every routed call returns an object naming the model that answered; a stream names
it from the first delta on.

**Blocks:** yielding structured chunk objects instead of text deltas; an
`on_answered=` callback.
**Why:** what answered is a property of the call, not of a delta, so a per-chunk
wrapper charges every consumer per delta for something none reads more than once,
and a callback splits one call's outcome across two places the caller must rejoin.
A stream can carry it earlier than `ask` can: the call id precedes the request and
the model stops moving at the first delta, so the object is readable while the
answer is still arriving.
```

## Work order

### 1. The identity surface — `broker/result.py`

`AsyncResult` already exposes `llm_name`, `operation`, `call_id` and
`record_quality` over private attributes. Extract that surface into a small base in
the same module; `AsyncResult` inherits it and adds `text` and `tool_calls`.
`record_quality` keeps its current body and is written once.

Add `StreamHandle` beside it:

- `__aiter__` — yields `str` deltas from the wrapped generator, so
  `async for delta in llms.stream(...)` is unchanged.
- `aclose()` — forwards to the wrapped generator, so `aclosing(...)` and the slot
  ownership contract in `rules/call-path.md` hold unchanged. **Do not restate that
  contract here; the handle only forwards.**
- `llm_name` / `call_id` — `None` until the first delta, fixed from then on.
  Before it failover may still move, so an earlier value would name a model that
  did not answer.
- `operation` — known at construction.
- `usage` — `None` until the attempt completes.
- `record_quality(score)` — inherited; raises if no answering model is known yet.

### 2. The router publishes identity — `broker/router.py`

`_stream_attempt` (line 529) already keeps a per-attempt sideband,
`_StreamProgress` (line 105), set on the first delta. Give the stream path a
receipt object filled at that same point with `config.name` and `attempt.call_id`.

`_route` is generic over the attempt callable and shared with `chat`, so bind the
receipt into the existing `partial(self._stream_attempt, messages=messages)` in
`router.stream` (line 503) rather than widening `_route`'s signature.

### 3. `AsyncLLMs.stream` returns the handle — `broker/llms.py:122`

Stops being an async generator; becomes a plain method that builds the receipt,
wraps the current generator body, and returns `StreamHandle`. The body is
unchanged, including the `_on_exhausted` second pass at line 153 — that retry stays
inside the wrapped generator so a stream failing over after a pool re-read fills
the same receipt.

### 4. `AsyncBroker.stream` delegates — `broker/broker.py:381`

Returns the handle from `self.llms.stream(...)` directly; the `aclosing` re-wrap
goes away, the handle owning `aclose`.

### 5. The tool loop returns its result — `chat.py`

`arun_tool_loop` (line 309) and `run_tool_loop` (line 329) return the last
`result` — the `AsyncResult` / `Result` from the `chat` call that produced the
tool-call-free reply — instead of the extracted text.

**Scope of what it names:** the final call only. Earlier rounds are separate routed
calls, each journaled on its own row; `usage` is that final call's, not a sum over
the loop. Do not invent summing — the per-round numbers are in the journal, and a
host that wants the total reads them there. One docstring line says so.

`ToolLoopLimitError` is unchanged: a loop that never converges raises rather than
returning a result.

### 6. `sync.py` — no change beyond the loop

Streaming is async-only; the blocking façade has no `stream`. `Broker.ask`/`chat`
already return `Result`. Stated so the implementer does not go looking.

### 7. Docs

- `docs/src/{en,ru}/async.md` and `docs/src/{en,ru}/direct.md` ("Streaming from
  the pool"): the streaming examples gain the handle in the one place each where it
  changes what the reader would write.
- `docs/src/{en,ru}/tools.md`: the loop returns a result — `print(reply.text)` —
  and names the model that produced the final reply.
- `docs/src/{en,ru}/usage.md`, "Quality rating": the key forms stay the taught
  path; add one line that a handle also rates directly, for callers that have one.

Nothing about `direct(...)` changes anywhere.

Both languages in the same batch.

### 8. Specs

`specs/reference/rules/call-path.md`, "Streaming": one paragraph — a streamed call
names what answered it, unset until the first delta because failover moves until
then. Local to the call path, so not `invariants.md`.

### 9. Version

`invoke ver-feature` — the maintainer's, skipped by the implementer.

## Tests

`tests/test_router_stream.py` — its existing ownership tests
(`test_abandoned_stream_releases_the_slot` line 622,
`test_held_iterator_releases_the_slot_when_closed` line 648) must pass
**untouched**; that is the proof the handle did not disturb the contract.

- `test_stream_handle_names_the_model_that_answered` — matches the journaled `OK`
  row.
- `test_stream_handle_identity_is_unset_before_the_first_delta`
- `test_stream_handle_identity_survives_failover` — first candidate 429s before any
  delta, second answers; the handle names the second.
- `test_stream_handle_identity_matches_the_interrupted_stream_error` — extends the
  mid-stream-death scenario at line 242.
- `test_stream_handle_identity_stands_after_an_abandoned_stream`
- `test_stream_handle_reports_usage_after_completion`
- `test_stream_handle_records_quality` and
  `test_stream_handle_record_quality_before_an_answer_raises`

`tests/test_chat.py` (or the tool-loop tests beside it):

- `test_tool_loop_returns_the_final_result_not_text`
- `test_tool_loop_result_names_the_model_of_the_final_round` — earlier round on one
  model, final on another.

No test may use `pytest.skip`/`importorskip`.

## Gate

`invoke pre` clean and `python -m pytest` reporting `N passed` with zero failures,
errors or skips.

## Handover

**Done:** work order 1–8 and every test the plan names. Section 9 (`invoke ver-feature`)
is the maintainer's and was skipped.

**Gate:** `invoke pre` clean (ruff, ruff-format, docstring cap, pyrefly — 0 errors);
`python -m pytest` → **1388 passed**, zero failures, errors or skips.

### What differs from the plan

- **The shared base holds `usage` too**, not only `llm_name`/`operation`/`call_id`/
  `record_quality`. The plan says `AsyncResult` "adds `text` and `tool_calls`", which
  only works if `usage` is already below it, and `StreamHandle` needs the same field.
- **The base's `llm_name`/`call_id` are `str | None`; `AsyncResult` narrows both back to
  `str`.** A routed `ask`/`chat` always names its model — that is a guarantee, and
  widening it to `str | None` for the sake of a shared base would have made every caller
  branch on a case that cannot happen.
- **The receipt is filled through the per-attempt sideband, not beside it.** `_StreamProgress`
  now carries the receipt plus this attempt's identity and gains two methods — one for the
  first delta, one for the settled attempt — so the fill happens at exactly the two points
  the plan names without threading a second object through `_stream_deltas`.
- **`Router.stream`'s `receipt=` is optional**, defaulting to a throwaway. That is what
  lets the router-level tests — including the two ownership tests the plan requires to
  pass untouched — keep calling `router.stream(ring, messages)` unchanged.
- **The tool loops moved out of `chat.py` into `tool_loop.py`.** Annotating what they now
  return needs the sync `Result`, and `chat.py` cannot import it: `router.py` imports
  `chat`, so `chat → sync → broker.broker → llms → router → chat` closes a cycle that
  breaks a bare `import llmbroker`. The cycle was a symptom — `chat.py` holds the HTTP
  primitives `router.py` reaches for, while the loops sit *above* the broker and only ever
  call its `chat`. Out of that file they import `AsyncResult` and `Result` for real, with
  no `TYPE_CHECKING` block and no string annotations. `execute_tool_calls` went with them
  (it runs the host's own functions); `parse_tool_calls`, which reads the wire format,
  stayed. `tests/test_tool_loop.py` mirrors the split.
- **`StreamHandle` is exported from the top-level package**, next to `AsyncResult`: it is
  now a public return type a host may want to name.
- **One test outside the plan's list changed**: `test_router.py`'s
  `test_mixed_keyed_and_keyless_pool_routes_over_keyed_only` asserted on the private
  `result._llm_name`, which no longer exists; it now asserts on `result.llm_name`.

### Decisions taken during implementation

- **Rating a stream before its first delta raises `ValueError`**, not a new exception
  type. The nearest precedent is `record_quality()` called with both keys or neither,
  which is also a `ValueError`: both are the caller reaching for a call that is not there
  to name, and a host can test `handle.llm_name is None` rather than catch.
- **`usage` on the handle is filled where the attempt completes** (a real answer or a
  consumer that stopped pulling), not in a `finally`. A mid-stream death leaves it unset,
  matching "None until the attempt completes".

### Deliberately left out

- No summing of `usage` across tool-loop rounds — the plan forbids it, and the docs in
  both languages now say the counts are the final round's and the total is in the journal.
- Nothing about `direct(...)` changed, and the blocking façade has no `stream` to change.

## Review fixes

Two defects found reviewing this plan's implementation, both in the handle it added.
Fixed in one batch with the small cleanups below.

**Gate:** `invoke pre` clean; `python -m pytest` → **1392 passed**, zero failures,
errors or skips.

### 1. A rating could reach the store before the row it names

The handle let a stream be rated from the first delta on, but a stream's journal row is
written only when its answer ends. On `FileStore` the rating then landed *above* the call
row, and the single reverse pass that folds the two never saw it: the score vanished with
no error, and a pool rebuild re-derives from that same read, so it never came back. SQL
drivers fold with a correlated subquery and were unaffected — the loss was silent and
backend-dependent.

Rating now waits for the call to settle; before that it is a `ValueError` naming the fix.
The rationale, and the alternatives weighed against it, are in
`decisions.md#identity-rides-the-object-a-call-returns` — not repeated here.

Invariant 1 gained the clause the whole thing rested on and nobody had written down: a
rating is always appended after the call it names, and nothing inside llmbroker may offer
a host a way to rate a call that is not on a row yet.

### 2. An answer carrying no delta was left anonymous

`test_empty_completion_is_a_success_not_a_garbage_200` already fixed that an empty
streamed answer is a success and is journaled `OK`. Its handle never reached the
first-delta point, so it named nothing — while still reporting the token counts of the
call it would not name, and refusing to rate a call that had answered.

Identity is now filled at the settle point as well as at the first delta. Invariant 18 is
untouched: settle is only reached on the paths where the attempt answered, long after
failover could still move.

### The mechanism

The receipt gained a settled marker; `AsyncResult` builds one already settled, being by
definition a call that has finished. The streaming attempt has two fill points: the first
delta (identity, for display while the answer arrives) and settle (identity again, the
counts, the marker). The marker goes up **after** the journal write returns, not before —
otherwise a concurrent holder of the same handle could rate inside the window between the
two, which is the original defect in miniature.

### Also in this batch

- `tool_loop.py`: the two `# noqa: ANN001` suppressions went away and both public loops
  now type their broker. The cycle they cited stopped existing when the loops moved out of
  `chat.py` — in the commit that added them.
- A tool-loop test against a real broker rather than `MagicMock`: nothing proved that what
  comes back is the routed call itself, which is what `tools.md` now teaches.

### Tests

`test_stream_handle_record_quality_mid_stream_raises`,
`test_stream_handle_rating_survives_a_broken_off_stream`,
`test_stream_handle_names_an_answer_that_carried_no_delta`,
`test_tool_loop_over_a_real_broker_hands_back_the_routed_call` — the first three are the
repros of the two defects and stay in the suite.
`test_stream_handle_record_quality_before_an_answer_raises` changed only the message it
matches. Both slot-ownership tests still pass untouched.

### `Router.stream`'s receipt is required

It was optional, defaulting to a throwaway, so the router had a mode where it worked out
who answered a call and dropped it — a branch no production caller ever takes, since
`AsyncLLMs` always passes one. It is now positional and required.

The first justification for keeping it optional was that making it required would edit the
two slot-ownership tests this plan required to pass untouched. That reading was wrong
twice: the requirement was a checkpoint on the original batch, already delivered and
reviewed, not a permanent freeze; and the edit shortens those tests rather than disturbing
them. Six tests called `Router.stream` directly, repeating the same ring and message
literal; they now go through one `_stream(router, **kwargs)` helper, as `_drain` already
did. Both ownership tests keep their bodies, assertions and meaning — only the call form
changed.

### Left as it is

- `sync.Result` keeps its hand-written surface rather than sharing the base — a blocking
  wrapper cannot inherit an async one, and the win would be cosmetic.
- `RoutedCall` stays unexported. `AsyncResult | StreamHandle` covers naming it.
