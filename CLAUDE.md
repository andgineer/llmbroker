# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
. ./activate.sh   # creates .venv and installs deps via uv; re-run after pulling
```

**Activate before every `invoke` or `pytest` run.** A shell without the venv picks up a global
`pyrefly`/`invoke` from `~/.local/bin` whose version and strictness differ from the pinned ones,
so the gate reports errors that do not exist. If untouched files suddenly go red, check
`which pyrefly` before believing the output.

## Common commands

| Task | Command |
|---|---|
| Run tests | `python -m pytest` |
| Run single test | `pytest -k 'test_name'` |
| Lint + format + type-check | `invoke pre` |
| Preview docs (English) | `invoke docs-en` |
| Bump version | `invoke ver-release` / `invoke ver-bug` / `invoke ver-feature` |

**Never edit `src/llmbroker/__about__.py` directly.** Version is managed by `invoke ver-*` commands only.
| Upgrade deps | `invoke reqs` |

**Never call ruff directly.** Always use `invoke pre` — it runs ruff, ruff-format, pyrefly, and pre-commit hygiene hooks in the correct order. Never bypass hooks with `--no-verify`.

## Non-negotiable done gate

Before claiming anything is done, both must be green:

1. `invoke pre` → no errors from ruff or pyrefly
2. `python -m pytest` → `N passed` with zero failures or errors

Run `invoke pre` after each discrete batch of changes, not only at the end.

## The specs, and what to load

**The code is the source of truth; the specs are not a second copy of it.** They carry exactly two
things the code cannot: a decision too large to fit in a comment, which edits would silently erode
if it were written nowhere; and a map — where a kind of thing lives, so a change lands in the right
place and does not break the structure already there. Anything you could learn faster by opening
the module does not belong in a spec, because a spec that restates code is a spec that will lie
about it.

**Read `specs/reference/invariants.md` before touching `src/`, on every task.** It is ~700 words
and holds the rules whose violation is *silent* — the code compiles, the gate is green, and the
system is wrong. It also indexes everything else, so it is what tells you which detail file the
task needs. Loading it is not optional and not conditional on the task looking relevant: the
invariants that bite are the ones in a subsystem you did not think you were touching.

| file (under `specs/reference/`) | what it answers | when to load |
|---|---|---|
| `invariants.md` | cross-cutting rules + the index | always |
| `rules/call-path.md` | one routed call: failure classification, `wait`, streaming, error contract | on demand |
| `rules/selection.md` | which model is picked: cooldown, demotion, priority, weights, pool health | on demand |
| `rules/model-list.md` | where the models come from, the merge into the registry, the four rebuild triggers | on demand |
| `rules/direct-by-name.md` | the paid catalog, `direct`, the alias contract | on demand |
| `rules/backends.md` | the three ports, source dispatch, lifecycle, DB schema, the journal, scoping | on demand |
| `decisions.md` | why a contested call went that way | one entry, by anchor, before proposing a mechanism |
| `mission.md` | what the library is for | rarely — it is the human entry point |

**Do not read a detail file "to be safe".** The index names what each one holds; if the task does
not touch that subject, its rules do not apply, and the cross-cutting ones are already loaded.

## Mission first, then simplicity, then the plan

The order of authority is: the mission (`specs/reference/mission.md`) → the simplest and most
reliable implementation that serves it → the plan and the recorded decisions. Plans and
`decisions.md` entries are means, not ends: they were written with less information than you have
while implementing, and a detail in them never outranks what the library is for.

When a plan step or a recorded decision pushes toward something more complex or more fragile than
the mission needs, neither implement it silently nor deviate silently:

1. **Judge it yourself first.** Most candidates do not survive: a recorded decision usually already
   names the counter-argument, and "simpler" that drops a stated requirement is not simpler. If your
   own assessment says the plan is right, follow the plan and move on.
2. **If it does survive, raise it before writing the code** — what the plan or decision says, where
   it fights the mission or reliability, what you propose instead, what it costs, and your
   recommendation.
3. **Proceed on confirmation.** If the maintainer reaffirms the plan, implement the plan as written.

**Never ask the maintainer to "choose a variant".** Every choice put to a human arrives as the
options with their trade-offs side by side, the one you recommend, and why — a question answerable
with "yes". Handing over a menu with no recommendation hands the work back.

## Executing a plan

Any request to implement a plan — "выполни очередной план", "выполни следующий план",
"выполни план X", "implement the next plan" — means all of the following, without being asked:

1. **No plan named is the normal case.** Open `specs/plans/README.md`, take the first row of its
   queue, and start. Do not ask which one. That file also states which plans must ship in one
   release together — honor it, implementing both before reporting done.
2. The plan is the suggested route, the code is the truth. Where they disagree, follow the code
   and say so; do not implement something the code has already made obsolete.
3. Run `. ./activate.sh` first. Every `invoke`/`pytest` call needs it.
4. Work in batches, and after each batch both must be green: `invoke pre` and `python -m pytest`
   (`N passed`, zero skips, zero errors). Docker must be running for testcontainer tests.
5. **Never bump the version.** Plans list `invoke ver-*` in their work order; skip that step —
   the maintainer bumps by hand.
6. **Never commit** unless explicitly asked.
7. **Spec-worthy content moves into `specs/reference/` as part of the work**, in the same batch as
   the behavior it describes — never as a final sweep. Spec-worthy means a rule that outlives this
   change: an architectural decision, a business requirement, an invariant a future reader must
   not break. Implementation detail (signatures, field names, line numbers) is not spec-worthy and
   is not copied anywhere: the code is its source of truth. Each plan names what to write and
   where; if it names nothing, nothing needs moving. Which file it lands in follows the table
   above: a rule whose violation is silent *and* cross-cutting goes to `invariants.md` and is
   stated only there; a rule local to one subsystem goes to that subsystem's file; a `decisions.md`
   entry is written only when a plausible alternative can be named in it.
8. **Never delete the plan file.** It is the review artifact: the reviewer reads the diff against
   it. Deletion happens only after review and merge, when the maintainer asks — then the file and
   its row in `specs/plans/README.md` go together.
9. **Close with a review handover** — in the final message *and* appended to the plan file as a
   `## Handover` section, so it outlives the session: which plan sections are done, what was done
   differently from the plan and why (stale plan, code disagreed, a better route), what was
   deliberately left out, decisions taken during implementation that the plan did not make, and
   the gate results. This is what the reviewer reads first.

