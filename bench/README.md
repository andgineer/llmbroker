# Recorded evidence from real runs

Measurements taken by driving the free pool and the paid direct client with a real
application's traffic, on real keys. This directory holds the rows, not the code
that produced them: the harness lives downstream, and
[`../specs/plans/load-harness.md`](../specs/plans/load-harness.md) is the plan to
bring the reusable half of it here.

The prose distilled from earlier runs is
[`../specs/reference/freetier-providers.md`](../specs/reference/freetier-providers.md#what-one-real-workload-met-measured-2026-08-17);
this is the data under it and under the runs after it.

This directory holds rows today. The harness module the plan builds lands beside
them, and the gate reaches it here: `pytest.ini` sets `--doctest-modules`, and
`bench/` is excluded from neither that nor pyrefly, so it is imported,
type-checked and its examples executed like anything else.

## The workload

A vocabulary tool: one prompt of ~1.2K tokens, ~800 read back, streamed, one
answer per user action. Each run sends the same 120 prompts — 40 items in each
of three source languages — and differs only in the load profile and the
caller's budget.

| run | build | budget | in flight | gap | what it is |
|---|---|---|---|---|---|
| `pool` | 1.5.1 | 45 s | 4 | — | burst, before the whole-answer budget |
| `rerun-burst` | 1.5.2 pre-release | 45 s | 4 | — | burst, with the withdrawn latency ordering in place |
| `burst-152` | 1.5.2 | 45 s | 4 | — | burst, released build |
| `burst-152-w25` | 1.5.2 | 25 s | 4 | — | the same burst, budget at the low end |
| `paced-152b` | 1.5.2 | 25 s | 1 | 5 s | one user at a human pace |

`burst-152` and `burst-152-w25` are a controlled pair: same prompts, same
concurrency, same afternoon, one variable changed.

## Files

- **`runs/profiles.jsonl`** — one row per request the application made. Fields:
  `run`, `build`, `budget_s`, `in_flight`, `gap_s`, `source_lang`, `model` (who
  answered, null when nothing did), `t_first` and `t_total` (seconds, as the
  caller saw them), `error` (exception class, null on success), `empty` (a 200
  that carried no text), `payload_ok` and `markup_ok`.

  The last two are the application's contract reduced to two booleans: whether
  the answer parsed as the structure the prompt demanded, and whether it obeyed
  the markup it demanded. They are here because per-model differences in them
  are already cited upstream — one pool member's markup discipline collapses on
  one source language. The rubric that computes them stays downstream.

- **`runs/calls.jsonl`** — llmbroker's own call journal from the same runs, in
  its native shape: `llm_name`, `operation`, `status`, `http_status`,
  `latency_ms`, `usage`. Recorded by the library, not by the application, so it
  is the view from inside.

- **`runs/cooldowns.md`** — the cooldown ladder each model was put on, per run.
  This is the part no journal row carries, and the reason the pair above was run.
