# Plan — the downstream check and the release gate: what the reviews left open

**Status: source-bound on `5dda8c429` (v1.10.2 tag).** Tooling and CI only; nothing under
`src/llmbroker/` changes.

Every item below was reproduced; repro scripts from the reviews are under
`/private/tmp/claude-501/-Users-andrei-sorokin2-projects-dinary/348cb93a-0019-47dc-a580-1a67bc50a039/scratchpad/review5/`
(`absent_check.py <scenario>`, outputs in `*.out`). None of 1–3 can occur with dinary or
echo-words as they are today; 4 blocked the v1.10.2 release.

## 1. A module skipped at collection is not a test

**Now.** `pytest.skip(allow_module_level=True)` or a module-level `importorskip` makes JUnit
write a skipped placeholder case whose id has no `::` (a module path, or a directory as
`tests.direct` / `file="tests/direct"`). When the candidate collects that module, the
placeholder disappears and `scripts/downstream.py` reports it as "ran on the baseline, absent
from the candidate", failing the host although every test inside ran and passed
(`absent_check.py module-skip-flip`, `dir-skip-flip`: `baseline: 1 passed, 1 skipped`,
`candidate: 2 passed`, `== h: FAIL ==`).

**Do.** A skipped case that stands for a collection node (no `::` in the rebuilt node id) is
never counted as "absent" when it disappears. When such a placeholder *appears* on the
candidate and tests of that node are absent, the absent tests stay regressions and the report
prints the skip message from the placeholder next to them — today it is lost
(`candidate-module-skip`).

## 2. Dotted fallback ids inside a failed collection node

**Now.** A test inherited from a class in another file gets a dotted fallback id
(`tests.test_child.TestSqlite::test_answers`). When its module fails to collect on the
candidate, the collection error is a regression and that test is *also* listed as absent, so
accepting the collection error alone still fails the host (`absent_check.py inherited`,
`inherited-accepted`).

**Do.** Map a case to its collection node through the JUnit `file` attribute (which carries
the real path) rather than through the id prefix, so the round-2 rule "tests inside a node that
failed to collect are that node's regression" covers fallback ids too.

## 3. A pytest killed by a signal after a complete report

**Now.** Once, on macOS, echo-words' baseline run wrote a complete JUnit report after
`847 passed` and then died with signal 6 at interpreter exit
(`libc++abi: ... recursive_mutex lock failed`); the runner reported a setup failure and a rerun
was clean. Any status other than 0/1 fails the host (round 1), so an exit-time crash of a
finished suite blocks the check at random.

**Do.** A negative exit status (killed by a signal) with a JUnit report that parses and whose
`<testsuite>` totals match the cases in it is judged by that report — the round-2 "absent
from the candidate" rule already catches a run that stopped early — and the report notes the
signal. Without a parseable complete report it stays a failure. Positive non-0/1 statuses
(interrupted, internal error, usage error, no tests) are unchanged.

## 4. The pages deployment cancels CI, and the publish gate then skips the release

**Now.** `ci.yml` job `primary-build` (tests + Allure report pushed to `gh-pages`) and
`docs.yml` job `deploy` share `concurrency: group: github-pages`. On a push both queue; GitHub
cancels the older pending job in a concurrency group, so `primary-build` was cancelled one
second after it started (run `34960328603`, commit `5dda8c429`). The run's conclusion became
`cancelled` although every test job and `downstream` passed, and `pip_publish.yml` — which now
requires `workflow_run.conclusion == 'success'` — skipped the v1.10.2 publish.

**Do.** Keep serializing writes to `gh-pages`, but never inside a job the release gate's
conclusion depends on:
- `primary-build` keeps running the tests with the Allure results, and no longer carries the
  `github-pages` concurrency group;
- the Allure report generation and the `gh-pages` push move to a separate workflow triggered by
  `workflow_run` of `CI` (the same shape as `pip_publish.yml`, checking out
  `workflow_run.head_sha`, downloading the results uploaded as an artifact by `primary-build`),
  and that workflow carries the `github-pages` group;
- a cancelled or failed report deployment affects neither CI's conclusion nor the publish.

Check the same pattern in dinary and echo-words only by reading their workflows, and report in
the handover whether they share it — do not edit those repos.

## 5. Report order

**Now.** The report prints one line per absent test and the pytest output tail (where the
`Exit:` reason is) once after all of them; a dinary candidate stopped early would print about
1,450 lines before the cause.

**Do.** Print the cause (exit status, `Exit:` line / output tail) before the list, and cap the
listed absent tests at 50 with a count of the rest.

## Tests

`tests/test_downstream.py`, each failing before its fix: module- and directory-level skip
placeholders disappearing (no regression) and appearing with absent tests (regressions, skip
message shown); an inherited fallback-id test inside a failed module covered by the module's
accepted entry; a signal-killed run with a complete report judged by the report, and with a
truncated report failing; the report order and cap. For §4, a test that loads both workflow
files and asserts that no job of `ci.yml` carries a concurrency group, that the report workflow
is triggered by `workflow_run` of `CI` and carries `github-pages`, and that `primary-build`
uploads the Allure results artifact.

## Gate

`. ./activate.sh`, `invoke pre`, `invoke test` (both passes), and one real
`invoke downstream --source local --working-copy` run on both hosts. GitHub Actions cannot be
run: validate the YAML and state what is unverified. No version bump, no commit.
