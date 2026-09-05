# Plan — a load harness

**Status: source-bound and revalidated against v1.7.0; ready for implementation.**
The binding below reflects the stream receipt and curated readers added by the
preceding plan. It deliberately leaves `src/` untouched.

## Goal

A runnable way to produce the measurements this repository already reasons from.
Drive a list of prompts through the pool or through a direct client at a chosen
concurrency and pacing, record what happened per call, and aggregate it per model.

Nothing in `src/` changes. No public API, no CLI verb, no rule, no runtime cost.

## Why this is not the failure mode the queue warns against

[`README.md`](README.md) names the check every new plan must pass: a mechanism
sized for a problem this pool does not have. Both plans dropped there were
runtime machinery — a module, a CLI verb, a permanent public function; a rule, a
recorded decision, a third copy of a rubric.

This is a script beside the library, run by a person, in the same category as the
refresh prompts that already live next to the presets. It adds no mechanism to
size wrongly. **Bound it accordingly: past ~300 lines it has failed the same
test**, and the way it fails is by growing opinions about what a good answer is.

It also overlaps the dropped reachability check, and the recorded reason for
dropping it applies here and must be argued against rather than stepped around:
*what was actually valuable in it was knowledge, and that already lives in
`../reference/freetier-providers.md`*. That is right about the reachability
check and does not carry to this. Knowledge of a free tier ages — providers
change quotas, presets change weights, and the released library changes what a
caller's settings do. What is missing is not another place to write the knowledge
down but the ability to take it again when something moves. The evidence below is
what that costs when it is missing.

## What it would have caught

`../../bench/runs/` holds the rows. Two runs there are a controlled pair: the
same 120 prompts, four in flight, the same afternoon, one variable changed.

The rate-limit streak decay was held out of the queue on this reading of them:

> the measurements in that same file show every cooldown of a model under
> ordinary load staying at the flat base, and the exponent growing only on an
> endpoint that refuses most requests — where growing is the defence working,
> not a defect.

With the caller's budget at 45 s that holds exactly: the highest-weighted model
took 15 cooldowns, all at the flat base. With the budget at 25 s and nothing else
changed, the same model on the same prompts climbed 60 → 120 → 240 → 480 s, and
the two slowest members reached 1920 s and the 3600 s cap. It is not an endpoint
that refuses most requests — it is the one answering most of the traffic.

The mechanism is a feedback loop between a caller-side setting and a
routing-side counter: a tight budget returns a miss quickly, the caller
re-enters the pool sooner, and the model meets more rate limits with fewer
successes between them to reset the streak. A tight budget is what
`docs/src/en/async.md` tells an interactive caller to set.

That premise was checkable only because the run could be repeated with one knob
moved. Nothing in this repository can repeat it today.

A second, smaller one from the same rows: `groq`'s ladder moves in irregular
steps no doubling produces, which is a provider-supplied `Retry-After` being
trusted — worth re-checking against the note that none was seen.

## Binding to the current source

The two supported routes follow the public boundaries already in the package:

- A pool route calls `AsyncBroker.stream()` (`broker/broker.py`). Its
  `StreamHandle` (`broker/result.py`) supplies the answering model and call id from
  the first delta onward, and usage after the stream settles. Read those properties;
  do not query the journal to reconstruct them.
- A direct route resolves one `AsyncDirectClient` through `AsyncBroker.direct()`
  before scheduling calls, then reuses it. It names exactly one declared model by
  alias or by name. Its stream is a plain async iterator: it exposes neither a
  receipt nor usage, so the route carries the expected target name and its rows have
  no call id or usage. Do not inspect private client fields to fill the gap.

There is no route that selects one member *inside an active pool*.
`AsyncBroker.get()` returns a read-only view, while `AsyncBroker.direct(name=...)`
raises `PoolModelError` for an entry in that broker's registry. Pool behaviour is
therefore measured through the pool and grouped by the model named on the returned
handle.

A concrete pooled endpoint is still measurable in isolation, and this is how the
third route in the earlier sketch is made honest: take its `LLMConfig` from
`curated_pool().configs` and declare that config in a separate benchmark broker whose
registry is empty and whose `sync=None`. Then call it by name through a
`DirectRoute`. In that broker it is a declared direct model, not a selected pool
member: the endpoint and key ref are the same, but failover, pool learning and pool
journaling are intentionally absent. Do not put the same name in that broker's
registry and `direct=` together; `check_overlay()` correctly rejects the collision.

The programmatic readers added in `broker/curated.py` are setup aids, not hidden
harness inputs. A caller may take a `CuratedModel` from `curated_paid()` and pass its
declaration to `AsyncBroker(direct=...)`, or take an `LLMConfig` from
`curated_pool().configs` for the isolated broker above. The chosen config is passed
explicitly when the benchmark broker is constructed, so a controlled run records
which model list it used rather than letting the runner choose a moving catalog row.

