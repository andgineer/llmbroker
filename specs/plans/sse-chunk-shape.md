# A decoded SSE payload that is not an object

## Goal

A streaming body whose `data:` payloads decode to JSON scalars or arrays escapes
the pool as a raw `AttributeError`/`TypeError` instead of being classified as a
garbage 200. Close it, and close the two test gaps the plan-2 review found next
to it.

## Why

`rules/call-path.md` is explicit: an HTTP 200 whose body is not an
OpenAI-compatible chat completion surfaces as `InvalidProviderResponseError`,
cools the model and fails over, so *a caller never receives a raw transport or
parsing error from a pool call while another model could still answer*. The
non-streaming path obeys this — `chat.py::_parse_completion` catches a
deliberately broad exception set and re-raises `_invalid_body`. The streaming
reader has no equivalent guard.

Reproduced against both `HEAD` (`8d0d0463`) and the post-plan-2 tree, so this
predates plan 2 and was only relocated by it:

| body | what reaches the caller |
|---|---|
| `data: [1, 2, 3]\n\ndata: [DONE]\n\n` | router: `AttributeError: 'list' object has no attribute 'get'` — from `parse_usage`, uncaught, no failover even with a healthy sibling in the pool |
| `data: 5\n\ndata: [DONE]\n\n` | `AsyncDirectClient.stream`: `TypeError: argument of type 'int' is not iterable` — from the `"choices" in chunk` membership test |

Neither type is in the router's failover set, so both take the "an unexpected
exception is a bug" path: the slot is released and the row journaled, but the
caller gets the raw error and the second model is never tried.

`decisions.md` records nothing on chunk-shape validation; no recorded decision
is being re-proposed.

## Work order

1. **`chat.py::aiter_sse_chunks` skips a payload that decodes to anything but an
   object.** It already skips payloads that do not decode at all, and a JSON
   scalar or array is not a chat-completion chunk by the same standard. One
   `isinstance(..., dict)` test at the single point where payloads are decoded
   makes every consumer safe at once — the membership test, `parse_usage` and
   `stream_delta`.

   Consequence, and the reason this is the right layer: a body made *entirely*
   of such payloads then decodes no chunk carrying `choices`, so
   `aiter_chat_chunks` raises `InvalidProviderResponseError` on exhaustion — the
   verdict `call-path.md` already prescribes, reached through the path that
   already exists, with no new error surface.

   *Alternative, rejected:* guarding inside `aiter_chat_chunks` instead. It
   leaves `aiter_sse_chunks` handing a non-dict to any future caller, and the
   docstring already promises decoded *objects*.

2. Confirm the non-streaming path needs nothing: a top-level JSON array reaches
   `_parse_completion`, whose `TypeError` branch already produces
   `_invalid_body`. Verify, do not change.

## Tests

- A streaming 200 whose payloads are `5`, `"hi"` and `[1, 2, 3]`: the router
  cools the model, fails over to a healthy sibling and journals the
  `no chat-completion chunks decoded` detail; `AsyncDirectClient.stream` raises
  `InvalidProviderResponseError`. Parametrize over the three shapes.
- A mixed body — one real completion chunk plus one scalar payload — still
  yields the delta and is journaled `OK`. The guard must not turn a working
  stream into a failure.
- **Gap found in review, unrelated to the guard:** no test covers *every
  streaming candidate rejecting the request with a 4xx*, where
  `client-4xx-never-cools` requires the provider's own error to be re-raised
  rather than `NoLLMAvailableError`. Verified by hand during the plan-2 review;
  the branch is shared driver code now, so it is cheap to pin. Add it to
  `tests/test_router_stream.py`.
- **Tighten the plan-2 pinning test:** it compares only the `detail` of the
  router's and the direct client's `InvalidProviderResponseError`, and the detail
  carries no model name, so it would still pass if one side alone began
  including one. Compare the rendered message too — "byte-identical" was the
  property plan 2 set out to pin.

## Spec updates

None. `rules/call-path.md` already states the rule this makes the code obey.

## Gate

`invoke pre` clean, `python -m pytest` green with the pre-existing count plus the
added tests.

## Handover

**Done, as written.** Work order steps 1 and 2, and all four test items.

- **Step 1** — `chat.py::aiter_sse_chunks` now decodes into a local and yields
  only when the result is a `dict`; a scalar or array payload is skipped by the
  same rule that already skipped an undecodable one. Docstring updated to say so.
  No other module changed: `aiter_chat_chunks`, `parse_usage`, `stream_delta`
  and both consumers are safe through that single point.
- **Step 2** — verified, unchanged. `_parse_completion` raises
  `InvalidProviderResponseError` for a top-level array, number and string alike
  (the `TypeError` branch), so the non-streaming path already obeys the rule.

**Tests** (`tests/test_router_stream.py`, +5):

- `test_non_object_sse_payload_is_a_garbage_200`, parametrized over `5`, `"hi"`
  and `[1, 2, 3]` — router cools `a`, fails over to `b`, journals
  `no chat-completion chunks decoded`; `AsyncDirectClient.stream` raises
  `InvalidProviderResponseError` with the same detail. All three fail on the
  pre-fix tree (`AttributeError`/`TypeError` escaping raw), confirmed by
  stashing the `chat.py` change.
- `test_a_non_object_payload_among_real_chunks_leaves_the_stream_working` — the
  mixed body still yields its delta and journals `OK`.
- `test_stream_reraises_the_provider_error_when_every_candidate_rejects_the_request`
  — the 4xx gap the plan-2 review found: `httpx.HTTPStatusError` reaches the
  caller instead of `NoLLMAvailableError`, neither model cooled.
- `test_garbage_200_reads_the_same_from_the_router_and_the_direct_client`
  tightened to compare the rendered message as well as the detail.

**Decision taken during implementation, which the plan did not make.** The
router never surfaces its `InvalidProviderResponseError` — it converts it to a
verdict — so "byte-identical message" is not observable from outside. The
tightened pinning test monkeypatches the router module's `_classify` with a spy
that records the exception instance and delegates, which is the narrowest point
where the router's own exception object exists. The alternative, wrapping
`aiter_chat_chunks` in a capturing async generator, pins the same property with
more machinery.

**Left out deliberately:** nothing. No spec change — `rules/call-path.md`
already states the rule this makes the code obey, per the plan.

**Gate:** `invoke pre` clean (ruff, ruff-format, docstring cap, pyrefly:
0 errors). `python -m pytest` → **1291 passed**, zero failures, zero skips
(1286 before this plan).
