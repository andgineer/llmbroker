# Routing worth enters the free-tier rubric

## Goal

The curated free pool holds five endpoints. One of them — `zai-glm-4.7-flash` — answered
**1 request in 8** when driven as the only candidate, and took **31 of its 34 seconds
before the first token** when it did (`../reference/freetier-providers.md`). Its entry is
correct in every way the rubric currently checks: the model exists, the key reaches it,
it is not withdrawn, and its quality is presentable. So the refresh keeps giving it a
mid-table weight, and it keeps being handed to callers ahead of the fastest sibling in
the pool.

Nothing in the code should fix this. The rubric already has the instrument, and this
plan points it at what the measurements show.

## The rule

**Weight carries routing worth, not quality alone.** The refresh rubric already says a
model still callable but not worth routing to takes the lowest weight instead of a
removal; what it does not say is that *how often an endpoint answers* and *how long it
takes to answer* are part of being worth routing to. They are, and the rubric says so
after this plan — with the calibration the measurements give: an endpoint that refuses
most requests, or that cannot complete an answer inside a budget an interactive caller
would offer, belongs at the floor.

The entry stays in the preset. It is the last candidate, not a deleted one: an
installation holding only that key keeps working, which is exactly what the low-weight
rule exists for.

## What the record already says, and why none of it blocks this

- **`presets/freetier-refresh-prompt.md`** — "A model still callable but not worth
  routing to gets the lowest weight instead of a removal. Reaching for a removal where a
  low weight would do is the failure mode this rule exists against." This plan applies
  that rule; it does not add one.
- **`decisions.md`, `speed-is-a-catalog-tier`** — speed already earns a place in the
  *paid* catalog's curation. The free list is the same knowledge about the same kind of
  fact.
- **`mission.md`, "Zero administration"** — the curated list keeping itself current is
  llmbroker's job, and the rubric is where that job is written down. Leaving the
  calibration out of the rubric is what makes the next refresh undo the weight by hand.
- **`decisions.md`, `no-bandit-machinery`** and the withdrawn latency ordering — both
  are about the *runtime* learning a slow model. This is curation, which is where the
  same fact costs nothing and applies from the first call, including a cold start no
  learning can help.
- **`presets/freetier.toml`'s own header** — "`weight` is the curated prior on the
  quality rating each entry is expected to earn" is the sentence a reader will quote
  against this plan, and it is the sentence the plan changes: it is narrower than the
  rubric two files away, which already spends weight on routing worth. Both say the same
  thing after this batch.
- **What this is not**: a second axis in the preset file. No new field, no tier column —
  one weight, chosen with one more thing in mind.

## Work order

`. ./activate.sh`, then `invoke pre` and `python -m pytest` green at the end.

1. **The rubric.** In the free-tier refresh prompt, extend the weight rubric with
   routing worth: an endpoint's measured availability and time to a complete answer
   count, an endpoint that refuses most requests or cannot finish inside an interactive
   budget takes the floor, and the refresh reports why it did. Keep it to the rubric's
   existing voice and length.
2. **The preset.** Set `zai-glm-4.7-flash` to the floor weight, so the shipped list
   matches the rule as of the last measurement, and correct the file's header sentence,
   which still describes weight as a quality prior alone.
3. **Specs**, in the same batch: the entry below.

## Tests

None of its own: this is curated data and a prompt. Confirm the existing preset-shape
tests still pass — every row carries a weight within [0, 1] — and do not extend them
with a pinned value, which would freeze the very number the refresh is meant to keep
current.

## Spec moves

- **`decisions.md`** — one new entry, verbatim below, beside the other curation entries.
- **`reference/freetier-providers.md`** — one sentence where that endpoint is already
  described, saying it is carried at the floor and why.

### decisions.md, verbatim

```markdown
### weight-carries-routing-worth

A curated weight answers "how worth routing to is this endpoint", of which expected
quality is one input and measured availability and time-to-answer are others. An
endpoint that refuses most requests, or cannot finish an answer inside a budget an
interactive caller would offer, is carried at the floor rather than removed.

**Blocks:** a second axis in the preset file; removing an endpoint for being slow or
flaky; a runtime signal invented to work around a weight the rubric could have set.
**Why:** the pool orders by weight before it has learned anything, so the first call
after every start, restart and refresh follows curation alone — no runtime learning
reaches that call, which is why the fact belongs where it is known in advance. The
floor rather than a removal because an installation holding only that provider's key
keeps working, and because a curated list is judged on what it can still call. What
this is not is a quality statement: an endpoint may answer well and still not be
worth routing to when it answers one request in eight.
```
