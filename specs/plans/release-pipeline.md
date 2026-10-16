# Plan — the pages deployment cancels CI, and the publish gate then skips the release

**Status: source-bound on `095c469ff`.** The GitHub workflows only; nothing under `src/` or
`scripts/` changes, so `invoke downstream` is not part of the gate.

## The defect

**Now.** `ci.yml` job `primary-build` (tests + Allure report pushed to `gh-pages`) and
`docs.yml` job `deploy` share `concurrency: group: github-pages`. On a push both queue; GitHub
cancels the older pending job in a concurrency group, so `primary-build` is cancelled seconds
after it starts. The run's conclusion becomes `cancelled` although every test job passed, and
`pip_publish.yml` — whose publish job requires `workflow_run.conclusion == 'success'` — skips
the release. v1.10.2 (run `34960328603`) and v1.10.4 are missing from PyPI for this reason.

**Do.** Keep serializing writes to `gh-pages`, but never inside a job the release gate's
conclusion depends on:
- `primary-build` keeps running the tests with the Allure results, and no longer carries the
  `github-pages` concurrency group;
- the Allure report generation and the `gh-pages` push move to a separate workflow triggered by
  `workflow_run` of `CI` (the same shape as `pip_publish.yml`, checking out
  `workflow_run.head_sha`, downloading the results uploaded as an artifact by `primary-build`),
  and that workflow carries the `github-pages` group;
- a cancelled or failed report deployment affects neither CI's conclusion nor the publish.

## The hosts — read, not edited

Checked 2026-10-16. All three hosts put `ci.yml` and `docs.yml` in the same `github-pages`
group. dinary's and echo-words' `pip_publish.yml` publish on a version tag without reading CI's
conclusion, so a cancelled run does not skip their release (and a failed one does not stop it).
lexiflux's `dockerhub.yml` requires `conclusion == 'success'`, so its image push has the same
skip. This plan does not edit those repos; the handover repeats this for their maintainers.

## Tests

New `tests/test_workflows.py`, failing before the fix: load both workflow files with PyYAML
(already a direct dependency) and assert that no job of `ci.yml` carries a concurrency group,
that the report workflow is triggered by `workflow_run` of `CI` and carries `github-pages`, and
that `primary-build` uploads the Allure results artifact.

## Gate

`. ./activate.sh`, `invoke pre`, `invoke test` (both passes). GitHub Actions cannot be run
locally: validate the YAML and state in the handover what is unverified — the first tagged
release after the change is the real check. No version bump, no commit.