An empty answer also has a new meaning at this boundary. `direct.py` raises
`InvalidProviderResponseError`; the routed stream records the bad attempt and fails
over, or ultimately raises `NoLLMAvailableError`. The harness can only mark
`empty=True` if an iterator ends normally without yielding text. That state is kept
to read the historical v1.5 rows and to catch a regression, not inferred from an
exception message or from a hidden HTTP response.

## What to build

One module, `bench/harness.py`, with the following repository-local surface.

**Inputs and routes.** `Case` carries a prompt plus flat JSON-serializable dimensions
such as `source_lang`. `PoolRoute` has only the label written to the row.
`DirectRoute` carries `target` plus exactly one of `alias` or `name`; its optional
`timeout` is passed to `AsyncDirectClient.stream()`. It is intentionally separate
from the pool's `wait`: the former is httpx's direct request timeout, while the
latter is the broker's whole-answer routing budget and produces learning.

`Row` has these fixed fields: `route`, `target`, `model`, `operation`, `call_id`,
`t_first`, `t_total`, `error`, `empty`, `usage`, and `score_error`. Case dimensions
and scorer values are held separately on the object and flattened only in JSON.
`model` means the model that completed the answer: from the pool handle for a pool
route, and the declared `target` after a successful direct call. A failed direct call
therefore keeps `target` but has `model=None`.

The driver shape is:

```python
async def drive(
    broker: AsyncBroker,
    cases: Iterable[Case],
    *,
    route: PoolRoute | DirectRoute,
    output: Path,
    operation: str | None = None,
    in_flight: int = 1,
    gap: float = 0.0,
    wait: float | None = None,
    score: Callable[[str], dict[str, object]] | None = None,
) -> AsyncIterator[Row]
```

Reject `in_flight < 1`, `gap < 0`, a malformed direct selector, non-JSON case
dimensions, and collisions with fixed row names before opening a provider request.
Resolve a direct client once before the workers start; a resolution failure is a
setup failure and produces no synthetic per-case rows.

Use a fixed worker set, not one unbounded task per case. A single start gate enforces
at least `gap` monotonic seconds between request starts across all workers, while the
worker count caps open calls at `in_flight`. Rows leave `drive()` in completion order.
Append and flush each JSON line before yielding it; if the consumer or a call is
cancelled, close its stream and cancel/await the remaining workers. A file error is
fatal rather than silently turning a durable run into an in-memory one.

`gap` is the part that is not obvious and is why the downstream harness grew it:
without it a caller sends a burst no user produces, trips every free-tier limit
and measures the failover tail instead of the model it meant to measure. The two
profiles worth having are a burst (`in_flight > 1`, `gap = 0`) and a paced
session (`in_flight = 1`, `gap` seconds apart).

Measure both latencies with `time.monotonic()`: `t_first` ends at the first yielded
text delta and stays `None` if none arrived; `t_total` ends when the iterator
finishes or raises. Store exception class names, not messages. For a pool call, read
`model`, `call_id`, and `usage` from the handle after the iterator settles even on
the error path, because a post-delta failure still has attribution. Serialize usage
in the same nested shape as `Call.usage`. Direct rows leave receipt fields unset,
matching the public surface as it exists now.

**The scorer seam.** `score` runs only after a non-empty successful answer, takes
the joined text, and returns a flat JSON-serializable mapping. The runner never
interprets it. A scorer exception or invalid/colliding mapping sets `score_error` to
the exception class and still appends the call row; it does not turn a provider
success into `error`. The default is `None` — latency and failure classes are enough
for the questions this repository asks. Answer text is discarded after scoring and
is never written to JSONL.

**JSONL and summary.** Add `read_rows(path)` and this pure reducer:

```python
def summarize(
    rows: Iterable[Mapping[str, object]],
    *,
    group_by: Sequence[str] = ("model",),
    budget: float | None = None,
    metrics: Sequence[str] = (),
) -> list[dict[str, object]]
```

Group by the requested flattened keys and sort the result deterministically by that
key tuple, with `None` before concrete values. Each group reports `count`,
`failures`, `empty`, p50 and p90 `t_total` over successful non-empty rows, and —
when `budget` is given — `over_budget` over every row carrying a numeric `t_total`.
Use linear interpolation at `(n - 1) * q`, returning the lone value for a one-row
group and `None` for an empty latency sample. Do not round stored results.

