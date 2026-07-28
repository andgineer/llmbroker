# Typed exceptions for provisioning and schema failures

**Source of truth: https://github.com/andgineer/llmbroker/issues/11** — the deliverable is the
functionality described there. This plan is the suggested route; if the code has drifted from what
the plan assumes, the issue wins.

## Context

Two host-visible failure conditions are raised as bare `RuntimeError`, and both surface through
the same calls (`snapshot`, `count`, `ask`, `chat`, `record_quality`, all of which run
`ensure_pool()` and a lazy `ensure_schema()`):

- **Empty registry** — `broker/catalog.py:57`, `provision()`. Benign and expected: nothing has
  been synced yet.
- **Schema-version mismatch** — `sqlite/driver.py:64`, `postgres/driver.py:79`,
  `mongodb/driver.py:66`. Fatal: the operator must act, and the message says how.

A host cannot separate them without matching on message text, so it writes
`except RuntimeError` for the benign one and silently swallows the fatal one — reporting "no
providers configured" when the real state is a schema the release cannot use.

The library is already inconsistent with itself here: `exceptions.py` types the *request-time*
empty pool as `NoLLMAvailableError(reason="empty_pool")`, while the *provision-time* empty
registry stays untyped.

Nothing inside `src/` catches `RuntimeError`, so no internal control flow depends on the current
type.

## 1. Exception types

In `exceptions.py`, alongside the existing `LLMRequestError` tree:

- `LLMBrokerError(RuntimeError)` — base for lifecycle failures that are not per-request.
  **Subclassing `RuntimeError` is deliberate**: every host currently catching `RuntimeError` keeps
  working, which is what makes this shippable as an additive change rather than a break.
- `EmptyRegistryError(LLMBrokerError)` — the registry holds no configs. Its docstring points at
  `NoLLMAvailableError(reason="empty_pool")` as the request-time sibling so the two "empty"
  conditions are discoverable from each other; they stay separate types because they answer
  different questions (nothing configured vs nothing usable right now).
- `SchemaVersionError(LLMBrokerError)` — the store holds a schema version this release cannot use.
  Carry the found and expected versions as attributes so a host can report them without parsing
  the message.

`LLMRequestError` stays rooted at `Exception` — request errors and lifecycle errors are different
axes, and re-parenting it would change what existing `except` clauses catch.

## 2. Raise sites

Replace the bare raises, messages unchanged:

- `broker/catalog.py:57` → `EmptyRegistryError`.
- `sqlite/driver.py:64`, `postgres/driver.py:79`, `mongodb/driver.py:66` → `SchemaVersionError`,
  passing the found/expected versions to the constructor.

Export both from the top-level package `__init__.py` with the rest of the public API surface — a
host that cannot import the type cannot catch it.

## 3. Tests

- `tests/test_catalog.py` — provisioning an empty registry raises `EmptyRegistryError`, and the
  message is unchanged.
- `tests/test_schema_migration.py` — a version mismatch raises `SchemaVersionError` carrying the
  found and expected versions; assert it for every backend the module already covers.
- Backward compatibility, its own case: `except RuntimeError` still catches both. This is the
  property that lets the change ship without a major bump, so it is asserted, not assumed.
- `tests/test_broker.py` — `EmptyRegistryError` propagates out of `snapshot()`/`count()` (the host
  entry points), so catching it at the host boundary is actually possible.

## 4. Specs and docs

- `specs/reference/decisions.md` — state the rule: every failure state a host is expected to
  handle has its own exception type; lifecycle failures subclass `RuntimeError` for
  backward compatibility.
- `docs/src/en/` — wherever the embedding/status flow is documented, show catching
  `EmptyRegistryError` for the "not configured yet" path instead of a broad `RuntimeError`.

## Work order and done gate

1. Types in `exceptions.py` + package exports (§1).
2. Raise sites (§2).
3. Tests (§3), specs and docs (§4).
4. `invoke ver-feature` — additive, no break.
5. Gate after every batch: `invoke pre` → no ruff/pyrefly errors, `python -m pytest` → `N passed`
   with zero skips.

## Consumer follow-up (not part of this plan)

dinary's `src/dinary/api/controllers/llm.py` (`llm_status`, `set_provider_disabled`) narrows its
`except RuntimeError` to `EmptyRegistryError` once this ships.
</content>
