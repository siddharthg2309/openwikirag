"""Static contract for the public repository CI gates."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/ci.yml"


def test_ci_is_read_only_locked_and_runs_deployment_smoke() -> None:
    workflow = WORKFLOW.read_text()

    assert "permissions:\n  contents: read" in workflow
    assert "astral-sh/setup-uv@" in workflow
    assert "version: \"0.8.9\"" in workflow
    assert "uv sync --locked --all-groups" in workflow
    assert "uv run --no-sync pytest -q" in workflow
    assert "uv run --no-sync ruff check ." in workflow
    assert "uv run --no-sync mypy" in workflow
    assert "uv lock --check" in workflow
    assert "docker build" in workflow
    assert "ops/smoke_compose.sh" in workflow