## Reviewing an implemented plan

1. Read the plan and its `## Handover` first, then the diff against them. The plan is the contract;
   the handover already answers "why is this different", so those are not findings.
2. **Stay inside the diff.** Adjacent modules are out of scope — name a problem you notice there in
   one line and move on. Hunting in them manufactures findings without end.
3. **A finding needs a failure scenario you can show.** Run it. Without a repro it is an
   observation, not a finding, and it goes in the observations bucket.
4. **Report in three buckets** — defects / deviations / observations — never as one flat list.
   A dozen mixed items reads as a failing process even when two are bugs.
5. Do not re-open what the plan or `specs/reference/` has already settled. Disagree in one
   sentence, then move on.
6. Confirm the gate (`invoke pre`, `python -m pytest`) but do not spend the pass on it.
7. Fix nothing unless asked; report in chat.
8. **The round is done when nothing found changes runtime behavior.** Remarks about docs, naming,
   or comments are not grounds for another round. If a fix batch follows, review that batch too —
   unreviewed fix code is the usual way a review loop stops converging.
9. The largest source of findings is scope the plan never asked for. When it turns up, say so
   plainly — it is a process signal, not just a defect.

## Testing quirks

- `pytest.ini` sets `addopts = --doctest-modules`, so doctests in source files run automatically — keep them up to date.
- Tests are excluded from ruff linting (format-only); strict ruff rules apply only to `src/`.
- Every new function needs tests in the same session. Never skip.
- **Never use `pytest.skip()`, `pytest.importorskip()`, or `skipIf` to hide missing services or packages.** Tests must fail, not silently pass as skipped. Postgres and MongoDB are spun up automatically via testcontainers — no external services needed. A green run with skipped tests is a false green.

## Code style

- **Formatter**: `ruff format` (line length 99)
- **Linter**: ruff with a broad ruleset — run `invoke pre` to catch issues before committing
- **Type checker**: `pyrefly` (not mypy) — excludes `tests/`

## Code conventions

### Language
- All comments, docstrings, plan files, and in-repo docs: **English only**.

### Comments and docstrings — a hard cap, enforced by the gate
- **A docstring is at most 3 prose lines; a run of `#` comment lines at most 2.** `invoke pre` runs
  `scripts/check_docstrings.py` over `src/` and fails past that. Doctest lines do not count, so an
  executable example is free.
