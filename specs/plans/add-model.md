# `add-model`: interactive paid-model picker

This plan is the suggested route; if the code has drifted from what it assumes, the code wins.
The feature emerged from the direct-client work (see `specs/plans/` siblings and
`docs/src/en/direct.md`): paid providers are few and picking their current models by hand is
tedious, so a CLI command lets a user pick a provider and model from a curated catalog and drop
a `[[custom]]` entry into their config.

## Context

- `[[custom]]` entries (`src/llmbroker/standalone/registry.py`) are user-owned models parsed
  alongside `[[llms]]`; a `custom` entry may be pooled (`pool=true`) or direct-only
  (`pool=false`, e.g. a paid model reached via `AsyncBroker.direct(name)`).
- `llmbroker preset <name>` (`src/llmbroker/cli.py`, `_cmd_preset`) downloads a curated TOML
  from the repo over `_PRESET_URL` and prints it; `preset <name> --merge <file>` refreshes the
  managed `[[llms]]`/`[keys]` in a file while keeping `[[custom]]` (via `tomli_w`).
- `llmbroker env <file>` already emits `.env` lines for `[[custom]]` keys too (their help comes
  from the shared `[keys]` table).
- The curated data is refreshed by an LLM runbook: `presets/freetier-refresh-prompt.md` (free
  pool) and now `presets/paid-catalog-refresh-prompt.md` (paid catalog).

## Design decisions

Commit to these; do not re-decide during implementation.

1. **Catalog is a git-fetched file, `presets/paid-catalog.toml`.** `add-model` downloads it with
   the same mechanism as `preset` (reserved name `paid-catalog`), so the model list refreshes
   through the repo without a package release. Schema (produced by the refresh prompt):
   `[[provider]]` with `id`, `label`, `base_url`, `api_key_ref`, `key_help`; nested
   `[[provider.models]]` with `model`, `label`, `verified`.
2. **The catalog is advisory, never authoritative over the pool.** It only feeds the menu and
   the emitted `[[custom]]` block. Keys still live in env/secrets; `add-model` writes only
   `api_key_ref` + the `[keys]` help, never a key value.
3. **Writing is append-only, comment-preserving.** `add-model` appends one rendered `[[custom]]`
   block (and, only if the ref is new to the file, one `[keys.REF]` block) to the target file.
   No parse-and-rewrite of the whole file — existing managed/custom comments survive. A
   duplicate `[keys.REF]` would be invalid TOML, so an already-present ref is skipped.
4. **Paid default is `pool=false`.** A picked model is direct-only unless `--pool` is passed.
5. **Interactive by default, flags for automation.** No `--provider`/`--model` → numbered menus
   over stdin (`input()`, no new dependency). Flags → non-interactive, validated against the
   catalog. `--into FILE` is always required (deterministic target).
6. **No runtime `/v1/models`.** Verification of model ids is the refresh prompt's job
   (web-verified, cited, fail-closed). A runtime cross-check may be added later, not now.

## Implementation

### 1. Shared helpers in `cli.py` (remove copy-paste)

- Extract the download + decode + TOML-validate body of `_cmd_preset` into
  `_fetch_preset_file(name) -> str | None` (prints errors, returns `None` on failure). Both
  `preset` and `add-model` call it.
- Extract the `tomli_w` custom-tail rendering shared with `--merge` into a helper that emits a
  `[[custom]]` block plus an optional `[keys.REF]` block as text.

### 2. `add-model` command

Signature: `llmbroker add-model --into FILE [--provider ID] [--model ID] [--name NAME] [--pool]`.

Flow:
1. `_fetch_preset_file("paid-catalog")` → parse providers.
2. Resolve selection: from flags (validate `--provider`/`--model` against the catalog; error on
   unknown) or interactively (provider menu → model menu → entry name, default = provider `id`
   → `pool?` default no).
3. Read the target file if it exists; collect existing entry names (`[[llms]]` + `[[custom]]`)
   and existing `[keys]` refs.
4. Refuse a name collision with a clear message.
5. Append the `[[custom]]` block (`name`, `base_url`, `model`, `api_key_ref`, and `pool=false`
   unless `--pool`); append `[keys.REF]` with the provider's `key_help` only if the ref is not
   already present.
6. Print a reminder to set the key (`llmbroker env FILE >> .env`) and `sync`.

Wire the subparser in `main`; `--merge`-style `PLR0911` noqa if the return count trips the hook.

### 3. Starter catalog

Land `presets/paid-catalog.toml` with a small, currently-verified set (Anthropic / OpenAI /
Google, one or two models each) so the command is usable and testable on real data immediately;
ongoing accuracy is the refresh prompt's job.

## Testing (`tests/test_cli.py`, mocked `urlopen`, no network)

- Non-interactive append: `--provider … --model … --into f` yields a `[[custom]]` block with
  `pool=false`; `--pool` yields `pool=true`; assert by parsing with `tomllib`.
- Unknown provider/model → rc 1.
- Name collision with an existing entry → rc 1.
- Ref already present in the file → `[keys]` not duplicated and the file still parses.
- Interactive path: patch `builtins.input` with a scripted sequence → correct append.
- Catalog fetch failure → rc 1.

## Docs

- `docs/src/en/direct.md` + `ru`: a short "Add a paid model" section with an `add-model` example.
- Update the command `--help`/description.

## Guardrails

- Keys never enter the catalog or the registry — only `api_key_ref` + help.
- Append-only writes; never clobber a user's file or its comments.
- The catalog is fetched, not bundled logic — a bad catalog degrades to a clear parse error, not
  a broken command.
