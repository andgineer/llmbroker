# Plan — every llmbroker change is checked against the hosts that use it

**Status: source-bound on the working tree after v1.10.0 plus the direct-without-pool change.**
Tooling only: nothing under `src/llmbroker/` changes.

## Goal

Before a change is reported done, reviewed, or published, the test suites of the known
downstream hosts — dinary and echo-words — run against it. A host test that passes on the
llmbroker *before* the change and fails on the llmbroker *with* it is a regression, and a
regression is a signal that demands a recorded decision, not a veto:

- **an llmbroker defect** — fixed before the change is done;
- **an intended change** (llmbroker has no backward-compatibility constraint) — accepted
  explicitly in `downstream.toml`, with the reason and what the host must change, and the
  host adopts it after the release;
- **a host test coupled to llmbroker internals** — accepted the same way until the host
  repo fixes its test.

Nobody but the maintainer accepts a regression in a commit, and the runner never edits a host.

Context. Checking v1.10.0-plus-changes against echo-words by hand took a scratch copy of the
repo, a separate venv, two pytest runs (baseline on the locked llmbroker, candidate on the
working tree), a pyrefly run and a comparison. That procedure is what this plan turns into
one command and one CI job. Both hosts are adding contract tests that drive the real
llmbroker with only provider HTTP mocked, so their own suites are the signal; this plan does
not copy host usage into llmbroker's tests (that copy would drift from the hosts' code).

## 1. The host list — `downstream.toml` at the repo root

One `[[host]]` table per host, data only:

```toml
[[host]]
name      = "dinary"
github    = "andgineer/dinary"
local     = "../dinary"                 # relative to the llmbroker checkout
extras    = ["sqlite"]                  # extras the host installs llmbroker with
setup     = ["bash scripts/setup-test-env.sh"]
tests     = ["-p", "no:cacheprovider", "tests/"]
typecheck = ["pyrefly", "check"]

[[host]]
name      = "echo-words"
github    = "andgineer/echo-words"
local     = "../echo-words"
extras    = []
setup     = ["uv sync --frozen"]
tests     = ["-p", "no:cacheprovider", "-m", "not e2e", "tests/"]
typecheck = ["pyrefly", "check", "--project-excludes=tests", "--project-excludes=experiments"]

# An accepted regression: a host test (or type-check line) the current change is known to
# break on purpose. Empty in normal times; entries leave once the host has adopted.
# [[host.accepted]]
# test   = "tests/test_x.py::test_y"        # pytest node id, or
# check  = "substring of a type-check line"
# reason = "direct() on a closed broker raises now; the host catches LLMRequestError only"
```

Values above were read from each host on 2026-09-14 (dinary `CLAUDE.md` "Environment setup"
and `scripts/setup-test-env.sh`; echo-words `CLAUDE.md` and `.github/workflows/ci.yml`, whose
pytest step already excludes `e2e`: those tests need a built PWA and a browser and reach
llmbroker only through the same fakes). Re-read them when implementing.

## 2. The runner — `scripts/downstream.py`, invoked by `invoke downstream`

For each host (all by default, `--host NAME` to pick one):

1. **Get the host's source into a fresh temporary directory.**
   - `--source local` (default): `git clone --quiet --no-hardlinks <local> <tmp>` — the
     committed HEAD of the sibling checkout, never its working tree or its `.venv`.
   - `--working-copy` (with `local`): copy the files `git ls-files -co --exclude-standard`
     lists instead, so uncommitted host work is included.
   - `--source github`: `git clone --depth 1 https://github.com/<github>.git <tmp>`.
2. **Set it up** by running each `setup` command in `<tmp>` (shell). The host's own
   `.venv` is created inside `<tmp>`, so nothing outside the temp dir is written. A failing
   setup fails the run for that host — never a skip.
3. **Baseline — llmbroker before the change**, not the version the host has locked: the
   host's lock may be several releases old, and a failure an *earlier* release caused must
   not be charged to this change. Export llmbroker at `--baseline-ref` (default `HEAD`, the
   committed state the working tree changes) with `git archive <ref> | tar -x` into
   `<tmp-llmbroker-baseline>`, install it with `uv pip install --python
   <tmp>/.venv/bin/python --reinstall-package llmbroker
   "llmbroker[<extras>] @ file://<tmp-llmbroker-baseline>"`, then run
   `<tmp>/.venv/bin/python -m pytest <tests> --junitxml=<tmp>/baseline.xml` and the
   typecheck command with `--python-interpreter-path=<tmp>/.venv/bin/python` (capture its
   error lines).
