"""The downstream runner: host list, JUnit and type-check parsing, the verdict, and a host run
driven through a fake subprocess seam. Nothing here reaches a network or a host checkout."""

import subprocess
from pathlib import Path

import pytest
from invoke import MockContext, Result

import tasks
from scripts import downstream
from scripts.downstream import (
    Accepted,
    Host,
    HostRunError,
    Options,
    Outcome,
    Phase,
)

REPO = Path(__file__).resolve().parent.parent


def _host(**overrides) -> Host:
    values = {
        "name": "h",
        "github": "owner/h",
        "local": "../h",
        "extras": ("sqlite",),
        "setup": ("uv sync --frozen",),
        "tests": ("tests/",),
        "typecheck": ("pyrefly", "check"),
    }
    return Host(**{**values, **overrides})


def _phase(outcomes: dict[str, str], errors: tuple[str, ...] = ()) -> Phase:
    return Phase({nid: Outcome(status) for nid, status in outcomes.items()}, list(errors))


def _junit(cases: str) -> str:
    return f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{cases}</testsuite></testsuites>'


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


# ── the host list ────────────────────────────────────────────────────────────


def test_downstream_toml_names_both_hosts_with_every_key():
    hosts = downstream.load_hosts(REPO / "downstream.toml")
    assert [h.name for h in hosts] == ["dinary", "echo-words"]
    for host in hosts:
        assert all(getattr(host, key) is not None for key in downstream.HOST_KEYS)
        assert host.setup and host.tests and host.typecheck
        assert host.accepted == ()


