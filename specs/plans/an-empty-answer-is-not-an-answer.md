# An empty answer is not an answer

## Goal

A model can return HTTP 200, a well-formed chat completion, and no text at all.
llmbroker treats that as a success today: the pool journals `OK`, the caller gets an
empty string or an exhausted stream, and the other providers that could have answered
are never tried. Measured in one real workload: **one pooled model returned a
well-shaped answer carrying no text on 3 of 14 requests for one language, in under a
second each** (`../reference/freetier-providers.md`, "What one real workload met").

That is the one failure mode a host cannot defend against on its own without
re-implementing failover: by the time it sees the empty string, the call is over and
the pool has moved on.

## The rule

**An answer with no text and no tool calls is not an answer.** It is treated as the
malformed response it is — the same surface a 200 that is not a chat completion
already lands on — so:

- **Pooled completion**: the attempt fails and the next candidate is tried, exactly as
  for any other unusable 200.
- **Pooled stream**: an attempt that ends without ever yielding a delta fails the same
  way. Nothing reached the caller, so failover is still available and invariant 18 is
  untouched — it forbids failover only *past* the first delta.
- **Direct client**: no pool, so nothing to fail over to; the call raises rather than
  returning an empty result.

A reply carrying tool calls and no prose is an answer and stays one.

## What the record already says, and why none of it blocks this

- **`mission.md`, requirement 1** — "the caller sees an error only once the whole pool
  is exhausted". An empty answer is the caller seeing nothing *while the pool still had
  candidates*. This plan is that requirement being enforced, not an extension of it.
- **`mission.md`, "Exact only where an error is destructive"** — the list names
  "telling a host the wrong reason a call failed". Reporting success for a call that
  produced nothing is exactly that.
- **`mission.md`, "Nothing wraps what is asked"** — no opinion about the content of a
  reply. Distinguishing *some text* from *no text* is not an opinion about content: the
  library already parses the response shape, already yields text deltas out of it, and
  already raises when a 200 is not a chat completion. This adds no judgement of what the
  text says.
- **`decisions.md`, `quality-is-the-hosts-verdict`** — nothing here enters the quality
  window, generates a score, or judges an answer. An empty answer is an availability
  fact, and availability and quality are separate axes.
- **`rules/call-path.md`** — "every failure before the first delta cools the model and
  moves to the next candidate through the same classification" already describes the
  behaviour this plan routes into. There is no new failure surface.

## The cooling question, decided

An empty answer **cools the model like any other malformed response**, rather than
getting a third disposal of its own ("fail over but do not cool"). The provider
misbehaved; the existing surface says what to do with that, and a model whose next
answer is fine has its streak reset by it. A third kind of failure would be new
machinery bought with no measurement — if a later run shows healthy models cooled by
this, that measurement is what re-opens it.

## Work order

`. ./activate.sh`, then `invoke pre` and `python -m pytest` green at the end of each.

1. **Completions through the pool.** Where an attempt's answer is settled, an answer
   with neither text nor tool calls raises the malformed-response error instead of
   yielding a result, so the existing classification fails it over.
2. **Streams through the pool.** An attempt whose deltas ran out without one ever
   arriving leaves the same verdict on its outcome instead of reporting an answer.
3. **The direct client.** `ask` and `stream` raise on an answer that carried no text.
4. **Specs and docs**, in this batch and not after it.

## Tests

New file `tests/test_empty_answer.py`:

- `test_an_empty_completion_fails_over_to_the_next_candidate`
- `test_an_empty_completion_is_journaled_as_a_failure`
- `test_a_stream_that_never_yields_a_delta_fails_over`
- `test_a_reply_carrying_only_tool_calls_is_an_answer`
- `test_the_last_candidate_returning_empty_raises_rather_than_answering`
- `test_a_direct_client_raises_on_an_empty_answer`
- `test_an_empty_answer_cools_the_model_like_any_malformed_response`

`tests/test_router_stream.py` and `tests/test_broker.py` are the neighbours; they
should need no edit, and needing one is a signal worth reporting.

## Spec moves

- **`exceptions.py`** — the malformed-response error's docstring widens by one clause:
  a 200 that is not a chat completion, *or one carrying no answer*. Reusing the type is
  the point; a reader who checks it must find the reuse described there.
- **`rules/call-path.md`** — one sentence in the failure classification: an answer
  carrying no text and no tool calls is a malformed response, and a stream that never
  produced a delta is the same fact on the streaming path.
- **`decisions.md`** — one new entry, verbatim below, beside the other call-path
  entries.

### decisions.md, verbatim

```markdown
### an-empty-answer-is-a-failure

A 200 carrying a well-formed completion with no text and no tool calls is not an
answer: pooled, it fails over like any malformed response; direct, it raises.

**Blocks:** returning an empty string as a successful answer; leaving the check to
the host; a disposal of its own that fails over without cooling; treating a
tool-call-only reply as empty.
**Why:** the host cannot defend against this without re-implementing failover — by
the time it sees the empty string the call is settled and the pool has moved on,
with candidates that were never tried. One real workload met it on 3 of 14 requests
to one pooled model. Judging *whether there is text* is not judging what the text
says, so nothing here wraps the reply or rates it; and an unusable 200 already has
a surface, which is the one this uses rather than inventing a third.
```
