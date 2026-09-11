from dataclasses import replace

import pytest

from oms_hub.anki.domain import SourceKind
from oms_hub.anki.sources import SourcePassage
from oms_hub.study_chat.contracts import ChatAnswer, ChatRequest, validate_answer
from oms_hub.study_chat.sources import select_passages


def passage(text, revision_id=1):
    return SourcePassage.create(
        revision_id=revision_id,
        lecture_id=1,
        artifact_id="upload",
        source_kind=SourceKind.TRANSCRIPT,
        locator="paragraph:1",
        text=text,
    )


def test_invented_citation_is_rejected():
    with pytest.raises(ValueError, match="citation"):
        validate_answer(
            ChatAnswer("answered", "Supported claim", ("other-lecture",)),
            {"selected-slide"},
            mode="lecture",
        )
    with pytest.raises(ValueError, match="citation"):
        validate_answer(ChatAnswer("answered", "Claim", ()), set(), mode="lecture")
    with pytest.raises(ValueError, match="citation"):
        validate_answer(
            ChatAnswer("no_support", "No evidence", ("slide",)), {"slide"}, mode="lecture"
        )


def test_contract_rejects_unknown_fields_and_invalid_scope():
    from uuid import uuid4

    values = dict(
        request_id=str(uuid4()),
        conversation_id=str(uuid4()),
        owner_id="owner",
        mode="lecture",
        question="why?",
        revision_ids=(1,),
    )
    assert ChatRequest(**values).revision_ids == (1,)
    for delta in (
        {"extra": True},
        {"request_id": "not-uuid"},
        {"owner_id": " "},
        {"revision_ids": (1, 1)},
        {"revision_ids": (True,)},
        {"mode": "general"},
        {"revision_ids": ()},
        {"question": " "},
    ):
        with pytest.raises(ValueError):
            ChatRequest(**(values | delta))


def test_lexical_selection_is_stable_bounded_and_does_not_expand_scope():
    first, second = passage("iron low"), passage("Iron high")
    irrelevant = passage("unrelated potassium")
    expected = tuple(sorted((first, second), key=lambda p: p.passage_id))
    assert select_passages("IRON!", (second, irrelevant, first)) == expected
    assert select_passages("IRON", expected, max_chars=len(expected[0].text)) == expected[:1]
    assert select_passages("unmatched", expected) == ()
    assert select_passages("IRON", (first,), max_chars=1) == ()
    assert select_passages("IRON", (replace(first, extraction_status="vision_unavailable"),)) == ()
    with pytest.raises(ValueError):
        select_passages("iron", (first,), max_chars=-1)


def test_lexical_paraphrase_ceiling_is_explicit():
    assert select_passages("low red blood cells", (passage("Anemia"),)) == ()