def test_a_host_missing_a_key_is_refused(tmp_path):
    path = tmp_path / "downstream.toml"
    path.write_text('[[host]]\nname = "x"\ngithub = "o/x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="lacks local, extras, setup, tests, typecheck"):
        downstream.load_hosts(path)


def test_accepted_entries_load_under_their_host(tmp_path):
    path = tmp_path / "downstream.toml"
    path.write_text(
        """
[[host]]
name = "x"
github = "o/x"
local = "../x"
extras = []
setup = []
tests = []
typecheck = []
[[host.accepted]]
test = "tests/test_a.py::test_b"
reason = "renamed on purpose"
[[host.accepted]]
check = "has no attribute"
reason = "attribute removed"
""",
        encoding="utf-8",
    )
    (host,) = downstream.load_hosts(path)
    assert host.accepted == (
        Accepted(reason="renamed on purpose", test="tests/test_a.py::test_b"),
        Accepted(reason="attribute removed", check="has no attribute"),
    )


@pytest.mark.parametrize(
    "entry",
    [
        'test = "t"',
        'reason = "r"',
        'test = "t"\ncheck = "c"\nreason = "r"',
    ],
)
def test_an_accepted_entry_needs_a_reason_and_exactly_one_target(tmp_path, entry):
    path = tmp_path / "downstream.toml"
    path.write_text(
        '[[host]]\nname = "x"\ngithub = "o/x"\nlocal = "../x"\nextras = []\nsetup = []\n'
        f"tests = []\ntypecheck = []\n[[host.accepted]]\n{entry}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="needs a reason"):
        downstream.load_hosts(path)


# ── JUnit ────────────────────────────────────────────────────────────────────


def test_junit_outcomes_by_node_id(tmp_path):
    junit = tmp_path / "r.xml"
    junit.write_text(
        _junit(
            '<testcase classname="tests.test_a" name="test_pass" file="tests/test_a.py"/>'
            '<testcase classname="tests.test_a" name="test_fail" file="tests/test_a.py">'
            '<failure message="assert 1 == 2">E   assert 1 == 2\ntests/test_a.py:3: AssertionError'
            "</failure></testcase>"
            '<testcase classname="tests.test_a" name="test_err" file="tests/test_a.py">'
            '<error message="failed on setup">E   RuntimeError: boom</error></testcase>'
            '<testcase classname="tests.test_a" name="test_skip" file="tests/test_a.py">'
            '<skipped type="pytest.skip" message="no"/></testcase>'
            '<testcase classname="tests.test_a" name="test_xfail" file="tests/test_a.py">'
            '<skipped type="pytest.xfail" message="known"/></testcase>'
            '<testcase classname="tests.sub.test_b.TestC" name="test_m[a.b]"'
            ' file="tests/sub/test_b.py"/>'
            '<testcase classname="" name="tests.test_broken" file="tests/test_broken.py">'
            '<error message="collection failure">ImportError</error></testcase>'
            '<testcase classname="tests.test_d.TestBase" name="test_inherited"'
            ' file="tests/base.py"/>',
        ),
        encoding="utf-8",
    )
    outcomes = downstream.read_outcomes(junit, "")
    assert {nid: result.status for nid, result in outcomes.items()} == {
        "tests/test_a.py::test_pass": "passed",
        "tests/test_a.py::test_fail": "failed",
        "tests/test_a.py::test_err": "error",
        "tests/test_a.py::test_skip": "skipped",
        "tests/test_a.py::test_xfail": "xfailed",
        "tests/sub/test_b.py::TestC::test_m[a.b]": "passed",
        "tests/test_broken.py": "error",
        "tests.test_d.TestBase::test_inherited": "passed",
    }
    failure = outcomes["tests/test_a.py::test_fail"]
    assert failure.message == "assert 1 == 2"
    assert "AssertionError" in failure.text


def test_node_ids_match_what_pytest_itself_writes(tmp_path):
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_x.py").write_text(
        "import pytest\n\n"
        "class TestC:\n"
        "    @pytest.mark.parametrize('v', ['a.b'])\n"
        "    def test_m(self, v):\n"
        "        assert False\n",
        encoding="utf-8",
    )
    junit = tmp_path / "r.xml"
    command = downstream.pytest_command(
        Path(downstream.sys.executable), _host(tests=("-p", "no:cacheprovider", "tests/")), junit
    )
    subprocess.run(command, cwd=project, capture_output=True, check=False)
    assert list(downstream.read_outcomes(junit, "")) == ["tests/test_x.py::TestC::test_m[a.b]"]


def test_a_missing_junit_report_is_a_problem(tmp_path):
    with pytest.raises(HostRunError, match="no JUnit report"):
        downstream.read_outcomes(tmp_path / "candidate.xml", "Traceback: boom")


def test_a_junit_report_with_no_tests_is_a_problem(tmp_path):
    junit = tmp_path / "r.xml"
    junit.write_text(_junit(""), encoding="utf-8")
    with pytest.raises(HostRunError, match="ran no tests"):
        downstream.read_outcomes(junit, "")


# ── the verdict ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("before", "after", "regression", "preexisting"),
    [
        ("passed", "failed", True, False),
        ("passed", "error", True, False),
        ("skipped", "failed", True, False),
        ("xfailed", "failed", True, False),
        ("failed", "failed", False, True),
        ("error", "failed", False, True),
        ("passed", "passed", False, False),
        ("failed", "passed", False, False),
    ],
)
def test_comparison(before, after, regression, preexisting):
    verdict = downstream.judge(_phase({"t": before}), _phase({"t": after}))
    assert (verdict.regressions == ["t"]) is regression
    assert (verdict.preexisting == ["t"]) is preexisting
    assert verdict.failed is regression


def test_a_failing_test_only_the_candidate_has_is_a_regression():
    verdict = downstream.judge(_phase({"a": "passed"}), _phase({"a": "passed", "new": "error"}))
    assert verdict.regressions == ["new"]


def test_type_check_comparison_ignores_moved_lines_and_reports_only_added_ones():
    baseline = (
        "ERROR src/a.py:10:5-12: Object of class `X` has no attribute `y` [missing-attribute]",
        "ERROR src/b.py:3:1-4:2: Cannot find module `z` [missing-import]",
    )
    candidate = (
        "ERROR src/a.py:14:5-12: Object of class `X` has no attribute `y` [missing-attribute]",
        "ERROR src/b.py:7:1-8:2: Cannot find module `z` [missing-import]",
        "ERROR src/a.py:20:5-12: Object of class `X` has no attribute `y` [missing-attribute]",
        "ERROR src/c.py:1:1-2: Argument `str` is not assignable [bad-argument-type]",
    )
    verdict = downstream.judge(
        _phase({"t": "passed"}, baseline), _phase({"t": "passed"}, candidate)
    )
    assert verdict.added_type_errors == list(candidate[2:])
    assert verdict.failed


