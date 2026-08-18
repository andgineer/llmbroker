# Cooldown ladders

What each model was put on, per run. A cooldown is held in memory by the process
that met it and written to no row, so this is recovered from the caller's log and
exists nowhere else.

The three runs below share everything except the load profile and the caller's
budget. See [`../README.md`](../README.md) for the workload.

### `burst-152` — 45 s budget, 4 in flight

| model | cooldowns | ladder (seconds × count) |
|---|---|---|
| `google-gemini-3.5-flash-lite` | 15 | 60 × 15 |
| `groq-gpt-oss-120b` | 10 | 3 × 2, 8, 20, 36, 48 × 2, 52, 128 × 2 |
| `openrouter-laguna-s-2.1` | 4 | 60 × 3, 120 |
| `openrouter-nemotron-3-ultra` | 2 | 60 × 2 |
| `zai-glm-4.7-flash` | 1 | 60 |

### `burst-152-w25` — 25 s budget, 4 in flight

| model | cooldowns | ladder (seconds × count) |
|---|---|---|
| `google-gemini-3.5-flash-lite` | 19 | 60 × 13, 120, 240 × 2, 480 × 3 |
| `groq-gpt-oss-120b` | 25 | 1, 2, 3, 4, 7, 12, 14, 16, 20, 24, 32 × 2, 34, 36, 80, 96 × 2, 128, 192 × 2, 208, 224, 256 × 2, 288 |
| `openrouter-laguna-s-2.1` | 6 | 60, 120, 240 × 4 |
| `openrouter-nemotron-3-ultra` | 9 | 60, 120 × 4, 1920 × 4 |
| `zai-glm-4.7-flash` | 10 | 60 × 5, 960 × 3, 3600 × 2 |

### `paced-152b` — 25 s budget, 1 in flight, 5 s apart

No cooldown was issued to any model. The highest-weighted one answered all 20
requests.

## Two things the pair shows

**The caller's budget drives the backoff exponent.** At 45 s the
highest-weighted model took 15 cooldowns and every one stayed at the flat base.
At 25 s the same model, on the same prompts, climbed 60 → 120 → 240 → 480 s, and
the two slowest members reached 1920 s and the 3600 s cap. The streak resets on
success and on nothing else; a tight budget returns a miss quickly, so the caller
re-enters the pool sooner, and the model meets more rate limits with fewer
successes between them. At the end of that run 56 of 120 requests had found
nothing at all.

This is a feedback loop between a caller-side setting and a routing-side counter,
and the tight budget is the one an interactive caller is told to set.

**At least one provider does send `Retry-After`.** The ladders split into two
shapes. `gemini` and the two openrouter entries move in clean doublings of the
60 s base — the exponent growing. `groq` moves in irregular steps (1, 2, 3, 7,
34, 80, 208, 288 s) that no doubling produces, which is the provider's own value
being trusted. Worth re-checking against the note that no provider sent one.
