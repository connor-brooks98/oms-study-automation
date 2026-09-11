from dataclasses import replace

import pytest

from oms_hub.anki.index import AnkiIndex
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.question_bank.anki_links import (
    TagRule,
    match_qid_tags,
    parse_candidate_query,
    preview_candidate_notes,
    topic_proposals,
)
from oms_hub.question_bank.contracts import QuestionKey


@pytest.fixture
def note():
    return NormalizedNote(
        note_id=101,
        model_name="AnKingOverhaul",
        text="alpha and beta",
        extra="Extra context",
        raw_fields={"Text": "{{c1::alpha}} and {{c2::beta}}", "Extra": "Extra context"},
        tags=("fixture::uworld::step1::00123",),
        card_ids=(201, 202),
        media=(),
        token_signature="alpha beta",
        content_sha256="a" * 64,
    )


def key(qid="00123", source="uworld", product="step1"):
    return QuestionKey(source=source, product=product, question_id=qid)


def test_qid_is_exact_and_all_cloze_cards_remain(note):
    rules = [TagRule("uworld", "step1", "fixture::uworld::step1::")]
    original = dict(note.raw_fields)
    links = match_qid_tags([key()], [note], rules)
    assert [(x.note_id, x.card_ids) for x in links] == [("101", ("201", "202"))]
    assert links[0].confidence == "exact_tag"
    assert links[0].review_state == "pending"
    assert links[0].snapshot_status == "unverified"
    assert match_qid_tags([key("123")], [note], rules) == ()
    assert match_qid_tags([key("001230")], [note], rules) == ()
    assert match_qid_tags([key(product="step2")], [note], rules) == ()
    assert match_qid_tags([key(source="truelearn")], [note], rules) == ()
    assert note.raw_fields == original
    assert "{{c2::beta}}" in note.raw_fields["Text"]


def test_matching_retains_all_notes_and_only_deduplicates_identical_evidence(note):
    rules = [TagRule("uworld", "step1", "fixture::uworld::step1::")]
    other = replace(note, note_id=102, card_ids=(302, 301))
    links = match_qid_tags([key(), key()], [other, note, note], rules + rules)
    assert [(x.note_id, x.card_ids) for x in links] == [
        ("101", ("201", "202")),
        ("102", ("301", "302")),
    ]
    assert links == match_qid_tags([key()], [note, other], rules)
    with pytest.raises(ValueError, match="snapshot"):
        match_qid_tags([key()], [note, replace(note, card_ids=(999,))], rules)


@pytest.mark.parametrize(
    "tag",
    [
        "fixture::uworld::step1::001230",
        "fixture::uworld::step1::00123::child",
        "fixture::UWorld::step1::00123",
        "prefix::fixture::uworld::step1::00123",
    ],
)
def test_tag_equality_never_prefix_substring_or_casefold(note, tag):
    assert (
        match_qid_tags(
            [key()],
            [replace(note, tags=(tag,))],
            [TagRule("uworld", "step1", "fixture::uworld::step1::")],
        )
        == ()
    )


@pytest.mark.parametrize(
    "rules",
    [
        [TagRule("uworld", "step1", "parent")],
        [TagRule("uworld", "step1", "::")],
        [TagRule("uworld", " step1", "parent::")],
        [TagRule("wrong", "step1", "parent::")],
        [TagRule("uworld", "step1", "a::"), TagRule("uworld", "step1", "b::")],
        [TagRule("uworld", "step1", "a::"), TagRule("truelearn", "step1", "a::")],
        [TagRule("uworld", "step1", "a::"), TagRule("truelearn", "step1", "a::b::")],
    ],
)
def test_invalid_or_conflicting_rules_rejected(note, rules):
    with pytest.raises(ValueError):
        match_qid_tags([key()], [note], rules)


def test_missing_note_cards_and_unconfigured_mapping_are_explicit(note):
    assert match_qid_tags([key()], [], []) == ()
    assert match_qid_tags([key()], [note], []) == ()
    match = match_qid_tags(
        [key()],
        [replace(note, card_ids=())],
        [TagRule("uworld", "step1", "fixture::uworld::step1::")],
    )
    assert match[0].card_ids == ()


