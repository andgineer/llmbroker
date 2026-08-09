# A stream chunk that is an object but not a completion

## Goal

Plan 11 made `aiter_sse_chunks` yield only objects. A chunk that *is* an object
but whose `choices` do not hold the shape the extractor assumes still escapes the
pool as a raw `AttributeError`/`KeyError`: no failover, no typed error, no model
name. Close it at the one point both streaming consumers already pass through.

## Why

`rules/call-path.md` classifies a malformed response as the provider's fault at
both positions in a stream — before the first delta it cools the model and moves
to the next candidate, after it the model is cooled and the caller gets
`StreamInterruptedError` with the deltas already yielded standing. Neither
happens today, because the exception types the extractor raises are not in the
router's failover set.

Reproduced against the post-plan-11 tree. Every row is an HTTP 200 SSE body whose
chunk decodes to an object and carries `choices`, so plan 11's guard passes it
through and `aiter_chat_chunks` counts it as a completion:

| chunk | what reaches the caller |
|---|---|
| `{"choices": [{"delta": null}]}` | `AttributeError: 'NoneType' object has no attribute 'get'` |
| `{"choices": [null]}` | `AttributeError: 'NoneType' object has no attribute 'get'` |
| `{"choices": ["x"]}` | `AttributeError: 'str' object has no attribute 'get'` |
| `{"choices": {"delta": {"content": "hi"}}}` | `KeyError: 0` |

The first row is the one that makes this worth taking now: it is one `null` away
from an ordinary finish chunk. `{"choices": [{"delta": {}, "finish_reason":
"stop"}]}` and `{"choices": []}` are both handled correctly — the dict default in
the extractor only applies when the key is absent, so a present-and-null `delta`
is fatal where an absent one is not.

The non-streaming path has no such hole: `_parse_completion` catches a
deliberately broad set and re-raises `_invalid_body`. This plan gives the
streaming path the same fault line — anything raised while interpreting the
provider's bytes is the provider's fault, anything else is a bug — and inherits
the whole disposal, since `InvalidProviderResponseError` is already in
`_FAILOVER_ERRORS` and `_fail_stream` already splits on `progress.started`.

`decisions.md` records nothing on chunk-field validation; no recorded decision is
being re-proposed.

## Work order

### Batch 1 — `chat.py` and its two consumers

1. **Extract `_invalid_stream(model, detail)`** next to `_invalid_body`, building
   `InvalidProviderResponseError` with the message
   `f"{model}: HTTP 200 body is not an OpenAI-compatible SSE stream"`, `model=`,
   and `detail=detail[:_BODY_SNIPPET]`. Point `aiter_chat_chunks`'s existing
   exhaustion raise at it, passing its current
   `f"content-type={...!r}, no chat-completion chunks decoded"` as the detail.
   One message, two details; the truncation is new for that call site and only
   bites a pathological `content-type` header.

2. **Rename `stream_delta` to `_stream_delta`.** After step 3 the only correct
   way to read a chunk is through the guarded function, and a public unguarded
   extractor beside it is the trap plan 11 named in its own rejected alternative.
   No caller outside `src/` exists — `grep -rn stream_delta tests/ docs/` is
   empty. Its doctests survive the rename: `--doctest-modules` collects
   underscore-prefixed module-level functions (verified).

3. **Add `parse_stream_chunk(chunk: dict, model: str) -> tuple[str, Usage | None]`**:

   ```python
   try:
       return _stream_delta(chunk), parse_usage(chunk)
   except (ArithmeticError, AttributeError, KeyError, IndexError, TypeError, ValueError) as exc:
       raise _invalid_stream(model, str(chunk)) from exc
   ```

   Same caught set as `_parse_completion`, for the same stated reason. The cost
   is accepted knowingly: a genuine bug inside a four-line extractor is reported
   as a provider fault. Docstring stays inside the 3-line cap.

4. **`broker/router.py::_stream_deltas`** — replace the two per-chunk calls with
   `delta, usage = parse_stream_chunk(chunk, model)` followed by
   `progress.usage = usage or progress.usage`. Drop `parse_usage` and
   `stream_delta` from the import list (`parse_usage` has no other use in the
   module), add `parse_stream_chunk`.

5. **`direct.py::AsyncDirectClient.stream`** — `delta, _ = parse_stream_chunk(chunk, self._model)`.
   Same import swap. The usage a streaming direct call carries is still dropped;
   that is unchanged.

6. **Verify, do not change:** `parse_usage` already returns `None` for a non-dict
   `usage`, and `_parse_completion` already covers every shape in the table above
   on the non-streaming path.

### Rejected alternatives

- ***Make the extractor defensive — return `""` for a shape it does not
  understand.*** A chunk carrying `choices` is counted as a completion by
  `aiter_chat_chunks`, so a body made entirely of malformed chunks would end as a
  **successful empty answer**: no failover, no error, silently wrong. Skipping is
  safe only for a payload that was never counted, which is exactly why plan 11
  could skip and this plan cannot.
