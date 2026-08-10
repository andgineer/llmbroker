# `env` prints the keys a preset needs, and nothing else

Plus one documentation gap found beside it: the second refresh clock is
undocumented. Both are in `cli.py` and the onboarding pages, which is why they
travel together rather than as two plans.

## Goal

`llmbroker env` exists for the one admin act the mission calls irreducible:
obtaining a provider key. It prints an `.env` skeleton — one variable per
`api_key_ref`, with the help text saying where that key comes from.

It has two forms and only one of them works everywhere. Without an argument it
reads this installation's own lineup *file*, so an installation whose registry is
a database has no file to read and gets an error telling it to name a preset
instead. The form that works is the one that names a preset, and it is what the
error message, the onboarding docs and the failure path all point at.

The no-argument form also stops meaning anything once a lineup file holds pool
entries only: the installation's own list then differs from the preset only by
what a sync has already removed from it, which is not what someone collecting
keys is asking about.

After this plan the command has one form, works on every backend, and its output
is a function of its argument.

## Why

No `decisions.md` entry. Nothing here is a contested mechanism — one of two
forms is removed because it does not work on half the supported configurations,
and the other is unchanged.

## Work order

One batch.

1. **`cli.py`.** The preset argument becomes required. `_env_own_lineup` goes,
   with the two error messages that exist only to explain its failure and the
   `home_dir` lookup it needed.
2. The `# REF already set` line goes. It reads the environment of whichever
   process happens to run the command, which makes the output of a generator
   depend on something other than what it was asked to generate — and the file
   being generated is normally the one that will supply those variables.
3. The help text says what the command is for: an `.env` skeleton for a curated
   preset. **It, and the `server.md` paragraph below, say "the model list", never
   "lineup"** — `model-list-vocabulary` strips the coined word from what a reader
   sees and comes after this plan, so writing it now only adds to its inventory.

## Tests

- The command with a preset name prints one line per distinct `api_key_ref`, in
  lineup order, each preceded by its help lines where the preset carries them.
- A ref with no `[keys]` entry still gets its variable line.
- Running it twice with the same preset gives byte-identical output, with the
  refs set in the environment and with them unset.
- The command with no argument exits non-zero and its usage names the preset
  argument.
- An unknown preset name is refused by the existing preset-name check.

## Spec updates

- **`rules/presets.md`** — the CLI section: `env` takes a preset name.

## Docs (en and ru, in step)

- Wherever onboarding shows `llmbroker env` without an argument, it names the
  preset.
- **`server.md`, one paragraph beside the existing daily-check passage.** Two
  clocks are documented as one today: the daily check against the curated preset
  is described, and the re-read that carries another node's registry edits and
  cooldowns to this one is not described at all. An operator who edits a registry
  row on a live cluster currently has no way to learn when it takes effect, and
  the honest answer — on the next call, no restart — is the reason the design
  needs no restart procedure. Say also that the re-read never interrupts calls in
  flight: a model removed while a request is using it finishes that request and
  is simply not a candidate for the next one. This is the first question the
  paragraph raises, so it answers it in place.

## The queue

Independent of everything. Small enough to take at any point; it touches only
`cli.py` and the onboarding lines in the docs. Taking it after
`named-models-are-declared` costs nothing and saves editing the same CLI section
of `rules/presets.md` twice.

## Gate

`invoke pre` clean and `python -m pytest` green.

## Handover

**Done, in one batch: every section of the plan.**

*Work order.* `cli.py`: `preset` is a required positional; `_env_own_lineup` is gone
with both of its error messages, and with it the `lineup_path` and `HOME_ENV_VAR`
imports (`home_dir` stays — the preset fetch needs it). The `# REF already set`
branch and the `os` import are gone. Help and description say "curated model list",
not "lineup".

*Done differently from the plan.* `_read_env_data` was folded into `_env_preset`
rather than kept as its own function: with only one caller left it was an
indirection, and its docstring — "a malformed lineup is the user's own file" —
described a form this plan removes. The reporting behavior it carried is unchanged.

*Tests.* `tests/test_cli_env.py` was rewritten around the fetched preset: the five
cases the plan names, plus the ones that survive from the old file (order, help
lines, distinct refs, `extra` passthrough, malformed preset, malformed `[keys]`).
The byte-identical check runs the same preset twice, once with the ref unset and
once set, and compares the two outputs. `tests/test_cli.py` lost the four env cases
that duplicated `test_cli_env.py` and the one that tested "no lineup yet"; its
leftover-`[[custom]]` case stayed and now arrives through a fetched preset.

*Docs.* `cli.md` and `secrets.md` (en+ru) show the one form. `server.md` (en+ru)
gained the second-clock paragraph beside the daily check.

*A decision the plan did not make.* The `{#no-fetch}` section said
`sync_interval=None` "stops every clock in the process", which the new paragraph
would have contradicted — the registry re-read keeps running there, since it opens
no connection. Both languages now say it stops every clock **that goes online**, and
the new paragraph says the re-read survives the switch.

*Gate.* `invoke pre` clean; `python -m pytest` → 1287 passed, zero skips.
