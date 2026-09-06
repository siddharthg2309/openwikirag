"""Offline contract tests for the redacted live WikiRAG smoke."""

import json
from typing import cast

import pytest

from openwikirag.application.wiki_generation import (
    DeterministicWikiProvider,
    WikiGenerationError,
    WikiGenerationRequest,
)
from ops.wiki_ollama_smoke import FIXTURES, run_smoke


def test_smoke_report_contains_only_redacted_structural_results() -> None:
    report = run_smoke(DeterministicWikiProvider(), config_hash="a" * 64)

    assert report["fixture_count"] == len(FIXTURES)
    assert "Raw uploads" not in json.dumps(report)
    fixtures = cast(list[dict[str, object]], report["fixtures"])
    assert all(cast(int, item["evidence_count"]) >= 1 for item in fixtures)


def test_smoke_fails_when_a_fixture_has_no_evidence() -> None:
    class EmptyProvider(DeterministicWikiProvider):
        def generate(self, *, request: WikiGenerationRequest) -> object:
            del request
            return {"summary": None, "definitions": [], "references": []}

    with pytest.raises(WikiGenerationError, match="produced no evidence"):
        run_smoke(EmptyProvider(), config_hash="a" * 64, fixtures=FIXTURES[:1])