def test_topic_proposals_use_only_explicit_metadata():
    proposals = topic_proposals([("system", "Heme"), ("topic", "Coagulation"), ("system", "Heme")])
    assert [(p.axis, p.label) for p in proposals] == [("system", "Heme"), ("topic", "Coagulation")]
    assert all(
        p.canonical_id is None and p.review_state == "pending" and p.method == "unmapped"
        for p in proposals
    )
    assert topic_proposals([]) == ()
    with pytest.raises(ValueError):
        topic_proposals([("qid", "00123")])


@pytest.mark.parametrize(
    "raw",
    [
        "tag:AMBOSS",
        "nid:123 OR deck:*",
        "-nid:123",
        "nid:123 AND nid:456",
        "nid:123,",
        "nid:0",
        "(nid:123)",
        "",
        "nid:123 nid:456",
        '"nid:123"',
        "nid:123\nOR nid:456",
        "nid:１２３",
        "NID:123",
        "nid:+123",
        "nid:1 OR",
        "nid:1;echo x",
        "nid:9223372036854775808",
        "nid:1.0",
        "nid:1, 2",
        None,
    ],
)
def test_candidate_query_is_not_general_anki_search(raw):
    with pytest.raises(ValueError):
        parse_candidate_query(raw)


def test_candidate_query_supported_forms_limits_and_deduplication():
    assert parse_candidate_query("nid:123,456 OR nid:123") == (123, 456)
    assert parse_candidate_query("nid:123 or nid:456") == (123, 456)
    assert parse_candidate_query(" \tnid:001\tOr\tnid:9223372036854775807 \n") == (
        1,
        9223372036854775807,
    )
    assert len(parse_candidate_query("nid:" + ",".join(str(n) for n in range(1, 2001)))) == 2000
    with pytest.raises(ValueError):
        parse_candidate_query("nid:" + ",".join("1" for _ in range(2001)))
    assert parse_candidate_query("nid:" + "0" * (65536 - 5) + "1") == (1,)
    with pytest.raises(ValueError):
        parse_candidate_query("nid:" + "0" * (65536 - 4) + "1")


def test_preview_is_snapshot_bound_missing_explicit_and_never_connects(note, tmp_path, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("No Anki/provider/refresh allowed")

    index = AnkiIndex(tmp_path / "fixture-index")
    index.rebuild_companion([note], snapshot_id="old-synthetic-snapshot", fingerprint="b" * 64)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(AnkiIndex, "refresh_from_anki", forbidden)
    monkeypatch.setattr(AnkiIndex, "search_tag", forbidden)
    output = preview_candidate_notes("nid:101,999 OR nid:101", index)
    assert [p.note_id for p in output] == ["101", "999"]
    assert output[0].text == "alpha and beta"
    assert output[0].card_ids == ("201", "202") and not output[0].missing
    assert output[1].text == "" and output[1].card_ids == () and output[1].missing
    assert all(
        p.provenance == "user_pasted_amboss_candidates"
        and p.review_state == "pending"
        and p.snapshot_status == "unverified"
        and p.snapshot_id == "old-synthetic-snapshot"
        for p in output
    )
    assert index.get_note(101).raw_fields == note.raw_fields


def test_absent_fixture_index_does_not_create_database(tmp_path):
    index = AnkiIndex(tmp_path / "absent")
    output = preview_candidate_notes("nid:123", index)
    assert output[0].missing and output[0].snapshot_id is None
    assert not index.database_path.exists()


def test_snapshot_change_rejects_mixed_preview(tmp_path, monkeypatch):
    index = AnkiIndex(tmp_path / "fixture")
    snapshots = iter(("before", "after"))
    monkeypatch.setattr(index, "snapshot_id", lambda: next(snapshots))
    with pytest.raises(ValueError, match="snapshot changed"):
        preview_candidate_notes("nid:123", index)


def test_invalid_query_fails_before_any_index_read(tmp_path, monkeypatch):
    index = AnkiIndex(tmp_path / "fixture")

    def forbidden():
        raise AssertionError("No index read for an invalid query")

    monkeypatch.setattr(index, "snapshot_id", forbidden)
    with pytest.raises(ValueError):
        preview_candidate_notes("nid:123 OR deck:*", index)