- **What fits in the cap:** one line saying what the thing is or returns, and — only when it is
  genuinely non-obvious from the code — one or two more for the WHY: a hidden constraint, a library
  quirk, an invariant, behavior that would surprise a reader.
- **What never goes in:** what the code does (names say that), the rationale for a design (that is
  `specs/reference/`), how to use the library (that is `docs/`), or a narration of the change being
  made. If it takes a paragraph to justify a decision, the paragraph belongs in a spec and the code
  gets a pointer at most.
- **When the cap binds, what gets cut is not a free choice.** A docstring already at the cap that
  must take something new evicts, in this order: first anything restating the signature — a
  parameter list, a type, "narrowed by any of X, Y and Z"; then anything a reader gets faster from
  the module itself. **A guarantee is never what gets cut.** A promise the signature cannot show —
  what the call never does, what it refuses, what still holds on a broken installation — is the
  reason the docstring exists. If it will not fit once the restatement is gone, then what is
  crowding it out is the thing that belongs in `specs/`. The gate measures length only; this order
  is yours to apply.
- **The surrounding file is not the standard.** A long docstring next to the one you are writing is
  debt, not precedent, and "match the local comment density" does not apply here. Touching a
  function whose docstring is over the cap means shrinking it in the same edit — that is how the
  over-long docstrings this rule was written against came to exist.

### Imports
- **No local (in-function) imports.** All imports at module top level, always. Narrow exception: a
  dispatch function that must select an optional backend package (e.g. sqlite/postgres/mongodb)
  at runtime from a string may import it locally with `# noqa: PLC0415`, so a bare `import llmbroker`
  never pulls in an optional driver.
- **No `from __future__ import annotations`** — use `X | Y` union syntax directly (Python 3.11+).
- **No re-export patterns** — when a symbol moves, update importers to point at the new module instead of leaving a shim re-export.
- **`__init__.py` files carry no code**, with two exceptions for re-exports:
  - The top-level package `src/llmbroker/__init__.py` imports and re-exports the public API
    surface (plus `__all__`), including the zero-dependency `standalone` backend. **What
    belongs there is decided by who touches it, both ways:** everything an application uses —
    entry points, the exceptions it catches, every DTO a public call returns or takes — is one
    import away and needs no submodule path, except what an optional extra would drag in. What
    only a backend or driver author builds — the port protocols, the DTOs they pass — stays in
    its own module, so a host's autocomplete on `llmbroker.` shows only what a host can use.
  - Each optional-dependency backend subpackage (`sqlite/`, `postgres/`, `mongodb/`, `aws/`,
    `vault/`) imports and re-exports its own classes (plus `__all__`) from its `__init__.py` —
    e.g. `sqlite/__init__.py` re-exports `Registry`, `Store`, `Secrets` so callers can write
    `from llmbroker.sqlite import Registry` instead of `from llmbroker.sqlite.registry import
    Registry`. These classes can't live on the top-level `__init__.py` the way `standalone` does,
    since that would force the optional driver import on a bare `import llmbroker`; re-exporting
    from the subpackage's own `__init__.py` keeps the short, compact syntax without that cost.
  - No other `__init__.py` (`broker/`, `standalone/`, and any internal-only subpackage) gets this
    treatment — **zero imports and zero code**, a docstring at most. Internal-only classes and
    functions belong in a named module, and every caller imports from that exact module.

