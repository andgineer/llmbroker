# Implementation plans

| # | plan | what it does |
|---|---|---|
| 1 | [`a-reachability-check.md`](a-reachability-check.md) | a read-only, human-run check of whether each key actually reaches its model |
| 2 | [`routing-worth-in-the-free-tier-rubric.md`](routing-worth-in-the-free-tier-rubric.md) | availability and time-to-answer enter the curated weight, so a barely-usable endpoint sorts last from the first call |

**The two are independent** — no order is implied beyond the numbering, and they may
ship in one release. Each carries the recorded decisions a reader will reach for and why
none of them blocks it, so a plan that looks like it fights the mission has already been
weighed against it: read that section before re-opening the argument.

[`latency-aware-fallback.md`](latency-aware-fallback.md) is a draft, not a queued plan.
What is still live in it is the rate-limit streak decay, which the queue deliberately
does not carry: the measurements in that same file show every cooldown of a model under
ordinary load staying at the flat base, and the exponent growing only on an endpoint that
refuses most requests — where growing is the defence working, not a defect. Plan 2 is the
answer to that endpoint.

A queued plan is a row in this file and a file beside it. How one is executed lives in `CLAUDE.md`
under "Executing a plan", and the rules binding on every plan live in
[`../reference/invariants.md`](../reference/invariants.md), which is loaded for every task. Neither
is restated here.
