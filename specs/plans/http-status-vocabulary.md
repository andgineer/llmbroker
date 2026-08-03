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

## Handover

### Done

All seven work-order steps. `src/llmbroker/http_status.py` is the single vocabulary
(`ERROR_FLOOR`, `DETAIL_SNIPPET`, and the predicates `is_rate_limit`, `is_unavailable`,
`is_auth_failure`, `is_client_error`, `is_permanent`, each with boundary doctests). Every status
number in the package now lives behind one of them. `chat.is_rate_limit` is gone;
`router.py`, `direct.py`, `learning.py` and `upstream.py` no longer define status constants and
read the predicates instead. Step 7's grep over `tests/`/`docs/` found only `tests/test_chat.py`;
nothing in `docs/` referenced the deleted names.

### Done differently from the plan

- **`http.HTTPStatus` stays imported in `broker/upstream.py`.** The plan asked for its removal
  along with `_PERMANENT_FAILURES`, but the import has a second, unrelated user: the preset fetch
  maps `urllib.error.HTTPError.code == HTTPStatus.NOT_FOUND` onto "preset not found". That is a
  *fetch* status, not a provider status, and belongs to no shared vocabulary. Only
  `_PERMANENT_FAILURES` was deleted.

- **A sixth predicate, `is_unavailable`, was added for router's inline split.** The plan keeps
  router's `RATE_LIMITED` / `UNAVAILABLE` choice inline — correct, it is router's own mapping onto
  a journal status — but the choice still has to test the code. Making that test a predicate
  obeys the plan's own rule for naming (`is_auth_failure`, not `is_401`): the caller asks "is the
  provider down, or is this key's quota spent", never "is this 503". A public numeric constant
  would have been a number-named name in the module that exists to remove them.

- **Predicates take `int`, so the four `Call.http_status` sites guard for `None`.** `http_status`
  is `int | None`; the old `in (401, 403)` form absorbed `None` silently. Call sites now read
  `row.http_status is not None and is_auth_failure(row.http_status)` — same truth value, and the
  predicates stay total functions over an `int` as the plan specified.

- **`tests/test_chat.py` was edited.** The plan says an edit to an existing test means the change
  is not behavior-neutral — that reading does not hold here: the four `test_is_rate_limit_*` cases
  imported a symbol step 2 deletes, so they had to move. They are re-covered in
  `tests/test_http_status.py`. No other existing test changed.

### Decisions taken during implementation

- **`test_http_status.py` asserts over integer ranges inside a test, not by parametrizing over
  them.** The partition properties (every 4xx is exactly one of the three; nothing outside 4xx is
  a client error; `is_permanent` is exactly 401/403/404) are each one test that loops. Only the
  plan's ten-code boundary set is parametrized. Parametrizing a `range()` yields one pytest node
  per integer — hundreds of nodes asserting the same property, which inflates the suite count
  without adding a case that could fail independently.

### Deliberately left out

Nothing. No spec update, per the plan's own "Spec updates: None" — `rules/call-path.md` already
states which codes mean what, and a module name is not spec-worthy.

### Gate

- `invoke pre` — clean (ruff, ruff-format, pyrefly `0 errors`, hygiene hooks).
- `python -m pytest` — **1177 passed**, zero failures, zero skips. Baseline was 1162: −4
  (`test_chat.py`) +19 (`test_http_status.py`: 14 cases + 5 doctests). The plan expected an
  unchanged count outside the new file; the −4 is the moved `is_rate_limit` coverage.

### Review outcome

Reviewed in the implementing session — a self-review, which is a weaker check than an independent
pass, so it leaned on executable verification rather than inspection:

- **No defects.** All five pre-change predicates were reimplemented verbatim from the parent commit
  and diffed against the new module over every status in 100–599, together with router's
  `CallStatus` split: zero divergences.
- The one place a silent change could have hidden — `_PERMANENT_FAILURES` held `HTTPStatus` enum
  members and is compared against plain `int`s from the journal — was checked directly: `IntEnum`
  hashes as its integer value, so the old membership test was already true for 401/403/404 and the
  rewrite changes no retirement verdict.
- The only line whose *form* changed rather than its constants (the 429/503 split, condition and
  branches both inverted) was mutation-tested: inverting it fails 10+ integration tests across all
  six backend configurations. Covered, if indirectly.

**One open decision for the maintainer:** whether `is_unavailable` should exist. It is a name the
plan did not ask for, with one caller; it exists so `429` need not appear in `router.py`. The
alternative is a public numeric constant. Either is defensible — reversing it is a two-line change.
