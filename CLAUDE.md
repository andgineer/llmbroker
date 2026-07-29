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
   where; if it names nothing, nothing needs moving.
8. **Never delete the plan file.** It is the review artifact: the reviewer reads the diff against
   it. Deletion happens only after review and merge, when the maintainer asks — then the file and
   its row in `specs/plans/README.md` go together.
9. **Close with a review handover**, in the final message: which plan sections are done, what was
   done differently from the plan and why (stale plan, code disagreed, a better route), anything
   deliberately left out, and the gate results. This is what the reviewer reads first.

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

### Imports
- **No local (in-function) imports.** All imports at module top level, always. Narrow exception: a
  dispatch function that must select an optional backend package (e.g. sqlite/postgres/mongodb)
  at runtime from a string may import it locally with `# noqa: PLC0415`, so a bare `import llmbroker`
  never pulls in an optional driver.
- **No `from __future__ import annotations`** — use `X | Y` union syntax directly (Python 3.11+).
- **No re-export patterns** — when a symbol moves, update importers to point at the new module instead of leaving a shim re-export.
- **`__init__.py` files carry no code**, with two exceptions for re-exports:
  - The top-level package `src/llmbroker/__init__.py` imports and re-exports the public API
    surface (plus `__all__`), including the zero-dependency `standalone` backend.
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
- Never reference plan file paths or step numbers inside code comments or docstrings.
- Specs in `specs/` capture architectural decisions and business requirements only — not implementation details (no function signatures, field names, or internal class structure).
- Specs describe current state only, including in `decisions.md`-style rationale docs: never narrate what a removed class/parameter/field used to be called (e.g. "the `Stack` classes, `stack=`, go away") — state the current shape and, if useful, *why* it's shaped that way. Old names belong to git history, not to a living spec.
- Module docstrings and code comments never document architecture in blocks (a design-essay docstring, a multi-paragraph "how this subsystem works" comment). Keep docstrings to 1-3 lines; if content explains *why* the system is shaped a certain way rather than a non-obvious local WHY, it belongs in `specs/reference/`, not in the code.

## Dependencies and optional extras

All storage backends (sqlite, redis, postgres, mongodb) are `[project.optional-dependencies]` in `pyproject.toml`. **Never add backend packages to the dev group** — they are installed in dev and CI via `uv sync --frozen --all-extras`. The dev group is for dev tooling only (pytest, invoke, pre-commit, etc.). `fakeredis` is the exception: it is a test-only mock with no corresponding optional extra, so it stays in dev.

## Architecture notes

- **No published users yet — backward compatibility is not a constraint.** Prefer the clean API
  over a compatibility shim: rename, change a signature, or delete a command outright rather than
  keeping a deprecated path alive. This says nothing about load: llmbroker is a general-purpose
  library and must hold up at the pool's throughput limit.

- Python 3.11+ required: uses `tomllib` (stdlib) and `from datetime import UTC`
- Secrets are pluggable via `src/llmbroker/secrets.py` (env vars, AWS, Vault)
- Backends (SQLite, Redis, Postgres, MongoDB) are optional submodules — all optional extras
- LLM registry is TOML-based (`presets/freetier.toml`); `api_key_ref` fields point to env var names