4. **Candidate**: the same install from the llmbroker checkout itself (`file://<checkout>`,
   uncommitted changes included), then the same pytest (`candidate.xml`) and typecheck.
   **Never `uv run` inside `<tmp>` after the setup step**: `uv run` re-syncs the venv to
   the host's lock and silently puts the locked llmbroker back.
5. **Compare.** Parse both JUnit files into `{nodeid: outcome}`. A regression is a test
   that passed (or was skipped/xfailed) on the baseline and failed or errored on the
   candidate. A test failing on both is pre-existing — the host is broken against llmbroker
   before this change too (its own defect, or an earlier llmbroker change it has not
   adopted) — and is reported, not counted. A pytest run that produced no JUnit file (crash,
   collection abort) is a failure. For the typecheck, a regression is an error line present
   on the candidate and absent from the baseline (message lines with line numbers
   stripped). A regression matching a `[[host.accepted]]` entry is reported as accepted,
   with its reason, and does not fail the run; an accepted entry that matches nothing
   failing is reported as stale so it gets removed.
6. **Report** per host: the baseline ref and the candidate, baseline and candidate counts,
   every unaccepted regression with its node id and the first lines of its failure, accepted
   regressions with reasons, stale accepted entries, typecheck lines added, and pre-existing
   failures as a separate short list. Exit non-zero on any unaccepted regression or setup
   failure. Remove temp dirs unless `--keep` is passed (print their paths).

Keep the runner a plain script with small functions (clone, setup, run, parse, compare,
report) so the parsing and comparison are unit-testable without cloning anything; the
invoke task is a thin wrapper.

**Do not:** run the hosts' frontend suites or e2e tests; modify the sibling checkouts or
their venvs in any way; cache host venvs between runs (a stale venv is the failure mode
this exists to avoid); make a missing sibling checkout silently fall back to GitHub — say
so and fail, `--source github` is explicit.

**Tests** — `tests/test_downstream.py`, no network, no cloning:
- parsing a JUnit file with passed, failed, errored, skipped and xfailed cases;
- comparison: passed→failed is a regression, failed→failed is pre-existing, passed→passed
  and failed→passed are neither, a test only in the candidate that fails is a regression;
- accepted entries: a matching regression (by node id, or by type-check substring) does not
  fail the run and is reported with its reason; an entry matching nothing is reported stale;
- the baseline install exports `--baseline-ref`, not the working tree;
- typecheck comparison ignores line-number differences and reports only added lines;
- a missing candidate JUnit file is a failure;
- `downstream.toml` loads and every host has the required keys;
- the install command carries the extras and the checkout path, and nothing in the
  post-install steps invokes `uv run`.

## 3. CI — a `downstream` job in `.github/workflows/ci.yml`

Ubuntu, Python 3.13, after the same `uv sync --frozen --all-extras` the other jobs use:
`uv run invoke downstream --source github --baseline-ref <latest v* tag>` (fetch tags in
the checkout step): in CI the question is what the version about to be published breaks
compared with the last published one. Both hosts are public, so no token. The job
belongs to the `CI` workflow, and `pip_publish.yml` runs only on a completed `CI` run, so a
host regression blocks the PyPI release. Check how `pip_publish.yml` decides success
(`workflow_run` fires on `completed`, not only on success) and make sure a failed
`downstream` job actually stops the publish; if it does not today for any failing job, make
the publish job require `github.event.workflow_run.conclusion == 'success'`.

## 4. The rule — `CLAUDE.md`

- "Non-negotiable done gate": add `invoke downstream` → no regressions, required for any
  change under `src/` (not for docs-only or spec-only changes), run once at the end of the
  work rather than after every batch (it takes minutes).
- "Executing a plan": the handover states the `invoke downstream` result per host, and
  classifies every regression as one of the three kinds in the Goal. A defect is fixed. For
  an intended change or a coupled host test the executor does **not** add an accepted
  entry itself: it names the host test, why it breaks, and the host-side change, and the
  maintainer decides whether to accept.
