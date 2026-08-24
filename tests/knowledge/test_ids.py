from hashlib import sha256
from pathlib import Path

from oms_hub.files.atomic import sha256_file as atomic_sha256_file
from oms_hub.knowledge.ids import evidence_id, sha256_file, sha256_text, source_revision_id


def test_source_revision_id_is_deterministic_and_namespaced() -> None:
    first = source_revision_id("source-1", "a" * 64)
    second = source_revision_id("source-1", "a" * 64)
    assert first == second
    assert first.startswith("sr_")
    assert len(first) == 29


def test_source_revision_id_uses_every_component_and_nul_separation() -> None:
    expected = "sr_6aamjf65m23bt5jshyd47oxjbk"
    assert source_revision_id("source-1", "a" * 64) == expected
    assert source_revision_id("source-2", "a" * 64) != expected
    assert source_revision_id("source-1", "b" * 64) != expected


def test_evidence_id_changes_when_content_changes() -> None:
    first = evidence_id("sr_abc", "slide:42", sha256_text("first"))
    second = evidence_id("sr_abc", "slide:42", sha256_text("second"))
    assert first != second
    assert first.startswith("ev_")
    assert len(first) == 29


def test_evidence_id_uses_every_component_and_nul_separation() -> None:
    first_content_sha256 = "a7937b64b8caa58f03721bb6bacf5c78cb235febe0e70b1b84cd99541461a08e"
    expected = "ev_qlmz4ftvxwu5vizxvx2p46w2ku"
    assert evidence_id("sr_abc", "slide:42", first_content_sha256) == expected
    assert evidence_id("sr_def", "slide:42", first_content_sha256) != expected
    assert evidence_id("sr_abc", "page:42", first_content_sha256) != expected
    assert evidence_id(
        "sr_abc",
        "slide:42",
        "16367aacb67a4a017c8da8ab95682ccb390863780f7114dda0a0e0c55644c7c4",
    ) != expected


def test_sha256_text_normalizes_newlines_only() -> None:
    assert sha256_text("a\r\nb") == sha256_text("a\nb")
    assert sha256_text("a\rb") == sha256_text("a\nb")
    assert sha256_text(" a\nb ") != sha256_text("a\nb")


def test_sha256_file_reuses_atomic_helper() -> None:
    assert sha256_file is atomic_sha256_file


def test_sha256_file_hashes_bytes_without_text_normalization(tmp_path: Path) -> None:
    path = tmp_path / "source.bin"
    payload = b"a\r\nb\x00"
    path.write_bytes(payload)

    assert sha256_file(path) == sha256(payload).hexdigest()
