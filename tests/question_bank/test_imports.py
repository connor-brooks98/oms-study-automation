import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from oms_hub.question_bank.imports import parse_import, preview_import


def payload():
    return json.loads((Path(__file__).parent / "fixtures/normalized-v1.json").read_bytes())


def raw(value):
    return json.dumps(value).encode()


def question(**changes):
    return dict(
        stem="Choose the first letter.",
        choices=["A", "B"],
        correct_index=0,
        rationale="A is the first letter.",
        **changes,
    )


def test_exact_ids_and_unknown_results():
    value = payload()
    value["rows"][0].update(attempt_id=None, result="unknown", occurred_at=None, elapsed_ms=None)
    parsed = parse_import(raw(value))
    assert parsed.rows[0].question_id == "00123"
    assert parsed.rows[0].result == "unknown"
    value["rows"][0]["question_id"] = 123
    with pytest.raises(ValueError):
        parse_import(raw(value))


@pytest.mark.parametrize(
    "field,value",
    [
        ("question_id", ""),
        ("question_id", " 123"),
        ("question_id", "123 "),
        ("question_id", "x" * 201),
        ("attempt_id", 123),
        ("attempt_id", " "),
        ("attempt_id", "x" * 201),
        ("result", "passed"),
        ("result", None),
        ("elapsed_ms", -1),
        ("elapsed_ms", True),
        ("elapsed_ms", 1.5),
        ("elapsed_ms", "1"),
        ("occurred_at", "2026-09-11T15:00:00"),
        ("occurred_at", "2026-09-11"),
        ("occurred_at", 1789140000),
        ("occurred_at", "1789140000"),
        ("occurred_at", "20260911"),
        ("occurred_at", "1789140000.5"),
        ("occurred_at", "-1789140000"),
        ("occurred_at", "yesterday"),
        ("user_note", "x" * 20001),
        ("user_note", 1),
        ("tags", [1]),
        ("tags", [""]),
        ("tags", ["x" * 301]),
        ("question", []),
        ("unknown", "field"),
    ],
)
def test_invalid_row_fields(field, value):
    data = payload()
    data["rows"][0][field] = value
    with pytest.raises(ValueError):
        parse_import(raw(data))


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-11T15:00:00Z",
        "2026-09-11T11:00:00-04:00",
        "2026-09-11T20:30:00.123456+05:30",
    ],
)
def test_explicit_timezone_datetimes_remain_supported(timestamp):
    data = payload()
    data["rows"][0]["occurred_at"] = timestamp
    parsed = parse_import(raw(data)).rows[0].occurred_at
    assert parsed is not None
    assert parsed.isoformat() == timestamp.replace("Z", "+00:00")


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("schema_version", "1"),
        ("schema_version", 2),
        ("source", "UWorld"),
        ("product", "step1 "),
        ("product", ""),
        ("product", 1),
        ("product", "x" * 101),
        ("export_id", " x"),
        ("export_id", "x" * 201),
        ("unknown", "field"),
        ("rows", [None]),
        ("rows", [1]),
        ("rows", []),
    ],
)
def test_invalid_envelope_fields(field, value):
    data = payload()
    data[field] = value
    with pytest.raises(ValueError):
        parse_import(raw(data))


@pytest.mark.parametrize(
    "field,value",
    [
        ("confidence", -0.1),
        ("confidence", 1.1),
        ("confidence", float("nan")),
        ("confidence", float("inf")),
        ("confidence", "0.5"),
        ("confidence", True),
        ("label", ""),
        ("label", "x" * 301),
        ("label", 1),
        ("canonical_id", 1),
        ("axis", "disease"),
        ("method", "guessed"),
        ("review_state", "verified"),
        ("extra", "field"),
    ],
)
def test_invalid_topic_fields(field, value):
    data = payload()
    data["rows"][0]["topics"][0][field] = value
    with pytest.raises(ValueError):
        parse_import(raw(data))


@pytest.mark.parametrize(
    "update",
    [
        {"result": "incorrect"},
        {"occurred_at": "2026-09-11T15:00:00Z"},
        {"elapsed_ms": 0},
    ],
)
def test_no_attempt_identity_cannot_claim_attempt_facts(update):
    data = payload()
    data["rows"][0].update(attempt_id=None, result="unknown", occurred_at=None, elapsed_ms=None)
    data["rows"][0].update(update)
    with pytest.raises(ValueError):
        parse_import(raw(data))


