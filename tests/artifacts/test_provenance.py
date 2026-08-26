from __future__ import annotations

from dataclasses import replace

import pytest

from oms_hub.artifacts.models import ArtifactKind
from oms_hub.artifacts.provenance import (
    ArtifactEvidenceLink,
    ArtifactRun,
    compute_artifact_input_hash,
)


def _run(**changes: object) -> ArtifactRun:
    run = ArtifactRun(
        artifact_id="artifact-1",
        artifact_kind=ArtifactKind.LECTURE_QUIZ,
        recipe_id="lecture-quiz-current",
        recipe_version="current-v1",
        provider="notebooklm",
        model=None,
        prompt_version="prompt-v1",
        schema_version="quiz-v1",
        source_revision_ids=("sr-b", "sr-a"),
        evidence_ids=("ev-b", "ev-a"),
        input_hash="a" * 64,
        output_hash="b" * 64,
        created_at="2026-08-25T12:00:00+00:00",
        validation_status="valid",
    )
    return replace(run, **changes)  # type: ignore[arg-type]


def test_artifact_input_hash_is_deterministic_across_dependency_order() -> None:
    first = compute_artifact_input_hash(
        {"prompt": "synthetic", "options": {"count": 5, "mode": "course_only"}},
        ("sr-b", "sr-a"),
        ("ev-b", "ev-a"),
    )
    second = compute_artifact_input_hash(
        {"options": {"mode": "course_only", "count": 5}, "prompt": "synthetic"},
        ("sr-a", "sr-b"),
        ("ev-a", "ev-b"),
    )

    assert first == second
    assert len(first) == 64
    assert first != compute_artifact_input_hash(
        {"prompt": "changed"},
        ("sr-a", "sr-b"),
        ("ev-a", "ev-b"),
    )


def test_artifact_run_is_frozen_and_canonicalizes_dependency_order() -> None:
    run = _run()

    assert run.source_revision_ids == ("sr-a", "sr-b")
    assert run.evidence_ids == ("ev-a", "ev-b")
    with pytest.raises(AttributeError):
        run.stale_reason = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"artifact_id": " "}, "artifact_id"),
        ({"source_revision_ids": ("sr-a", "sr-a")}, "duplicate"),
        ({"input_hash": "not-a-hash"}, "input_hash"),
        ({"created_at": "2026-08-25"}, "created_at"),
        ({"validation_status": ""}, "validation_status"),
    ],
)
def test_artifact_run_rejects_invalid_trust_boundary_values(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _run(**changes)


def test_artifact_evidence_link_rejects_blank_identifiers() -> None:
    with pytest.raises(ValueError, match="evidence_id"):
        ArtifactEvidenceLink(
            artifact_id="artifact-1",
            source_revision_id="sr-a",
            evidence_id=" ",
        )
