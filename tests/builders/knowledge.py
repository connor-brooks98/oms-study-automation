"""Deterministic test builders for grounded-learning provider contracts."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import TypedDict

from oms_hub.providers import AuthorityClass, EvidenceRef, RetrievalScope, TruthMode


class SourceRevisionPayload(TypedDict):
    source_document_id: str
    source_revision_id: str
    file_sha256: str
    state: str


def build_source_revision(
    *,
    source_document_id: str = "source_fixture_course_l13",
    source_revision_id: str = "sr_fixture_course_l13_v1",
    file_sha256: str = "0" * 64,
    state: str = "ready",
) -> SourceRevisionPayload:
    return {
        "source_document_id": source_document_id,
        "source_revision_id": source_revision_id,
        "file_sha256": file_sha256,
        "state": state,
    }


def build_evidence_ref(
    *,
    evidence_id: str = "ev_fixture_course_l13_hemophilia_a",
    source_revision_id: str = "sr_fixture_course_l13_v1",
    authority_class: AuthorityClass = AuthorityClass.COURSE_MATERIAL,
    locator_kind: str = "slide",
    locator_value: str = "5",
    excerpt: str = "Hemophilia A is factor VIII deficiency with prolonged PTT and normal PT.",
    checksum: str | None = None,
) -> EvidenceRef:
    resolved_checksum = (
        "sha256:" + hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        if checksum is None
        else checksum
    )
    return EvidenceRef(
        evidence_id,
        source_revision_id,
        authority_class,
        locator_kind,
        locator_value,
        excerpt,
        resolved_checksum,
    )

def build_retrieval_scope(
    *,
    course_id: str = "heme",
    exam_id: str | None = "e2",
    lecture_ids: Iterable[str] = ("l13",),
    truth_mode: TruthMode = TruthMode.COURSE_ONLY,
    source_revision_ids: Iterable[str] = (),
) -> RetrievalScope:
    return RetrievalScope(
        course_id,
        exam_id,
        tuple(lecture_ids),
        truth_mode,
        tuple(source_revision_ids),
    )