def test_limits_allow_boundary_values_and_reject_excess():
    data = payload()
    data.update(product="p" * 100, export_id="e" * 200)
    data["rows"][0].update(
        question_id="q" * 200,
        attempt_id="a" * 200,
        elapsed_ms=0,
        user_note="n" * 20000,
        tags=["t" * 300],
    )
    assert parse_import(raw(data)).rows[0].elapsed_ms == 0
    data = payload()
    data["rows"] *= 10000
    assert len(parse_import(raw(data)).rows) == 10000
    data["rows"].append(data["rows"][0])
    with pytest.raises(ValueError):
        parse_import(raw(data))
    small = raw(payload())
    assert parse_import(small + b" " * (10 * 1024 * 1024 - len(small)))
    with pytest.raises(ValueError, match="10 MiB"):
        parse_import(b" " * (10 * 1024 * 1024 + 1))


def test_canonical_digest_and_immutable_records():
    data = payload()
    preview = preview_import(raw(data))
    canonical = json.dumps(
        preview.envelope.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    assert preview.digest == hashlib.sha256(canonical.encode()).hexdigest()
    assert preview_import(json.dumps(data, sort_keys=True, indent=4).encode()) == preview
    assert preview.issues == ()
    assert preview.ready_question_rows == ()
    with pytest.raises(ValueError):
        preview.envelope.rows[0].question_id = "123"
    with pytest.raises(AttributeError):
        preview.digest = "tampered"


def test_duplicates_retain_original_row_numbers_and_conflicts():
    data = payload()
    data["rows"] *= 2
    data["rows"].append(dict(data["rows"][0], user_note="Contradictory note"))
    data["rows"].append(dict(data["rows"][0], attempt_id="second-attempt"))
    preview = preview_import(raw(data))
    assert len(preview.envelope.rows) == 4
    assert [(i.row, i.code) for i in preview.issues] == [
        (2, "duplicate_row"),
        (3, "duplicate_conflict"),
    ]


def test_ready_body_uses_native_parser_and_external_identity_stays_separate():
    data = payload()
    data["provenance"]["kind"] = "authorized_question_export"
    data["rows"][0]["question"] = question()
    data["rows"].append(deepcopy(data["rows"][0]))
    preview = preview_import(raw(data))
    assert preview.ready_question_rows == (1,)
    assert preview.envelope.rows[0].question_id == "00123"
    assert preview.envelope.rows[0].question == question()
    assert [(i.row, i.code) for i in preview.issues] == [(2, "duplicate_row")]


@pytest.mark.parametrize(
    "change",
    [
        {"correct_index": -1},
        {"correct_index": 2},
        {"correct_index": True},
        {"rationale": ""},
        {"choices": ["A", "a"]},
        {"unknown": "field"},
    ],
)
def test_invalid_question_body_is_held_while_metadata_survives(change):
    data = payload()
    data["provenance"]["kind"] = "authorized_question_export"
    body = question()
    body.update(change)
    data["rows"][0]["question"] = body
    preview = preview_import(raw(data))
    assert preview.ready_question_rows == ()
    assert [(i.row, i.code) for i in preview.issues] == [(1, "invalid_question")]
    assert preview.envelope.rows[0].question == body
    assert preview.envelope.rows[0].result == "incorrect"


def test_image_is_held_and_preview_has_no_external_side_effects(monkeypatch):
    import socket
    import sqlite3

    from oms_hub.study_generation.native_quiz import NativeQuizPublisher

    def forbidden(*args, **kwargs):
        raise AssertionError("preview must not publish or access external services")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(NativeQuizPublisher, "publish", forbidden)
    data = payload()
    data["provenance"]["kind"] = "authorized_question_export"
    data["rows"][0]["question"] = question(
        image_ref={
            "key": "figure-1",
            "source_title": "Synthetic source",
            "locator": "page 1",
            "description": "Fixture figure",
        }
    )
    preview = preview_import(raw(data))
    assert preview.ready_question_rows == ()
    assert [(i.row, i.code) for i in preview.issues] == [(1, "missing_image")]


def test_body_requires_authorized_provenance():
    data = payload()
    data["rows"][0]["question"] = question()
    with pytest.raises(ValueError, match="authorized_question_export"):
        parse_import(raw(data))


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_numbers_in_nested_raw_bodies_rejected(token):
    data = payload()
    data["provenance"]["kind"] = "authorized_question_export"
    data["rows"][0]["question"] = {"unrecognized": {"value": "TOKEN"}}
    with pytest.raises(ValueError):
        parse_import(raw(data).replace(b'"TOKEN"', token.encode()))


@pytest.mark.parametrize("value", [b"not json", b"[]", b"null", b"\xff"])
def test_malformed_documents_rejected(value):
    with pytest.raises(ValueError):
        parse_import(value)
