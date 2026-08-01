# Stable model aliases: version-proof direct(), catalog-managed custom models

This file replaces the original add-model plan: the `add-model` command shipped
(`src/llmbroker/cli.py`) and this plan reworks it and the surfaces around it. If the code has
drifted from what the plan assumes, the code wins.

## Problem

Application code must not change when a model version changes: switching a followed model from
`claude-opus-4-8` to `claude-opus-5` should touch zero caller code — the app asks for "opus" and
llmbroker supplies whatever the curated catalog currently recommends. Today `direct()` takes the
exact registry name, and preset/catalog refreshes rename or replace models, so every published
name like `groq-llama-3.3-70b` is an invariant that the next refresh breaks. The main audience
happily runs catalog-recommended versions; pinning an exact version must stay possible for the
minority that needs it.

## Design decisions

Commit to these; do not re-decide during implementation.

1. **Two identifiers per `[[custom]]` entry, disjoint roles.**
   - `name` — full identity, following the convention the preset already uses for pool entries
     (`google-gemini-2.5-flash`, `groq-llama-3.3-70b`): the provider id, then the model id. It
     carries the version, and the registry, journal, learning, and visibility key on it exactly
     as they do for pool entries. The provider prefix is what keeps it unique — the same model id
     is served by several providers. The only new thing is who writes it: for an alias entry the
     tooling does (`add-model` creates it, merge-refresh rewrites it together with `model`),
     because it must change when the followed version changes. The parser keeps requiring `name`.
   - `alias` — the eternal handle: what the app passes to `direct("opus")`, and the id of the
     catalog line the entry follows. "Alias" is the industry term for a floating model name
     (provider docs use it for exactly this), so the permanence contract reads for free.
2. **Alias presence is the followed/pinned switch.** Alias present → the entry's provider fields
   (`model`, `name`, `base_url`, `api_key_ref`) are catalog-managed on refresh. No alias → the
   entry is entirely the user's and refresh never touches it; a pin is simply today's syntax
   (write `name`, omit `alias`).
3. **Learning resets by name change — no dedicated mechanism.** A refresh rewrites `model` and
   `name` together; journal rows for the old name orphan naturally, the new model starts clean.
   Scores learned for one version never apply to another.
4. **Catalog lines carry `alias`, unique across the whole catalog.** Contract for
   `presets/paid-catalog-refresh-prompt.md`: a published alias never disappears and never
   renames — a generation change re-points it to the successor model; version substrings in
   aliases are forbidden. A duplicate alias makes the catalog invalid (fail with a clear error,
   like any bad catalog).
5. **Refresh lives in `preset <name> --merge FILE`.** The file stays the single source of truth
   and `sync` stays offline; the CLI rewrites the file, then the user syncs as usual. No runtime
   or sync-time catalog access.
6. **`direct()` takes exactly one of `alias` (positional) or `name` (keyword-only).** Disjoint
   lookup keyspaces — no cross-uniqueness rules, self-documenting call sites, and
   `direct(name="anthropic-claude-opus-4-8")` doubles as a call-level version assertion: after a
   refresh moves the alias, that call fails loudly instead of silently running a newer model.
