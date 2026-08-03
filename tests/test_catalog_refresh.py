"""The catalog-refresh prompt is a manual maintenance runbook, not runtime code.

No network, no LLM call — just confirm the entry point can find its prompt.
"""

from pathlib import Path

from invoke import Context

import tasks


def test_catalog_refresh_prompt_file_exists():
    prompt_path = Path("src/llmbroker/presets/freetier-refresh-prompt.md")
    assert prompt_path.is_file()
    assert prompt_path.read_text().strip()


def test_catalog_refresh_task_reads_prompt_file(capsys):
    tasks.catalog_refresh(Context())
    out = capsys.readouterr().out
    assert "Freetier preset refresh prompt" in out
