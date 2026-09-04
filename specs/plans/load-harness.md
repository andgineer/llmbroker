# Plan — a load harness

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

## What to build

One module. Three things in it.

**The driver.** Given a broker, an iterable of prompts and a route, run them and
yield a row per call:

```python
async def drive(
    broker: AsyncBroker,
    prompts: Iterable[str],
    *,
    route: Route,                 # the pool, a direct alias, or one named pool member
    operation: str | None = None,
    in_flight: int = 1,
    gap: float = 0.0,             # minimum seconds between call starts
    wait: float | None = None,
    score: Callable[[str], dict[str, object]] | None = None,
) -> AsyncIterator[Row]
```

`gap` is the part that is not obvious and is why the downstream harness grew it:
without it a caller sends a burst no user produces, trips every free-tier limit
and measures the failover tail instead of the model it meant to measure. The two
profiles worth having are a burst (`in_flight > 1`, `gap = 0`) and a paced
session (`in_flight = 1`, `gap` seconds apart).

`Row` carries: the route, the model that answered (`None` when nothing did),
time to the first delta, time to the whole answer, the exception class on
failure, whether a 200 carried no text, and whatever `score` returned, flattened
alongside. Rows append to JSONL as they complete, so a run that dies keeps what
it measured.

**The scorer seam.** `score` takes the answer text and returns a mapping. The
runner never interprets it: numeric values are averaged in the summary, booleans
counted. The default is `None` — latency and error classes are enough for the
questions this repository asks. A caller with a real answer contract attaches it
and gets per-model contract columns for free; that is where the boolean pair in
`bench/runs/profiles.jsonl` came from.

**The summary.** Group rows by model, by route or by any recorded key and reduce:
count, failures, empty answers, p50 and p90 whole-answer time, how many exceeded
a stated budget, and the mean of every numeric scorer key. This is the table the
prose in `../reference/freetier-providers.md` was written from.

## Where it lives, and what the gate does to it

`bench/` at the repo root, beside the recorded rows already there.

`pytest.ini` sets `--doctest-modules`, and `bench/` is excluded from neither
that nor pyrefly, so the module is imported, type-checked and its examples
executed on every gate run. Keep that arrangement rather than working around it:
ruff's strict ruleset reaches `src/` only and pyrefly skips `tests/`, so
collection here is the only check this code would otherwise get, and its output
is what recorded decisions rest on. Two things follow:

- it must import with no keys and no network — everything provider-facing sits
  behind `drive`, so this costs nothing;
- doctests run for real, so examples belong over rows and summaries, never over
  a call that would reach a provider.

## What stays out

- **Any opinion about a good answer.** No card parsing, no markup rules, no
  language detection, no judge rubric. Those belong to whoever has a contract.
- **A CLI verb.** This is not a command the library offers its hosts.
- **Anything read at runtime.** No row here feeds routing, quality or the
  registry. Reading `bench/` from `src/` is the failure this plan is one step
  away from and must not take.
- **CI.** It calls real models with real keys, and a direct route against a paid
  alias spends money.

## Work order

1. `bench/harness.py` — `Row`, `Route`, `drive`, the JSONL append.
2. `bench/harness.py` — the summary reducer over rows read back from JSONL.
3. `tests/test_bench_harness.py` — against a fake broker, no network.
4. Re-derive one existing table from `bench/runs/profiles.jsonl` and check it
   against `../reference/freetier-providers.md`. A summary that cannot reproduce
   the prose already in the specs is not finished.
5. Skip the version bump — the maintainer bumps by hand.

## Tests

Everything except the provider call is pure and gets covered against a fake
broker whose `stream()` yields scripted deltas and whose handle names a model:

- pacing: with `gap`, call starts are at least `gap` apart; with `in_flight > 1`,
  no more than that many are open at once;
- recording: a failing call records the exception class and no model where none
  answered; a 200 with no text records as empty, not as an answer;
- the scorer seam: a scorer returning a mapping lands its keys on the row; a
  scorer raising does not lose the row;
- the summary: percentiles, counts, and the mean of a numeric scorer key over a
  known set of rows.

## What moves into the specs

Nothing, on this plan alone. It adds no rule and changes no behaviour.

The cooldown finding above is a different matter and is **not** part of this
plan's scope: it says a recorded premise is conditional on a caller-side setting,
and [`README.md`](README.md) carries that as the standing reason the streak decay
is weighed and unqueued.

## Gate

`invoke pre` and `python -m pytest` green, per `CLAUDE.md`.
