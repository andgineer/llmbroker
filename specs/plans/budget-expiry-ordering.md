# A budget expiry teaches ordering

Closes the open question left by [`mission-conformance-fixes.md`](mission-conformance-fixes.md):
with any `wait` below the global HTTP ceiling — i.e. essentially always — a budget expiry
cools nothing, so a hung model stays first in curated order and burns every subsequent
caller's whole budget. Per call that behavior is right; across calls nothing is learned.

## Context

Decision 11 of the previous plan is not in question: a caller's own deadline running out is
not the model's fault, so it must not cool the model or advance its failure streak. What was
missing is that the expiry is still *evidence*, and the only evidence available — a hung model
produces no successful rows, so its latency cannot be measured any other way. What an expiry
proves is a **lower bound on the model's latency**: "this one did not answer within X seconds".

That bound is enough to stop handing the same model to the next caller with the same budget,
without penalising it in any way.

## Design decisions

1. **An expiry teaches ordering, never availability.** The model is not cooled, its
   `fail_count` and the optimizer's streak do not move, it stays selectable — it merely stops
   being the *first* choice for callers whose budget it has already failed to meet. The pool
   already has exactly this lever: `is_demoted` enters the sort key in `LLMPool.acquire` and is
   soft and self-healing.
2. **The signal is budget-relative, and that is what makes it safe.** A caller with a larger
   budget, or none, ignores it. Therefore the flag can reorder a pool but never overturn one:
   if nobody can meet a budget, every candidate is flagged, the term is equal for all, and
   curated order stands. The flag can only ever express "this one is slower than its siblings".
3. **The state lives in the pool, not the optimizer.** It is live routing state next to
   `cooldown_until`/`fail_count`, it is not derived from the journal, it is not about quality —
   and it must work with `optimize=False`.
4. **Node-local by design, not by economy.** Latency is a property of the *path* (this node's
   egress, region, DNS); node A failing to reach a model in 10s is weak evidence for node B.
   A cooldown is shared because a 429 quota is a property of the *key*, which genuinely is
   shared. Two technical facts point the same way: an expiry row is distinguishable from a
   client-side 4xx only by `error_detail` text, and the previous plan settled that an expiry
   changes no stored shapes. If sharing is ever wanted, the honest route is a distinct
   `CallStatus`, which is a stored-shape change and a plan of its own.
5. **An expiry that happened before the attempt started does not feed the bound.** When the
   budget was already spent at slot acquisition (`timeout == 0.0`), the model never got a
   chance; recording that as slowness would be slander.
6. **A ceiling-bound timeout does not feed the bound either** — it already cools the model like
   a 5xx. The two mechanisms must never stack on one failure.
7. **The comparison carries one second of slack.** The bound is recorded from the attempt's
   real budget and compared against the next caller's real remaining budget, so both carry
   sub-millisecond noise; without slack, "the same `wait` twice" is a coin flip. One second is
   not a tuning ratio but a physical statement: at LLM latencies, budget differences under a
   second are noise. A caller is steered away from the model unless its budget is at least a
   second more than what the model already failed to meet.
8. **The window is long and flat — no backoff counter.** A false positive costs "not first in
   line for tight callers, undone by the model's next success"; a false negative costs a caller
   its whole budget. The asymmetry says err long. Ten minutes, and each fresh expiry pushes the
   window out, so a permanently hung model stays deprioritised continuously without any streak
   state. Residual cost, accepted: one burned caller per window per node, instead of every
   caller today.
9. **The two deadlines get their real names.** `Router.chat` already computes a queue deadline
   and an answer deadline whose difference is load-bearing (`wait=0` bounds queueing only), and
   today the difference survives on a comment alone. They become `queue_deadline` and
   `answer_deadline` through the router and `LLMPool.acquire`.

## Work order

### Phase 1 — the signal (`src/llmbroker/broker/pool.py`)

1. `_Slot` gains `unmet_budget: float | None = None` and `unmet_until: datetime | None = None`.
2. Module constants: `_UNMET_WINDOW_SEC = 600.0`, `_UNMET_SLACK_SEC = 1.0`.
3. `note_unmet_budget(self, config: LLMConfig, budget: float) -> None` — sync, like
   `clear_cooling` (single-field assignment, no await, one event loop):
   `unmet_budget = max(existing or 0.0, budget)`, `unmet_until = now + _UNMET_WINDOW_SEC`.
   A missing slot is a legal no-op, as in `release`.
4. `clear_unmet_budget(self, name: str) -> None` — sync, resets both fields.
5. `_over_budget(self, slot: _Slot, remaining: float | None, now: datetime) -> bool` —
   `False` when `remaining is None`, when the window has lapsed, or when no bound is recorded;
   otherwise `remaining < slot.unmet_budget + _UNMET_SLACK_SEC`.
6. `acquire` renames its first parameter to `queue_deadline` (every caller passes it
   positionally — verified) and gains keyword-only `answer_deadline: float | None = None`.
   Inside the wait loop, `remaining = None if answer_deadline is None else answer_deadline -
   time.monotonic()`, recomputed per iteration so a long queue wait makes the choice stricter.
7. The sort key becomes
   `(self._over_budget(s, remaining, now), self._is_demoted(s.config.name, operation), s.order)`.
   Order matters: "probably will not answer in time" outranks "answers worse".