Only keys explicitly listed in `metrics` are reduced, so numeric dimensions such as
`budget_s` or `in_flight` are never averaged by accident. A boolean metric yields
`<key>_true` and `<key>_count`; another numeric metric yields `<key>_mean` and
`<key>_count`. Missing and wrong-typed values are excluded from that metric's count.
This shape reads both new rows and the already-flat historical
`bench/runs/profiles.jsonl`.

## Where it lives, and what the gate does to it

`bench/` at the repo root, beside the recorded rows already there.

The current gate reaches this location three ways: the strict ruff hook excludes
only `tests/`, pyrefly excludes `tests/`, and pytest's `--doctest-modules` collects
Python modules outside the test-name pattern. The module is therefore linted,
formatted, type-checked, imported and doctested. Keep that arrangement rather than
adding a special exclusion. Two things follow:

- it must import with no keys and no network — everything provider-facing sits
  behind `drive`, so this costs nothing;
- doctests run for real, so examples belong over rows and summaries, never over
  a call that would reach a provider.

## What stays out

- **Any opinion about a good answer.** No card parsing, no markup rules, no
  language detection, no judge rubric. Those belong to whoever has a contract.
- **A CLI verb.** This is not a command the library offers its hosts.
- **Selecting a member inside an active pool.** It would contradict the direct/pool
  boundary merely to make a benchmark convenient. Isolated endpoint measurement
  uses the same config as a declaration in a separate direct-only broker.
- **Anything read at runtime.** No row here feeds routing, quality or the
  registry. Reading `bench/` from `src/` is the failure this plan is one step
  away from and must not take.
- **CI.** It calls real models with real keys, and a direct route against a paid
  alias spends money.

## Work order

1. `bench/harness.py` — inputs, routes, row serialization and the bounded/paced
   driver, consuming only the public broker/client surfaces named above.
2. Gate the batch with `invoke pre` and the full suite.
3. `bench/harness.py` — `read_rows()` and `summarize()`.
4. `tests/test_bench_harness.py` — deterministic fake-stream tests plus two
   contract tests through a real `AsyncBroker` over `httpx.MockTransport`.
5. `bench/README.md` — replace the future-plan wording with a minimal import-based
   usage example and document the new fields. Keep the rule that committed runs
   contain no answer text.
6. Re-derive the historical `run == "pool"` table. It must yield 120 rows: 83
   Gemini, 30 Nemotron, 3 Groq, 1 Laguna and 3 with no answering model; the Nemotron
   group has 3 historical empties, and successful median whole-answer times round
   to 1.7 s, 39.6 s, 3.5 s and 42.7 s respectively. Those are the counts and
   medians stated in `reference/freetier-providers.md`.
7. Gate the completed batch again. Skip the version bump — the maintainer does it.

## Tests

The test module reaches no network and covers:

- pacing: with `gap`, call starts are at least `gap` apart; with `in_flight > 1`,
  no more than that many streams are open; invalid controls fail before the first
  stream call;
- completion order and durability: a faster later case is appended/yielded first,
  every yielded row is already present as one complete JSON line, and cancellation
  closes streams and leaves no worker running;
- pool recording: pre-delta failure leaves `model`/`call_id` unset, post-delta
  failure retains them, successful completion captures final usage, and normal
  no-delta completion is marked empty;
- direct recording: the client is resolved once, alias and name selectors are both
  forwarded correctly, a successful row uses the declared target, and receipt/usage
  stay absent rather than reading private client state;
- concrete free-model isolation: a config selected from `curated_pool()` is callable
  by name when declared on a separate empty-registry broker, while the same name is
  still rejected by `direct()` on a broker whose registry contains it;
- scorer isolation: values flatten into the row, answer text does not, and a raised,
  non-JSON or colliding result preserves the call with `score_error`;
- serialization rejects colliding/non-JSON case dimensions before calls and can
  round-trip a row through `read_rows()`;
- summary grouping and deterministic order, the zero/one/even-sized percentile
  cases, failure/empty/budget counts, boolean counts, numeric means, missing or
  wrong-typed metric values, and the historical `pool` acceptance table above.

The two surface contract tests use the repository's existing patterns: standalone
registry/secrets/store plus `httpx.MockTransport`, as in `test_broker_direct.py`,
and an SSE usage tail, as in `test_router_stream.py`. They pin that the fake used by
the scheduler tests still matches v1.7.0 without calling a provider.

## What moves into the specs

Nothing, on this plan alone. It adds no rule and changes no behaviour.

The cooldown finding above is a different matter and is **not** part of this
plan's scope: it says a recorded premise is conditional on a caller-side setting,
and [`README.md`](README.md) carries that as the standing reason the streak decay
is weighed and unqueued.

## Gate

`invoke pre` and `python -m pytest` green, per `CLAUDE.md`.
