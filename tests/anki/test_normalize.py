import json
from pathlib import Path

from oms_hub.anki.contracts import SnapshotNote
from oms_hub.anki.normalize import normalize_html, normalize_snapshot_note

FIXTURE = Path(__file__).parent / "fixtures" / "anking_notes.json"


def _raw_note(index: int = 0) -> dict[str, object]:
    notes = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return notes[index]


def test_html_and_cloze_normalization_is_deterministic() -> None:
    html = (
        "<style>discard me</style><script>discard me too</script>"
        "<p>{{c12::Visible answer::hidden hint}} &amp; stable</p>"
        "<img src='never-index.jpg'>"
    )

    assert normalize_html(html) == "Visible answer & stable"
    assert normalize_html(html) == normalize_html(html)


def test_snapshot_normalization_retains_raw_fields_and_ordered_media() -> None:
    note = SnapshotNote.model_validate(
        {
            "note_id": 101,
            "model_name": "AnKingOverhaul",
            "fields": {
                name: value["value"]
                for name, value in _raw_note()["fields"].items()  # type: ignore[union-attr]
            },
            "tags": (
                "#AK_Step1_v12::Pharmacology::Hematology",
                "AnkiHub_Optional::LMU_OMS_II::HemeLymph::Block1",
            ),
            "card_ids": (1002, 1001),
            "media": ("warfarin.png", "warfarin.mp3", "inr.jpg"),
            "content_sha256": "0" * 64,
        }
    )

    normalized = normalize_snapshot_note(note)

    assert normalized.text.startswith("Warfarin inhibits")
    assert "ignore" not in normalized.text
    assert normalized.extra == "Monitor the INR."
    assert normalized.raw_fields["Text"].startswith("<style>")
    assert [
        (item.field_name, item.filename, item.source_order)
        for item in normalized.media
    ] == [
        ("Text", "warfarin.png", 0),
        ("Extra", "warfarin.mp3", 0),
        ("Extra", "inr.jpg", 1),
    ]


def test_kept_tags_and_content_hash_ignore_source_tag_order() -> None:
    payload = {
        "note_id": 102,
        "model_name": "AnKingOverhaul",
        "fields": {"Text": "Anemia", "Extra": "Iron"},
        "tags": (
            "discard_this",
            "#Pathoma::Hematology::Anemia",
            "AnkiHub_Optional::LMU_OMS_II::HemeLymph",
        ),
        "card_ids": (1003,),
        "media": (),
        "content_sha256": "0" * 64,
    }
    first = normalize_snapshot_note(SnapshotNote.model_validate(payload))
    payload["tags"] = tuple(reversed(payload["tags"]))
    second = normalize_snapshot_note(SnapshotNote.model_validate(payload))

    assert first.tags == (
        "#Pathoma::Hematology::Anemia",
        "AnkiHub_Optional::LMU_OMS_II::HemeLymph",
    )
    assert first.content_sha256 == second.content_sha256
