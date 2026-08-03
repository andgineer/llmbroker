# One HTTP-status vocabulary

## Goal

The meaning of a provider's HTTP status is decided in five modules today, each
with its own constants. Move the vocabulary into one module and have every site
call a predicate instead of comparing numbers.

## Why

`401/403 means the key is dead` is currently encoded independently in
`router.py`, `direct.py`, `learning.py` and `upstream.py`. Invariants 10 and 11
hold only as long as four copies agree. This plan changes no behavior; it makes
the agreement structural.

## Current duplication

| module | constants it defines | what it decides |
|---|---|---|
| `broker/router.py` | `HTTP_429`, `HTTP_401`, `HTTP_403`, `_HTTP_ERROR_FLOOR`, `_DETAIL_SNIPPET`, `_is_client_error` | cool / fail over / budget |
| `direct.py` | `_HTTP_401`, `_HTTP_403`, `_HTTP_429`, `_HTTP_503`, `_HTTP_ERROR_FLOOR`, `_DETAIL_SNIPPET` | exception class |
| `broker/learning.py` | `_HTTP_UNAUTHORIZED`, `_HTTP_FORBIDDEN` | dead key / shared cooldown |
| `broker/upstream.py` | `_PERMANENT_FAILURES` (401/403/404) | retirement evidence |
| `chat.py` | literals `429, 503` in `is_rate_limit` | rate-limit test |

`_HTTP_ERROR_FLOOR = 400` and `_DETAIL_SNIPPET = 300` are declared twice
verbatim; 401/403 four times.

## Work order

1. **New `src/llmbroker/http_status.py`.** No imports from the package — pure
   predicates over an `int`, safe to import anywhere.

   - `ERROR_FLOOR = 400` — the status at or above which a response is an error.
   - `DETAIL_SNIPPET = 300` — how much of an error body is journaled.
   - `is_rate_limit(code)` — 429 or 503.
   - `is_auth_failure(code)` — 401 or 403. Name it for the meaning, not the
     numbers: every caller asks "is this key rejected", never "is this 401".
   - `is_client_error(code)` — `ERROR_FLOOR <= code < 500` and not
     `is_rate_limit` and not `is_auth_failure`. This is invariant 10's subject.
   - `is_permanent(code)` — `is_auth_failure(code)` or 404. The retirement
     evidence test; 404 belongs here and nowhere else.

   Each gets a one-line docstring naming *which decision* reads it, plus
   doctests for the boundaries (399/400/429/499/500).

2. **`chat.py`** — delete `is_rate_limit`, re-point its importers. Keep
   `_BODY_SNIPPET` local: it truncates a *200* body, a different thing from
   an error snippet, and merging the two would tie unrelated limits together.

3. **`broker/router.py`** — delete the five constants and `_is_client_error`.
   `_classify_status` reads the predicates in the same order it does now:
   `is_rate_limit` → `is_auth_failure` → `is_client_error` → fall through.
   The `CallStatus.RATE_LIMITED if code == 429 else UNAVAILABLE` split stays
   inline — it is this module's mapping, not shared vocabulary.

4. **`direct.py`** — delete the six constants. `_provider_error` reads
   `is_auth_failure` then `is_rate_limit`.

5. **`broker/learning.py`** — delete both constants; `_drive` and
   `_cooldown_applies` read `is_auth_failure`.

6. **`broker/upstream.py`** — delete `_PERMANENT_FAILURES` and the
   `http.HTTPStatus` import; `dead_entries` reads `is_permanent`.

7. **Check the public surface before deleting.** `router.HTTP_429`, `HTTP_401`
   and `HTTP_403` have no leading underscore. Run
   `grep -rn "HTTP_429\|HTTP_401\|HTTP_403" tests/ docs/` and re-point any hit;
   they are not exported from `llmbroker/__init__.py`, so nothing outside the
   repo can depend on them.

## Tests

- New `tests/test_http_status.py`: each predicate over the boundary set
  `{399, 400, 401, 403, 404, 418, 429, 499, 500, 503}`, asserting the partition
  is total and non-overlapping where it must be — in particular that
  `is_client_error` excludes every code the other three claim.
- Existing router/direct/learning tests must pass untouched. If one needs
  editing, the change is not behavior-neutral — stop and report.

## Spec updates

None. Which codes mean what is already stated in `rules/call-path.md`; a module
name is implementation detail and does not belong in a spec.

## Gate

`invoke pre` clean, `python -m pytest` with the same passed-count as before the
change (no test is added to the count except `test_http_status.py`).
