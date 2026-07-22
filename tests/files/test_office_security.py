from types import SimpleNamespace

import pytest

from oms_hub.files import office_security


def test_office_encryption_result_is_preserved(tmp_path, monkeypatch) -> None:
    path = tmp_path / "source.pptx"
    path.write_bytes(b"PK")
    monkeypatch.setattr(
        office_security.msoffcrypto,
        "OfficeFile",
        lambda stream: SimpleNamespace(is_encrypted=lambda: True),
    )
    assert office_security.office_file_is_encrypted(path) is True


def test_office_parse_error_becomes_safe_domain_error(tmp_path, monkeypatch) -> None:
    path = tmp_path / "source.pptx"
    path.write_bytes(b"PK")

    def fail(stream):
        raise RuntimeError("parser detail")

    monkeypatch.setattr(office_security.msoffcrypto, "OfficeFile", fail)
    with pytest.raises(office_security.OfficeSecurityError, match="could not be verified"):
        office_security.office_file_is_encrypted(path)