- "Reviewing an implemented plan": confirming the gate includes `invoke downstream`, and
  checking that each regression's classification holds — a defect passed off as intended is
  a finding.
- Common commands table: `invoke downstream`, with `--host`, `--working-copy`,
  `--source github`, `--baseline-ref`, `--keep`.

Nothing here is spec-worthy for `specs/reference/`: it is repository process, and its home
is `CLAUDE.md`.

## Gate

`. ./activate.sh`, `invoke pre`, `invoke test` (both passes), and one real
`invoke downstream` run (both hosts, `--source local`) whose report is pasted into the
handover. No version bump, no commit.

## Handover

### Sections done

All four, including the amendment (baseline at `--baseline-ref`, `[[host.accepted]]`, the
classification rule):

1. `downstream.toml` — both hosts, no accepted entries, a commented example of one.
2. `scripts/downstream.py` + `invoke downstream` (`--host`, `--source`, `--working-copy`,
   `--baseline-ref`, `--keep`) + `tests/test_downstream.py` (53 tests, every case the plan lists).
3. `downstream` job in `ci.yml`; `pip_publish.yml` fixed (see below).
4. `CLAUDE.md` — done gate item 3 with the three regression kinds, the handover rule under
   "Executing a plan", the review check, the commands-table row. Nothing moved to `specs/reference/`.

### Deviations from the plan, and why

- **dinary's type check is `pyrefly check --project-excludes=tests`**, not `pyrefly check`.
  Re-reading dinary as the plan asks: its gate is the pre-commit pyrefly hook, which excludes
  `tests` exactly like echo-words' does.
- **Baseline export is `git archive --output=<tar> <ref>` plus Python `tarfile`
  (`filter="data"`)**, not a `| tar -x` shell pipe. Same tree, no shell, one failure point per
  command.
- **Layout:** one baseline export per run, shared by the hosts; each host's clone sits at
  `<workdir>/<host>` with JUnit files and per-command logs beside it, not inside the checkout.
- **A regression's failure text is the message plus the last 8 lines of the traceback**, not
  the first lines: pytest puts the `E` lines and the location at the end.
- **Node ids are real pytest node ids** (`tests/test_x.py::TestC::test_m[p]`): the host suite
  runs with `-o junit_family=xunit1`, whose `file` attribute lets the runner rebuild them. This is
  what an accepted `test` entry matches and what a reviewer can re-run. A test inherited from a
  class in another file falls back to the dotted `classname::name`.

### Decisions the plan did not make

- **`--continue-on-collection-errors` on the host suite.** Without it one module that fails
  to import stops the whole pytest run, so every other regression stays hidden.
- **The install is verified, not trusted.** After each install, and again after each suite
  and type check, the runner hashes the `*.py`/`*.toml` of the `llmbroker` the host venv
  imports and compares that hash with the source tree. A mismatch fails the host as a setup
  failure. Baseline and candidate usually carry the same version number, so a reused uv build
  cache, or a host test that re-syncs the venv, would otherwise pass silently.
- **Host environment:** `VIRTUAL_ENV` and `PYTHONPATH` are dropped, and the host's
  `.venv/bin` goes first on `PATH`, so `pyrefly` is the host's pinned one (in CI,
  `andgineer/uv-venv` exports llmbroker's `VIRTUAL_ENV`).
- **Fail on "no signal", not on "0 found":** a type check that exits non-zero with no `ERROR`
  line, or a JUnit report with zero test cases, is a failure. The type check runs with
  `--output-format=min-text` (one line per error).
- **Accepted entries:** each needs a `reason` and exactly one of `test`/`check`, or loading
  fails. A `test` entry is stale when its node id is not failing on the candidate. A `check`
  entry is stale when no candidate type-check line contains its text. An entry for a test
  that fails on both sides is pre-existing, not stale.
- **CI baseline is `git tag --list 'v[0-9]*' --sort=-v:refname --no-contains HEAD | head -n 1`.**
  Taken literally, "the latest `v*` tag" on a release push is the release commit's own tag,
  which would make baseline equal candidate exactly when the check matters. Checked locally:
  HEAD gives `v1.10.0`, and HEAD~1 (the v1.10.0 release commit) gives `v1.9.0`.
