# A reachability check

## Goal

Nothing in the library answers the first question an operator has: **does this key
actually reach this model?** `list` prints what is curated, `env` prints which refs a
preset needs, and `snapshot()` reports what the pool has learned from traffic — none of
them settles reachability, because only a request does.

The gap is measured, not imagined: the downstream application wrote this check by hand
before it could start, and the result was not cosmetic. It showed that one provider's
"insufficient balance" error came only from its **paid** models while its free one was
reachable — the opposite of what the error text invites, and a diagnosis nothing in the
library would have corrected.

## The rule

**One checking function, read-only, human-invoked.** It takes model configs and a key
resolver and reports per row: no key / key refused / model refused / answered, and how
long it took. Two thin wrappers: the CLI passing a curated preset with env-backed
secrets, and a host passing its own.

**What it must never do**, and this is the load-bearing half:

- **Write nothing.** No journal row, no registry row, no stored state of any kind.
- **Teach nothing.** No cooldown, no quality window, no budget bound, no pool state.
  The pool learns from the traffic a host sends and from nothing else.
- **Decide nothing.** It does not remove, disable or reorder an entry; it prints facts
  and a human reads them.
- **Run only when asked.** No timer, no startup hook, no call on the library's own
  initiative.

## What the record already says, and why none of it blocks this

- **`decisions.md`, rejected: "Proving a model dead before removing it"** — this is the
  entry a reader will reach for, and it does not cover this. What it rejects is probing
  that *decides* something: a merge weighing an entry's fitness. This decides nothing
  and touches no merge.
- **`decisions.md`, `no-alerts-api`** — rejects a *runtime* events API pushed at a host.
  This is a command a person runs and reads.
- **`mission.md`, requirement 6** — "Visibility from the host UI: every per-model fact,
  the call journal, and the pool itself" is the requirement this serves; reachability is
  the one per-model fact none of the existing surfaces can produce.
- **`mission.md`, "llmbroker serves facts and never a verdict"** — the check reports one
  outcome per row and no severity, status enum or judgement.
- **`invariants.md`, invariant 8** — the journal is the only durable state and one read
  of its tail is all that is re-derived from it. The check reads nothing and writes
  nothing, so it sits outside that rule rather than against it.

## Work order

`. ./activate.sh`, then `invoke pre` and `python -m pytest` green at the end of each.

1. **The check.** A new module holding the function and its per-row result type,
   re-exported from the top-level package because a host calls it. One minimal request
   per model, concurrent across models, with a short per-row timeout the caller may set.
   Errors are classified with the existing status helpers so "key refused" and "model
   refused" are told apart the way the router already tells them apart.
2. **The CLI wrapper.** A `check` subcommand beside `env` and `list`, taking a preset
   name, resolving refs from the environment, printing one line per model with its
   outcome and elapsed time, and exiting non-zero when no model answered.
3. **Specs and docs**, in this batch and not after it.

## Tests

New file `tests/test_reachability.py`, against a mock transport — no network:

- `test_a_missing_key_is_reported_without_a_request`
- `test_a_refused_key_is_told_from_a_refused_model`
- `test_an_answering_model_reports_its_elapsed_time`
- `test_a_transport_failure_is_a_row_not_an_exception`
- `test_the_check_writes_no_journal_row`
- `test_the_check_leaves_pool_state_untouched` — no cooldown, no bound, no quality
- `test_the_cli_exits_non_zero_when_nothing_answered`

## Spec moves

- **`rules/direct-by-name.md`** or **`rules/model-list.md`**, whichever the reader of
  "how do I find out my keys work" reaches first: two sentences on what the check
  reports and that it feeds nothing. Name no function.
- **`decisions.md`** — one new entry, verbatim below.
- **`docs/src/{en,ru}/cli.md`** — the new subcommand, with the caveat that it spends one
  request per model against the provider's own quota.

### decisions.md, verbatim

```markdown
### a-reachability-check-reports-and-nothing-else

Whether a key reaches a model is settled only by a request, so llmbroker offers one
that a human runs. It writes no row, teaches the pool nothing, decides nothing, and
never runs on the library's own initiative.

**Blocks:** journaling its calls; feeding cooldowns, quality or ordering from them;
a periodic or startup health check; a probe that removes or disables an entry.
**Why:** `list`, `env` and the pool snapshot answer what is configured and what
traffic has shown, and none of them answers whether a key works — the one question
an operator has before anything runs at all. The application this library was built
for wrote the check by hand before it could start, and found a provider whose
"insufficient balance" applied only to its paid models. What keeps it from becoming
a probe the pool learns from is that it is invoked by a person and its results go
to that person: routing state comes from the host's own traffic, never from calls
llmbroker made up.
```