def test_type_errors_are_the_error_lines_of_the_output():
    output = "ERROR src/a.py:1:1-2: bad [kind]\n INFO 1 error\nWARN src/a.py:2:1-2: meh [w]\n"
    assert downstream.type_errors(output) == ["ERROR src/a.py:1:1-2: bad [kind]"]


def test_an_accepted_test_regression_does_not_fail_and_carries_its_reason():
    entry = Accepted(reason="closed broker raises now", test="tests/test_a.py::test_b")
    verdict = downstream.judge(
        _phase({"tests/test_a.py::test_b": "passed"}),
        _phase({"tests/test_a.py::test_b": "failed"}),
        [entry],
    )
    assert verdict.regressions == []
    assert verdict.accepted == [("tests/test_a.py::test_b", entry)]
    assert verdict.stale == []
    assert not verdict.failed


def test_an_accepted_type_check_substring_does_not_fail():
    line = (
        "ERROR src/a.py:1:1-2: Object of class `Broker` has no attribute `pool` [missing-attribute]"
    )
    entry = Accepted(reason="pool left the public surface", check="has no attribute `pool`")
    verdict = downstream.judge(_phase({"t": "passed"}), _phase({"t": "passed"}, (line,)), [entry])
    assert verdict.added_type_errors == []
    assert verdict.accepted == [(line, entry)]
    assert verdict.stale == []
    assert not verdict.failed


def test_an_accepted_entry_matching_nothing_failing_is_stale():
    by_test = Accepted(reason="adopted by the host", test="tests/test_a.py::test_b")
    by_check = Accepted(reason="adopted by the host", check="has no attribute `pool`")
    verdict = downstream.judge(
        _phase({"tests/test_a.py::test_b": "passed"}),
        _phase({"tests/test_a.py::test_b": "passed"}),
        [by_test, by_check],
    )
    assert verdict.stale == [by_test, by_check]
    assert not verdict.failed


def test_an_accepted_test_failing_on_both_sides_is_not_stale():
    entry = Accepted(reason="the baseline tag already carries the change", test="t")
    verdict = downstream.judge(_phase({"t": "failed"}), _phase({"t": "failed"}), [entry])
    assert verdict.preexisting == ["t"]
    assert verdict.stale == []


# ── commands ─────────────────────────────────────────────────────────────────


def test_the_install_command_carries_the_extras_and_the_source_tree(tmp_path):
    command = downstream.install_command(tmp_path / "python", tmp_path / "llmbroker", ["sqlite"])
    assert command[:3] == ["uv", "pip", "install"]
    assert command[command.index("--python") + 1] == str(tmp_path / "python")
    assert "--reinstall-package" in command
    assert command[-1] == f"llmbroker[sqlite] @ {(tmp_path / 'llmbroker').as_uri()}"


def test_an_install_without_extras_names_the_bare_package(tmp_path):
    command = downstream.install_command(tmp_path / "python", tmp_path, [])
    assert command[-1] == f"llmbroker @ {tmp_path.as_uri()}"


def test_the_type_check_is_pointed_at_the_host_venv(tmp_path):
    command = downstream.typecheck_command(tmp_path / "python", _host())
    assert command[:2] == ["pyrefly", "check"]
    assert f"--python-interpreter-path={tmp_path / 'python'}" in command


def test_the_host_env_puts_the_host_venv_first_and_drops_the_callers(tmp_path):
    env = downstream.host_env(
        tmp_path, {"PATH": "/usr/bin", "VIRTUAL_ENV": "/x", "PYTHONPATH": "t", "HOME": "/h"}
    )
    assert env["PATH"].split(downstream.os.pathsep) == [str(tmp_path / ".venv" / "bin"), "/usr/bin"]
    assert "VIRTUAL_ENV" not in env
    assert "PYTHONPATH" not in env
    assert env["HOME"] == "/h"