- **The new files are marked `git add --intent-to-add`.** `pre-commit run --all-files` only
  sees tracked files, so without this `invoke pre` passed without linting them. This is not a
  commit; `git reset <file>` undoes it.
- A test for the invoke wrapper (`MockContext`) and for `main`.

### `pip_publish.yml` — what I found

- **Any failed CI run could publish.** `workflow_run` with `types: [completed]` fires on
  failure too, and neither job checked the conclusion. A tagged commit whose CI failed on any
  job, the test matrix included, still went to PyPI. Fixed: `publish` now requires
  `github.event.workflow_run.conclusion == 'success'`.
- **The publish could build the wrong commit.** In a `workflow_run` workflow,
  `actions/checkout` checks out the tip of the default branch, not the commit CI tested. Say
  CI for commit A is still running when a tagged commit B is pushed. A's green run then finds
  B's tag at the tip and publishes B, even if B's own CI fails. Fixed: both checkouts pin
  `ref: ${{ github.event.workflow_run.head_sha }}`. This goes past the plan's literal fix, but
  without it a failed `downstream` job does not always stop the publish.
- Observation, not touched: "Create Release" passes `tag_name: <version>` with no `v` and no
  target commit, so that tag lands on the branch tip.

### Not verified live

GitHub Actions was not run. What was checked: both workflows parse (`yaml.safe_load` and the
`check-yaml` hook), and the tag query above. Never exercised: `--source github` clones (both
hosts assumed public), dinary's `setup-test-env.sh` on ubuntu-latest (it `sudo apt-get`s
`zstd`/`sqlite3` if they are missing), invoke's pty under Actions, and the `workflow_run`
conclusion/`head_sha` behaviour. `--working-copy` was not run against the real siblings, as
briefed. A unit test covers it on a git repository created in `tmp_path`.

### Left out

Nothing the plan asks for.

### Gate

- `invoke pre`: every hook passed; pyrefly `0 errors (25 suppressed)`. The 25 suppressions
  already existed; the new code adds none.
- `invoke test`: pass 1 `1741 passed`, pass 2 (coarse clock) `1250 passed, 491 deselected`.
  No failures, errors or skips.
- `invoke downstream --source local` (both hosts, final script):

```
== dinary: ok ==
llmbroker baseline HEAD (2c6f91f6a) 1.10.0 -> candidate working tree 1.10.0
baseline: 1442 passed; type check: 0 errors
candidate: 1442 passed; type check: 0 errors
regressions (0):
type-check lines added (0):

== echo-words: ok ==
llmbroker baseline HEAD (2c6f91f6a) 1.10.0 -> candidate working tree 1.10.0
baseline: 826 passed; type check: 0 errors
candidate: 826 passed; type check: 0 errors
regressions (0):
type-check lines added (0):
```

  No regressions, so nothing to classify. This change touches no `src/`, so baseline and
  candidate are the same llmbroker, and the run proves the tooling, not a library change.
  Both hosts' committed HEADs lock llmbroker 1.9.0; each ran on 1.10.0 here.
  echo-words has uncommitted work from another session (`tests/test_llmbroker_contract.py`).
  `--source local` cloned committed HEAD, so that work was not part of the run.
- **Negative control:** a scratch clone of llmbroker (in the session scratchpad, not this
  repo) with `SchemaVersionError` removed from the package exports in its working tree, run
  against echo-words. Result: `== echo-words: FAIL ==`, `candidate: 816 passed, 1 error`, and
  regression `tests/test_broker.py [error]` with
  `ImportError: cannot import name 'SchemaVersionError' from 'llmbroker'`. So an unchanged
  version number did not mask the change, and the other tests still ran.

### Files changed

- `downstream.toml` (new)
- `scripts/downstream.py` (new)
- `tests/test_downstream.py` (new)
- `tasks.py` — `downstream` task
- `.github/workflows/ci.yml` — `downstream` job
- `.github/workflows/pip_publish.yml` — conclusion guard, `head_sha` checkouts
- `CLAUDE.md`
- this plan — this section
