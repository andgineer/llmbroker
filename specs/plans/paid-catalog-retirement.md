# When a model an alias follows is withdrawn

## Goal

An alias is a promise that application code survives a version bump. The promise
has one hole: it assumes every alias always has a live model behind it. When a
provider withdraws a model and publishes no successor under the same alias, the
library today has nothing to say. Two outcomes, both silent:

1. The curation leaves the alias pointing at the withdrawn model. Every refresh
   reports "no change", `direct("haiku")` hands back a working client, and the
   calls fail with the provider's own 404 — the exact failure
   `decisions.md#the-paid-catalog-is-curated-too` says the curated catalog exists
   to prevent ("without a curated catalog every host pins a version and silently
   runs a retired model until it breaks").
2. The curation drops the alias row. The refresh then reports it as *unknown*,
   which is the same signal a malformed catalog and a hand-typed alias produce —
   a warning that repeats forever and names no action.

After this plan the catalog can state that a model is going away, with the date
and, where there is one, the alias to move to. A withdrawal stops being something
the installation discovers from a failing request.

## Why

The two `decisions.md` entries below carry it. Nothing else in this plan argues.

`the-paid-catalog-is-curated-too` names this failure as the catalog's reason to
exist and is amended by neither entry. The **rejected** item *A deprecation-tier
field* is adjacent and does not cover this: it rejects a third state for a
**registry entry** under the free pool's removal rule, where an entry is either
removed or kept as it was and nothing is lost by removal. The subject here is the
**catalog** — an upstream recommendation source whose aliases are permanent by
rule, so it cannot express anything by dropping a row, and whose rows describe a
model rather than a routing state.

## The two entries, to land in `decisions.md` verbatim

### a-withdrawn-alias-is-marked-not-dropped

A model the provider is withdrawing keeps its catalog row and gains the date it
stops working. The row is never deleted, because a published alias is permanent
and a missing row already means something else: that the catalog is malformed or
that nobody ever published this alias. The date, rather than a flag, is what the
library compares against: an alias whose date has not arrived still works and is
followed as before, and one whose date has passed is dead and is refused at the
call. A curation that does not know the date writes the date it curated, which
reads as already withdrawn.

**Blocks:** expressing a withdrawal by removing the row; a boolean marker; a
per-entry state stored in the registry ahead of a catalog that states it;
probing a provider to find out whether a model still answers.
**Why:** the two failures this closes need opposite handling and are today one
signal. A dropped row is indistinguishable from a curation mistake, so the
warning it produces cannot name an action and is ignored. A date separates
*announced* from *gone*, which is the difference between a warning a deployment
has time to act on and an error it must see at the call — a boolean collapses
them and would either cry wolf for months or arrive after the first outage.
**Accepted cost:** one optional field per catalog row and one more comparison in
the refresh. The library gains a dependency on the curation being timely, which
it already has for every alias target it follows.

### retirement-does-not-move-an-alias

A withdrawn alias's row may name a successor alias. Nothing moves an entry onto
it: the successor is carried into the report, the CLI and the error message, and
the installation decides.

**Blocks:** re-pointing a followed entry at a different alias automatically;
treating a successor as a rename of the alias.
**Why:** a version bump under one alias is the same product at the same tier,
which is what the installation agreed to when it named the alias. A different
alias is a different model at a different price, and choosing it is not a
consequence of anything the installation stated. The failure mode is also
asymmetric: an alias left where it is produces an error the deployment sees, and
an alias silently moved produces a bill and a behavior change it does not.
**Accepted cost:** a deployment whose model is withdrawn must change one word and
redeploy. The report, the `add-model` output and the raised error all carry that
word, so nobody has to read the catalog to find it.

## The shape

Comparing the row's date against today, in UTC, gives three states:

| catalog row | the declared model is | resolution | `direct()` |
|---|---|---|---|
| no date | live | resolves as today | works |
| date in the future | announced | resolves as today, plus one warning | works |
| date today or earlier | withdrawn | marked, one warning | raises |

An announced model still works, so nothing about it changes except that the log
says it is going away. A withdrawn one is not re-pointed because there is nothing
live to point at, and the declaration is not dropped either: the application
stated it, and an entry vanishing from under a caller is exactly the silence this
plan removes.

## Work order

Four batches. Each ends green.

### 1. The catalog can say it

1. **`presets/paid-catalog.toml` + `broker/aliases.py`.** A `[[provider.models]]`
   row gains two optional keys: the withdrawal date, and the successor alias.
   `catalog_alias_targets` carries both onto `AliasTarget`. A successor naming an
   alias the catalog does not carry makes the catalog invalid, the same way a
   duplicate alias already does — the message names the row.
2. The comparison is against today's UTC date. A malformed date makes the catalog
   invalid; the catalog is curated, so a bad value is a curation bug, not
   something to tolerate at runtime.

### 2. The resolution reports it

3. **`AliasChange`** gains two members, for *announced* and *withdrawn*.
   `resolve_declared` emits one per declared model whose target carries a date;
   `now` carries the date and `was` the successor alias or the empty string, so
   `AliasFact` keeps one shape.
4. **`broker/report.py`** renders both as warnings — a deployment must act on
   either — and each line names the date and, where there is one, the successor.
   The lines are logged where `named-models-are-declared` puts the other alias
   facts, on the re-resolution path.

### 3. The call refuses

5. **`models.py`.** `LLMConfig` gains the withdrawal date and the successor.
   Neither is serialized: a followed entry is declared, not stored, so the fact
   rides on the config the resolution built and no registry ever sees it. No
   metadata round trip, and no backend owes it anything.
6. **`exceptions.py`.** A new `LLMRequestError` subclass for a withdrawn model.
   It names the alias, the date and the successor.
7. **`broker/broker.py`.** `direct()` raises it for a declared entry whose date
   has passed. The check reads the resolved config only — the request path may
   not parse the catalog — and an announced entry logs nothing per call, since
   the resolution already logged the warning.
8. **`broker/aliases.py`.** `_entry_for_alias` raises the same error for a
   withdrawn alias at first resolution. A *re-resolution* that finds an alias
   withdrawn marks it withdrawn rather than keeping the working resolution:
   "only the first resolution may fail" protects a deployment from a catalog it
   cannot read, and a catalog that says a model is gone is information, not
   absence of it.
9. **`cli.py`.** `list` marks a withdrawn model and names its successor; an
   announced one carries its date. Nothing is refused — the command writes
   nothing to refuse.

### 4. Specs, docs and the curation prompt

Listed below; part of the work, not a sweep after it.

**The two new report lines, the `list` markers and the `direct.md` section say
"the model list", never "lineup"** — `model-list-vocabulary` comes after this
plan, and a string written in the coined word now is a string it has to find
later.

## Out of scope, deliberately

- **The free pool.** A withdrawn free model is removed from `freetier.toml` and
  the removal rule already handles it; nothing there is the installation's to
  keep.
- **Pinned entries.** A pin means the host tracks the version itself. Matching a
  pinned entry's model id against the catalog would make the catalog a register
  of every model rather than of aliases, which is a different file with a
  different curation cost.

## Tests

- A row with a future date: the declared model still resolves to it, and exactly
  one warning fact is produced.
- A row with a past date: one warning fact, and the resolution is marked
  withdrawn.
- Both report lines name the date, and the successor where the row has one.
- A declared model whose date has passed: `direct()` raises, and the message
  carries the alias, the date and the successor.
- The same model with a future date: `direct()` returns a client.
- `direct()` on a withdrawn model parses no catalog — assert against the preset
  source, not by timing.
- A withdrawn alias raises at the first resolution; one already resolved and
  later found withdrawn raises on the next `direct()`.
- `list` marks a withdrawn model and names its successor, and prints an
  announced one with its date.
- A catalog whose successor names an alias it does not carry is refused, and so
  is one whose date does not parse.
- Nothing about a withdrawal reaches a registry — assert the stored entries are
  byte-identical across a resolution that finds one.

## Spec updates

- **`rules/direct-aliases.md`** — the retirement paragraph: an alias is permanent,
  so a withdrawal is stated on the row and never by removing it; the three states
  and what each does to a followed entry; that a successor is reported and never
  followed; and that the request path reads the stored fact rather than the
  catalog. The existing sentence about an alias the catalog no longer carries
  stays as it is — it now covers only what it always meant, a catalog that is
  wrong or unreachable.
- **`decisions.md`** — the two entries above, verbatim.
- **`mission.md`**, one passage: *Reaching a model by name* says llmbroker "keeps
  it pointing at the current version", which assumes there always is one. Add
  that where an alias has no live model left, llmbroker says so rather than
  leaving the entry pointing at a model that no longer answers. Intent only — no
  dates, no states, no mechanism.
- **`invariants.md`** — no new entry. The rule is local to one subsystem and the
  file is at its cap.
- **`presets/paid-catalog-refresh-prompt.md`** — the curation half. Never drop an
  alias row; state the withdrawal date and the successor where the provider
  published one. The current instruction — "either keep the alias pointing at the
  provider's successor, or accept that every refresh will warn on it forever" —
  is replaced: the second branch is what this plan removes.

## Docs (en and ru, in step)

- `direct.md` — one short section: what happens when a model you follow is
  withdrawn, the warning you get before the date, the error you get after it, and
  that moving to the successor is one word in your own code.

## The queue

**After `named-models-are-declared`**, which is what makes a followed entry
declared rather than stored and moves the alias facts to the resolution this plan
extends. Taken before it, this plan would build the withdrawal into a metadata
blob that plan then deletes.

**After `one-broker-many-callers`**, which moves `direct()` off the broker onto
the caller and gives the sync side its own mirror of it. Nothing this plan
decides changes — the refusal, the two entries and the catalog fields are the
same — but the batch that makes the call refuse is written against a method that
plan has already moved, so scope it against the caller rather than against
`AsyncBroker`.

Independent of 10 and 11. It must land **before 12 and 14**: 12 moves the
validators out of `models.py` and 14 rewrites the `direct=` overlay, and both
would otherwise be written against the pre-withdrawal shape of the field set this
plan adds to.

## Gate

`invoke pre` clean and `python -m pytest` green after each batch. Docker up for
the testcontainer tests.