def test_the_package_digest_follows_content_not_bytecode(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "m.py").write_text("x = 1\n", encoding="utf-8")
    first = downstream.package_digest(tmp_path)
    (tmp_path / "a" / "__pycache__").mkdir()
    (tmp_path / "a" / "__pycache__" / "m.cpython-313.pyc").write_bytes(b"\0")
    assert downstream.package_digest(tmp_path) == first
    (tmp_path / "a" / "m.py").write_text("x = 2\n", encoding="utf-8")
    assert downstream.package_digest(tmp_path) != first


# ── sources: a real git, on repositories made here ───────────────────────────


def test_the_baseline_export_is_the_committed_ref_not_the_working_tree(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src" / "llmbroker").mkdir(parents=True)
    module = repo / "src" / "llmbroker" / "m.py"
    module.write_text("committed = True\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "first")
    module.write_text("committed = 'second'\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "second")
    module.write_text("uncommitted = True\n", encoding="utf-8")
    (repo / "src" / "llmbroker" / "untracked.py").write_text("", encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()

    tree, commit = downstream.export_baseline(repo, "HEAD~1", workdir)

    assert (tree / "src" / "llmbroker" / "m.py").read_text(encoding="utf-8") == "committed = True\n"
    assert not (tree / "src" / "llmbroker" / "untracked.py").exists()
    assert commit


def test_an_unknown_baseline_ref_is_a_problem(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "f").write_text("", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "first")
    with pytest.raises(HostRunError, match="cannot export llmbroker at 'v0.0.0'"):
        downstream.export_baseline(repo, "v0.0.0", tmp_path)


def test_a_working_copy_brings_uncommitted_files_but_not_ignored_ones(tmp_path):
    repo = tmp_path / "llmbroker"
    local = tmp_path / "h"
    local.mkdir()
    (local / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (local / "tracked.py").write_text("old\n", encoding="utf-8")
    (local / "gone.py").write_text("", encoding="utf-8")
    _git(local, "init", "-q")
    _git(local, "add", ".")
    (local / "tracked.py").write_text("new\n", encoding="utf-8")
    (local / "gone.py").unlink()
    (local / "sub").mkdir()
    (local / "sub" / "untracked.py").write_text("", encoding="utf-8")
    (local / ".venv").mkdir()
    (local / ".venv" / "python").write_text("", encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    run = downstream.HostRun(_host(), workdir)

    run.fetch(Options(working_copy=True), repo)

    assert (run.checkout / "tracked.py").read_text(encoding="utf-8") == "new\n"
    assert (run.checkout / "sub" / "untracked.py").is_file()
    assert not (run.checkout / "gone.py").exists()
    assert not (run.checkout / ".venv").exists()


# ── a host run through the fake subprocess seam ──────────────────────────────


class FakeProcesses:
    """Answers every command a host run makes, and remembers them in order."""

    def __init__(self, candidate_cases: str, candidate_type_check: tuple[int, str] = (0, "")):
        self.commands: list[str | list[str]] = []
        self.sources: dict[str, Path] = {}
        self.installed: Path | None = None
        self.candidate_cases = candidate_cases
        self.candidate_type_check = candidate_type_check

    def __call__(self, command, cwd, env, log):
        self.commands.append(command)
        argv = [command] if isinstance(command, str) else list(command)
        junit = next(
            (a.removeprefix("--junitxml=") for a in argv if a.startswith("--junitxml=")), None
        )
        if argv[:2] == ["git", "clone"]:
            Path(argv[-1]).mkdir(parents=True)
        elif argv[:3] == ["uv", "pip", "install"]:
            self.installed = self.sources[argv[-1].split(" @ ")[1]]
        elif downstream._PACKAGE_QUERY in argv:
            return 0, f"{self.installed}\n"
        elif downstream._VERSION_QUERY in argv:
            return 0, "1.10.0\n"
        elif junit is not None:
            candidate = Path(junit).name == "candidate.xml"
            cases = (
                self.candidate_cases
                if candidate
                else '<testcase classname="tests.test_a" name="test_b" file="tests/test_a.py"/>'
            )
            Path(junit).write_text(_junit(cases), encoding="utf-8")
            return (1 if "<failure" in cases else 0), ""
        elif argv[:2] == ["pyrefly", "check"] and "candidate-typecheck" in log.name:
            return self.candidate_type_check
        return 0, ""


def _llmbroker_tree(root: Path, text: str) -> Path:
    (root / "src" / "llmbroker").mkdir(parents=True)
    (root / "src" / "llmbroker" / "__init__.py").write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def layout(tmp_path, monkeypatch):
    (tmp_path / "temp").mkdir()
    monkeypatch.setattr(downstream.tempfile, "tempdir", str(tmp_path / "temp"))
    repo = _llmbroker_tree(tmp_path / "llmbroker", "candidate = True\n")
    baseline = _llmbroker_tree(tmp_path / "baseline", "baseline = True\n")
    (tmp_path / "h" / ".git").mkdir(parents=True)
    return repo, baseline


def _run_host(monkeypatch, layout, fake, host=None, options=Options()):
    repo, baseline = layout
    fake.sources = {
        repo.as_uri(): repo / "src" / "llmbroker",
        baseline.as_uri(): baseline / "src" / "llmbroker",
    }
    monkeypatch.setattr(downstream, "run_command", fake)
    return downstream.check_host(host or _host(), options, repo, baseline)


_FAILING_B = (
    '<testcase classname="tests.test_a" name="test_b" file="tests/test_a.py">'
    '<failure message="LLMRequestError: closed">E   LLMRequestError: closed</failure></testcase>'
)
_PASSING_B = '<testcase classname="tests.test_a" name="test_b" file="tests/test_a.py"/>'


def test_a_host_run_installs_the_baseline_export_then_the_checkout(monkeypatch, layout):
    fake = FakeProcesses(_PASSING_B)
    report = _run_host(monkeypatch, layout, fake)
    repo, baseline = layout
    installs = [
        c[-1] for c in fake.commands if isinstance(c, list) and c[:3] == ["uv", "pip", "install"]
    ]
    assert installs == [
        f"llmbroker[sqlite] @ {baseline.as_uri()}",
        f"llmbroker[sqlite] @ {repo.as_uri()}",
    ]
    assert report.problems == []
    assert not report.failed
    assert report.versions == {"baseline": "1.10.0", "candidate": "1.10.0"}


def test_nothing_after_the_setup_runs_uv_run(monkeypatch, layout):
    fake = FakeProcesses(_PASSING_B)
    _run_host(monkeypatch, layout, fake, _host(setup=("uv sync --frozen",)))
    first_install = next(
        i
        for i, c in enumerate(fake.commands)
        if isinstance(c, list) and c[:3] == ["uv", "pip", "install"]
    )
    after_setup = fake.commands[first_install:]
    assert len(after_setup) > 6
    for command in after_setup:
        words = command.split() if isinstance(command, str) else command
        assert not any(a == "uv" and b == "run" for a, b in zip(words, words[1:], strict=False))


def test_a_regression_fails_the_host_and_is_rendered_with_its_failure(monkeypatch, layout):
    report = _run_host(monkeypatch, layout, FakeProcesses(_FAILING_B))
    assert report.failed
    assert report.verdict is not None
    assert report.verdict.regressions == ["tests/test_a.py::test_b"]
    text = downstream.render(report, "HEAD (abc123)", keep=False)
    assert "== h: FAIL ==" in text
    assert "tests/test_a.py::test_b [failed]" in text
    assert "LLMRequestError: closed" in text


def test_an_accepted_regression_passes_the_host_and_shows_its_reason(monkeypatch, layout):
    entry = Accepted(reason="closed broker raises now", test="tests/test_a.py::test_b")
    report = _run_host(monkeypatch, layout, FakeProcesses(_FAILING_B), _host(accepted=(entry,)))
    assert not report.failed
    text = downstream.render(report, "HEAD (abc123)", keep=False)
    assert "== h: ok ==" in text
    assert "tests/test_a.py::test_b — closed broker raises now" in text


def test_a_stale_accepted_entry_is_rendered(monkeypatch, layout):
    entry = Accepted(reason="adopted", test="tests/test_a.py::test_gone")
    report = _run_host(monkeypatch, layout, FakeProcesses(_PASSING_B), _host(accepted=(entry,)))
    assert not report.failed
    assert "tests/test_a.py::test_gone — adopted" in downstream.render(report, "HEAD", keep=False)


def test_a_candidate_run_without_a_junit_report_is_a_setup_failure(monkeypatch, layout):
    fake = FakeProcesses(_PASSING_B)

    def no_candidate_report(command, cwd, env, log):
        if log.name == "candidate-pytest.log":
            return 2, "INTERNALERROR"
        return fake(command, cwd, env, log)

    repo, baseline = layout
    fake.sources = {
        repo.as_uri(): repo / "src" / "llmbroker",
        baseline.as_uri(): baseline / "src" / "llmbroker",
    }
    monkeypatch.setattr(downstream, "run_command", no_candidate_report)
    report = downstream.check_host(_host(), Options(), repo, baseline)
    assert report.failed
    assert "candidate: pytest wrote no JUnit report" in report.problems[0]
    assert "INTERNALERROR" in report.problems[0]


def test_a_type_check_that_crashes_is_a_setup_failure(monkeypatch, layout):
    report = _run_host(
        monkeypatch, layout, FakeProcesses(_PASSING_B, (2, "error: unexpected argument"))
    )
    assert report.failed
    assert "candidate: type check exited 2" in report.problems[0]


def test_an_install_that_is_not_the_requested_tree_is_a_setup_failure(monkeypatch, layout):
    fake = FakeProcesses(_PASSING_B)
    repo, baseline = layout
    fake.sources = {
        repo.as_uri(): baseline / "src" / "llmbroker",
        baseline.as_uri(): baseline / "src" / "llmbroker",
    }
    monkeypatch.setattr(downstream, "run_command", fake)
    report = downstream.check_host(_host(), Options(), repo, baseline)
    assert report.failed
    assert "candidate: the llmbroker installed" in report.problems[0]


def test_a_host_run_that_swaps_llmbroker_back_is_a_setup_failure(monkeypatch, layout):
    fake = FakeProcesses(_PASSING_B)
    repo, baseline = layout

    def resyncing_suite(command, cwd, env, log):
        result = fake(command, cwd, env, log)
        if log.name == "candidate-pytest.log":
            fake.installed = baseline / "src" / "llmbroker"
        return result

    fake.sources = {
        repo.as_uri(): repo / "src" / "llmbroker",
        baseline.as_uri(): baseline / "src" / "llmbroker",
    }
    monkeypatch.setattr(downstream, "run_command", resyncing_suite)
    report = downstream.check_host(_host(), Options(), repo, baseline)
    assert report.failed
    assert "candidate-after-run: the llmbroker installed" in report.problems[0]


def test_a_failing_setup_fails_the_host(monkeypatch, layout):
    fake = FakeProcesses(_PASSING_B)

    def failing_setup(command, cwd, env, log):
        if isinstance(command, str):
            return 1, "zstd: not found"
        return fake(command, cwd, env, log)

    repo, baseline = layout
    monkeypatch.setattr(downstream, "run_command", failing_setup)
    report = downstream.check_host(_host(), Options(), repo, baseline)
    assert report.failed
    assert "setup-1 exited 1" in report.problems[0]
    assert "zstd: not found" in report.problems[0]


def test_a_missing_sibling_checkout_fails_without_reaching_github(monkeypatch, tmp_path):
    fake = FakeProcesses(_PASSING_B)
    monkeypatch.setattr(downstream, "run_command", fake)
    report = downstream.check_host(
        _host(local="../absent"), Options(), tmp_path / "llmbroker", tmp_path
    )
    assert report.failed
    assert "no git checkout at" in report.problems[0]
    assert fake.commands == []


def test_a_github_source_clones_the_named_repository(monkeypatch, layout):
    fake = FakeProcesses(_PASSING_B)
    _run_host(monkeypatch, layout, fake, options=Options(source="github"))
    assert fake.commands[0][:2] == ["git", "clone"]
    assert "https://github.com/owner/h.git" in fake.commands[0]


def test_the_temporary_directory_is_removed_unless_kept(monkeypatch, layout):
    removed = _run_host(monkeypatch, layout, FakeProcesses(_PASSING_B))
    kept = _run_host(monkeypatch, layout, FakeProcesses(_PASSING_B), options=Options(keep=True))
    assert not removed.workdir.exists()
    assert kept.workdir.is_dir()
    assert f"kept: {kept.workdir}" in downstream.render(kept, "HEAD", keep=True)


# ── the command line ─────────────────────────────────────────────────────────


def test_an_unknown_host_is_refused():
    with pytest.raises(SystemExit):
        downstream.parse_args(["--host", "nope"], [_host()])


def test_a_working_copy_needs_the_local_source():
    with pytest.raises(SystemExit):
        downstream.parse_args(["--working-copy", "--source", "github"], [_host()])


def test_the_arguments_pick_the_host_and_the_baseline():
    options, hosts = downstream.parse_args(
        ["--host", "b", "--baseline-ref", "v1.9.0", "--keep"],
        [_host(name="a"), _host(name="b")],
    )
    assert [h.name for h in hosts] == ["b"]
    assert options == Options(keep=True, baseline_ref="v1.9.0")


def test_the_invoke_task_passes_every_option_through():
    c = MockContext(run=Result())
    tasks.downstream(c, host="dinary", source="github", baseline_ref="v1.9.0", keep=True)
    command = c.run.call_args.args[0]
    assert command.startswith("python scripts/downstream.py")
    for part in ("--host dinary", "--source github", "--baseline-ref v1.9.0", "--keep"):
        assert part in command
    assert "--working-copy" not in command


def _main_repo(tmp_path: Path) -> Path:
    (tmp_path / "downstream.toml").write_text(
        '[[host]]\nname = "h"\ngithub = "o/h"\nlocal = "../h"\nextras = []\nsetup = []\n'
        "tests = []\ntypecheck = []\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.parametrize(("regressions", "exit_code"), [([], 0), (["tests/test_a.py::test_b"], 1)])
def test_main_exits_non_zero_only_on_a_failing_host(
    monkeypatch, tmp_path, capsys, regressions, exit_code
):
    seen = {}

    def fake_export(repo, ref, workdir):
        seen["ref"] = ref
        return workdir / "llmbroker", "abc1234"

    def fake_check(host, options, repo, baseline):
        report = downstream.HostReport(host.name, tmp_path)
        report.phases = {"baseline": _phase({"tests/test_a.py::test_b": "passed"})}
        report.phases["candidate"] = _phase(
            {nid: "failed" for nid in regressions} or {"tests/test_a.py::test_b": "passed"}
        )
        report.verdict = downstream.judge(report.phases["baseline"], report.phases["candidate"])
        return report

    monkeypatch.setattr(downstream, "export_baseline", fake_export)
    monkeypatch.setattr(downstream, "check_host", fake_check)
    assert downstream.main(["--baseline-ref", "v1.9.0"], repo=_main_repo(tmp_path)) == exit_code
    assert seen["ref"] == "v1.9.0"
    assert "llmbroker baseline v1.9.0 (abc1234)" in capsys.readouterr().out


def test_main_fails_when_the_baseline_cannot_be_exported(monkeypatch, tmp_path, capsys):
    def failing_export(repo, ref, workdir):
        raise downstream.HostRunError(f"cannot export llmbroker at {ref!r}")

    monkeypatch.setattr(downstream, "export_baseline", failing_export)
    assert downstream.main(["--baseline-ref", "nope"], repo=_main_repo(tmp_path)) == 1
    assert "cannot export llmbroker at 'nope'" in capsys.readouterr().err
