# Every routed call hands back what answered it

**Depends on nothing; ships after `rating-by-key.md`** — that plan removes the
urgency this one used to carry, and the docs it rewrites are the ones this plan
touches.

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

`rating-by-key.md` removed the main reason: a host no longer needs `llm_name` to
rate a streamed call, it rates by its own `trace_id`. What is left is real but
smaller, and this plan should be judged on it alone:

- **`usage`** — token counts exist nowhere else in the caller's reach. Both shapes
  discard them entirely today.
- **Which model answered, without a journal read.** A status screen naming the
  model that served the last request should not have to query the journal for it,
  and on a non-queryable store it cannot.
- **Automatic raters that pass no `trace_id`.** They can rate from the handle
  directly rather than being forced to invent an id.

If none of those matter to the maintainer, this plan is droppable — the rule it
buys is consistency, not capability.

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

### 3. `AsyncLLMs.stream` returns the handle — `broker/llms.py:107`

Stops being an async generator; becomes a plain method that builds the receipt,
wraps the current generator body, and returns `StreamHandle`. The body is
unchanged, including the `_on_exhausted` second pass at line 140 — that retry stays
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
