"""The gate that enforces the docstring and comment cap CLAUDE.md sets."""

from pathlib import Path

from scripts.check_docstrings import (
    comment_offences,
    docstring_offences,
    main,
    prose_lines,
)


def _module(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "m.py"
    path.write_text(source, encoding="utf-8")
    return path


def _docstring_offences(tmp_path: Path, source: str) -> list[str]:
    import ast

    return docstring_offences(_module(tmp_path, source), ast.parse(source))


# ── prose_lines ──────────────────────────────────────────────────────────────


def test_blank_lines_are_not_prose():
    assert prose_lines("one\n\ntwo\n") == 2


def test_a_doctest_and_everything_after_it_is_free():
    assert prose_lines("one\n\n>>> f()\n1\nstill not counted\n") == 1


# ── docstrings ───────────────────────────────────────────────────────────────


def test_three_prose_lines_pass(tmp_path):
    assert _docstring_offences(tmp_path, '"""one\ntwo\nthree"""\n') == []


def test_a_fourth_prose_line_is_an_offence(tmp_path):
    offences = _docstring_offences(tmp_path, '"""one\ntwo\nthree\nfour"""\n')
    assert len(offences) == 1
    assert "is 4 lines" in offences[0]


def test_an_executable_example_costs_nothing(tmp_path):
    source = '"""one\n\n>>> 1 + 1\n2\n>>> 2 + 2\n4\n"""\n'
    assert _docstring_offences(tmp_path, source) == []


def test_a_function_docstring_is_named_in_the_offence(tmp_path):
    source = 'def f():\n    """one\n    two\n    three\n    four"""\n'
    assert "'f'" in _docstring_offences(tmp_path, source)[0]


def test_classes_and_methods_are_checked_too(tmp_path):
    source = 'class C:\n    def m(self):\n        """a\n        b\n        c\n        d"""\n'
    assert "'m'" in _docstring_offences(tmp_path, source)[0]


# ── comments ─────────────────────────────────────────────────────────────────


def test_two_comment_lines_pass(tmp_path):
    assert comment_offences(_module(tmp_path, "# one\n# two\nx = 1\n")) == []


def test_a_third_comment_line_is_an_offence(tmp_path):
    offences = comment_offences(_module(tmp_path, "# one\n# two\n# three\nx = 1\n"))
    assert len(offences) == 1
    assert "is 3 lines" in offences[0]


def test_a_rule_of_dashes_carries_no_prose(tmp_path):
    source = "# ────────────────\n# one\n# two\nx = 1\n"
    assert comment_offences(_module(tmp_path, source)) == []


def test_a_banner_title_counts_as_its_one_line(tmp_path):
    source = "# ── Title ───────\n# one\n# two\nx = 1\n"
    assert "is 3 lines" in comment_offences(_module(tmp_path, source))[0]


def test_a_trailing_comment_is_not_part_of_a_block(tmp_path):
    source = "# one\n# two\nx = 1  # three\n"
    assert comment_offences(_module(tmp_path, source)) == []


def test_separate_blocks_are_counted_separately(tmp_path):
    source = "# one\n# two\nx = 1\n# three\n# four\ny = 2\n"
    assert comment_offences(_module(tmp_path, source)) == []


# ── the entry point ──────────────────────────────────────────────────────────


def test_a_clean_file_exits_zero(tmp_path, capsys):
    path = _module(tmp_path, '"""one"""\n\n# a note\nx = 1\n')
    assert main([str(path)]) == 0
    assert capsys.readouterr().out == ""


def test_an_offence_exits_one_and_is_printed(tmp_path, capsys):
    path = _module(tmp_path, '"""one\ntwo\nthree\nfour"""\n')
    assert main([str(path)]) == 1
    assert "max 3" in capsys.readouterr().out


def test_a_file_that_cannot_be_parsed_is_reported_not_raised(tmp_path, capsys):
    path = _module(tmp_path, "def f(:\n")
    assert main([str(path)]) == 1
    assert "cannot be parsed" in capsys.readouterr().out