7. **`direct()` refuses pool (`[[llms]]`) entries with a typed error.** The pool is anonymous:
   callers reach it via `ask`/`chat`/`stream`, never by a preset-managed name. Choosing or
   debugging individual pool models from application code contradicts the mission (routing and
   per-operation learning are the broker's job).
8. **The pool gets `stream()`** so the restriction does not remove the only streaming path for
   free models: route with failover until the first delta (429/503/transport errors happen at
   request start), stream the chosen model after it, and raise on mid-stream death — failover
   after emitted deltas is impossible in any design. Ships in the same release as (7).

## Implementation

### 1. Catalog and refresh prompt

- `presets/paid-catalog.toml` — add `alias` to every `[[provider.models]]` line (`opus`,
  `fable`, `sonnet`, `gpt`, `gpt-mini`, `flash`, ...).
- `presets/paid-catalog-refresh-prompt.md` — add the alias contract from design decision 4.

### 2. Registry and model

- `src/llmbroker/models.py` — `LLMConfig` gains `alias: str | None = None`; round-trip it
  through `metadata` exactly like `custom` (`to_metadata`/`from_metadata`, :67-111). `to_metadata`
  carries a doctest that runs under `--doctest-modules` — extend it, do not break it.
- `src/llmbroker/standalone/registry.py` — `_config_from_entry` (:14) reads `alias` for
  `[[custom]]` entries; an `alias` key on a `[[llms]]` entry is a config error. Aliases must be
  unique among aliases, names among names (the latter is already enforced).

### 3. Exceptions and direct()

- `src/llmbroker/exceptions.py` — `PoolModelError(LLMRequestError)`: raised when `direct()` is
  pointed at a preset-managed entry. Message teaches the contract: pool models are anonymous,
  use `ask`/`chat`/`stream`, add a `[[custom]]` entry for direct access. Export from the
  top-level `__init__.py`.
- `src/llmbroker/broker/broker.py` (`direct`, :252) and `src/llmbroker/sync.py` — new signature
  `direct(alias: str | None = None, *, name: str | None = None)`; exactly one argument or
  `ValueError`. `alias=` searches custom entries by alias; `name=` searches custom entries by
  name; `name=` matching a non-custom entry raises `PoolModelError`; no match raises
  `UnknownModelError`, and when the given string exists in the *other* keyspace the message says
  so ("no alias 'frontier'; an entry with this name exists — call direct(name='frontier')").
- **Breaking change note for the maintainer:** the positional argument changes meaning from name
  to alias, and pool names stop working entirely. Both are deliberate; the version bump decision
  is the maintainer's.

### 4. Pool stream()

Unblocked: the router's failure surface now exists and this must reuse it rather than grow a
second one. `Router._attempt` classifies every failed attempt through one helper into a verdict
(cool down / fail over without cooling / hand back the caller's expired `wait`) and disposes of
the slot on every path including cancellation. A streaming attempt must go through that same
classification, not its own `except` chain.

- `src/llmbroker/broker/router.py` — a streaming counterpart to `chat`: identical candidate
  selection and journaling; each attempt opens a streaming request (reuse the transport streaming
  the direct client already uses in `chat.py`); any failure before the first delta cools down and
  fails over exactly like a `chat` attempt; after the first delta, errors are wrapped and raised.
  Journal one row per attempt (OK with latency to stream end and usage if the provider sends a
  final usage chunk; ERROR otherwise).
- **`wait` applies to a stream too, and needs a decision this plan does not make.** For `chat`,
  `wait` bounds slot acquisition *and* the whole attempt; expiry mid-attempt neither cools the
  model nor advances its streak, and records a latency lower bound that deprioritises it for
  equally tight callers (`architecture.md`). A stream has no single "the attempt finished"
  moment, so decide and record what the budget bounds — time to first delta, or the whole
  stream — before writing the loop. Do not leave a stream running unbounded while `chat` is
  bounded; do not cool a model for a slow *consumer*.
- The slot must be released when the consumer abandons the iterator (`break`, an exception, a
  cancelled task), not only on normal exhaustion — the `chat` path's cancellation guard has no
  equivalent inside a generator, so this needs its own `finally`.
- `src/llmbroker/broker/broker.py` — `AsyncBroker.stream(...)` with the same parameters as
  `ask`, returning an async iterator of text deltas. Async-only, like the direct client's
  streaming; the sync `Broker` gets no counterpart.

### 5. Merge-refresh in the CLI

- `src/llmbroker/cli.py`, `_merge_preset` (:126) — when the target file has any alias entries,
  fetch the catalog via `_fetch_preset_file("paid-catalog")` and, while re-emitting the custom
  tail, replace each alias entry's `model`, `name`, `base_url`, `api_key_ref` from its catalog
  line (add the `[keys.REF]` help if the ref is new to the file). Print one diff line per
  changed entry (`opus: claude-opus-4-8 -> claude-opus-5`); an alias absent from the catalog is
  a warning and the entry is kept untouched; entries without alias are never touched. No alias
  entries → no catalog fetch, `--merge` behaves as today.

### 6. add-model rework

- Menus show `alias — label (current model)`; the default entry it appends becomes an alias
  block: `alias`, machine `name` (`<provider-id>-<model-id>`), `model`, `base_url`,
  `api_key_ref`, and `pool = false` unless `--pool`.
- `--pin` writes today's name-only block instead (entry name from `--name` or the interactive
  prompt); `--name` without `--pin` is an error — an alias entry's name is machine-formed.
- Collision checks extend to aliases (an alias already used in the file is refused).

### 7. Tests

- Registry: alias parsed on `[[custom]]`, rejected on `[[llms]]`; duplicate aliases refused;
  metadata round-trip preserves alias.
- `direct()`: alias hit; name hit; both/neither → `ValueError`; pool name → `PoolModelError`;
  cross-keyspace hint in `UnknownModelError`; sync mirror.
- Stream (mocked streaming transport): failover before the first delta (429 then success);
  error after the first delta propagates and journals ERROR; deltas arrive incrementally;
  journal rows per attempt.
- CLI merge-refresh (mocked `urlopen`): alias entry rewritten (`model` + `name` + diff line
  printed); pinned entry byte-identical in the re-emitted tail; unknown alias warns and keeps
  the entry; file with no alias entries fetches no catalog; new `api_key_ref` brings its
  `[keys]` help.
- CLI add-model: alias block by default; `--pin` name block; `--name` without `--pin` errors;
  alias collision refused.
- Doctests stay green (`--doctest-modules`).

### 8. Docs

- `docs/src/en/direct.md` + `ru` — rewrite around aliases: drop the pool-model `direct` example,
  show `direct("opus")`, the pin block with `direct(name=...)`, the version-assertion trick, and
  pool `stream()`. State the alias permanence contract in one line.
- Wherever pool usage is documented, mention `stream()`.
- The hand-written block in `direct.md` is the pin template; keep it in sync with §6's `--pin`
  output. `presets/` holds data only (`freetier.toml`, `paid-catalog.toml`) and the two refresh
  runbooks — do not reintroduce a fetched template file.

## Work order and done gate

1. Catalog + refresh prompt (§1), registry/model alias (§2) — additive, land first.
2. Exceptions + `direct()` signature (§3).
3. Merge-refresh (§5), add-model rework (§6).
4. Pool `stream()` (§4) — release together with §3's restriction, since §3 removes the only
   other streaming path for pool models.
5. Docs (§8) with the batch they describe; tests (§7) with every batch.
6. Gate after every batch: `invoke pre` → no ruff/pyrefly errors, `python -m pytest` →
   `N passed` with zero skips. Version bump is the maintainer's call (breaking: see §3).

## Handover

Implemented in full: §1–§8. Gate green after every batch; final run `invoke pre` clean
(ruff, ruff-format, pyrefly, hygiene hooks) and `python -m pytest` → **849 passed**, zero
skips, zero errors (Docker up, so the postgres/mongodb/localstack/vault testcontainer
suites all ran). Version deliberately **not** bumped — the maintainer's call, and the
change is breaking (see §3).

### Decisions the plan left open

- **What `wait` bounds on a stream** (§4's explicit open question): **slot acquisition plus
  the wait for the first delta**, not the whole stream. That is the one stretch failover can
  still rescue; past the first delta the pace is the consumer's as much as the model's, so a
  wall-clock budget there would blame the model for its reader. The stream is not left
  unbounded — every read after the first delta stays under the global HTTP ceiling, so an
  endpoint that goes quiet mid-answer still dies. Recorded in `architecture.md` next to the
  existing `wait` contract.
- **Mid-stream death cools the model.** The plan said only "wrapped and raised". It goes
  through the same `_classify`/`_dispose` surface as any other failure, so a provider that
  drops connections mid-answer is cooled for the *next* caller even though this one cannot
  be rescued. This is what "reuse the router's failure surface, do not grow a second one"
  means in practice.
- **A consumer that abandons the iterator ends a successful attempt** — slot released,
  cooling/unmet-budget cleared, one `OK` row journaled. Journaling `ERROR` would teach the
  pool that a model failed because its reader stopped; journaling nothing would drop the
  attempt from the journal and break "one row per attempt".
- **The wrapper type is a new `StreamInterruptedError(LLMRequestError)`** carrying
  `llm_name` and the original error as `__cause__`, exported from the top-level package.
- **Alias fields in the entry block are ordered** `alias, name, model, base_url,
  api_key_ref, pool` (the plan's §6 order); pinned blocks are the same minus `alias`.
- **`--merge` is atomic.** A file with alias entries whose catalog fetch fails, or whose
  catalog has a duplicate alias, exits 1 with nothing written — a half-refreshed file that
  *looks* refreshed is worse than none. An alias merely absent from a valid catalog stays a
  warning with the entry untouched, exactly as specified.
- **Catalog aliases chosen** (§1): `opus`, `fable`, `sonnet`, `gpt`, `gpt-mini`, `flash`
  (see review round 3 for the one dropped after review).
- **A catalog model with no alias plus no `--pin`** is a clean error telling the user to
  pass `--pin`, rather than a silent fallback to a pinned block.

### Done differently from the plan

- **§2's "names among names (the latter is already enforced)" is not true of the file
  registry** — it never checked for duplicate names and still does not; DB registries get it
  from the primary key. Alias uniqueness *is* enforced there, as asked. Left the name case
  alone: it is pre-existing behaviour outside this diff. Worth a one-line fix later.
- **§4 asked for a `finally`; the code uses `except GeneratorExit`.** Same guarantee, but it
  distinguishes an abandoned stream (a completed attempt, journaled `OK`) from cancellation
  (re-raised untouched, unjournaled — mirroring `_attempt`), which a bare `finally` could
  not.
- **`_attempt` was refactored, not just extended.** Its record closure, its OK path and its
  failure disposal became `_record`/`_finish_ok`/`_dispose` on `Router`, keyed by a small
  `_Attempt` value. Streaming reuses all three; behaviour is unchanged (the existing router,
  wait-budget, degraded-transport and cluster-cooldown suites cover it).
- **`cli.md` was not touched.** §8 names `direct.md` (en+ru) and "wherever pool usage is
  documented"; `cli.md` never documented `add-model` or `--merge` in the first place, and
  `direct.md` documents both plus `--pin`. Flagging it rather than widening scope: a
  reviewer may want an `add-model` stanza there.

### Left out

Nothing from §1–§8. The consumer follow-up below (echo-words specs) is explicitly out of
scope and untouched.

### Spec-worthy content moved

`specs/reference/architecture.md` gained two sections — "Direct model access and stable
aliases" (the two identifiers, the followed/pinned switch, learning reset by name change,
disjoint keyspaces as a version assertion, alias permanence/uniqueness, refresh as a file
rewrite) and "Pool streaming" (failover ends at the first delta, one row per attempt,
abandonment is success) — plus the streaming `wait` rule inside the existing `wait`
contract and the two new CLI behaviours in the CLI list.
`presets/paid-catalog-refresh-prompt.md` gained §0a, the alias contract the catalog
refresher is bound by.

### Review round 1 — fixes applied

- **A non-SSE HTTP 200 now cools down and fails over** (`_stream_deltas`). It used to
  decode zero chunks, exit normally and journal `OK`: a proxy error page or a provider
  ignoring `stream` handed the caller an empty answer with no exception and no failover,
  while `chat` on the same body cooled the model and moved on. A 200 that decodes no
  chunk at all now raises `InvalidProviderResponseError` and goes through the existing
  classification. This is what the "Pool streaming" spec section already claimed.
- **`direct("<pool name>")` raises `PoolModelError` on the first hop.** The alias branch
  matched the name keyspace without checking `custom`, so the pre-alias call shape spent
  one error pointing at `direct(name=...)` only for that call to raise `PoolModelError`.
- **The abandoned-iterator claim in `architecture.md` was corrected rather than the
  mechanism.** "The slot goes back immediately" is not achievable: an async generator has
  no signal other than being closed, and the provider connection is open until then, so
  holding the slot is correct. Python closes it for `break`, an exception and cancellation
  (the last reference drops); a consumer that parks the iterator in a variable owns closing
  it. The spec now states that contract, `direct.md` (en+ru) shows `aclosing`, and a test
  pins both shapes.

Not changed, flagged for the maintainer: streaming's `note_unmet_budget` records a
time-to-first-delta budget into the same learned signal `chat` fills with a whole-attempt
budget (valid in one direction, pessimistic in the other — a semantics call, not a bug);
alias uniqueness is enforced on the file registry only, as with names; `AsyncDirectClient.stream`
has the same non-SSE blindness that was just fixed in the router, but there is no failover
there and it is outside this diff.

### Review round 2 — fixes applied

- **An SSE-framed provider error on HTTP 200 now cools down and fails over.**
  Round 1's guard counted decoded chunks, so a body of
  `data: {"error": …}` + `[DONE]` decoded one chunk, exited normally and
  journaled `OK` — an empty answer handed to the caller with no exception and
  no failover, while `chat` on the equivalent body cooled the model and moved
  on. The guard now counts chunks carrying `choices`, which is what makes a
  chunk a chat completion. Counting *deltas* instead would have rejected a
  legitimately empty answer, so both shapes are pinned by tests.
- **A name now identifies exactly one entry, and the file registry enforces it.**
  §2 assumed this was already true; it was not. It became load-bearing when §6
  started machine-forming names in the preset's own `<provider>-<model>`
  convention: `add-model --provider google --model gemini-2.5-flash` into a
  fresh file, then `preset freetier --merge`, produced a file carrying
  `google-gemini-2.5-flash` in both `[[llms]]` and `[[custom]]`. Nothing
  refused it, and every store keys on the name — a DB sync upserts, so one
  entry vanished and the alias following it started raising
  `UnknownModelError`. `Registry.load` now refuses a duplicate name across both
  arrays, and `--merge` refuses to *write* one (before touching the file, so
  atomicity holds). `add-model`'s own collision check already covered its half.
- **`direct(name=…)` resolves the custom entry, not a pool namesake.** §3 says
  "`name=` searches custom entries by name"; the code searched every entry and
  raised `PoolModelError` on the first match, so a pool entry sorted ahead of
  the user's own entry of that name made a real custom entry unreachable behind
  a false error. The name keyspace is now custom-only, with pool entries
  consulted solely to choose which error comes back.

Spec: `architecture.md` gained the name-uniqueness invariant next to the two
identifiers; `direct.md` (en+ru) documents the `--merge` refusal. Gate after the
fixes: `invoke pre` clean, `python -m pytest` → **857 passed**, zero skips.

Still not changed, and still flagged for the maintainer: streaming's
`note_unmet_budget` records a time-to-first-delta budget into the signal `chat`
fills with a whole-attempt budget (a semantics call); alias uniqueness is
enforced on the file registry only; `AsyncDirectClient.stream` has the non-SSE
blindness just fixed in the router, but has no failover to lose and is outside
this diff.

### Review round 3 — fixes applied

- **`flash-mini` is out of the paid catalog.** It named `gemini-2.5-flash` at
  the same endpoint, with the same `api_key_ref`, as the `freetier` preset's own
  pool entry — so it machine-formed to that entry's name and `add-model` refused
  it on any freetier config. Nothing was being sold there either: the billing
  tier lives in the user's Google account, not in a config field. Removing it
  before release is free; after one, the alias-permanence contract would have
  made the collision permanent.
- **The rule is now the catalog refresher's, not folklore.** §0a of
  `paid-catalog-refresh-prompt.md` states that a model a shipped preset already
  pools does not belong in the catalog, and §4 gained the mechanical check: no
  `<provider id>-<model id>` in the catalog may equal a preset pool entry's
  `name`.

Still flagged for the maintainer, deliberately unfixed in this round: `--merge`
reports only a changed `model` id, so a changed `api_key_ref` is never announced
— when a refresh edits a provider block's ref and nothing else, the merge prints
no line at all and the entry quietly starts needing an env var the user has
never set (`MissingKeyError` at the next call). The same refusal message advises
"rename the `[[custom]]` entry", which cannot work: an alias entry's name is
machine-formed on every merge, before the check runs.

## Consumer follow-up (not part of this plan)

echo-words (`spec/tickets/llmbroker-direct-streaming-client.md` and its implementation plan)
predates the shipped `direct()`: the ticket's `DirectClient(provider, model, api_key)` shape is
superseded by registry-based `direct()`, its `ECHOWORDS_API_PROVIDER`/`ECHOWORDS_API_MODEL`/
`ECHOWORDS_API_KEY` triple collapses to one alias (the key rides `api_key_ref`, which may point
at any env var), and pool `stream()` upgrades its "llmbroker pool is non-streaming" latency
assumptions. Update those specs once this ships.
