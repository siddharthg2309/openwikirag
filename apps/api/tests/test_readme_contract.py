"""Keep the public README concise and aligned with the current implementation."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_readme_contains_architecture_and_explicit_scope_boundaries() -> None:
    readme = (ROOT / "README.md").read_text()

    assert "## Architecture" in readme
    assert "## End-to-end workflow" in readme
    assert "## Technology stack" in readme
    assert "actions/workflows/ci.yml/badge.svg" in readme
    assert "filesystem-backed object-volume adapter" in readme.lower()
    assert "Voice" in readme and "deferred" in readme
    assert "Qdrant and Neo4j are rebuildable projections" in readme
    assert "Poppler" in readme and "Tesseract" in readme
    assert "optional Ollama JSON-schema adapter" in readme
    assert "optional Ollama `/api/embed` adapter" in readme
    assert "implementation.md" not in readme
    assert "metrics.md" not in readme
