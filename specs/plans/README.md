# Implementation plans

These ship in this order — each depends on the one above it.

| # | plan | what it does |
|---|---|---|
| 1 | [`observed-latency-ordering.md`](observed-latency-ordering.md) | latency read off the answers a model gave orders the fallbacks for a caller with a tight budget |
| 2 | [`stream-stall-timeout.md`](stream-stall-timeout.md) | a caller may bound the gap between deltas, and the pool learns from it |

**Plans 1 and 2 ship in one release.** Plan 1 learns only the wait for the first token; the
trickle after it is Plan 2's, and the case both were written for is a trickle.

A queued plan is a row in this file and a file beside it. How one is executed lives in `CLAUDE.md`
under "Executing a plan", and the rules binding on every plan live in
[`../reference/invariants.md`](../reference/invariants.md), which is loaded for every task. Neither
is restated here.
