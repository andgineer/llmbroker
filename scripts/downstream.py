"""Run each downstream host's suite and type check on llmbroker before and after a change.

The hosts and their commands are `downstream.toml`; `invoke downstream` is the entry point.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

REPO = Path(__file__).resolve().parent.parent
HOST_KEYS = ("name", "github", "local", "extras", "setup", "tests", "typecheck")
STATUSES = ("passed", "failed", "error", "skipped", "xfailed")
FAILING = frozenset({"failed", "error"})
PACKAGE_FILES = ("*.py", "*.toml")
PYPI_JSON = "https://pypi.org/pypi/llmbroker/json"
PYPI_TIMEOUT = 30
TAIL_LINES = 30
DETAIL_LINES = 8
_LOCATION = re.compile(r"^ERROR (?P<path>.+?):\d+:\d+(?:-\d+(?::\d+)?)?: ")
_VERSION_QUERY = "import importlib.metadata as m; print(m.version('llmbroker'))"
_PACKAGE_QUERY = (
    "import importlib.util as u; print(u.find_spec('llmbroker').submodule_search_locations[0])"
)
_PYTEST_STOPS = {2: "interrupted", 3: "internal error", 4: "usage error", 5: "no tests collected"}


class HostRunError(Exception):
    """A run that cannot reach a verdict: no baseline, or a host fetch, setup or install that
    failed, or a suite that stopped early or left no report."""


@dataclass(frozen=True)
class Accepted:
    reason: str
    test: str = ""
    check: str = ""


@dataclass(frozen=True)
class Host:
    name: str
    github: str
    local: str
    extras: tuple[str, ...]
    setup: tuple[str, ...]
    tests: tuple[str, ...]
    typecheck: tuple[str, ...]
    accepted: tuple[Accepted, ...] = ()


@dataclass(frozen=True)
class Outcome:
    status: str
    message: str = ""
    text: str = ""


@dataclass
class Phase:
    outcomes: dict[str, Outcome]
    type_errors: list[str]


@dataclass
class Verdict:
    regressions: list[str] = field(default_factory=list)
    added_type_errors: list[str] = field(default_factory=list)
    accepted: list[tuple[str, Accepted]] = field(default_factory=list)
    stale: list[Accepted] = field(default_factory=list)
    preexisting: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.regressions or self.added_type_errors)


@dataclass(frozen=True)
class Options:
    source: str = "local"
    working_copy: bool = False
    keep: bool = False
    baseline_ref: str = "HEAD"
    baseline_published: bool = False


@dataclass
class HostReport:
    name: str
    workdir: Path
    problems: list[str] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)
    phases: dict[str, Phase] = field(default_factory=dict)
    verdict: Verdict | None = None

    @property
    def failed(self) -> bool:
        return bool(self.problems) or self.verdict is None or self.verdict.failed


def load_hosts(path: Path) -> list[Host]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return [_host(entry, path) for entry in data.get("host", [])]


def _host(entry: dict, path: Path) -> Host:
    missing = [key for key in HOST_KEYS if key not in entry]
    if missing:
        raise ValueError(f"{path}: host {entry.get('name', '?')!r} lacks {', '.join(missing)}")
    return Host(
        name=entry["name"],
        github=entry["github"],
        local=entry["local"],
        extras=tuple(entry["extras"]),
        setup=tuple(entry["setup"]),
        tests=tuple(entry["tests"]),
        typecheck=tuple(entry["typecheck"]),
        accepted=tuple(_accepted(item, entry["name"], path) for item in entry.get("accepted", [])),
    )


def _accepted(item: dict, host: str, path: Path) -> Accepted:
    if not item.get("reason") or bool(item.get("test")) == bool(item.get("check")):
        raise ValueError(
            f"{path}: an accepted entry of {host!r} needs a reason and one of test, check",
        )
    return Accepted(reason=item["reason"], test=item.get("test", ""), check=item.get("check", ""))


def read_outcomes(junit: Path, log_tail: str) -> dict[str, Outcome]:
    """Every test case in a pytest JUnit report, by node id; a missing or empty report raises."""
    if not junit.is_file():
        raise HostRunError(f"pytest wrote no JUnit report\n{log_tail}")
    try:
        root = ElementTree.parse(junit).getroot()  # noqa: S314 - written by the run just made
    except ElementTree.ParseError as exc:
        raise HostRunError(f"unreadable JUnit report {junit}: {exc}\n{log_tail}") from exc
    outcomes = {node_id(case): outcome(case) for case in root.iter("testcase")}
    if not outcomes:
        raise HostRunError(f"pytest ran no tests\n{log_tail}")
    return outcomes


def pytest_stop(code: int) -> str:
    """Why a pytest exit status is no verdict on the whole suite; empty for 0 and 1, the only
    statuses after which every collected test has run."""
    if code in (0, 1):
        return ""
    if code < 0:
        return f"killed by signal {-code}"
    return _PYTEST_STOPS.get(code, "unknown exit status")


def node_id(case: ElementTree.Element) -> str:
    """The pytest node id, rebuilt from the xunit1 attributes; a test whose class lives in
    another file keeps the dotted JUnit name."""
    classname = case.get("classname", "")
    name = case.get("name", "")
    file = case.get("file", "").replace("\\", "/")
    module = file.removesuffix(".py").replace("/", ".")
    if file and not classname and name == module:
        return file
    if file and (classname == module or classname.startswith(f"{module}.")):
        return "::".join([file, *filter(None, classname[len(module) :].split(".")), name])
    return f"{classname}::{name}" if classname else name


def outcome(case: ElementTree.Element) -> Outcome:
    for tag, status in (("error", "error"), ("failure", "failed")):
        element = case.find(tag)
        if element is not None:
            return Outcome(status, element.get("message", ""), element.text or "")
    skipped = case.find("skipped")
    if skipped is None:
        return Outcome("passed")
    return Outcome("xfailed" if skipped.get("type") == "pytest.xfail" else "skipped")


def type_errors(output: str) -> list[str]:
    return [line.rstrip() for line in output.splitlines() if line.startswith("ERROR ")]


def _without_location(line: str) -> str:
    return _LOCATION.sub(r"ERROR \g<path>: ", line, count=1)


def added_lines(baseline: Sequence[str], candidate: Sequence[str]) -> list[str]:
    """Candidate type-check lines the baseline lacks, compared with line and column stripped."""
    remaining = Counter(map(_without_location, baseline))
    added = []
    for line in candidate:
        key = _without_location(line)
        if remaining[key]:
            remaining[key] -= 1
        else:
            added.append(line)
    return added


def judge(baseline: Phase, candidate: Phase, accepted: Sequence[Accepted] = ()) -> Verdict:
    verdict = Verdict()
    failing = [nid for nid, result in candidate.outcomes.items() if result.status in FAILING]
    for nid in failing:
        before = baseline.outcomes.get(nid)
        if before is not None and before.status in FAILING:
            verdict.preexisting.append(nid)
        elif entry := next((a for a in accepted if a.test and a.test == nid), None):
            verdict.accepted.append((nid, entry))
        else:
            verdict.regressions.append(nid)
    for line in added_lines(baseline.type_errors, candidate.type_errors):
        if entry := next((a for a in accepted if a.check and a.check in line), None):
            verdict.accepted.append((line, entry))
        else:
            verdict.added_type_errors.append(line)
    verdict.stale = [a for a in accepted if not _matches_a_failure(a, failing, candidate)]
    return verdict


def _matches_a_failure(entry: Accepted, failing: Sequence[str], candidate: Phase) -> bool:
    if entry.test:
        return entry.test in failing
    return any(entry.check in line for line in candidate.type_errors)


def venv_python(checkout: Path) -> Path:
    return checkout / ".venv" / "bin" / "python"


def host_env(checkout: Path, base: Mapping[str, str]) -> dict[str, str]:
    """The caller's environment with this checkout's venv in charge instead of llmbroker's."""
    env = {key: value for key, value in base.items() if key not in {"VIRTUAL_ENV", "PYTHONPATH"}}
    env["PATH"] = os.pathsep.join([str(checkout / ".venv" / "bin"), base.get("PATH", "")])
    return env