- ***Skip the malformed chunk and do not count it.*** Before the first delta it
  buys nothing — raising costs no answer, because failover delivers one from a
  sibling. After the first delta it silently truncates, where raising gives the
  caller `StreamInterruptedError` and the deltas already yielded. `call-path.md`
  already classifies a malformed response as a failure rather than something to
  step over.
- ***Guard at the two call sites instead of in one function.*** Writes the same
  broad catch twice and leaves the next consumer with none.

## Tests

Every new routed test must be shown to fail on the pre-fix tree (stash the
`chat.py` change and re-run), as plan 11's did.

`tests/test_router_stream.py`:

- `test_malformed_chunk_shape_is_a_garbage_200`, parametrized over the four
  chunks in the table (ids `delta-null`, `choice-null`, `choice-not-an-object`,
  `choices-not-a-list`). Model `a` serves the malformed body, `b` a good one:
  deltas come out as `["ok"]`, the journal reads `[("a", ERROR), ("b", OK)]`,
  `pool.state("a").phase is COOLING`, and `AsyncDirectClient.stream` against `a`
  raises `InvalidProviderResponseError`. Mirrors
  `test_non_object_sse_payload_is_a_garbage_200`.
- `test_a_malformed_chunk_after_the_first_delta_interrupts_the_stream` — body is
  one good delta chunk followed by `{"choices": [{"delta": null}]}`. Single-model
  pool: `StreamInterruptedError` reaches the caller, the delta already yielded
  stands, one journal row with `status ERROR` and a non-null `cooldown_until`,
  `pool.state("a").phase is COOLING`. This is the position where the rejected
  "skip it" alternative would differ, so it is the test that pins the choice.
- `test_a_finish_chunk_with_no_choices_is_not_malformed` — a body whose first
  chunk is `{"choices": []}` and whose last carries `usage` still streams and
  journals `OK` with the usage recorded. The guard must not fire on the ordinary
  preamble/finish shapes.

`tests/test_chat.py`:

- `parse_stream_chunk` on a well-formed chunk returns the delta and the `Usage`;
  on `{"choices": [], "usage": {...}}` returns `("", Usage)`.
- `parse_stream_chunk` on each malformed shape raises
  `InvalidProviderResponseError` carrying `model=` and the chunk in `detail`,
  with the original exception as `__cause__`.
- A malformed chunk far longer than `_BODY_SNIPPET` gets a `detail` capped at it.

Unchanged and must stay green without edits:
`test_garbage_200_reads_the_same_from_the_router_and_the_direct_client` — both
paths still raise through the same two functions, which is the property it pins.

## Spec updates

None. `rules/call-path.md` already states both halves of the rule this makes the
code obey: a 200 whose body is not an OpenAI-compatible chat completion is the
provider's fault, and for a stream every failure before the first delta cools and
fails over while a mid-stream death cools and raises. Writing it a second time
would put the same rule in two places.

No `decisions.md` entry: the contested call — raise rather than skip — is decided
by the spec sentence above, not by this plan, and the alternatives are recorded
here as the route they are.

## Gate

`invoke pre` clean, `python -m pytest` green with the pre-existing count plus the
added tests, zero skips.

## Handover

**Done, as written.** Every step of the work order, both test files, no spec
change (the plan asked for none, and `rules/call-path.md` already carries the
rule).

- `chat.py`: `_invalid_stream(model, detail)` added beside `_invalid_body`;
  `aiter_chat_chunks`'s exhaustion raise now goes through it, so the two details
  share one message. `stream_delta` → `_stream_delta` (its doctests are still
  collected under `--doctest-modules`). `parse_stream_chunk(chunk, model)` wraps
  the extractor and `parse_usage` in `_parse_completion`'s catch set.
- `broker/router.py::_stream_deltas` and `direct.py::AsyncDirectClient.stream`
  each call it once; `parse_usage`/`stream_delta` are out of both import lists.
- Step 6 verified, nothing changed: `parse_usage` returns `None` for a non-dict
  `usage`, and `_parse_completion` already caught every shape in the table on the
  non-streaming path.

**Nothing done differently, nothing left out.** No decision the plan did not
already make came up during implementation.

**Repro shown, as required.** With `chat.py`, `router.py` and `direct.py`
stashed, the five new routed cases fail on the pre-fix tree — the four
`test_malformed_chunk_shape_is_a_garbage_200` ids with the raw
`AttributeError`/`KeyError` the plan predicted, and
`test_a_malformed_chunk_after_the_first_delta_interrupts_the_stream`.
`test_a_finish_chunk_with_no_choices_is_not_malformed` passes on both trees by
design: it pins that the guard does *not* fire on the ordinary preamble and
finish shapes, so it is a regression guard rather than a repro.

**Gate:** `invoke pre` clean (ruff, ruff-format, docstring cap, pyrefly 0
errors); `python -m pytest` → **1304 passed**, zero skips, zero errors — 1291
before, plus 13 (6 routed, 7 in `test_chat.py`).
