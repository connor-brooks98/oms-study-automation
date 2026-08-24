"""Fail-closed tests for the question-version authority boundary."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest

from oms_hub.questions import (
    QuestionResolution,
    QuestionResolutionError,
    QuestionResolutionFailure,
    resolve_question_version,
)

QUESTION_VERSION_ID = "question-1-v2"


class StaticResolutionProvider:
    def __init__(self, resolution: QuestionResolution | None) -> None:
        self.resolution = resolution
        self.requested_ids: list[str] = []

    def resolve(self, question_version_id: str) -> QuestionResolution | None:
        self.requested_ids.append(question_version_id)
        return self.resolution


def _approved_resolution() -> QuestionResolution:
    return QuestionResolution(
        question_version_id=QUESTION_VERSION_ID,
        approved_objective_ids=("objective-1",),
        source_snapshot_hash="sha256:source-1",
        approved=True,
        nonstale=True,
        verifiable=True,
    )


def test_approved_resolution_is_returned_for_the_exact_canonical_id() -> None:
    provider = StaticResolutionProvider(_approved_resolution())

    resolved = resolve_question_version(QUESTION_VERSION_ID, provider)

    assert resolved is provider.resolution
    assert provider.requested_ids == [QUESTION_VERSION_ID]


def test_resolution_record_is_immutable() -> None:
    resolution = _approved_resolution()

    with pytest.raises(FrozenInstanceError):
        cast(Any, resolution).question_version_id = "other-question-v2"


@pytest.mark.parametrize(
    "resolution, reason",
    (
        (None, QuestionResolutionFailure.ABSENT),
        (
            QuestionResolution(
                question_version_id="other-question-v2",
                approved_objective_ids=("objective-1",),
                source_snapshot_hash="sha256:source-1",
                approved=True,
                nonstale=True,
                verifiable=True,
            ),
            QuestionResolutionFailure.MISMATCHED,
        ),
        (
            QuestionResolution(
                question_version_id=QUESTION_VERSION_ID,
                approved_objective_ids=("objective-1",),
                source_snapshot_hash="sha256:source-1",
                approved=True,
                nonstale=False,
                verifiable=True,
            ),
            QuestionResolutionFailure.STALE,
        ),
        (
            QuestionResolution(
                question_version_id=QUESTION_VERSION_ID,
                approved_objective_ids=("objective-1",),
                source_snapshot_hash="sha256:source-1",
                approved=False,
                nonstale=True,
                verifiable=True,
            ),
            QuestionResolutionFailure.UNAPPROVED,
        ),
        (
            QuestionResolution(
                question_version_id=QUESTION_VERSION_ID,
                approved_objective_ids=("objective-1",),
                source_snapshot_hash="sha256:source-1",
                approved=True,
                nonstale=True,
                verifiable=False,
            ),
            QuestionResolutionFailure.UNVERIFIABLE,
        ),
        (
            QuestionResolution(
                question_version_id=QUESTION_VERSION_ID,
                approved_objective_ids=(),
                source_snapshot_hash="sha256:source-1",
                approved=True,
                nonstale=True,
                verifiable=True,
            ),
            QuestionResolutionFailure.MISSING_OBJECTIVES,
        ),
        (
            QuestionResolution(
                question_version_id=QUESTION_VERSION_ID,
                approved_objective_ids=("objective-1",),
                source_snapshot_hash=" \t",
                approved=True,
                nonstale=True,
                verifiable=True,
            ),
            QuestionResolutionFailure.MISSING_SOURCE_SNAPSHOT_HASH,
        ),
    ),
)
def test_resolution_fails_closed_for_every_invalid_authority_state(
    resolution: QuestionResolution | None,
    reason: QuestionResolutionFailure,
) -> None:
    provider = StaticResolutionProvider(resolution)

    with pytest.raises(QuestionResolutionError) as caught:
        resolve_question_version(QUESTION_VERSION_ID, provider)

    assert caught.value.reason is reason


def test_resolution_rejects_blank_requested_id_before_provider_lookup() -> None:
    provider = StaticResolutionProvider(_approved_resolution())

    with pytest.raises(QuestionResolutionError) as caught:
        resolve_question_version(" \t", provider)

    assert caught.value.reason is QuestionResolutionFailure.INVALID_QUESTION_VERSION_ID
    assert provider.requested_ids == []
