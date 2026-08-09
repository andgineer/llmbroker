"""Fail on a docstring or comment block longer than the cap CLAUDE.md sets.

Prose lines only: doctest lines and everything after them do not count.
"""

import ast
import sys
import tokenize
from pathlib import Path

MAX_DOCSTRING_LINES = 3
MAX_COMMENT_LINES = 2


def prose_lines(text: str) -> int:
    """Non-empty lines before the first doctest prompt."""
    lines: list[str] = []
    for raw in text.splitlines():
        if raw.strip().startswith(">>>"):
            break
        if raw.strip():
            lines.append(raw)
    return len(lines)


def docstring_offences(path: Path, tree: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        text = ast.get_docstring(node, clean=False)
        if text is None:
            continue
        count = prose_lines(text)
        if count > MAX_DOCSTRING_LINES:
            name = getattr(node, "name", "<module>")
            line = node.body[0].lineno if node.body else 1
            out.append(
                f"{path}:{line}: docstring of {name!r} is {count} lines"
                f" (max {MAX_DOCSTRING_LINES}) — move the reasoning to specs/ or docs/",
            )
    return out


def comment_offences(path: Path) -> list[str]:
    """Consecutive own-line ``#`` comments, counted as one block. A rule of dashes
    carries no prose, so a banner is its title line alone."""
    out = []
    with path.open("rb") as fh:
        tokens = list(tokenize.tokenize(fh.readline))
    run_start = 0
    run_len = 0
    previous_line = -2
    for token in tokens:
        if token.type is not tokenize.COMMENT:
            continue
        line, col = token.start
        if col != len(token.line) - len(token.line.lstrip()):
            continue  # trailing comment on a code line
        prose = any(ch.isalnum() for ch in token.string.lstrip("#"))
        if line == previous_line + 1:
            run_len += prose
        else:
            if run_len > MAX_COMMENT_LINES:
                out.append(f"{path}:{run_start}: comment block is {run_len} lines")
            run_start, run_len = line, int(prose)
        previous_line = line
    if run_len > MAX_COMMENT_LINES:
        out.append(f"{path}:{run_start}: comment block is {run_len} lines")
    return [
        f"{o} (max {MAX_COMMENT_LINES}) — say the WHY in one line or move it to specs/" for o in out
    ]


def main(paths: list[str]) -> int:
    offences: list[str] = []
    for name in paths:
        path = Path(name)
        source = path.read_text(encoding="utf-8")
        offences += docstring_offences(path, ast.parse(source))
        offences += comment_offences(path)
    for line in offences:
        print(line)
    return 1 if offences else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
