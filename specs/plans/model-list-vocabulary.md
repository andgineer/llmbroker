# "lineup" is a word we invented; the reader gets "the model list"

## Goal

Nothing a user reads calls the stored set of models a *lineup*. Documentation
and every string the program prints say **the model list** (Russian: «список
моделей»), and *pool* keeps its own job — the live routable set, which is what
failover happens across. No behavior changes.

## Why

The word is coined, and it reaches the reader without ever being defined:

- 30 occurrences across the English docs.
- The CLI's own help and errors (`omit to read this installation's lineup`,
  `this installation has no lineup yet`, `add it to your lineup`).
- The sync report an admin reads in a deploy log (`the lineup no longer carries
  OPENROUTER_API_KEY`, `the lineup dropped it too`).
- The Russian docs already avoid it — the translation says «список» and
  «курируемый список» throughout, and nothing was lost. The only place a Russian
  reader meets `lineup` is the example report output, where the word appears
  with no definition anywhere on the page.

That last point is the whole case: the term is not merely invented, it arrives
undefined precisely where it cannot be defined. The translation is also the
evidence that plain words carry it — the Russian pages say everything the
English ones do without the coinage.

`decisions.md` records nothing about vocabulary; nothing is being re-proposed.

## Scope

**In:** the documentation on both languages, and every string the program emits —
CLI `help=`/`description=`, error and refusal messages, the sync report, log
lines.

**Out:** internal identifiers (`Lineup`, `parse_lineup`, `lineup_file.py`,
`lineup.toml`, `lineup_path`, `LineupRefresher`) and the spec files, including
the name `rules/lineup-refresh.md`. A spec is read beside the code it maps to,
and renaming the prose without renaming the identifiers would make that mapping
harder, not easier. If the identifiers are ever renamed, the specs move with
them in one step; this plan does not open that.

The boundary is the reader: what a user sees changes, what an engineer reads
beside the code does not.

## The wording

- Full form on first use in a page or a message: **the model list** / «список
  моделей». After that, plain *the list* / «список».
- With *curated*, drop the noun: **the curated list** / «курируемый список» —
  "the curated model list" is a mouthful and says nothing more.
- *pool* is untouched everywhere. Where a sentence is about what can answer right
  now, it was already the right word and stays.

## Work order

One batch; it is a text change and ends green in one gate run.

1. **The strings.** `cli.py` (help and descriptions), `broker/report.py` (three
   report lines), `broker/merge.py` (the clash message, the refusal),
   `broker/presets.py` (the curated-lineup refusal), `broker/source.py` (the
   unrecognized-source error). **Re-inventory rather than trusting this list**,
   and expect it to be *shorter* than it was: `named-models-are-declared`,
   `env-one-form`, `paid-catalog-retirement` and `no-automatic-fetch` each delete
   some of the strings it was built from, and each writes its own new ones in the
   wording this plan establishes rather than in the coined word. What is left
   here is the strings none of them touched.
2. **The tests that assert on those strings** — grep for the asserted fragments
   rather than assuming; several match on wording.
3. **`docs/src/en/`** — `usage.md` carries most of it, including the section
   heading *Where the lineup lives*; then `cli.md`, `server.md`, `direct.md`,
   `secrets.md`, `index.md`.
4. **`docs/src/ru/`** — the prose already reads correctly; the only edits are the
   example outputs that quote the program's own English strings, which must match
   what the program now prints.

## Tests

No new behavior, so no new test. The existing assertions that match on message
text move with the strings, and `python -m pytest` proves nothing else broke.

## Spec updates

None — the specs are out of scope by the boundary above. Verify only that no
spec sentence quotes a program string that this plan changes.

## The queue

After `named-models-are-declared`, `env-one-form` and `paid-catalog-retirement`,
and before the skeletons. After those three, because between them they rewrite
`direct.md`, both `usage.md` pages and most of the CLI's strings — renaming first
would rename them twice, and a third of the inventory above would be renamed into
a file that then deletes it. Before `models-purity`, which relocates the prose
and log lines out of `models.py`: renaming afterwards means chasing the same
strings into their new home.

## Gate

`invoke pre` clean and `python -m pytest` green.