### Plan and spec files
- **Before proposing a mechanism in a plan, check `specs/reference/decisions.md` for it.** It is an addressable registry — open the entry, not the file. A mechanism recorded there was already weighed, and the entry names the counter-argument the new proposal is about to hand-wave. Proposing it again wastes a review round. If a recorded decision is genuinely wrong now, say so explicitly in the plan and argue against the recorded reason — never re-propose in silence.
- **A plan does not argue a contested call in prose — it carries the `decisions.md` entry verbatim, in one block, and says nothing else about it.** A plan is an implementation guide: goal, work order, tests, what moves into the specs, the gate. Detail in those is a virtue and has no budget. A defence of a position is not detail — it outlives the plan, so its only home is the entry, which lands in `decisions.md` in the same batch as the behavior it explains (specs describe current state, so it is not written there ahead of the code). Arguing at length in the plan and compressing the same reasoning into an entry afterwards writes it twice and inflates the file the reviewer must read — the usual way a plan reaches several hundred lines of which a third is rhetoric. A plan past ~200 lines is worth re-reading for exactly this: if the excess is signatures, commands and test names, it has earned its size; if it is a defence of a position, that belongs in the entry and nowhere else.
- Never reference plan file paths or step numbers inside code comments or docstrings.
- Specs in `specs/` capture architectural decisions and business requirements only — not implementation details (no function signatures, field names, or internal class structure).
- **A code identifier appearing in a spec is a smell — assume it does not belong until it earns one of three exemptions.** Naming a thing ties the spec to a rename and buys nothing a reader could not get from the module. It earns its place only as (1) *navigation* — where a kind of thing lives, or what the shape of a change must be ("adding a DB backend is one new driver file"); (2) a *host-facing contract* — an exception class a host catches, a protocol it implements, a public entry point, a key in the TOML a human writes; or (3) the *name of a rejected alternative*, which is the search key that stops it being re-proposed. Tuning-knob names, field and column names, logger names, module paths used as prose, and literal configured values fail all three: state the rule without them.
- **`invariants.md` is capped at ~25 entries; past that an entry enters only by displacing another.** It is loaded on every task, so its size is a tax on all work. An entry belongs there only when breaking it is both silent and cross-cutting — a rule local to one subsystem lives in that subsystem's file, where the task itself leads a reader to it.
- Specs describe current state only, including in `decisions.md`-style rationale docs: never narrate what a removed class/parameter/field used to be called (e.g. "the `Stack` classes, `stack=`, go away") — state the current shape and, if useful, *why* it's shaped that way. Old names belong to git history, not to a living spec.
- **A rule is written in exactly one place.** Everywhere else links to it. Duplicated rules drift, and a reader cannot tell which copy is current.
- **`mission.md` is written for a human who will not read `rules/`, and every line in it obeys one test — predictive, not factual.** A statement belongs there when a reader who skips the rule files would otherwise mispredict how the library behaves, or would propose a change that fights the design without knowing it. Being true is not enough: most rules are true and still belong only in their own file. So the mission states *what* and *why*, never the mechanism that carries it out — no call counts, no HTTP-code classifications, no thresholds, no order of operations, no exception lists, no counts of what the curated list happens to hold today. That is not a style preference: every drift found between the mission and the code so far has been in a sentence that described machinery, and none in a sentence that described intent. The four sections divide as: the **requirements** say what it must do, one line each; **what it is not** states the boundaries, which are decisions and must be defended as such, not gaps; the **positioning** says what no alternative combines, in claims that survive being checked; **the design this produced** carries the load-bearing decisions, each a few lines, and is the strictest of the four. Adding an item anywhere is a claim that it changes what a reader would expect. Re-read the mission against the code whenever a subsystem it describes changes.
- **Links point one way: a rule may cite the mission, the mission never cites a rule.** `mission.md` answers *what the library is for* and *why it is shaped that way*; a rule file answers *how*, and may name the mission requirement it serves. The reverse direction is a symptom, not a formatting choice: a mission sentence that needs a link into `rules/` is asserting a mechanism, and the fix is to raise the sentence back to the level of intent, not to add the link. This is also where the mission goes stale first — every place it has drifted from the code so far was a place it described machinery.

## Dependencies and optional extras

All storage backends (sqlite, postgres, mongodb) and the secrets backends (aws, vault) are `[project.optional-dependencies]` in `pyproject.toml`. **Never add backend packages to the dev group** — they are installed in dev and CI via `uv sync --frozen --all-extras`. The dev group is for dev tooling only (pytest, invoke, pre-commit, etc.).

## Architecture notes

- **No published users yet — backward compatibility is not a constraint.** Prefer the clean API
  over a compatibility shim: rename, change a signature, or delete a command outright rather than
  keeping a deprecated path alive. This says nothing about load: llmbroker is a general-purpose
  library and must hold up at the pool's throughput limit.

- Python 3.11+ required: uses `tomllib` (stdlib) and `from datetime import UTC`
- Secrets are pluggable via `src/llmbroker/secrets.py` (env vars, AWS, Vault)
- Backends (SQLite, Redis, Postgres, MongoDB) are optional submodules — all optional extras
- LLM registry is TOML-based (`src/llmbroker/presets/freetier.toml`); `api_key_ref` fields point to env var names