def install_command(python: Path, source: Path, extras: Sequence[str]) -> list[str]:
    requirement = f"llmbroker[{','.join(extras)}]" if extras else "llmbroker"
    return [
        "uv",
        "pip",
        "install",
        "--python",
        str(python),
        "--reinstall-package",
        "llmbroker",
        f"{requirement} @ {source.as_uri()}",
    ]


def pytest_command(python: Path, host: Host, junit: Path) -> list[str]:
    """One module failing to import must not hide every other test from the comparison."""
    return [
        str(python),
        "-m",
        "pytest",
        *host.tests,
        "--continue-on-collection-errors",
        "-o",
        "junit_family=xunit1",
        f"--junitxml={junit}",
    ]


def typecheck_command(python: Path, host: Host) -> list[str]:
    return [*host.typecheck, "--output-format=min-text", f"--python-interpreter-path={python}"]


def llmbroker_digest(tree: Path) -> str:
    """What a host installs from an llmbroker tree: the package and the project metadata."""
    pyproject = tree / "pyproject.toml"
    metadata = pyproject.read_bytes() if pyproject.is_file() else b""
    return package_digest(tree / "src" / "llmbroker") + hashlib.sha256(metadata).hexdigest()


def package_digest(root: Path) -> str:
    digest = hashlib.sha256()
    paths = sorted(
        p.relative_to(root).as_posix() for pattern in PACKAGE_FILES for p in root.rglob(pattern)
    )
    for relative in paths:
        digest.update(relative.encode())
        digest.update((root / relative).read_bytes())
    return digest.hexdigest()