8. Deliberately unchanged: `_wake_timeout` (the flag never makes a candidate available, so
   there is nothing to wake for), `state()`/`LifecyclePhase` (the model *is* `AVAILABLE`;
   turning the flag into a phase would be a lie), `snapshot()` (behavior is automatic — the
   host has nothing to do with it).

### Phase 2 — feeding it (`src/llmbroker/broker/router.py`)

1. Rename `deadline` → `queue_deadline` and `attempt_deadline` → `answer_deadline` in `chat`,
   `_attempt`, and `_attempt_timeout`; pass `answer_deadline=` through to `acquire`.
2. In the disposal block after the attempt, when `verdict.outcome` is `_BudgetExpired`, call
   `self._pool.note_unmet_budget(config, timeout)` — `timeout` is exactly the budget the model
   failed to meet. The pre-attempt short-circuit returns before the `try` and therefore feeds
   nothing (decision 5), and a ceiling-bound timeout never produces this outcome (decision 6).
3. On the success path, `clear_unmet_budget(config.name)` next to the existing
   `clear_cooling(config.name)`.

### Phase 3 — tests (`tests/test_budget_ordering.py`, new)

No real multi-second sleeps: the window constant is patched, as `chat.HTTP_TIMEOUT` already is.
Two models, `a` first in curated order and hanging, `b` healthy, unless stated otherwise.

1. `test_the_next_caller_does_not_walk_into_the_same_hang` — `wait=0.2`: first call raises
   `NoLLMAvailableError`, second call is answered by `b` well inside its budget. Plus the
   no-penalty assertions: `a` is `AVAILABLE`, `fail_count == 0`, no journal row for `a` carries
   `cooldown_until`.
2. `test_a_caller_without_a_budget_ignores_the_bound` — `wait=None` picks `a` first.
3. `test_a_comfortably_larger_budget_ignores_the_bound` — bound from `wait=0.2`, then `wait=30`
   picks `a`.
4. `test_wait_zero_ignores_the_bound` — the attempt is unbounded there, so the flag is
   meaningless.
5. `test_a_success_clears_the_bound` — one OK from `a` (via a large budget), then a tight call
   picks `a` again.
6. `test_when_nobody_can_meet_the_budget_curated_order_stands` — both models hang once, both
   then answer; a tight call picks `a`, not `b`.
7. `test_an_expiry_before_the_attempt_does_not_slander_the_model` — `wait=-1.0`:
   `unmet_budget is None`.
8. `test_the_window_lapses` — with a patched short window, `a` is first again.

One assertion added to `tests/test_wait_budget.py::test_a_hung_provider_cools_down_at_the_global_ceiling`:
the ceiling path cools the model **and** leaves `unmet_budget is None`.

### Phase 4 — spec and docs

1. `specs/reference/architecture.md`, the "A spent budget is never a model's fault" paragraph:
   add the rule — an expiry teaches ordering, not availability; the signal is a learned lower
   bound on latency and the only observable one; it is budget-relative, so it can reorder a pool
   but never overturn it; it is node-local because latency is a property of the path while a
   shared cooldown exists because a quota is a property of the key; an expiry before the attempt
   started does not feed it. State the window qualitatively ("a bounded window, extended by each
   fresh expiry") — the number is tuning, not a decision, and stays in the code.
2. `specs/reference/optimizer.md` and `decisions.md`: deliberately untouched. This is not
   optimizer state, and `decisions.md` already says a failure that changed no shared state waits
   for the debounce — an expiry is exactly such a failure.
3. `docs/src/en/usage.md` and `docs/src/ru/usage.md`, next to `wait`: two lines — a model that
   misses your budget stops being the first choice for equally tight budgets, and it recovers on
   its own.

## Verification

`invoke pre` → no ruff/pyrefly errors; `python -m pytest` → all pass, zero skips.

## Out of scope

- Any change to what a budget expiry stores. It remains an `ERROR` row with no `cooldown_until`;
  a `CallStatus` of its own is what cluster-wide sharing would need, and that is a separate plan.
- Splitting one caller's budget across candidates, and cooling on expiry — both rejected by the
  previous plan and not reopened here.
- Latency-aware routing in general (preferring faster models when budgets are comfortable). The
  bound here is derived from failures only; a real latency model would derive percentiles from
  `Call.latency_ms` over the journal tail, which is a larger feature with no waiting consumer.

## Handover

All four phases are implemented. Gate at hand-off: `invoke pre` clean, `pytest`
green with zero skips.

**Decisions taken during implementation that this plan did not make:** none of
substance. Decision 7's one second of slack was already argued here before the
code existed, and the constant carries a comment saying why it is a wall-clock
statement rather than a tuning ratio.

**Done differently from the plan:**

- Phase 3's `test_a_comfortably_larger_budget_ignores_the_bound` asserted the
  recorded bound *after* the larger-budget call. That call is answered by the
  same model, and a success clears the bound by design — the assertion moved
  ahead of the call. The plan's wording was wrong, not the behavior.

**Added beyond the plan:** `test_the_bound_survives_a_config_refresh`. Every
registry resync re-adds each slot through `LLMPool.add`, so a bound that did not
survive the upsert would be erased about once a minute and the pool would learn
nothing. Nothing in the plan pinned that down.

**Open for the maintainer:** the accepted residual cost — on a permanently hung
endpoint one caller per window per node still burns its budget, instead of every
caller before this change. Lowering it further means cooling on expiry, which
[`mission-conformance-fixes.md`](mission-conformance-fixes.md) decision 11
forbids for good reason.
