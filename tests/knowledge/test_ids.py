from hashlib import sha256
from pathlib import Path

from oms_hub.knowledge.ids import evidence_id, sha256_file, sha256_text, source_revision_id


def test_source_revision_id_is_deterministic_and_namespaced() -> None:
    first = source_revision_id("source-1", "a" * 64)
    second = source_revision_id("source-1", "a" * 64)
    assert first == second
    assert first.startswith("sr_")
    assert len(first) == 29


def test_evidence_id_changes_when_content_changes() -> None:
    first = evidence_id("sr_abc", "slide:42", sha256_text("first"))
    second = evidence_id("sr_abc", "slide:42", sha256_text("second"))
    assert first != second
    assert first.startswith("ev_")
    assert len(first) == 29


def test_sha256_text_normalizes_newlines_only() -> None:
    assert sha256_text("a\r\nb") == sha256_text("a\nb")
    assert sha256_text("a\rb") == sha256_text("a\nb")
    assert sha256_text(" a\nb ") != sha256_text("a\nb")


def test_sha256_file_hashes_bytes_without_text_normalization(tmp_path: Path) -> None:
    path = tmp_path / "source.bin"
    payload = b"a\r\nb\x00"
    path.write_bytes(payload)

    assert sha256_file(path) == sha256(payload).hexdigest()
