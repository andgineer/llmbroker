# One failover loop, one SSE reader

## Goal

`Router.chat()` and `Router.stream()` are the same failover algorithm written
twice with two different control protocols. The SSE reader is written twice as
well — once in the router, once in the direct client — including the error
message. Reduce each to one.

## Why

Invariants 10, 17 and 18 are enforced by the failover loop. Two copies mean a
fix to one is a silent divergence in the other. `router.py:238-289` and
`router.py:461-519` differ only in what an attempt produces and how a retry is
signalled: a return value (`_Failed | _BudgetExpired | None`) in one, a
purpose-built exception (`_StreamRetry`) in the other.

Blocked by `http-status-vocabulary` — `_classify_status` is rewritten there.

## Part 1 — one SSE reader

`router.py::_stream_deltas` (103-149) and `direct.py::AsyncDirectClient.stream`
(157-187) both run: error-floor check, `aiter_sse_chunks` loop,
`completions += "choices" in chunk`, `stream_delta`, and an identical
`InvalidProviderResponseError` whose message and `detail` are byte-identical
(`router.py:144-149` vs `direct.py:78-83`).

1. **`chat.py`** gains:

   ```python
   async def aiter_chat_chunks(resp: httpx.Response, model: str) -> AsyncIterator[dict]:
   ```

   Yields each decoded chunk that carries `choices`, and on exhaustion raises
   `InvalidProviderResponseError` if none did. The rule "what makes a body a
   chat-completion stream" lives here and only here. Status-floor checking stays
   with the callers: the router raises through `raise_for_status` into its
   classifier, the direct client raises a typed `ProviderError` — two different
   contracts over the same condition.

2. **`router.py::_stream_deltas`** keeps only what is the router's: the
   `asyncio.timeout` bound on the first delta, `bound.reschedule(None)` once it
   arrives, and `progress.usage = parse_usage(chunk) or progress.usage`.

3. **`direct.py`** drops `_invalid_stream` and the counting loop.

Two lines (`stream_delta`, `parse_usage`) remain at both call sites. That is
fine — they are reads, not the invariant.

## Part 2 — one failover loop

Preferred shape: the attempt is always an async generator, and one driver
consumes it.

1. **Delete `_StreamRetry`.** Both attempts settle their own slot and journal
   their own row (they already do, in `_dispose`), then report the same
   `_Failed | _BudgetExpired | None` verdict.

2. **`_attempt` becomes an async generator** that yields nothing and stores its
   `AsyncResult`, or — simpler and preferred — `chat()` is expressed as
   `stream()` over an attempt that yields a single terminal item. Pick whichever
   survives contact with the code; the requirement is that **one** method owns:
   - the `while True` acquire loop with `exclude=frozenset(client_failed)`,
   - `NoLLMAvailableError(reason="excluded")` → re-raise `last_client_error`,
   - `_BudgetExpired` → `last_client_error` if any, else
     `NoLLMAvailableError(reason="timeout")`,
   - `_Failed` → add to `client_failed`, remember `.error` when set.

3. If a single driver proves genuinely awkward (the generator has to yield
   *and* return a value), fall back to extracting the two decision helpers —
   `_next_candidate(...)` and `_after_outcome(...)` — leaving two thin shells
   that share every decision. **Say so in the handover if you take the
   fallback**: it is a weaker result, not a defect.

4. `stream()`'s post-first-delta behavior is unchanged: `_fail_stream` still
   raises `StreamInterruptedError` once `started`, and that branch must remain
   unreachable from the non-streaming path.

## Tests

- `tests/test_router.py`, `test_router_stream.py`,
  `test_router_degraded_transport.py`, `test_wait_budget.py` pass **unedited**.
  This plan is behavior-neutral; an edited assertion means a behavior change,
  so stop and report rather than adjusting the test.
- Add one test that a 200 whose body is not an SSE chat stream produces the
  same `InvalidProviderResponseError.detail` from the router and from
  `AsyncDirectClient` — the duplication this plan removes, pinned.

## Spec updates

None. `rules/call-path.md` already states failover, the first-delta boundary and
the error contract; this plan changes none of them.

## Gate

`invoke pre` clean, `python -m pytest` green with the pre-existing count plus
the one added test.
