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
  through `metadata` exactly like `custom` (`to_record`/`from_record`, :82-111).
- `src/llmbroker/standalone/registry.py` — `_config_from_entry` (:14) reads `alias` for
  `[[custom]]` entries; an `alias` key on a `[[llms]]` entry is a config error. Aliases must be
  unique among aliases, names among names (the latter is already enforced).

### 3. Exceptions and direct()

- `src/llmbroker/exceptions.py` — `PoolModelError(LLMRequestError)`: raised when `direct()` is
  pointed at a preset-managed entry. Message teaches the contract: pool models are anonymous,
  use `ask`/`chat`/`stream`, add a `[[custom]]` entry for direct access. Export from the
  top-level `__init__.py`.
- `src/llmbroker/broker/broker.py` (`direct`, :232) and `src/llmbroker/sync.py` — new signature
  `direct(alias: str | None = None, *, name: str | None = None)`; exactly one argument or
  `ValueError`. `alias=` searches custom entries by alias; `name=` searches custom entries by
  name; `name=` matching a non-custom entry raises `PoolModelError`; no match raises
  `UnknownModelError`, and when the given string exists in the *other* keyspace the message says
  so ("no alias 'frontier'; an entry with this name exists — call direct(name='frontier')").
- **Breaking change note for the maintainer:** the positional argument changes meaning from name
  to alias, and pool names stop working entirely. Both are deliberate; the version bump decision
  is the maintainer's.

### 4. Pool stream()

Blocked by `mission-conformance-fixes.md` (see README order): streaming failover must sit on the
corrected transport-error surface, not re-invent it.

- `src/llmbroker/broker/router.py` — a streaming counterpart to `chat`: identical candidate
  selection and journaling; each attempt opens a streaming request (reuse the transport streaming
  the direct client already uses in `chat.py`); any failure before the first delta cools down and
  fails over exactly like a `chat` attempt; after the first delta, errors are wrapped and raised.
  Journal one row per attempt (OK with latency to stream end and usage if the provider sends a
  final usage chunk; ERROR otherwise).
- `src/llmbroker/broker/broker.py` — `AsyncBroker.stream(...)` with the same parameters as
  `ask`, returning an async iterator of text deltas. Async-only, like the direct client's
  streaming; the sync `Broker` gets no counterpart.

### 5. Merge-refresh in the CLI

- `src/llmbroker/cli.py`, `_merge_preset` (:106) — when the target file has any alias entries,
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
4. Pool `stream()` (§4) — after mission-conformance-fixes; release together with §3's
   restriction.
5. Docs (§8) with the batch they describe; tests (§7) with every batch.
6. Gate after every batch: `invoke pre` → no ruff/pyrefly errors, `python -m pytest` →
   `N passed` with zero skips. Version bump is the maintainer's call (breaking: see §3).

## Consumer follow-up (not part of this plan)

echo-words (`spec/tickets/llmbroker-direct-streaming-client.md` and its implementation plan)
predates the shipped `direct()`: the ticket's `DirectClient(provider, model, api_key)` shape is
superseded by registry-based `direct()`, its `ECHOWORDS_API_PROVIDER`/`ECHOWORDS_API_MODEL`/
`ECHOWORDS_API_KEY` triple collapses to one alias (the key rides `api_key_ref`, which may point
at any env var), and pool `stream()` upgrades its "llmbroker pool is non-streaming" latency
assumptions. Update those specs once this ships.
