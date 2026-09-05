# Plan — protect one in-flight call from a silent first choice

**Status: problem statement only.** The need and its evidence are concrete; no
runtime shape, public surface, ownership boundary or test placement is settled.
Do not hand this file to an implementation executor until those questions have
been discussed and the plan has been concretized against the then-current call
path.

## Need

An interactive caller may prefer the pool's highest-ranked available model and
still need protection when that model consumes nearly all of the caller's answer
budget without completing. The desired outcome is an answer materially sooner
than the caller's overall timeout — 25 seconds in the observed host — when another
eligible model could answer, without defining the product as an unconditional
race for the first response on every healthy call.

The need is not yet a timing rule. In particular, it does not say that a reserve
must start beside the preferred model immediately, nor that its answer must replace
the preferred answer as soon as it is available. Whether either event should happen
at once, after evidence of silence, or at some other point is deliberately open.

## Why the existing two forms of parallelism do not state this need

Explicit fastest-answer routing asks several ordinary candidates to race on every
call. That is a latency preference and spends provider quota even when the first
choice is healthy. For a stream it commits at the first delta, which can select a
model that starts quickly but finishes slowly or answers below the host's semantic
bar.

Recovery protection covers a different uncertainty: the pool is rechecking a
model after its own cooldown expires. An ordinary highest-ranked model can instead
be healthy by every stored signal and still spend most or all of one caller's
budget before this call learns that it will not finish in time.

Budget-aware ordering protects later calls after such a miss is observed. It does
not recover the call that supplied the evidence, so the reader of that call may
wait for the full budget and receive nothing despite another pool member being
able to answer.

## Evidence from an interactive host

The host has a 25-second complete-answer budget and streams a linguistic analysis.
With two-lane fastest-answer routing, a production-prompt smoke tier returned all
64 answers and had a 2.0-second median. The result was not an unconditional latency
or quality win:

- across 53 full answers, p90 was 11.7 seconds, p95 21.3 seconds and the maximum
  27.9 seconds;
- two Serbian answers committed to `openrouter-laguna-s-2.1` after its first delta
  at 0.7 and 1.2 seconds, then completed at 11.7 and 21.3 seconds;
- both answers were semantically unfit for the host, including corrupted forms, a
  missed learning unit and one explanation in the wrong target language.

The same host's earlier measurements show the opposite failure without ordinary
parallelism: a top choice can remain silent for the caller's whole budget, leaving
no time inside that call for a sequential fallback. The library learns from the
miss and improves later ordering, but that reader has already waited in vain.

Together these observations define the gap. Always racing from time zero is too
eager for this caller; learning only after the full miss is too late for the call
that needs protection.

## Questions deliberately left open

- What observation, if any, should make another candidate eligible while the
  preferred attempt is still in flight?
- When may another attempt start, given that the one complete-answer budget remains
  the caller's and requests consume finite provider quota?
- If more than one attempt produces output, what makes one answer authoritative,
  and can that differ between a completion and a stream?
- How should the call behave when the preferred answer begins before the reserve
  starts, begins after it starts, or finishes after another answer is available?
- What evidence belongs to the current call only, and what may teach later routing
  without confusing slowness, availability and host-rated quality?
- How can the host express the need without selecting model names or recreating the
  pool's routing policy outside llmbroker?

These questions interact with the no-splice stream boundary, the single caller
budget, provider quotas, cancellation and journaling. They are not answered here.

## Out of scope at this stage

- A proposed scheduling algorithm or delay.
- A new argument, result type, exception or configuration field.
- A rule that every call runs more than one ordinary provider request.
- A promise that a reserve answer is acceptable merely because it is available.
- Changes to cooldown, quality demotion, priority or budget-aware ordering.
- Implementation work, tests or spec edits.

## Gate to concretization

Before this becomes executable, compare candidate semantics against the standing
call-path decisions, design a controlled measurement that separates protection of
the in-flight call from unconditional fastest-answer routing, and settle the
questions above. Only then bind the need to current source, public behavior and
tests.