def tail(output: str, lines: int = TAIL_LINES) -> str:
    return "\n".join(output.splitlines()[-lines:])


def run_command(
    command: str | Sequence[str],
    cwd: Path,
    env: Mapping[str, str] | None,
    log: Path,
) -> tuple[int, str]:
    """Run to completion with stdout and stderr as one stream, also written to ``log``.
    A str is a shell line, a sequence an argv."""
    completed = subprocess.run(  # noqa: S603 - commands are downstream.toml's and this script's
        command,
        shell=isinstance(command, str),
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        check=False,
    )
    log.write_text(completed.stdout, encoding="utf-8")
    return completed.returncode, completed.stdout


def published_version() -> str:
    """The newest llmbroker version on PyPI."""
    try:
        with urllib.request.urlopen(PYPI_JSON, timeout=PYPI_TIMEOUT) as response:  # noqa: S310
            return str(json.load(response)["info"]["version"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HostRunError(
            f"cannot read the published llmbroker version from {PYPI_JSON}: {exc!r}",
        ) from exc


def release_ref(version: str, tags: Sequence[str]) -> str:
    """The `v` tag of a published version. A missing tag is an error, never a nearby tag: the
    newest tag may be a release whose publish was blocked."""
    ref = f"v{version}"
    if ref not in tags:
        raise HostRunError(
            f"llmbroker {version} is the newest release on PyPI, but there is no tag {ref}"
            " in this checkout (CI needs the tags fetched)",
        )
    return ref


def published_baseline(repo: Path, workdir: Path) -> str:
    version = published_version()
    code, output = run_command(["git", "tag", "--list", "v*"], repo, None, workdir / "tags.log")
    if code:
        raise HostRunError(f"cannot list llmbroker's tags: {tail(output)}")
    return release_ref(version, output.split())


def export_baseline(repo: Path, ref: str, workdir: Path) -> tuple[Path, str]:
    """Extract the committed tree at ``ref`` — never the working tree — and name its commit."""
    tree = workdir / "llmbroker"
    archive = workdir / "llmbroker.tar"
    commands = (
        ["git", "rev-parse", "--short", f"{ref}^{{commit}}"],
        ["git", "archive", "--format=tar", f"--output={archive}", ref],
    )
    outputs = []
    for index, command in enumerate(commands):
        code, output = run_command(command, repo, None, workdir / f"export-{index}.log")
        if code:
            raise HostRunError(f"cannot export llmbroker at {ref!r}: {tail(output)}")
        outputs.append(output)
    with tarfile.open(archive) as bundle:
        bundle.extractall(tree, filter="data")
    return tree, outputs[0].strip()


class HostRun:
    """One host's temporary checkout, its venv, and the logs of every command run in it."""

    def __init__(self, host: Host, workdir: Path) -> None:
        self.host = host
        self.workdir = workdir
        self.checkout = workdir / host.name
        self.python = venv_python(self.checkout)
        self.env = host_env(self.checkout, os.environ)
        self.started = time.monotonic()

    def say(self, message: str) -> None:
        print(f"[{self.host.name} +{time.monotonic() - self.started:.0f}s] {message}", flush=True)

    def run(
        self,
        label: str,
        command: str | Sequence[str],
        cwd: Path | None = None,
    ) -> tuple[int, str]:
        self.say(command if isinstance(command, str) else f"{label}: {shlex.join(command)}")
        return run_command(command, cwd or self.checkout, self.env, self.workdir / f"{label}.log")

    def step(self, label: str, command: str | Sequence[str], cwd: Path | None = None) -> str:
        code, output = self.run(label, command, cwd)
        if code:
            raise HostRunError(f"{label} exited {code}\n{tail(output)}")
        return output

    def fetch(self, options: Options, repo: Path) -> None:
        if options.source == "github":
            url = f"https://github.com/{self.host.github}.git"
            self.step(
                "fetch",
                ["git", "clone", "--quiet", "--depth", "1", url, str(self.checkout)],
                self.workdir,
            )
            return
        local = (repo / self.host.local).resolve()
        if not (local / ".git").exists():
            raise HostRunError(
                f"no git checkout at {local}; `--source github` clones {self.host.github}",
            )
        if not options.working_copy:
            self.step(
                "fetch",
                ["git", "clone", "--quiet", "--no-hardlinks", str(local), str(self.checkout)],
                self.workdir,
            )
            return
        listing = self.step("fetch", ["git", "ls-files", "-z", "-co", "--exclude-standard"], local)
        for name in filter(None, listing.split("\0")):
            source = local / name
            if source.exists() or source.is_symlink():
                target = self.checkout / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target, follow_symlinks=False)

    def install(self, label: str, source: Path) -> str:
        self.step(f"{label}-install", install_command(self.python, source, self.host.extras))
        return self.verify(label, source)

    def verify(self, label: str, source: Path) -> str:
        """The version of the venv's llmbroker, which must be exactly the tree at ``source``."""
        query = [str(self.python), "-c", _PACKAGE_QUERY]
        installed = Path(self.step(f"{label}-package", query).strip().splitlines()[-1])
        if package_digest(installed) != package_digest(source / "src" / "llmbroker"):
            raise HostRunError(
                f"{label}: the llmbroker installed at {installed} is not the tree at {source}",
            )
        query = [str(self.python), "-c", _VERSION_QUERY]
        return self.step(f"{label}-version", query).strip().splitlines()[-1]

    def phase(self, label: str) -> Phase:
        junit = self.workdir / f"{label}.xml"
        code, output = self.run(f"{label}-pytest", pytest_command(self.python, self.host, junit))
        if stop := pytest_stop(code):
            raise HostRunError(
                f"{label}: pytest exited {code} ({stop}), not a verdict on the whole suite"
                f"\n{tail(output)}",
            )
        try:
            outcomes = read_outcomes(junit, tail(output))
        except HostRunError as problem:
            raise HostRunError(f"{label}: {problem}") from problem
        code, output = self.run(f"{label}-typecheck", typecheck_command(self.python, self.host))
        errors = type_errors(output)
        if code and not errors:
            raise HostRunError(
                f"{label}: type check exited {code} with no error line\n{tail(output)}",
            )
        return Phase(outcomes, errors)


def check_host(host: Host, options: Options, repo: Path, baseline: Path) -> HostReport:
    workdir = Path(tempfile.mkdtemp(prefix=f"llmbroker-downstream-{host.name}-"))
    run = HostRun(host, workdir)
    report = HostReport(host.name, workdir)
    try:
        run.fetch(options, repo)
        for index, line in enumerate(host.setup, 1):
            run.step(f"setup-{index}", line)
        for label, source in (("baseline", baseline), ("candidate", repo)):
            report.versions[label] = run.install(label, source)
            report.phases[label] = run.phase(label)
            run.verify(f"{label}-after-run", source)
        report.verdict = judge(report.phases["baseline"], report.phases["candidate"], host.accepted)
    except HostRunError as problem:
        report.problems.append(str(problem))
    finally:
        if not options.keep:
            shutil.rmtree(workdir, ignore_errors=True)
    return report


def summary(phase: Phase) -> str:
    counts = Counter(result.status for result in phase.outcomes.values())
    tests = ", ".join(f"{counts[status]} {status}" for status in STATUSES if counts[status])
    return f"{tests}; type check: {len(phase.type_errors)} errors"


def _indented(text: str) -> list[str]:
    return [f"      {line}" for line in text.splitlines() if line.strip()]


def render(report: HostReport, baseline_label: str, keep: bool) -> str:
    lines = [f"== {report.name}: {'FAIL' if report.failed else 'ok'} =="]
    lines.append(
        f"llmbroker baseline {baseline_label} {report.versions.get('baseline', '?')}"
        f" -> candidate working tree {report.versions.get('candidate', '?')}",
    )
    lines += [f"{label}: {summary(phase)}" for label, phase in report.phases.items()]
    if report.verdict is not None:
        lines += _verdict_lines(report.verdict, report.phases["candidate"])
    for problem in report.problems:
        lines += ["setup failure:", *_indented(problem)]
    if keep:
        lines.append(f"kept: {report.workdir}")
    return "\n".join(lines)


def _verdict_lines(verdict: Verdict, candidate: Phase) -> list[str]:
    lines = [f"regressions ({len(verdict.regressions)}):"]
    for nid in verdict.regressions:
        result = candidate.outcomes[nid]
        lines.append(f"  {nid} [{result.status}]")
        lines += _indented("\n".join([result.message, tail(result.text, DETAIL_LINES)]))
    lines.append(f"type-check lines added ({len(verdict.added_type_errors)}):")
    lines += [f"  {line}" for line in verdict.added_type_errors]
    if verdict.accepted:
        lines.append(f"accepted ({len(verdict.accepted)}):")
        lines += [f"  {what} — {entry.reason}" for what, entry in verdict.accepted]
    if verdict.stale:
        lines.append(f"stale accepted entries, matching nothing failing ({len(verdict.stale)}):")
        lines += [f"  {entry.test or entry.check} — {entry.reason}" for entry in verdict.stale]
    if verdict.preexisting:
        lines.append(
            f"pre-existing failures, failing on the baseline too ({len(verdict.preexisting)}):",
        )
        lines += [f"  {nid}" for nid in verdict.preexisting]
    return lines


def unchanged_notice(baseline_label: str) -> str:
    return (
        f"note: llmbroker at {baseline_label} is identical to the working tree, so both runs used"
        " the same llmbroker and nothing was compared.\n"
        "      To check a committed change, pass the commit before it, e.g."
        " `invoke downstream --baseline-ref HEAD~1`, or a release tag.\n"
    )


def parse_args(argv: Sequence[str] | None, hosts: Sequence[Host]) -> tuple[Options, list[Host]]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="run only this host")
    parser.add_argument("--source", choices=("local", "github"), default="local")
    parser.add_argument(
        "--working-copy",
        action="store_true",
        help="local only: include uncommitted host files",
    )
    baseline = parser.add_mutually_exclusive_group()
    baseline.add_argument("--baseline-ref", help="llmbroker before the change (default: HEAD)")
    baseline.add_argument(
        "--baseline-published",
        action="store_true",
        help="the newest llmbroker release on PyPI as the baseline, as CI runs it",
    )
    parser.add_argument("--keep", action="store_true", help="keep the temporary directories")
    args = parser.parse_args(argv)
    if args.working_copy and args.source != "local":
        parser.error("--working-copy needs --source local")
    chosen = [host for host in hosts if args.host in (None, host.name)]
    if not chosen:
        parser.error(
            f"unknown host {args.host!r}; downstream.toml names {', '.join(h.name for h in hosts)}",
        )
    options = Options(
        args.source,
        args.working_copy,
        args.keep,
        args.baseline_ref or "HEAD",
        args.baseline_published,
    )
    return options, chosen


def main(argv: Sequence[str] | None = None, repo: Path = REPO) -> int:
    options, hosts = parse_args(argv, load_hosts(repo / "downstream.toml"))
    workdir = Path(tempfile.mkdtemp(prefix="llmbroker-downstream-baseline-"))
    try:
        ref = (
            published_baseline(repo, workdir)
            if options.baseline_published
            else options.baseline_ref
        )
        baseline, commit = export_baseline(repo, ref, workdir)
        unchanged = llmbroker_digest(baseline) == llmbroker_digest(repo)
        reports = [check_host(host, options, repo, baseline) for host in hosts]
    except HostRunError as problem:
        print(f"downstream: {problem}", file=sys.stderr)
        return 1
    finally:
        if not options.keep:
            shutil.rmtree(workdir, ignore_errors=True)
    print()
    if unchanged:
        print(unchanged_notice(f"{ref} ({commit})"))
    print("\n\n".join(render(r, f"{ref} ({commit})", options.keep) for r in reports))
    if options.keep:
        print(f"kept: {workdir}")
    return 1 if any(report.failed for report in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
