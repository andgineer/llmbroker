# The lineup file is rendered, not assembled

## Goal

Stop merging an arriving lineup **into the text** of a config file. Split the
file configuration by owner, render llmbroker's half wholesale from configs, and
never open the host's half for writing. Then split `upstream.py`, which is the
1045-line file this machinery grew inside.

This is the largest plan in the queue and the only one that deletes a mechanism
rather than tidying one.

## Why

`sync_file` assembles the target from three sources: the arriving preset text
verbatim, kept entries re-emitted under a generated header, and the file's own
`[[custom]]` blocks carried as raw dicts — then verifies the result parses back
to exactly the merge (`_check_render_faithful`). That verification exists
because the assembly is hand-rolled; it is a guard against the plan's own
complexity.

The database path has none of this. It merges configs and writes configs. The
file path can have the same shape.

The reason it does not today is that one file holds three ownerships:

| entry | owned by | what sync does to it |
|---|---|---|
| `[[llms]]` | llmbroker | rewrites wholesale |
| `[[custom]]` **with** `alias` | llmbroker owns the contents | rewrites `name`, `model`, `base_url`, `api_key_ref` |
| `[[custom]]` without `alias` | the host | never touched |

Make ownership a file boundary and the question "is this entry rewritten"
becomes "which file is it in".

## The shape

| file | holds | sync |
|---|---|---|
| the named config (`lineup.toml`, or whatever path the host gave) | pooled entries **and** alias-following entries | rendered wholesale from configs |
| sibling `custom.toml` | pinned host entries | opened read-only, always |

Sibling-by-convention has precedent in the codebase: `.env` and `store/` are
already located as `registry.path.parent / …`. The public API stays
single-path — `AsyncBroker("llms.toml")` is unchanged.

A database registry has neither file. Ownership there is already a field
(`custom` in the metadata blob) and `mirror` already respects it; nothing on
that path changes.

## Work order

Blocked by `lineup-parser` — this plan assumes one `parse_lineup`.

1. **Registry reads both files.** `standalone/registry.py::Registry.load()`
   reads its own path, then the sibling `custom.toml` if present, parsing both
   through `parse_lineup` and flagging the second's entries `custom=True`
   without an alias. Uniqueness is checked across the union, not per file.
   `key_info()` merges both `[keys]` tables, the host's file winning — a hint
   someone wrote by hand outranks a curated one, which is the rule
   `Catalog.key_help` already follows.

   Note before writing this: `load()` and `key_info()` each read and parse the
   path themselves, so a single sync source already reads one file three times
   (both methods plus the raw text). Two files per method makes that five, and
   the reads are not of one instant — the halves can disagree if the file is
   edited between them. Decide here whether the pair reads once; `lineup-parser`
   left this open on purpose, since collapsing it means either caching a parse
   or changing the two-method registry protocol, and this plan is the one that
   reshapes both.

2. **Render wholesale.** One function renders a list of `LLMConfig` plus a
   `[keys]` table to TOML text. Delete `render_merged_toml`, `_keys_tail`,
   `_check_render_faithful`, `entry_block` and `_KEPT_HEADER`. Nothing is
   preserved from the previous text, so nothing has to be verified against it.

   `KeyInfo.extra` must survive the round trip — it is a documented passthrough
   (`decisions.md::keyinfo-is-a-passthrough`) and today it survives only because
   the raw table is copied verbatim. Render it back.

3. **One sync target.** `sync_file` and `AsyncBroker._sync_registry_target`
   collapse into one operation: load current configs, merge, write. The file
   writer writes text; the registry writer calls `mirror`. Everything above the
   writer is shared, which removes the duplicated five-step merge-site assembly
   between `cli.py::_sync_target` and `broker.py::_sync_file_target`.

4. **One alias refresh.** Delete `refresh_alias_entries` (the raw-dict half).
   `refresh_alias_configs` is now the only one, because both targets carry
   configs by the time the refresh runs.

5. **`KeyEvidence`.** `present`, `keys_visible`, `keys_scoped`, `have_keys` and
   `scope` travel together through `merge_upstream`, `retirement_candidates`,
   `_removal_plan`, `sync_file` and both call sites. Resolve them once into a
   frozen value object and pass that. Drops four parameters from three
   signatures and the `noqa: PLR0913` from `merge_upstream`. `SyncReport` keeps
   its two boolean fields — they are what the report explains.

6. **Preset source.** Replace `cached_preset_text`, `local_paid_catalog_text`,
   `paid_catalog_text`, `bundled_preset_text`, `refresh_cached_preset` and the
   `bundled: bool` flag threaded through five frames with one object holding an
   explicit precedence. The bundled copy **seeds the cache on first read**
   rather than being a third branch; "never roll an alias backwards to the
   wheel's copy" then falls out of the cache entry being older, not out of a
   boolean passed down five call frames.

7. **Structural alias notices.** `AliasRefresh.notices` / `.warnings` are
   ready-to-print English produced in the merge layer. Replace with typed facts
   (alias moved; key ref changed; alias unknown). The CLI formats them for
   `print`, the broker for `logger`. Same for `SyncReport.__str__`, which moves
   out of `models.py` to a renderer next to its two consumers.

8. **Split `upstream.py`.** With the above removed the file is roughly half its
   size; split what remains by subject:

   ```
   broker/presets.py        fetch, https refusal, the PresetSource of step 6
   broker/aliases.py        AliasTarget, catalog targets, resolve_declared, refresh
   broker/keys.py           KeyEvidence, present_refs
   broker/merge.py          merge_upstream, _removal_plan, retirement, check_not_emptying
   broker/lineup_file.py    load, render, write the file target
   util/atomic.py           write_atomic
   ```

   `broker/stamps.py` imports `write_atomic` from `util/atomic.py`, so the
   stamp module stops depending on the sync engine.

9. **CLI.** `cli.py` calls the shared seam from step 3. Its imports from the
   old `upstream` drop from nine symbols to two. `add-model` writes a pinned
   entry to `custom.toml` and an alias-following entry to the lineup file —
   which is the ownership rule, applied at the point of creation.

## Tests

- `tests/test_upstream.py` (879 lines) splits along the new modules.
- `tests/test_cli.py`: `--sync` against a target with a sibling `custom.toml`;
  the sibling is byte-identical afterwards. Assert on bytes — "we did not write
  it" is the whole guarantee.
- A test that a comment inside `custom.toml` survives a sync, and that the
  lineup file is regenerated without preserving its own.
- `KeyInfo.extra` round-trips through a render.
- Alias re-pointing over both targets produces the same typed facts.

## Spec updates

- `rules/sync-merge.md` — the target is now two files by owner; the merge itself
  is unchanged. State the current shape only.
- `rules/presets.md` — one precedence, bundled seeds the cache.
- `rules/direct-aliases.md` — an alias-following entry lives in the llmbroker
  file. This is the file-level consequence of what `mission.md` already says:
  such an entry is not the host's to hand-edit.
- `decisions.md` — one new entry, `ownership-is-a-file-boundary`, naming the
  alternative that was rejected: one file rendered wholesale, which costs the
  host the comments and formatting of their own entries.

## Gate

`invoke pre` clean, `python -m pytest` green after each batch — steps 1-2, 3-5,
6-7, 8-9 are four batches, not one.
