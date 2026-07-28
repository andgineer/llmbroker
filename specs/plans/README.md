# Implementation plans

Order for the plans queued now. A plan that is fully implemented and merged is deleted (anything
spec-worthy moves into `specs/` first) and its row here goes with it.

## Order

| # | Plan | Issue | Blocked by | Notes |
|---|---|---|---|---|
| 1 | `typed-exceptions.md` | #11 | — | Smallest, purely additive; supplies the `SchemaVersionError` #3 raises |
| 2 | `journal-stats-window.md` | — | — | The only one with a waiting consumer; ship with #1 in one release |
| 3 | `sqlite-schema-version-table.md` | #12 | #1 | No waiting consumer; wants the typed error from #1 |

Pre-existing plans in this directory (`add-model.md`, `llm-judge.md`,
`mission-conformance-fixes.md`) are outside this ordering — their status was not reviewed here.

## Why this order

**#1 first because #3 depends on it.** The schema-marker plan raises on a version mismatch, and
the right type for that is `SchemaVersionError` from #1. Landing #3 first means either raising a
bare `RuntimeError` and converting it a week later, or inventing a second type. #1 is also the
smallest change in the queue and cannot break a host: the new types subclass `RuntimeError`, so
every existing `except RuntimeError` keeps working.

**#1 and #2 ship in one release.** Both are consumed by the same file in dinary
(`src/dinary/api/controllers/llm.py`): #2 supplies the windowed statistics its LLM screen needs,
#1 lets the same function narrow `except RuntimeError` to `EmptyRegistryError`. Released together,
the host bumps the dependency once and edits that controller once. They touch disjoint code here
(`exceptions.py` and the raise sites vs the journal read path), so the order between them does not
matter — only that neither waits for the other's release.

**#2 is the only one blocking someone.** dinary's `specs/plans/llm-view-status-details.md` is
written against a windowed journal aggregate that does not exist yet; until it ships, that plan
either waits or reimplements the journal's record model inside the host. #1 and #3 improve
correctness with no one waiting.

**#3 last.** Its breakage is latent — it bites on the next SQLite schema-version change, and the
one host that hit it already carries a workaround (a `PRAGMA user_version = 0` line in a
migration). Nothing degrades while it waits, and going last lets it use #1's typed error from the
first commit.

No shared files across the three: `exceptions.py` + raise sites (#1), the journal read path and a
new `broker/stats.py` (#2), `sqlite/driver.py`'s schema section (#3). They can run in parallel if
#3 starts after #1's types exist.
</content>
