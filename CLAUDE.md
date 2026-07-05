# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
. ./activate.sh   # creates .venv and installs deps via uv; re-run after pulling
```

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
- **`__init__.py` files carry no code.** The top-level package `src/llmbroker/__init__.py` is the
  only exception: it may import and re-export the public API surface (plus `__all__`), nothing else
  — no class/function definitions, no internal-only names. Every other `__init__.py` (every
  subpackage: `sqlite/`, `postgres/`, `mongodb/`, `aws/`, `vault/`, `broker/`, `standalone/`, …) has
  **zero imports and zero code** — a docstring at most. A class or function belongs in a named
  module (e.g. `sqlite/registry.py`), and every caller — internal or external — imports it from
  that exact module (`from llmbroker.sqlite.registry import Registry`), never via a subpackage
  shortcut like `llmbroker.sqlite.Registry`.

### Plan and spec files
- Never reference plan file paths or step numbers inside code comments or docstrings.
- Specs in `specs/` capture architectural decisions and business requirements only — not implementation details (no function signatures, field names, or internal class structure).

## Dependencies and optional extras

All storage backends (sqlite, redis, postgres, mongodb) are `[project.optional-dependencies]` in `pyproject.toml`. **Never add backend packages to the dev group** — they are installed in dev and CI via `uv sync --frozen --all-extras`. The dev group is for dev tooling only (pytest, invoke, pre-commit, etc.). `fakeredis` is the exception: it is a test-only mock with no corresponding optional extra, so it stays in dev.

## Architecture notes

- Python 3.11+ required: uses `tomllib` (stdlib) and `from datetime import UTC`
- Secrets are pluggable via `src/llmbroker/secrets.py` (env vars, AWS, Vault)
- State backends are optional submodules: SQLite, Redis, Postgres, MongoDB — all optional extras
- LLM registry is TOML-based (`presets/freetier.toml`); `api_key_ref` fields point to env var names
